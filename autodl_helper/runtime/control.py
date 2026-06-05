from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import ScheduledStartJob
from ..storage import SQLiteStore, utc_now_iso
from .pid import pid_exists


DAEMON_STATUS_TTL_SECONDS = 90
DAEMON_LAUNCH_STARTING_TTL_SECONDS = 10
DAEMON_LAUNCH_FUSE_AFTER_FAILURES = 3
DAEMON_LAUNCH_FUSE_COOLDOWN_SECONDS = 30


def _format_mtime_value(value: float | int | str | None) -> str:
    if value in {None, ''}:
        return ''
    try:
        return f'{float(value):.6f}'
    except (TypeError, ValueError):
        return str(value)


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.astimezone().astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


def _pid_exists(pid: int | None) -> bool:
    return pid_exists(pid)


def _parse_iso_datetime(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def scheduled_job_identity(job: ScheduledStartJob) -> str:
    if job.name:
        return job.name
    if job.instance_id:
        return job.instance_id
    if job.selector and job.selector.gpu_model:
        return job.selector.gpu_model
    return 'scheduled-start'


def _normalize_job_signature_payload(
    job: ScheduledStartJob,
    *,
    target_time: str | None = None,
    advance_hours: float | None = None,
) -> dict[str, Any]:
    selector = job.selector
    return {
        'instance_id': str(job.instance_id or ''),
        'target_time': str(target_time if target_time is not None else job.target_time or ''),
        'advance_hours': float(advance_hours if advance_hours is not None else job.advance_hours or 0),
        'schedule_mode': str(getattr(job, 'schedule_mode', 'daily') or 'daily'),
        'weekdays': list(getattr(job, 'weekdays', []) or []),
        'run_date': str(getattr(job, 'run_date', '') or ''),
        'timezone': str(getattr(job, 'timezone', 'Asia/Shanghai') or 'Asia/Shanghai'),
        'selector': {
            'regions': list(getattr(selector, 'regions', []) or []),
            'gpu_model': str(getattr(selector, 'gpu_model', '') or ''),
            'gpu_count': int(getattr(selector, 'gpu_count', 1) or 1),
            'charge_types': list(getattr(selector, 'charge_types', []) or []),
        } if selector is not None else None,
    }


def scheduled_job_signature(
    job: ScheduledStartJob,
    *,
    target_time: str | None = None,
    advance_hours: float | None = None,
) -> str:
    return json.dumps(
        _normalize_job_signature_payload(job, target_time=target_time, advance_hours=advance_hours),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )


def get_task_enabled(store: SQLiteStore, account_name: str, task_type: str, *, default_enabled: bool) -> bool:
    control = store.get_task_control(account_name, task_type)
    return default_enabled if control is None else control


def apply_runtime_controls_to_scheduled_jobs(store: SQLiteStore, account_name: str, jobs: list[ScheduledStartJob]) -> list[ScheduledStartJob]:
    effective: list[ScheduledStartJob] = []
    for job in jobs:
        if not getattr(job, 'enabled', True):
            continue
        key = scheduled_job_identity(job)
        control = store.get_scheduled_job_control(account_name, key)
        if control and not control['enabled']:
            continue
        next_job = job
        if control:
            if control.get('target_time_override'):
                next_job = replace(next_job, target_time=str(control['target_time_override']))
            if control.get('advance_hours_override') is not None:
                next_job = replace(next_job, advance_hours=float(control['advance_hours_override']))
        effective.append(next_job)
    return effective


def mark_daemon_heartbeat(
    store: SQLiteStore,
    *,
    mode: str,
    pid: int | None = None,
    account: str | None = None,
    origin: str | None = None,
) -> None:
    pid = pid if pid is not None else os.getpid()
    store.set_runtime_values({
        'daemon_state': 'running',
        'daemon_mode': mode,
        'daemon_pid': str(pid),
        'daemon_account': str(account or ''),
        'daemon_origin': str(origin or ''),
        'daemon_last_seen_at': utc_now_iso(),
    })


def clear_daemon_heartbeat(store: SQLiteStore) -> None:
    store.set_runtime_values({
        'daemon_state': 'stopped',
        'daemon_mode': '',
        'daemon_pid': '',
        'daemon_account': '',
        'daemon_origin': '',
        'daemon_last_seen_at': utc_now_iso(),
    })


def read_daemon_status(store: SQLiteStore) -> dict[str, Any]:
    snapshot = store.get_runtime_snapshot()
    last_seen_raw = snapshot.get('daemon_last_seen_at', '')
    last_seen_at = None
    if last_seen_raw:
        try:
            last_seen_at = datetime.fromisoformat(last_seen_raw)
        except ValueError:
            last_seen_at = None
    running = snapshot.get('daemon_state') == 'running'
    if running and last_seen_at is not None and last_seen_at.tzinfo is not None:
        running = (datetime.now(timezone.utc) - last_seen_at.astimezone(timezone.utc)) <= timedelta(seconds=DAEMON_STATUS_TTL_SECONDS)
    return {
        'running': running,
        'mode': snapshot.get('daemon_mode', ''),
        'pid': int(snapshot['daemon_pid']) if snapshot.get('daemon_pid', '').isdigit() else None,
        'account': snapshot.get('daemon_account', ''),
        'origin': snapshot.get('daemon_origin', ''),
        'last_seen_at': last_seen_raw,
    }


def read_daemon_launch_status(
    store: SQLiteStore,
    *,
    pid_exists_fn=_pid_exists,
    starting_ttl_seconds: int = DAEMON_LAUNCH_STARTING_TTL_SECONDS,
) -> dict[str, Any]:
    snapshot = store.get_runtime_snapshot()
    daemon_status = read_daemon_status(store)
    state = str(snapshot.get('daemon_launch_state') or 'idle')
    started_at = str(snapshot.get('daemon_launch_started_at') or '')
    started_dt = _parse_iso_datetime(started_at)
    pid = int(snapshot['daemon_launch_pid']) if snapshot.get('daemon_launch_pid', '').isdigit() else None
    fused_until = str(snapshot.get('daemon_launch_fused_until') or '')
    fused_until_dt = _parse_iso_datetime(fused_until)
    now = datetime.now(timezone.utc)

    if state == 'fused' and fused_until_dt is not None and fused_until_dt.tzinfo is not None and fused_until_dt.astimezone(timezone.utc) <= now:
        state = 'idle'
    if state == 'starting':
        if pid is not None and daemon_status.get('running') and daemon_status.get('pid') == pid:
            state = 'running'
        elif started_dt is not None and started_dt.tzinfo is not None:
            if (now - started_dt.astimezone(timezone.utc)).total_seconds() > max(1, starting_ttl_seconds):
                state = 'idle'
    if state == 'running' and pid is not None and not pid_exists_fn(pid):
        state = 'idle'

    return {
        'launch_state': state,
        'launch_account': str(snapshot.get('daemon_launch_account') or ''),
        'launch_pid': pid,
        'launch_started_at': started_at,
        'launch_last_error': str(snapshot.get('daemon_launch_last_error') or ''),
        'launch_error_count': int(snapshot.get('daemon_launch_error_count') or 0),
        'launch_fused_until': fused_until,
    }


def claim_daemon_launch(
    store: SQLiteStore,
    *,
    account: str | None,
    starting_ttl_seconds: int = DAEMON_LAUNCH_STARTING_TTL_SECONDS,
) -> dict[str, Any]:
    status = read_daemon_launch_status(store, starting_ttl_seconds=starting_ttl_seconds)
    if status['launch_state'] in {'running', 'fused'}:
        return {**status, 'claimed': False}
    if status['launch_state'] == 'starting':
        return {**status, 'claimed': False}
    now_iso = utc_now_iso()
    store.set_runtime_values({
        'daemon_launch_state': 'starting',
        'daemon_launch_account': str(account or ''),
        'daemon_launch_pid': '',
        'daemon_launch_started_at': now_iso,
    })
    status = read_daemon_launch_status(store, starting_ttl_seconds=starting_ttl_seconds)
    return {**status, 'claimed': True}


def mark_daemon_launch_running(store: SQLiteStore, *, account: str | None, pid: int | None) -> None:
    store.set_runtime_values({
        'daemon_launch_state': 'running',
        'daemon_launch_account': str(account or ''),
        'daemon_launch_pid': str(pid or ''),
        'daemon_launch_started_at': utc_now_iso(),
        'daemon_launch_last_error': '',
        'daemon_launch_error_count': '0',
        'daemon_launch_fused_until': '',
    })


def mark_daemon_launch_failure(
    store: SQLiteStore,
    *,
    account: str | None,
    error: str,
    fuse_after_failures: int = DAEMON_LAUNCH_FUSE_AFTER_FAILURES,
    cooldown_seconds: int = DAEMON_LAUNCH_FUSE_COOLDOWN_SECONDS,
) -> dict[str, Any]:
    snapshot = store.get_runtime_snapshot()
    error_count = int(snapshot.get('daemon_launch_error_count') or 0) + 1
    state = 'idle'
    fused_until = ''
    if error_count >= max(1, fuse_after_failures):
        state = 'fused'
        fused_until = (datetime.now(timezone.utc) + timedelta(seconds=max(1, cooldown_seconds))).isoformat()
    store.set_runtime_values({
        'daemon_launch_state': state,
        'daemon_launch_account': str(account or ''),
        'daemon_launch_pid': '',
        'daemon_launch_started_at': utc_now_iso(),
        'daemon_launch_last_error': str(error or ''),
        'daemon_launch_error_count': str(error_count),
        'daemon_launch_fused_until': fused_until,
    })
    return read_daemon_launch_status(store)


def clear_daemon_launch_state(store: SQLiteStore) -> None:
    store.set_runtime_values({
        'daemon_launch_state': 'idle',
        'daemon_launch_account': '',
        'daemon_launch_pid': '',
        'daemon_launch_started_at': '',
        'daemon_launch_fused_until': '',
    })


def request_config_reload(store: SQLiteStore) -> dict[str, Any]:
    now = utc_now_iso()
    generation = int(store.get_runtime_value('config_generation', '0') or '0') + 1
    store.set_runtime_values({
        'reload_requested_at': now,
        'config_generation': str(generation),
    })
    return read_config_reload_status(store)


def read_config_reload_status(store: SQLiteStore) -> dict[str, Any]:
    snapshot = store.get_runtime_snapshot()
    return {
        'requested_at': str(snapshot.get('reload_requested_at') or ''),
        'requested_generation': int(snapshot.get('config_generation') or 0),
        'processed_generation': int(snapshot.get('config_last_processed_generation') or 0),
        'applied_generation': int(snapshot.get('config_last_applied_generation') or 0),
        'last_loaded_at': str(snapshot.get('config_last_loaded_at') or ''),
        'last_loaded_mtime': str(snapshot.get('config_last_loaded_mtime') or ''),
        'last_processed_mtime': str(snapshot.get('config_last_processed_mtime') or ''),
        'last_reload_status': str(snapshot.get('config_last_reload_status') or ''),
        'last_reload_error': str(snapshot.get('config_last_reload_error') or ''),
    }


def mark_config_reload_success(
    store: SQLiteStore,
    *,
    generation: int | None = None,
    config_mtime: float | int | str | None = None,
    loaded_at: str | None = None,
) -> dict[str, Any]:
    current = read_config_reload_status(store)
    generation_value = current['requested_generation'] if generation is None else max(0, int(generation))
    loaded_value = str(loaded_at or utc_now_iso())
    mtime_value = _format_mtime_value(config_mtime)
    store.set_runtime_values({
        'config_last_processed_generation': str(generation_value),
        'config_last_applied_generation': str(generation_value),
        'config_last_loaded_at': loaded_value,
        'config_last_loaded_mtime': mtime_value,
        'config_last_processed_mtime': mtime_value,
        'config_last_reload_status': 'success',
        'config_last_reload_error': '',
    })
    return read_config_reload_status(store)


def mark_config_reload_failure(
    store: SQLiteStore,
    *,
    generation: int | None = None,
    config_mtime: float | int | str | None = None,
    error: str,
) -> dict[str, Any]:
    current = read_config_reload_status(store)
    generation_value = current['requested_generation'] if generation is None else max(0, int(generation))
    store.set_runtime_values({
        'config_last_processed_generation': str(generation_value),
        'config_last_processed_mtime': _format_mtime_value(config_mtime),
        'config_last_reload_status': 'failed',
        'config_last_reload_error': str(error or ''),
    })
    return read_config_reload_status(store)


def task_due(store: SQLiteStore, task_type: str, *, interval_seconds: int, now: datetime | None = None) -> bool:
    now = _ensure_aware_utc(now or datetime.now(timezone.utc))
    key = f'last_run:{task_type}'
    raw = store.get_runtime_value(key, '')
    if not raw:
        return True
    try:
        previous = datetime.fromisoformat(raw)
    except ValueError:
        return True
    previous = _ensure_aware_utc(previous)
    elapsed_seconds = (now - previous).total_seconds()
    required_seconds = max(1.0, float(interval_seconds))
    grace_seconds = min(0.5, required_seconds * 0.1)
    return elapsed_seconds + grace_seconds >= required_seconds


def mark_task_run(store: SQLiteStore, task_type: str, *, now: datetime | None = None) -> None:
    now = _ensure_aware_utc(now or datetime.now(timezone.utc))
    store.set_runtime_value(f'last_run:{task_type}', now.isoformat())
