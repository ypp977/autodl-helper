from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Sequence

from autodl_helper.core.auth import AUTH_CODE_SIGNALS, AUTH_MESSAGE_SIGNALS
from autodl_helper.core.models import AuthEventSummary, HistoryRecord, KeeperResult
from autodl_helper.tasks.keeper import compute_keeper_schedule, format_duration_seconds
from autodl_helper.tasks.keeper_results import keeper_reason_label, keeper_result_label


GPU_SPEC_RE = re.compile(r'(?P<model>.+?)\s*[*×x]\s*(?P<count>\d+)\s*(?:卡)?\s*$')


def _stringify(value: object) -> str:
    return str(value or '').strip()


def _instance_gpu_model(item: dict[str, object]) -> str:
    explicit_spec = _stringify(item.get('spec'))
    if explicit_spec:
        match = GPU_SPEC_RE.match(explicit_spec)
        if match:
            return _stringify(match.group('model'))
        return explicit_spec
    for key in ('user_define_gpu_name', 'snapshot_gpu_alias_name', 'machine_describe'):
        value = _stringify(item.get(key))
        if value:
            return value
    return ''


def _instance_gpu_count(item: dict[str, object]) -> str:
    for key in ('gpu_all_num',):
        value = _stringify(item.get(key))
        if value.isdigit() and int(value) > 0:
            return value
    explicit_spec = _stringify(item.get('spec'))
    if explicit_spec:
        match = GPU_SPEC_RE.match(explicit_spec)
        if match:
            return _stringify(match.group('count'))
    return ''


def normalize_instance_spec(item: dict[str, object]) -> str:
    explicit_spec = _stringify(item.get('spec'))
    if explicit_spec:
        return explicit_spec
    model = _instance_gpu_model(item)
    count = _instance_gpu_count(item)
    if model and count:
        return f'{model} * {count}卡'
    return model


def normalize_instance(item: dict[str, object], *, account_name: str = '') -> dict[str, object]:
    row = {
        'instance_id': item.get('uuid', ''),
        'name': item.get('instance_name') or item.get('name') or '',
        'region': item.get('region_name', ''),
        'status': item.get('status', ''),
        'machine_alias': item.get('machine_alias') or item.get('spec') or '',
        'charge_type': item.get('charge_type', ''),
        'release_at': item.get('release_at', ''),
        'status_at': item.get('status_at', ''),
    }
    if account_name:
        row['account'] = account_name
    return row


def extract_instance_time(item: dict[str, object], field_name: str) -> str:
    payload = item.get(field_name, '')
    if isinstance(payload, dict):
        if payload.get('Valid'):
            return str(payload.get('Time', '') or '')
        return ''
    return str(payload or '')


def normalize_instance_debug(item: dict[str, object], keeper_settings=None, *, account_name: str = '') -> dict[str, object]:
    row = normalize_instance(item, account_name=account_name)
    keeper_projection = compute_keeper_schedule(
        item=item,
        shutdown_release_after_hours=getattr(keeper_settings, 'shutdown_release_after_hours', 360),
        keeper_trigger_before_hours=getattr(keeper_settings, 'keeper_trigger_before_hours', 6),
        fallback_to_status_at=getattr(keeper_settings, 'fallback_to_status_at', True),
    )
    row.update(
        {
            'gpu_idle_num': item.get('gpu_idle_num', ''),
            'gpu_all_num': item.get('gpu_all_num', ''),
            'start_mode': item.get('start_mode', ''),
            'spec': normalize_instance_spec(item),
            'machine_id': item.get('machine_id', ''),
            'started_at': extract_instance_time(item, 'started_at'),
            'stopped_at': extract_instance_time(item, 'stopped_at'),
            'release_deadline': keeper_projection['release_deadline'],
            'next_keeper_time': keeper_projection['next_keeper_time'],
        }
    )
    return row


def extract_watch_fields(item: dict[str, object], keeper_settings=None) -> dict[str, object]:
    row = item if 'instance_id' in item else normalize_instance_debug(item, keeper_settings=keeper_settings)
    return {
        'account': row.get('account', ''),
        'instance_id': row['instance_id'],
        'status': row['status'],
        'gpu_idle_num': row['gpu_idle_num'],
        'gpu_all_num': row['gpu_all_num'],
        'start_mode': row['start_mode'],
        'release_at': row['release_at'],
        'status_at': row['status_at'],
        'started_at': row['started_at'],
        'stopped_at': row['stopped_at'],
        'release_deadline': row['release_deadline'],
        'next_keeper_time': row['next_keeper_time'],
    }


def format_watch_change(snapshot: dict[str, object]) -> str:
    keys = ('account', 'instance_id', 'status', 'gpu_idle_num', 'gpu_all_num', 'start_mode', 'release_at', 'status_at', 'started_at', 'stopped_at', 'release_deadline', 'next_keeper_time')
    return ' '.join(f'{key}={snapshot.get(key, "")}' for key in keys if snapshot.get(key, '') != '' or key not in {'account'})


def format_table(rows: Sequence[dict[str, object]], columns: Sequence[tuple[str, str]], *, empty: str = '-') -> str:
    widths: dict[str, int] = {key: len(title) for key, title in columns}
    normalized_rows: list[dict[str, str]] = []
    for row in rows:
        normalized: dict[str, str] = {}
        for key, _title in columns:
            value = row.get(key, '')
            text = str(value).strip() if value is not None else ''
            if not text:
                text = empty
            normalized[key] = text
            widths[key] = max(widths[key], len(text))
        normalized_rows.append(normalized)
    header = '  '.join(title.ljust(widths[key]) for key, title in columns)
    separator = '  '.join('-' * widths[key] for key, _title in columns)
    body = ['  '.join(row[key].ljust(widths[key]) for key, _title in columns) for row in normalized_rows]
    return '\n'.join([header, separator, *body]) if body else '\n'.join([header, separator])


def format_instances_table(instances: list[dict[str, object]]) -> str:
    columns = []
    if any('account' in item for item in instances):
        columns.append(('account', 'account'))
    columns.extend([
        ('instance_id', 'instance_id'),
        ('name', 'name'),
        ('region', 'region'),
        ('status', 'status'),
        ('machine_alias', 'machine_alias/spec'),
        ('charge_type', 'charge_type'),
        ('release_at', 'release_at'),
        ('status_at', 'status_at'),
    ])
    rows = [item if 'instance_id' in item else normalize_instance(item) for item in instances]
    return format_table(rows, columns)


def release_source_label(value: str) -> str:
    return {
        'stopped_at': '关机时间',
        'fallback_status_at': 'status_at兜底',
        'none': '无关机时间',
    }.get(value, value)


def probe_result_label(value: str) -> str:
    probe_labels = {
        'keeper_executed': '已执行keeper',
        'skip_already_executed_in_cycle': '本周期已执行',
        'skip_missing_instance_id': '缺少实例ID',
        'skip_missing_shutdown_time': '缺少关机时间',
        'skip_not_due': '未到keeper窗口',
        'skip_recently_started': '最近启动冷却中',
        'skip_recently_stopped': '最近关机冷却中',
    }
    if value in probe_labels:
        return probe_labels[value]
    return keeper_result_label(value)


def probe_reason_label(value: str) -> str:
    labels = {
        'before_next_keeper_time': '还没到下次 keeper 时间',
        'stopped_within_cooldown': '最近关机时间未超过冷却窗口',
        'started_within_cooldown': '最近启动时间未超过冷却窗口',
        'fallback_status_at_recently_stopped': '仅能用 status_at 兜底，且当前仍处于关机冷却窗口',
        'fallback_status_at_recently_started': '仅能用 status_at 兜底，且当前仍处于启动冷却窗口',
        'fallback_status_at_ready': '仅能用 status_at 兜底，但当前已到 keeper 窗口',
        'keeper_window_reached': '已到 keeper 执行窗口',
        'missing_shutdown_time': '实例没有可用的关机时间字段',
        'already_executed_in_release_cycle': '该实例在当前释放周期已经执行过 keeper',
        'power_on_failed': '开机接口执行失败',
        'power_off_failed': '关机接口执行失败',
        'missing_instance_id': '实例缺少 uuid',
    }
    return labels.get(value, keeper_reason_label(value))


def _keeper_response_suffix(result: KeeperResult) -> str:
    if result.response_code or result.response_msg:
        return f' | 接口返回={result.response_code or "-"}:{result.response_msg or "-"}'
    return ''


def format_keeper_probe_line(result: KeeperResult, *, account_name: str = '', executed_in_cycle: bool = False) -> str:
    parts = []
    if account_name:
        parts.append(f'账号={account_name}')
    parts.extend([
        f'实例ID={result.instance_id}',
        f'状态={result.status}',
        f'判断来源={release_source_label(result.release_source)}',
        f'关机时长={format_duration_seconds(result.shutdown_duration_seconds)}',
        f'启动后时长={format_duration_seconds(result.started_duration_seconds)}',
        f'预计释放时间={result.release_deadline or "-"}',
        f'下次keeper时间={result.next_keeper_time or "-"}',
        f'keeper达标={"是" if result.eligible else "否"}',
        f'本周期是否已执行={"是" if executed_in_cycle else "否"}',
        f'结果={probe_result_label(result.result)}',
        f'原因={probe_reason_label(result.reason)}',
        f'最近启动时间={result.started_at or "-"}',
        f'最近关机时间={result.stopped_at or "-"}',
    ])
    if result.result in {'keeper_failed_power_on', 'keeper_failed_power_off'}:
        parts.append(_keeper_response_suffix(result))
    if result.release_source == 'fallback_status_at' and result.status_at:
        parts.append(f'辅助状态时间={result.status_at}')
    return ' | '.join(parts)


def history_subject(row: HistoryRecord) -> str:
    payload = row.payload or {}
    if row.task_type == 'keeper':
        return str(payload.get('instance_id') or row.instance_id or '-')
    return str(payload.get('selected_instance_id') or payload.get('instance_id') or row.instance_id or '-')


def history_summary(row: HistoryRecord) -> str:
    if row.summary:
        return row.summary
    payload = row.payload or {}
    if row.task_type == 'keeper':
        return f"释放时间={payload.get('release_deadline') or '-'} 下次keeper={payload.get('next_keeper_time') or '-'}"
    candidate_details = payload.get('candidate_details') or []
    candidate_brief = ''
    if isinstance(candidate_details, list) and candidate_details:
        fragments = []
        for item in candidate_details[:3]:
            if not isinstance(item, dict):
                continue
            marker = '*' if item.get('selected') else ''
            fragments.append(f"{marker}{item.get('instance_id') or '-'}({item.get('reason') or '-'})")
        if fragments:
            candidate_brief = f" 候选={','.join(fragments)}"
            if len(candidate_details) > 3:
                candidate_brief += f"...+{len(candidate_details) - 3}"
    return f"目标时间={payload.get('target_time') or '-'} deadline={payload.get('deadline') or '-'}{candidate_brief}"


def history_row_to_json(row: HistoryRecord) -> dict[str, object]:
    return {
        'created_at': row.created_at,
        'account': row.account_name,
        'task': row.task_type,
        'event_type': row.event_type,
        'severity': row.severity,
        'result': row.result,
        'reason': row.reason,
        'instance_id': row.instance_id,
        'subject': history_subject(row),
        'summary': history_summary(row),
        'payload': row.payload,
    }


def format_history_table(
    rows: Sequence[HistoryRecord],
    *,
    history_subject_fn=history_subject,
    history_summary_fn=history_summary,
) -> str:
    columns = [
        ('created_at', 'created_at'),
        ('account', 'account'),
        ('task', 'task'),
        ('event_type', 'event_type'),
        ('severity', 'severity'),
        ('subject', 'subject'),
        ('summary', 'summary'),
    ]
    payload = [
        {
            'created_at': row.created_at,
            'account': row.account_name,
            'task': row.task_type,
            'event_type': row.event_type,
            'severity': row.severity or 'info',
            'subject': history_subject_fn(row),
            'summary': history_summary_fn(row),
        }
        for row in rows
    ]
    return format_table(payload, columns)


def auth_report_row_to_json(row: AuthEventSummary) -> dict[str, object]:
    return {
        'code': row.code,
        'msg': row.msg,
        'count': row.count,
        'last_seen_at': row.last_seen_at,
        'accounts': row.accounts,
        'mapped': row.mapped,
        'matched_by': row.matched_by,
    }


def auth_report_match_label(row: AuthEventSummary) -> str:
    if row.mapped:
        return {'code': '已覆盖(code)', 'message': '已覆盖(message)'}.get(row.matched_by, '已覆盖')
    return '未覆盖'


def normalize_auth_signal_literal(value: str) -> str:
    return str(value or '').strip().lower()


def likely_auth_candidate(row: AuthEventSummary) -> bool:
    combined = f"{row.code} {row.msg}".lower()
    hints = ('auth', 'login', 'token', 'session', 'credential', 'reject', 'passport', 'forbid', 'unauth', '未登录', '登录', '鉴权', '令牌')
    return any(hint in combined for hint in hints)


def build_auth_signal_patch(rows: Sequence[AuthEventSummary]) -> dict[str, list[str]]:
    code_candidates: list[str] = []
    message_candidates: list[str] = []
    existing_codes = set(AUTH_CODE_SIGNALS)
    existing_messages = set(AUTH_MESSAGE_SIGNALS)
    for row in rows:
        if row.mapped or not likely_auth_candidate(row):
            continue
        code_value = normalize_auth_signal_literal(row.code)
        msg_value = normalize_auth_signal_literal(row.msg)
        if code_value and code_value not in existing_codes and ' ' not in code_value:
            existing_codes.add(code_value)
            code_candidates.append(code_value)
        if msg_value and msg_value not in existing_messages:
            existing_messages.add(msg_value)
            message_candidates.append(msg_value)
    return {'codes': sorted(code_candidates), 'messages': sorted(message_candidates)}


def render_auth_signal_patch(rows: Sequence[AuthEventSummary], *, file_path: str | Path) -> str:
    patch = build_auth_signal_patch(rows)
    if not patch['codes'] and not patch['messages']:
        return '# 没有可建议自动加入的鉴权信号。\n'
    lines = [f'# 建议补丁：人工确认后可写入 {file_path}', '']
    if patch['codes']:
        lines.append('AUTH_CODE_SIGNALS 新增建议:')
        lines.extend([f'  - {item}' for item in patch['codes']])
        lines.append('')
    if patch['messages']:
        lines.append('AUTH_MESSAGE_SIGNALS 新增建议:')
        lines.extend([f'  - {item}' for item in patch['messages']])
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def render_python_signal_block(name: str, values: Sequence[str], *, collection_type: str) -> str:
    lines = [f'{name} = {{' if collection_type == 'set' else f'{name} = (']
    for value in values:
        lines.append(f'    {json.dumps(value, ensure_ascii=False)},')
    lines.append('}' if collection_type == 'set' else ')')
    return '\n'.join(lines)


def replace_python_signal_block(source: str, name: str, rendered_block: str, *, collection_type: str) -> str:
    bracket_open = r'\{' if collection_type == 'set' else r'\('
    bracket_close = r'\}' if collection_type == 'set' else r'\)'
    pattern = rf'{name}\s*=\s*{bracket_open}\n.*?\n{bracket_close}'
    return re.sub(pattern, rendered_block, source, count=1, flags=re.S)


def apply_auth_signal_patch(rows: Sequence[AuthEventSummary], *, file_path: str | Path) -> tuple[int, int, str]:
    patch = build_auth_signal_patch(rows)
    target_path = Path(file_path)
    text = target_path.read_text(encoding='utf-8')
    merged_codes = sorted(set(AUTH_CODE_SIGNALS) | set(patch['codes']))
    merged_messages = list(dict.fromkeys([*AUTH_MESSAGE_SIGNALS, *patch['messages']]))
    text = replace_python_signal_block(
        text,
        'AUTH_CODE_SIGNALS',
        render_python_signal_block('AUTH_CODE_SIGNALS', merged_codes, collection_type='set'),
        collection_type='set',
    )
    text = replace_python_signal_block(
        text,
        'AUTH_MESSAGE_SIGNALS',
        render_python_signal_block('AUTH_MESSAGE_SIGNALS', merged_messages, collection_type='tuple'),
        collection_type='tuple',
    )
    target_path.write_text(text + ('\n' if not text.endswith('\n') else ''), encoding='utf-8')
    return len(patch['codes']), len(patch['messages']), str(target_path)
