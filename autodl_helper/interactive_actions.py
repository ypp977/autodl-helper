from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from autodl_helper.auth import inspect_auth_state
from autodl_helper.config import AccountSettings, Settings
from autodl_helper.models import AuthEventSummary, HistoryRecord
from autodl_helper.runtime_control import get_task_enabled, scheduled_job_identity, scheduled_job_signature
from autodl_helper.runtime_control import read_daemon_launch_status, read_daemon_status, request_config_reload
from autodl_helper.service_launchd import read_launch_agent_status
from autodl_helper.storage import SQLiteStore

SERVICE_HEARTBEAT_OK_SECONDS = 75


def scheduled_task_status(
    *,
    enabled: bool,
    daemon_running: bool,
    schedule_mode: str,
    latest_result: str,
) -> tuple[str, str]:
    latest_result = str(latest_result or '')
    schedule_mode = str(schedule_mode or 'daily')
    if schedule_mode == 'once' and latest_result in {'started', 'already_running', 'power_on_submitted'}:
        return '单次已完成', 'ok'
    if not enabled:
        return '已暂停', 'warn'
    if daemon_running:
        return '轮询中', 'ok'
    return '等待执行', 'info'


def list_instances_panel_rows(
    settings: Settings,
    store: SQLiteStore,
    *,
    account_name: str | None = None,
    select_accounts_fn: Callable[..., list[AccountSettings]],
    build_client_fn: Callable[..., object],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    multi_account = len(settings.accounts) > 1 or bool(account_name)
    for account in select_accounts_fn(settings, account_name):
        client = build_client_fn(settings, False, account=account, store=store)
        instances = client.list_instances() if client is not None else []
        total = len(instances)
        running = sum(1 for item in instances if str(item.get('status', '')).lower() == 'running')
        shutdown = sum(1 for item in instances if str(item.get('status', '')).lower() == 'shutdown')
        rows.append(
            {
                'account_name': account.name if multi_account or account.name != 'default' else '',
                'total': total,
                'running': running,
                'shutdown': shutdown,
            }
        )
    return rows


def history_panel_rows(store: SQLiteStore, *, account_name: str | None = None, limit: int = 5) -> list[HistoryRecord]:
    return store.read_history(account_name=account_name, limit=limit)


def failure_panel_rows(store: SQLiteStore, *, account_name: str | None = None, limit: int = 5) -> list[HistoryRecord]:
    rows = store.read_history(account_name=account_name, limit=max(limit * 4, 20))
    failures: list[HistoryRecord] = []
    for row in rows:
        event_type = row.event_type or ''
        result = row.result or ''
        reason = row.reason or ''
        if row.severity in {'error', 'warning'} and (
            '.failed.' in event_type
            or result.endswith('_failed')
            or result in {'deadline_failed', 'waiting_for_gpu'}
            or reason in {'deadline_missed', 'power_on_failed', 'power_off_failed', 'selector_no_match'}
        ):
            failures.append(row)
        if len(failures) >= limit:
            break
    return failures


def failure_account_summary(rows: list[HistoryRecord]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.account_name] = counts.get(row.account_name, 0) + 1
    return [
        {'account_name': account_name, 'count': count}
        for account_name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def auth_panel_rows(store: SQLiteStore, *, account_name: str | None = None, limit: int = 5) -> list[AuthEventSummary]:
    return store.summarize_auth_failures(account_name=account_name, limit=limit)


def account_panel_rows(settings: Settings, store: SQLiteStore) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    accounts = settings.accounts or [settings.auth]
    for account in accounts:
        if isinstance(account, AccountSettings):
            auth_settings = account.to_auth_settings()
            name = account.name
            enabled = account.enabled
        else:
            auth_settings = account
            name = 'default'
            enabled = True
        state = inspect_auth_state(auth_settings, store=store, account_name=name)
        rows.append(
            {
                'account_name': name,
                'enabled': enabled,
                'status': state['status'],
                'auth_source': state['auth_source'],
                'cached_at_iso': state['cached_at_iso'],
                'has_credentials': state['has_credentials'],
                'has_config_token': state['has_config_token'],
            }
        )
    return rows


def _scheduled_job_name_variants(job_name: str, *, account_name: str | None = None) -> set[str]:
    raw = str(job_name or '').strip()
    if not raw:
        return set()
    variants = {raw}
    normalized = raw.split(':', 1)[-1]
    variants.add(normalized)
    if account_name and ':' not in raw:
        variants.add(f'{account_name}:{raw}')
    return variants


def _effective_job_signature(job, control: dict[str, Any]) -> str:
    target_time = str(control.get('target_time_override') or job.target_time or '')
    advance_hours = (
        int(control['advance_hours_override'])
        if control.get('advance_hours_override') is not None
        else int(job.advance_hours or 0)
    )
    return scheduled_job_signature(job, target_time=target_time, advance_hours=advance_hours)


def _legacy_payload_matches_job(payload: dict[str, Any], job, *, target_time: str, advance_hours: int) -> bool:
    payload_target_time = str(payload.get('target_time') or '')
    if payload_target_time and payload_target_time != target_time:
        return False
    payload_advance_hours = payload.get('advance_hours')
    if payload_advance_hours not in {None, ''}:
        try:
            if int(payload_advance_hours) != advance_hours:
                return False
        except (TypeError, ValueError):
            return False
    payload_timezone = str(payload.get('timezone') or '')
    job_timezone = str(getattr(job, 'timezone', 'Asia/Shanghai') or 'Asia/Shanghai')
    if payload_timezone and payload_timezone != job_timezone:
        return False
    payload_schedule_mode = str(payload.get('schedule_mode') or '')
    job_schedule_mode = str(getattr(job, 'schedule_mode', 'daily') or 'daily')
    if payload_schedule_mode and payload_schedule_mode != job_schedule_mode:
        return False
    if job.instance_id:
        payload_instance_id = str(payload.get('job_instance_id') or payload.get('configured_instance_id') or payload.get('instance_id') or '')
        return not payload_instance_id or payload_instance_id == str(job.instance_id or '')
    selector = job.selector
    if selector is None:
        return True
    payload_selector = payload.get('selector')
    if isinstance(payload_selector, dict):
        if list(payload_selector.get('regions') or []) != list(getattr(selector, 'regions', []) or []):
            return False
        if str(payload_selector.get('gpu_model') or '') != str(getattr(selector, 'gpu_model', '') or ''):
            return False
        payload_gpu_count = payload_selector.get('gpu_count')
        if payload_gpu_count not in {None, ''} and int(payload_gpu_count) != int(getattr(selector, 'gpu_count', 1) or 1):
            return False
        if list(payload_selector.get('charge_types') or []) != list(getattr(selector, 'charge_types', []) or []):
            return False
        return True
    payload_selector_summary = str(payload.get('selector_summary') or '')
    current_selector_summary = '; '.join(
        part for part in [
            f"regions={','.join(selector.regions)}" if selector and selector.regions else '',
            f"gpu_model={selector.gpu_model}" if selector and selector.gpu_model else '',
            f"gpu_count={selector.gpu_count}" if selector and selector.gpu_count else '',
            f"charge_types={','.join(selector.charge_types)}" if selector and selector.charge_types else '',
        ] if part
    )
    return not payload_selector_summary or payload_selector_summary == current_selector_summary


def _matching_scheduled_history_row(
    history_rows: list[dict[str, Any]],
    job,
    control: dict[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    signature = _effective_job_signature(job, control)
    target_time = str(control.get('target_time_override') or job.target_time or '')
    advance_hours = (
        int(control['advance_hours_override'])
        if control.get('advance_hours_override') is not None
        else int(job.advance_hours or 0)
    )
    any_rows = False
    for row in history_rows:
        payload = (row.get('payload') or {}) if isinstance(row, dict) else {}
        any_rows = True
        payload_signature = str(payload.get('job_signature') or '').strip()
        if payload_signature:
            if payload_signature == signature:
                return row, True
            continue
        if _legacy_payload_matches_job(payload, job, target_time=target_time, advance_hours=advance_hours):
            return row, True
    return None, any_rows


def _latest_scheduled_history_row(history_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return history_rows[0] if history_rows else None


def scheduled_jobs_panel_rows(settings: Settings, store: SQLiteStore, *, account_name: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    account = account_name or (settings.accounts[0].name if settings.accounts else 'default')
    daemon_status = read_daemon_status(store)
    task_enabled = get_task_enabled(store, account, 'scheduled_start', default_enabled=settings.tasks.scheduled_start.enabled)
    controls = {
        row['job_name']: row
        for row in store.list_scheduled_job_controls(account_name=account)
    }
    latest_rows = store.read_scheduled_candidates(account_name=account, limit=50)
    latest_by_job: dict[str, list[dict[str, Any]]] = {}
    for row in latest_rows:
        for variant in _scheduled_job_name_variants(str(row.get('job_name') or ''), account_name=account):
            latest_by_job.setdefault(variant, []).append(row)
    for job in settings.tasks.scheduled_start.jobs:
        identity = scheduled_job_identity(job)
        control = controls.get(identity, {})
        history_rows = latest_by_job.get(identity, [])
        latest, _ = _matching_scheduled_history_row(history_rows, job, control)
        latest_actual = _latest_scheduled_history_row(history_rows)
        enabled = bool(task_enabled) and bool(control.get('enabled', True))
        schedule_mode = str(getattr(job, 'schedule_mode', 'daily') or 'daily')
        latest_result = str((latest_actual or {}).get('result') or '')
        daemon_running = bool(daemon_status.get('running'))
        task_status_label, task_status_tone = scheduled_task_status(
            enabled=enabled,
            daemon_running=daemon_running,
            schedule_mode=schedule_mode,
            latest_result=latest_result,
        )
        rows.append(
            {
                'job_name': identity,
                'instance_id': job.instance_id,
                'target_time': str(control.get('target_time_override') or job.target_time or ''),
                'advance_hours': control.get('advance_hours_override')
                if control.get('advance_hours_override') is not None
                else job.advance_hours,
                'enabled': enabled,
                'schedule_mode': schedule_mode,
                'latest_result': latest_result,
                'latest_created_at': str((latest_actual or {}).get('created_at') or ''),
                'daemon_running': daemon_running,
                'task_status_label': task_status_label,
                'task_status_tone': task_status_tone,
                'selector': '; '.join(
                    part for part in [
                        f"regions={','.join(job.selector.regions)}" if job.selector and job.selector.regions else '',
                        f"gpu_model={job.selector.gpu_model}" if job.selector and job.selector.gpu_model else '',
                        f"gpu_count={job.selector.gpu_count}" if job.selector and job.selector.gpu_count else '',
                    ] if part
                ),
            }
        )
    return rows


def keeper_probe_rows(
    settings: Settings,
    store: SQLiteStore,
    *,
    account_name: str | None = None,
    select_accounts_fn: Callable[..., list[AccountSettings]],
    build_client_fn: Callable[..., object],
    evaluate_keeper_instance_fn: Callable[..., Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for account in select_accounts_fn(settings, account_name, require_explicit_for_multi=False):
        client = build_client_fn(settings, False, account=account, store=store)
        for item in client.list_instances():
            result = evaluate_keeper_instance_fn(
                client=client,
                item=item,
                shutdown_release_after_hours=settings.tasks.keeper.shutdown_release_after_hours,
                keeper_trigger_before_hours=settings.tasks.keeper.keeper_trigger_before_hours,
                start_cooldown_minutes=settings.tasks.keeper.start_cooldown_minutes,
                stop_cooldown_minutes=settings.tasks.keeper.stop_cooldown_minutes,
                fallback_to_status_at=settings.tasks.keeper.fallback_to_status_at,
                now=datetime.now(),
            )
            executed_in_cycle = bool(
                result.release_deadline
                and result.instance_id
                and store.was_keeper_executed_in_cycle(account.name, result.instance_id, result.release_deadline)
            )
            display_result = result
            if getattr(result, 'result', '') == 'ready' and executed_in_cycle:
                display_result = replace(
                    result,
                    eligible=False,
                    result='skip_already_executed_in_cycle',
                    reason='already_executed_in_release_cycle',
                )
            rows.append(
                {
                    'account_name': account.name,
                    'instance_id': display_result.instance_id,
                    'status': display_result.status,
                    'result': display_result.result,
                    'reason': display_result.reason,
                    'eligible': bool(display_result.eligible),
                    'release_deadline': display_result.release_deadline,
                    'next_keeper_time': display_result.next_keeper_time,
                    'stopped_at': getattr(display_result, 'stopped_at', ''),
                    'executed_in_cycle': executed_in_cycle,
                }
            )
    return rows


def scheduled_job_status_rows(
    settings: Settings,
    store: SQLiteStore,
    *,
    account_name: str | None = None,
    job_name: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    account = account_name or (settings.accounts[0].name if settings.accounts else 'default')
    daemon_status = read_daemon_status(store)
    task_enabled = get_task_enabled(store, account, 'scheduled_start', default_enabled=settings.tasks.scheduled_start.enabled)
    latest_rows = store.read_scheduled_candidates(account_name=account, job_name=job_name, limit=max(limit * 10, 50))
    latest_by_job: dict[str, list[dict[str, Any]]] = {}
    for row in latest_rows:
        for variant in _scheduled_job_name_variants(str(row.get('job_name') or ''), account_name=account):
            latest_by_job.setdefault(variant, []).append(row)
    rows: list[dict[str, Any]] = []
    for job in settings.tasks.scheduled_start.jobs:
        identity = scheduled_job_identity(job)
        if job_name and identity != job_name and job.name != job_name and job.instance_id != job_name:
            continue
        control = store.get_scheduled_job_control(account, identity) or {}
        history_rows = latest_by_job.get(identity, [])
        latest, any_history = _matching_scheduled_history_row(history_rows, job, control)
        latest_actual = _latest_scheduled_history_row(history_rows)
        enabled = bool(task_enabled) and bool(control.get('enabled', True))
        schedule_mode = str(getattr(job, 'schedule_mode', 'daily') or 'daily')
        latest_result = (latest_actual or {}).get('result', '')
        daemon_running = bool(daemon_status.get('running'))
        task_status_label, task_status_tone = scheduled_task_status(
            enabled=enabled,
            daemon_running=daemon_running,
            schedule_mode=schedule_mode,
            latest_result=str(latest_result or ''),
        )
        rows.append(
            {
                'job_name': identity,
                'enabled': enabled,
                'target_time': str(control.get('target_time_override') or job.target_time),
                'advance_hours': control.get('advance_hours_override')
                if control.get('advance_hours_override') is not None
                else job.advance_hours,
                'schedule_mode': schedule_mode,
                'timezone': getattr(job, 'timezone', 'Asia/Shanghai') or 'Asia/Shanghai',
                'latest_result': latest_result,
                'latest_reason': (latest_actual or {}).get('reason', ''),
                'latest_summary': (latest_actual or {}).get('summary', ''),
                'latest_created_at': (latest_actual or {}).get('created_at', ''),
                'latest_matching_created_at': (latest or {}).get('created_at', ''),
                'latest_payload': (latest_actual or {}).get('payload', {}) or {},
                'latest_instance_id': (latest_actual or {}).get('instance_id', ''),
                'has_history': any_history,
                'latest_matches_current_rule': latest is not None,
                'target_mode': 'instance' if job.instance_id else 'selector',
                'target_summary': (
                    f'固定实例={job.instance_id}'
                    if job.instance_id
                    else '; '.join(
                        part for part in [
                            f"地区={','.join(job.selector.regions)}" if job.selector and job.selector.regions else '',
                            f"GPU={job.selector.gpu_model}" if job.selector and job.selector.gpu_model else '',
                            f"数量={job.selector.gpu_count}" if job.selector and job.selector.gpu_count else '',
                        ] if part
                    ) or '未设置'
                ),
                'daemon_running': daemon_running,
                'task_status_label': task_status_label,
                'task_status_tone': task_status_tone,
                'daemon_last_seen_at': daemon_status.get('last_seen_at', ''),
            }
        )
    return rows


def latest_candidate_summary(store: SQLiteStore, *, account_name: str | None = None) -> dict[str, Any]:
    rows = store.read_scheduled_candidates(account_name=account_name, limit=10)
    for row in rows:
        payload = row.get('payload') if isinstance(row, dict) else None
        details = payload.get('candidate_details') if isinstance(payload, dict) else None
        if not isinstance(details, list):
            continue
        selected = next((item for item in details if item.get('selected')), None)
        reason_counter = Counter()
        for item in details:
            if item.get('selected'):
                continue
            label = str(item.get('reason_label') or item.get('reason') or '')
            if label:
                reason_counter[label] += 1
        return {
            'job_name': str(row.get('job_name') or payload.get('job_name') or row.get('instance_id') or ''),
            'selected_instance_id': str((selected or {}).get('instance_id') or row.get('instance_id') or ''),
            'candidate_count': len(details),
            'top_reasons': [f'{reason} x{count}' for reason, count in reason_counter.most_common(3)],
        }
    return {
        'job_name': '',
        'selected_instance_id': '',
        'candidate_count': 0,
        'top_reasons': [],
    }


def keeper_summary_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() + 7 * 24 * 60 * 60
    expiring_soon = 0
    for row in rows:
        raw = str(row.get('release_deadline') or '').strip()
        if not raw:
            continue
        try:
            deadline = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if deadline.tzinfo is None:
            deadline = deadline.astimezone()
        ts = deadline.timestamp()
        if now.timestamp() <= ts <= cutoff:
            expiring_soon += 1
    pending = sum(1 for row in rows if bool(row.get('eligible')))
    not_due = sum(1 for row in rows if str(row.get('result') or '') == 'skip_not_due')
    abnormal = sum(1 for row in rows if str(row.get('result') or '') in {'skip_missing_shutdown_time', 'skip_missing_instance_id'})
    failed = sum(
        1
        for row in rows
        if str(row.get('result') or '') in {'keeper_failed_power_on', 'keeper_failed_power_off'}
    )
    return {
        'pending': pending,
        'not_due': not_due,
        'abnormal': abnormal,
        'expiring_soon': expiring_soon,
        'failed': failed,
    }


def build_dashboard_view(
    settings: Settings,
    store: SQLiteStore,
    *,
    account_name: str | None = None,
    current_account_name: str | None = None,
    include_instance_rows: bool = False,
    list_instances_panel_rows_fn: Callable[..., list[dict[str, Any]]],
    history_panel_rows_fn: Callable[..., list[Any]],
    auth_panel_rows_fn: Callable[..., list[Any]],
    keeper_probe_rows_fn: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    enabled_accounts = [account for account in settings.accounts if account.enabled] if settings.accounts else [settings.auth]
    task_controls = store.list_task_controls(account_name=account_name)
    job_controls = store.list_scheduled_job_controls(account_name=account_name)
    recent_failures = failure_panel_rows(store, account_name=account_name, limit=5)
    instance_rows: list[dict[str, Any]] = []
    if include_instance_rows:
        try:
            instance_rows = list_instances_panel_rows_fn(settings, store, account_name=account_name)
        except Exception:
            instance_rows = []
    accounts = account_panel_rows(settings, store)
    current_account = current_account_name or account_name or (enabled_accounts[0].name if enabled_accounts and isinstance(enabled_accounts[0], AccountSettings) else 'default')
    current_account_row = next((row for row in accounts if row['account_name'] == current_account), accounts[0] if accounts else None)
    effective_keeper_enabled = get_task_enabled(store, current_account, 'keeper', default_enabled=settings.tasks.keeper.enabled)
    effective_scheduled_enabled = get_task_enabled(store, current_account, 'scheduled_start', default_enabled=settings.tasks.scheduled_start.enabled)
    if keeper_probe_rows_fn is not None:
        try:
            keeper_rows = keeper_probe_rows_fn(settings, store, account_name=current_account)
        except Exception:
            keeper_rows = []
    else:
        keeper_rows = []
    runtime_status = read_daemon_status(store)
    launch_agent = read_launch_agent_status()
    service_installed = bool(launch_agent.get('installed'))
    service_loaded = bool(launch_agent.get('loaded'))
    daemon_running = bool(runtime_status.get('running'))
    launch_state = str(read_daemon_launch_status(store).get('state') or '') if store is not None else ''
    last_error = str(runtime_status.get('last_error') or '')
    heartbeat_age_seconds: float | None = None
    raw_last_seen = str(runtime_status.get('last_seen_at') or '').strip()
    if raw_last_seen:
        try:
            last_seen_dt = datetime.fromisoformat(raw_last_seen)
        except ValueError:
            last_seen_dt = None
        if last_seen_dt is not None:
            if last_seen_dt.tzinfo is None:
                last_seen_dt = last_seen_dt.replace(tzinfo=timezone.utc)
            heartbeat_age_seconds = max(0.0, (datetime.now(timezone.utc) - last_seen_dt.astimezone(timezone.utc)).total_seconds())
    if not service_installed:
        service_state_label, service_state_tone = '未安装', 'warn'
    elif launch_state == 'starting':
        service_state_label, service_state_tone = '启动中', 'info'
    elif service_loaded and daemon_running and heartbeat_age_seconds is not None and heartbeat_age_seconds <= SERVICE_HEARTBEAT_OK_SECONDS:
        service_state_label, service_state_tone = '运行中', 'ok'
    elif service_loaded and (not daemon_running or launch_state == 'fused' or last_error or (heartbeat_age_seconds is not None and heartbeat_age_seconds > SERVICE_HEARTBEAT_OK_SECONDS)):
        service_state_label, service_state_tone = '状态异常', 'bad'
    else:
        service_state_label, service_state_tone = '已停止', 'warn'
    return {
        'runtime_status': runtime_status,
        'current_account': current_account,
        'current_account_row': current_account_row,
        'account_rows': accounts,
        'enabled_accounts': len(enabled_accounts),
        'keeper_enabled': settings.tasks.keeper.enabled,
        'scheduled_enabled': settings.tasks.scheduled_start.enabled,
        'effective_keeper_enabled': effective_keeper_enabled,
        'effective_scheduled_enabled': effective_scheduled_enabled,
        'paused_task_count': sum(1 for row in task_controls if not row['enabled']),
        'paused_job_count': sum(1 for row in job_controls if not row['enabled']),
        'scheduled_jobs': scheduled_jobs_panel_rows(settings, store, account_name=current_account),
        'instance_rows': instance_rows,
        'recent_history': history_panel_rows_fn(store, account_name=account_name, limit=5),
        'recent_failures': recent_failures,
        'failure_account_summary': failure_account_summary(recent_failures),
        'recent_auth_rows': auth_panel_rows_fn(store, account_name=account_name, limit=5),
        'candidate_summary': latest_candidate_summary(store, account_name=account_name),
        'keeper_summary': keeper_summary_rows(keeper_rows),
        'service_state_label': service_state_label,
        'service_state_tone': service_state_tone,
        'service_last_seen_at': runtime_status.get('last_seen_at', ''),
        'service_pid': runtime_status.get('pid'),
    }


def set_task_enabled(store: SQLiteStore, account_name: str, task_type: str, enabled: bool, *, source: str = 'interactive') -> None:
    store.set_task_control(account_name, task_type, enabled=enabled, source=source)


def set_job_enabled(store: SQLiteStore, account_name: str, job_name: str, enabled: bool, *, source: str = 'interactive') -> None:
    current = store.get_scheduled_job_control(account_name, job_name) or {}
    store.upsert_scheduled_job_control(
        account_name,
        job_name,
        enabled=enabled,
        target_time_override=str(current.get('target_time_override') or ''),
        advance_hours_override=current.get('advance_hours_override'),
        source=source,
    )


def set_job_override(
    store: SQLiteStore,
    account_name: str,
    job_name: str,
    *,
    target_time: str | None = None,
    advance_hours: int | None = None,
    source: str = 'interactive',
) -> None:
    current = store.get_scheduled_job_control(account_name, job_name) or {}
    store.upsert_scheduled_job_control(
        account_name,
        job_name,
        enabled=bool(current.get('enabled', True)),
        target_time_override=str(target_time if target_time is not None else current.get('target_time_override') or ''),
        advance_hours_override=advance_hours if advance_hours is not None else current.get('advance_hours_override'),
        source=source,
    )


def request_reload(store: SQLiteStore) -> None:
    request_config_reload(store)


def clear_runtime_controls(store: SQLiteStore, *, account_name: str | None = None) -> None:
    task_controls = store.list_task_controls(account_name=account_name)
    for row in task_controls:
        store.set_task_control(row['account_name'], row['task_type'], enabled=True, source='interactive')
    job_controls = store.list_scheduled_job_controls(account_name=account_name)
    for row in job_controls:
        store.upsert_scheduled_job_control(
            row['account_name'],
            row['job_name'],
            enabled=True,
            target_time_override='',
            advance_hours_override=None,
            source='interactive',
        )


def runtime_controls_snapshot(store: SQLiteStore, *, account_name: str | None = None) -> dict[str, Any]:
    return {
        'runtime': store.get_runtime_snapshot(),
        'task_controls': store.list_task_controls(account_name=account_name),
        'job_controls': store.list_scheduled_job_controls(account_name=account_name),
    }


def resolve_job_config(settings: Settings, job_name: str) -> dict[str, Any] | None:
    normalized = str(job_name or '').strip()
    suffix = normalized.split(':', 1)[-1] if ':' in normalized else normalized
    for job in settings.tasks.scheduled_start.jobs:
        candidates = {job.name, job.instance_id}
        if normalized in candidates or suffix in candidates:
            return {
                'job_name': job.name or job.instance_id,
                'target_time': job.target_time,
                'advance_hours': job.advance_hours,
                'selector_summary': '; '.join(
                    part for part in [
                        f"regions={','.join(job.selector.regions)}" if job.selector and job.selector.regions else '',
                        f"gpu_model={job.selector.gpu_model}" if job.selector and job.selector.gpu_model else '',
                        f"gpu_count={job.selector.gpu_count}" if job.selector and job.selector.gpu_count else '',
                        f"charge_types={','.join(job.selector.charge_types)}" if job.selector and job.selector.charge_types else '',
                    ] if part
                ),
                'priority': [
                    {
                        'index': index + 1,
                        'instance_id': rule.instance_id,
                        'region': rule.region,
                        'machine_alias': rule.machine_alias,
                    }
                    for index, rule in enumerate(job.priority)
                ],
            }
    return None


def _match_priority_rule(priority_rules: list[dict[str, Any]], detail: dict[str, Any]) -> tuple[int | None, str]:
    for item in priority_rules:
        if item.get('instance_id') and item.get('instance_id') != detail.get('instance_id'):
            continue
        if item.get('region') and item.get('region') != detail.get('region_name'):
            continue
        if item.get('machine_alias') and item.get('machine_alias') != detail.get('machine_alias'):
            continue
        matcher = ', '.join(
            part for part in [
                f"instance_id={item.get('instance_id')}" if item.get('instance_id') else '',
                f"region={item.get('region')}" if item.get('region') else '',
                f"machine_alias={item.get('machine_alias')}" if item.get('machine_alias') else '',
            ] if part
        ) or '-'
        return int(item.get('index')), matcher
    return None, ''


def scheduled_candidate_panel_data(
    settings: Settings,
    store: SQLiteStore,
    *,
    account_name: str | None = None,
    job_name: str | None = None,
) -> dict[str, Any] | None:
    rows = store.read_scheduled_candidates(account_name=account_name, job_name=job_name, limit=1 if job_name else 10)
    target = None
    for row in rows:
        payload = row.get('payload', {})
        if isinstance(payload.get('candidate_details'), list):
            target = row
            break
    if target is None:
        return None
    payload = target['payload']
    job_label = str(target.get('job_name') or payload.get('job_name') or '')
    job_config = resolve_job_config(settings, job_label) or {}
    priority_rules = job_config.get('priority') or []
    candidate_details = []
    for item in payload.get('candidate_details') or []:
        detail = dict(item)
        if detail.get('matched_priority_index') is None and priority_rules:
            matched_index, matched_rule = _match_priority_rule(priority_rules, detail)
            detail['matched_priority_index'] = matched_index
            detail['matched_priority_rule'] = matched_rule
        candidate_details.append(detail)
    return {
        'created_at': target['created_at'],
        'account_name': target['account_name'],
        'job_name': job_label,
        'result': target['result'],
        'reason': target['reason'],
        'selector_summary': str(payload.get('selector_summary') or job_config.get('selector_summary') or ''),
        'selected_instance_id': str(payload.get('selected_instance_id') or ''),
        'selected_instance_label': str(payload.get('selected_instance_label') or ''),
        'candidate_details': candidate_details,
        'priority': job_config.get('priority') or [],
        'target_time': job_config.get('target_time') or payload.get('target_time') or '',
        'advance_hours': job_config.get('advance_hours'),
    }
