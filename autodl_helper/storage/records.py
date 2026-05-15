from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from .models import HistoryRecord


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_payload(raw: Any) -> dict[str, Any]:
    try:
        payload = json.loads(raw or '{}')
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def dump_payload(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)


def scheduled_job_name_variants(job_name: str, *, account_name: str | None = None) -> list[str]:
    raw = str(job_name or '').strip()
    if not raw:
        return []
    variants = {raw}
    normalized = raw.split(':', 1)[-1]
    variants.add(normalized)
    if account_name and ':' not in raw:
        variants.add(f'{account_name}:{raw}')
    return sorted(variants)


def legacy_scheduled_payload_matches(payload: dict[str, Any], expected: dict[str, Any]) -> bool:
    expected_target_time = str(expected.get('target_time') or '')
    payload_target_time = str(payload.get('target_time') or '')
    if expected_target_time and payload_target_time and payload_target_time != expected_target_time:
        return False
    expected_instance_id = str(expected.get('instance_id') or '')
    if expected_instance_id:
        payload_instance_id = str(payload.get('job_instance_id') or payload.get('configured_instance_id') or payload.get('instance_id') or '')
        return not payload_instance_id or payload_instance_id == expected_instance_id
    expected_selector = expected.get('selector')
    if not isinstance(expected_selector, dict):
        return True
    payload_selector = payload.get('selector')
    if isinstance(payload_selector, dict):
        return payload_selector == expected_selector
    expected_selector_summary = str(expected.get('selector_summary') or '')
    payload_selector_summary = str(payload.get('selector_summary') or '')
    return not payload_selector_summary or not expected_selector_summary or payload_selector_summary == expected_selector_summary


def keeper_history_record(row: Mapping[str, Any]) -> HistoryRecord:
    return HistoryRecord(
        created_at=row['created_at'],
        account_name=row['account_name'],
        task_type='keeper',
        result=row['result'],
        reason=row['reason'],
        instance_id=row['instance_id'],
        payload=load_payload(row['payload']),
        event_type=row['event_type'] or '',
        severity=row['severity'] or 'info',
        summary=row['summary'] or '',
    )


def scheduled_history_record(row: Mapping[str, Any]) -> HistoryRecord:
    return HistoryRecord(
        created_at=row['created_at'],
        account_name=row['account_name'],
        task_type='scheduled_start',
        result=row['result'],
        reason=row['reason'],
        instance_id=row['instance_id'],
        payload=load_payload(row['payload']),
        event_type=row['event_type'] or '',
        severity=row['severity'] or 'info',
        summary=row['summary'] or '',
    )


def service_history_record(row: Mapping[str, Any]) -> HistoryRecord:
    payload = load_payload(row['payload'])
    return HistoryRecord(
        created_at=row['created_at'],
        account_name=row['account_name'],
        task_type='service',
        result=row['message'],
        reason=str(payload.get('detail') or row['msg'] or ''),
        instance_id='',
        payload=payload,
        event_type=str(payload.get('action') or '') or str(row['code'] or ''),
        severity=row['level'] or 'info',
        summary=row['message'] or '',
    )


def scheduled_candidate_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'created_at': str(row['created_at']),
        'account_name': str(row['account_name']),
        'job_name': str(row['job_name']),
        'instance_id': str(row['instance_id']),
        'result': str(row['result']),
        'reason': str(row['reason']),
        'event_type': str(row['event_type'] or ''),
        'severity': str(row['severity'] or 'info'),
        'summary': str(row['summary'] or ''),
        'payload': load_payload(row['payload']),
    }


def task_control_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'account_name': str(row['account_name']),
        'task_type': str(row['task_type']),
        'enabled': bool(row['enabled']),
        'source': str(row['source']),
        'updated_at': str(row['updated_at']),
    }


def scheduled_job_control_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'account_name': str(row['account_name']),
        'job_name': str(row['job_name']),
        'enabled': bool(row['enabled']),
        'target_time_override': str(row['target_time_override'] or ''),
        'advance_hours_override': row['advance_hours_override'],
        'source': str(row['source']),
        'updated_at': str(row['updated_at']),
    }
