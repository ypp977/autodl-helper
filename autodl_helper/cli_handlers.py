from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO

from apscheduler.schedulers.blocking import BlockingScheduler

from autodl_helper.api import AutoDLClient
from autodl_helper.auth import AuthError, alert_auth_failure, inspect_auth_state, resolve_authorization
from autodl_helper.auth_policy import resolve_auth_runtime_policy
from autodl_helper.config import AccountSettings, LIGHTWEIGHT_MODES, NotificationSettings, Settings, load_settings, read_raw_settings, write_raw_settings
from autodl_helper.interactive_actions import (
    auth_panel_rows,
    build_dashboard_view,
    clear_runtime_controls,
    history_panel_rows,
    keeper_probe_rows,
    list_instances_panel_rows,
    request_reload,
    scheduled_candidate_panel_data,
    scheduled_job_status_rows,
    runtime_controls_snapshot,
    set_job_enabled,
    set_job_override,
    set_task_enabled,
)
from autodl_helper.interactive_app import run_interactive
from autodl_helper.interactive_views import render_candidate_explanation, render_dashboard
from autodl_helper.lock import FileLock, LockAcquisitionError
from autodl_helper.notify import EmailNotifier, NotificationManager, PushPlusNotifier, ServerChanNotifier
from autodl_helper.runtime_control import (
    apply_runtime_controls_to_scheduled_jobs,
    clear_daemon_heartbeat,
    clear_daemon_launch_state,
    get_task_enabled,
    mark_config_reload_failure,
    mark_config_reload_success,
    mark_daemon_heartbeat,
    mark_task_run,
    read_config_reload_status,
    read_daemon_status,
    request_config_reload,
    scheduled_job_identity,
    scheduled_job_signature,
    task_due,
)
from autodl_helper.service_launchd import append_service_lifecycle_log
from autodl_helper.services.manager import (
    install_service,
    restart_service,
    service_status,
    start_service,
    stop_service,
    uninstall_service,
)
from autodl_helper.state import StateStore
from autodl_helper.storage import SQLiteStore
from autodl_helper.tasks.keeper import evaluate_keeper_instance, format_duration_seconds, run_keeper_cycle
from autodl_helper.tasks.scheduled_start import ScheduledStartJobRuntime, run_scheduled_start_job

logger = logging.getLogger(__name__)
TIME_RE = re.compile(r'^(?:[01]\d|2[0-3]):[0-5]\d$')
DAEMON_HEARTBEAT_INTERVAL_SECONDS = 30


def _scheduled_start_reason_label(reason: str) -> str:
    if reason in {'window_already_succeeded'}:
        return '当前窗口已完成'
    if reason in {'outside_window'}:
        return '未到执行窗口'
    if reason in {'selector_no_match', 'instance_missing'}:
        return '暂无可用目标'
    if reason in {'gpu_idle_zero', 'no_eligible_candidate', 'running_without_gpu', 'retrying'}:
        return '候选暂不可抢'
    if reason in {'already_running', 'started'}:
        return '实例已在运行'
    if reason in {'power_on_submitted'}:
        return '已提交开机'
    if reason in {'deadline_failed', 'deadline_missed'}:
        return '已过截止时间'
    if reason in {'task_paused'}:
        return '任务已暂停'
    if reason in {'scheduled_disabled'}:
        return '配置未启用'
    return reason or '-'


def _format_scheduled_window(*, target_time: str, advance_hours: int, now: datetime) -> str:
    try:
        hh, mm = map(int, target_time.split(':'))
        target_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        window_start = target_dt - timedelta(hours=max(0, advance_hours))
        return f'{window_start.strftime("%H:%M")}-{target_dt.strftime("%H:%M")}'
    except Exception:
        return '-'


def _format_next_check(*, now: datetime, poll_interval_seconds: int) -> str:
    try:
        return (now + timedelta(seconds=max(1, poll_interval_seconds))).strftime('%H:%M:%S')
    except Exception:
        return '-'


def _format_local_time_label(value: str) -> str:
    raw = (value or '').strip()
    if not raw:
        return '-'
    try:
        parsed = datetime.fromisoformat(raw.replace(' ', 'T'))
    except ValueError:
        return raw
    return parsed.strftime('%m-%d %H:%M:%S')


def _format_keeper_window(*, next_keeper_time: str, release_deadline: str) -> str:
    start = _format_local_time_label(next_keeper_time)
    end = _format_local_time_label(release_deadline)
    if start == '-' and end == '-':
        return '-'
    return f'{start} ~ {end}'


def _log_scheduled_start_summary(

    *,
    account_name: str,
    job_name: str,
    target_time: str,
    advance_hours: int,
    schedule_mode: str,
    poll_interval_seconds: int,
    status: str,
    reason: str,
    now: datetime,
    instance_id: str = '',
    candidate_count: int | None = None,
) -> None:
    status_label = {
        'skip': '跳过',
        'started': '已开机',
        'success': '已开机',
        'already_running': '已在运行',
        'power_on_submitted': '已提交开机',
        'started_without_gpu': '等待',
        'retrying': '等待',
        'waiting_for_instance': '等待',
        'waiting_for_gpu': '等待',
        'deadline_failed': '失败',
        'instance_missing': '失败',
        'failure': '失败',
        'outside_window': '跳过',
    }.get(status, status or '-')
    fields = [
        f'账号={account_name}',
        f'任务={job_name}',
        f'目标={target_time}',
        f'计划={"单次" if schedule_mode == "once" else "每天"}',
        f'间隔={poll_interval_seconds}秒',
        f'当前窗口={_format_scheduled_window(target_time=target_time, advance_hours=advance_hours, now=now)}',
        f'下次检查={_format_next_check(now=now, poll_interval_seconds=poll_interval_seconds)}',
        f'结果={status_label}',
        f'原因={_scheduled_start_reason_label(reason)}',
    ]
    if instance_id:
        fields.append(f'实例={instance_id}')
    if candidate_count is not None:
        fields.append(f'候选数={candidate_count}')
    logger.info('[抢机检查] %s', ' '.join(fields))


def build_named_notifiers(notifications: NotificationSettings) -> dict[str, object]:
    notifiers: dict[str, object] = {}
    if notifications.pushplus.enabled and notifications.pushplus.token:
        notifiers['pushplus'] = PushPlusNotifier(token=notifications.pushplus.token)
    if notifications.serverchan.enabled and notifications.serverchan.token:
        notifiers['serverchan'] = ServerChanNotifier(token=notifications.serverchan.token)
    if notifications.email.enabled and notifications.email.username and notifications.email.to:
        notifiers['email'] = EmailNotifier(
            smtp_host=notifications.email.smtp_host,
            smtp_port=notifications.email.smtp_port,
            username=notifications.email.username,
            password=notifications.email.password,
            to=notifications.email.to,
        )
    return notifiers


def build_notifiers(notifications: NotificationSettings) -> list[object]:
    return list(build_named_notifiers(notifications).values())


def get_enabled_accounts(settings: Settings) -> list[AccountSettings]:
    if settings.accounts:
        return [account for account in settings.accounts if account.enabled]
    return [
        AccountSettings(
            name='default',
            enabled=True,
            authorization=settings.auth.authorization,
            autodl_phone=settings.auth.autodl_phone,
            autodl_password=settings.auth.autodl_password,
            login_retries=settings.auth.login_retries,
            login_timeout_ms=settings.auth.login_timeout_ms,
            post_login_wait_seconds=settings.auth.post_login_wait_seconds,
            cache_file=settings.auth.cache_file,
            cache_max_age_seconds=settings.auth.cache_max_age_seconds,
            lightweight_mode=settings.auth.lightweight_mode,
            runtime_auth_revalidate_seconds=settings.auth.runtime_auth_revalidate_seconds,
            force_refresh_min_interval_seconds=settings.auth.force_refresh_min_interval_seconds,
            auth_failure_backoff_seconds=settings.auth.auth_failure_backoff_seconds,
        )
    ]


def select_accounts(
    settings: Settings,
    account_name: str | None = None,
    *,
    require_explicit_for_multi: bool = False,
    get_enabled_accounts_fn: Callable[[Settings], list[AccountSettings]] = get_enabled_accounts,
) -> list[AccountSettings]:
    accounts = get_enabled_accounts_fn(settings)
    if not accounts:
        raise ValueError('At least one enabled account is required.')
    if account_name:
        selected = [account for account in accounts if account.name == account_name]
        if not selected:
            raise ValueError(f'Account not found or disabled: {account_name}')
        return selected
    if require_explicit_for_multi and len(accounts) > 1:
        raise ValueError('检测到多个启用账号，请使用 --account 明确指定账号。')
    return accounts


def create_store(
    settings: Settings,
    store_cls: type[SQLiteStore] = SQLiteStore,
    *,
    get_enabled_accounts_fn: Callable[[Settings], list[AccountSettings]] = get_enabled_accounts,
) -> SQLiteStore:
    store = store_cls(settings.storage.database_file)
    store.init_schema()
    store.register_accounts(settings.accounts or get_enabled_accounts_fn(settings))
    return store


def _account_status_label(status: str) -> str:
    mapping = {
        'logged_in': '已登录(runtime)',
        'cached': '已缓存登录',
        'token_configured': '已配置 token',
        'login_ready': '可密码登录',
        'not_configured': '未配置登录信息',
    }
    return mapping.get(status, status or '-')


def _account_source_label(source: str) -> str:
    mapping = {
        'runtime': 'runtime',
        'sqlite-cache': 'sqlite-cache',
        'file-cache': 'file-cache',
        'config': 'config',
        'password-login-ready': 'password',
        'missing': '-',
    }
    return mapping.get(source, source or '-')


def account_status_rows(
    settings: Settings,
    store: SQLiteStore,
    *,
    account_name: str | None = None,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for account in select_accounts_fn(settings, account_name):
        state = inspect_auth_state(account.to_auth_settings(), store=store, account_name=account.name)
        rows.append(
            {
                'account_name': account.name,
                'enabled': account.enabled,
                'status': state['status'],
                'status_label': _account_status_label(str(state['status'])),
                'auth_source': state['auth_source'],
                'auth_source_label': _account_source_label(str(state['auth_source'])),
                'cached_at': state['cached_at'],
                'cached_at_iso': state['cached_at_iso'],
                'has_credentials': state['has_credentials'],
                'has_config_token': state['has_config_token'],
                'cache_file': state['cache_file'],
                'lightweight_mode': state['lightweight_mode'],
            }
        )
    return rows


def record_auth_event(store: SQLiteStore | None, account_name: str, payload: dict[str, object]) -> None:
    if store is None:
        return
    code = str(payload.get('code', '') or '')
    msg = str(payload.get('msg', '') or '')
    store.add_event(
        account_name,
        'auth',
        'warning',
        '命中鉴权失败刷新判定',
        code=code,
        msg=msg,
        payload=dict(payload),
    )


def create_client(
    settings: Settings,
    headed: bool,
    account: AccountSettings | None = None,
    store: SQLiteStore | None = None,
    *,
    get_enabled_accounts_fn: Callable[[Settings], list[AccountSettings]] = get_enabled_accounts,
    resolve_authorization_fn: Callable[..., str] = resolve_authorization,
    client_cls: type[AutoDLClient] = AutoDLClient,
):
    selected_account = account or get_enabled_accounts_fn(settings)[0]
    auth_settings = selected_account.to_auth_settings()
    authorization = resolve_authorization_fn(
        auth_settings,
        headed=headed,
        store=store,
        account_name=selected_account.name,
    )
    return client_cls(
        authorization=authorization,
        min_day=settings.tasks.keeper.min_day,
        auth_refresh_callback=lambda: resolve_authorization_fn(
            auth_settings,
            headed=headed,
            force_refresh=True,
            store=store,
            account_name=selected_account.name,
        ),
        auth_failure_event_callback=lambda payload: record_auth_event(store, selected_account.name, payload),
    )


def build_client(
    settings: Settings,
    headed: bool,
    account: AccountSettings | None = None,
    store: SQLiteStore | None = None,
    *,
    create_client_fn: Callable[..., object] = create_client,
):
    try:
        return create_client_fn(settings, headed, account=account, store=store)
    except TypeError:
        return create_client_fn(settings, headed)


def compute_cycle_interval_seconds(settings: Settings) -> int:
    candidates: list[int] = []
    if settings.tasks.keeper.enabled:
        candidates.append(max(60, settings.tasks.keeper.interval_minutes * 60))
    if settings.tasks.scheduled_start.enabled and settings.tasks.scheduled_start.jobs:
        candidates.append(max(5, settings.tasks.scheduled_start.poll_interval_seconds))
    return min(candidates) if candidates else 3600


def compute_dispatch_interval_seconds(settings: Settings) -> int:
    return 5


def compute_interval_for_mode(settings: Settings, mode: str) -> int:
    if mode == 'keeper':
        return max(60, settings.tasks.keeper.interval_minutes * 60)
    if mode == 'scheduled_start':
        return max(5, settings.tasks.scheduled_start.poll_interval_seconds)
    return compute_cycle_interval_seconds(settings)


def _sync_primary_auth(settings: Settings) -> None:
    if not settings.accounts:
        return
    enabled = [account for account in settings.accounts if account.enabled]
    primary = enabled[0] if enabled else settings.accounts[0]
    settings.auth = primary.to_auth_settings()


def _resolve_account_override_targets(settings: Settings, account_name: str | None) -> list[AccountSettings]:
    if not settings.accounts:
        return []
    if account_name:
        matches = [account for account in settings.accounts if account.name == account_name]
        if not matches:
            raise ValueError(f'Account not found or disabled: {account_name}')
        return matches
    return list(settings.accounts)


def _resolve_job_override_targets(settings: Settings, job_name: str | None, *, require_single: bool) -> list[Any]:
    jobs = list(settings.tasks.scheduled_start.jobs)
    if not jobs:
        return []
    if job_name:
        matches = [job for job in jobs if job.name == job_name or job.instance_id == job_name]
        if not matches:
            raise ValueError(f'scheduled-start job not found: {job_name}')
        settings.tasks.scheduled_start.jobs = matches
        return matches
    if require_single:
        if len(jobs) != 1:
            raise ValueError('检测到多个 scheduled-start jobs，请使用 --scheduled-job 指定要覆盖的任务。')
        return jobs
    return jobs


def apply_cli_overrides(args: argparse.Namespace, settings: Settings) -> Settings:
    effective = copy.deepcopy(settings)

    keeper = effective.tasks.keeper
    if getattr(args, 'shutdown_release_after_hours', None) is not None:
        keeper.shutdown_release_after_hours = args.shutdown_release_after_hours
    if getattr(args, 'keeper_trigger_before_hours', None) is not None:
        keeper.keeper_trigger_before_hours = args.keeper_trigger_before_hours
    if getattr(args, 'start_cooldown_minutes', None) is not None:
        keeper.start_cooldown_minutes = args.start_cooldown_minutes
    if getattr(args, 'stop_cooldown_minutes', None) is not None:
        keeper.stop_cooldown_minutes = args.stop_cooldown_minutes
    if getattr(args, 'fallback_to_status_at', None) is not None:
        keeper.fallback_to_status_at = bool(args.fallback_to_status_at)

    scheduled = effective.tasks.scheduled_start
    if getattr(args, 'scheduled_poll_interval', None) is not None:
        scheduled.poll_interval_seconds = args.scheduled_poll_interval

    if getattr(args, 'scheduled_job', None):
        _resolve_job_override_targets(effective, args.scheduled_job, require_single=False)

    job_target_time = getattr(args, 'target_time', None)
    job_advance_hours = getattr(args, 'advance_hours', None)
    if job_target_time is not None or job_advance_hours is not None:
        targets = _resolve_job_override_targets(effective, getattr(args, 'scheduled_job', None), require_single=True)
        for job in targets:
            if job_target_time is not None:
                job.target_time = job_target_time
            if job_advance_hours is not None:
                job.advance_hours = job_advance_hours

    auth_override_fields = (
        'lightweight_mode',
        'runtime_auth_revalidate_seconds',
        'force_refresh_min_interval_seconds',
        'auth_failure_backoff_seconds',
    )
    if any(getattr(args, field, None) is not None for field in auth_override_fields):
        for account in _resolve_account_override_targets(effective, getattr(args, 'account', None)):
            if getattr(args, 'lightweight_mode', None) is not None:
                account.lightweight_mode = args.lightweight_mode
            if getattr(args, 'runtime_auth_revalidate_seconds', None) is not None:
                account.runtime_auth_revalidate_seconds = args.runtime_auth_revalidate_seconds
            if getattr(args, 'force_refresh_min_interval_seconds', None) is not None:
                account.force_refresh_min_interval_seconds = args.force_refresh_min_interval_seconds
            if getattr(args, 'auth_failure_backoff_seconds', None) is not None:
                account.auth_failure_backoff_seconds = args.auth_failure_backoff_seconds
        _sync_primary_auth(effective)

    return effective


def serialize_settings(settings: Settings, *, resolved: bool = False, account_name: str | None = None) -> dict[str, Any]:
    display = copy.deepcopy(settings)
    if account_name:
        display.accounts = _resolve_account_override_targets(display, account_name)
        _sync_primary_auth(display)
    payload = asdict(display)
    if resolved:
        payload['auth']['resolved_auth_runtime_policy'] = asdict(resolve_auth_runtime_policy(display.auth))
        for account_payload, account in zip(payload.get('accounts', []), display.accounts):
            account_payload['resolved_auth_runtime_policy'] = asdict(resolve_auth_runtime_policy(account.to_auth_settings()))
    if payload['auth'].get('authorization'):
        payload['auth']['authorization'] = '<redacted>'
    if payload['auth'].get('autodl_password'):
        payload['auth']['autodl_password'] = '<redacted>'
    for account_payload in payload.get('accounts', []):
        if account_payload.get('authorization'):
            account_payload['authorization'] = '<redacted>'
        if account_payload.get('autodl_password'):
            account_payload['autodl_password'] = '<redacted>'
    notifications = payload.get('notifications', {})
    for channel in ('pushplus', 'serverchan'):
        channel_payload = notifications.get(channel, {})
        if channel_payload.get('token'):
            channel_payload['token'] = '<redacted>'
    email_payload = notifications.get('email', {})
    if email_payload.get('password'):
        email_payload['password'] = '<redacted>'
    return payload


def _has_config_edit_args(args: argparse.Namespace) -> bool:
    keys = (
        'shutdown_release_after_hours',
        'keeper_trigger_before_hours',
        'start_cooldown_minutes',
        'stop_cooldown_minutes',
        'fallback_to_status_at',
        'scheduled_poll_interval',
        'scheduled_job',
        'target_time',
        'advance_hours',
        'lightweight_mode',
        'runtime_auth_revalidate_seconds',
        'force_refresh_min_interval_seconds',
        'auth_failure_backoff_seconds',
    )
    return any(getattr(args, key, None) is not None for key in keys)


def _prompt_optional_text(prompt: str) -> str | None:
    value = input(prompt).strip()
    return value or None


def _prompt_optional_int(prompt: str) -> int | None:
    value = input(prompt).strip()
    return int(value) if value else None


def _prompt_optional_bool(prompt: str) -> bool | None:
    value = input(prompt).strip().lower()
    if not value:
        return None
    if value in {'y', 'yes', 'true', '1'}:
        return True
    if value in {'n', 'no', 'false', '0'}:
        return False
    raise ValueError(f'Invalid boolean input: {value}')


def collect_config_edit_args(args: argparse.Namespace) -> argparse.Namespace:
    if _has_config_edit_args(args):
        return args
    args.lightweight_mode = _prompt_optional_text('lightweight_mode (off/normal/aggressive, blank=skip): ')
    args.runtime_auth_revalidate_seconds = _prompt_optional_int('runtime_auth_revalidate_seconds (blank=skip): ')
    args.force_refresh_min_interval_seconds = _prompt_optional_int('force_refresh_min_interval_seconds (blank=skip): ')
    args.auth_failure_backoff_seconds = _prompt_optional_int('auth_failure_backoff_seconds (blank=skip): ')
    args.shutdown_release_after_hours = _prompt_optional_int('shutdown_release_after_hours (blank=skip): ')
    args.keeper_trigger_before_hours = _prompt_optional_int('keeper_trigger_before_hours (blank=skip): ')
    args.start_cooldown_minutes = _prompt_optional_int('start_cooldown_minutes (blank=skip): ')
    args.stop_cooldown_minutes = _prompt_optional_int('stop_cooldown_minutes (blank=skip): ')
    args.fallback_to_status_at = _prompt_optional_bool('fallback_to_status_at (y/n, blank=skip): ')
    args.scheduled_poll_interval = _prompt_optional_int('scheduled_poll_interval (blank=skip): ')
    args.scheduled_job = _prompt_optional_text('scheduled_job name/instance_id (blank=skip): ')
    args.target_time = _prompt_optional_text('target_time (HH:MM, blank=skip): ')
    args.advance_hours = _prompt_optional_int('advance_hours (blank=skip): ')
    return args


def _ensure_account_payloads(payload: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    accounts_payload = payload.get('accounts')
    if isinstance(accounts_payload, list) and accounts_payload:
        return accounts_payload
    payload['accounts'] = [
        {
            'name': account.name,
            'enabled': account.enabled,
            'authorization': account.authorization,
            'autodl_phone': account.autodl_phone,
            'autodl_password': account.autodl_password,
            'cache_file': account.cache_file,
            'cache_max_age_seconds': account.cache_max_age_seconds,
            'lightweight_mode': account.lightweight_mode,
            'runtime_auth_revalidate_seconds': account.runtime_auth_revalidate_seconds,
            'force_refresh_min_interval_seconds': account.force_refresh_min_interval_seconds,
            'auth_failure_backoff_seconds': account.auth_failure_backoff_seconds,
        }
        for account in settings.accounts
    ]
    return payload['accounts']


def _select_account_payloads(payload: dict[str, Any], settings: Settings, account_name: str | None) -> list[dict[str, Any]]:
    accounts_payload = _ensure_account_payloads(payload, settings)
    if account_name:
        matches = [item for item in accounts_payload if str(item.get('name', '')).strip() == account_name]
        if not matches:
            raise ValueError(f'Account not found or disabled: {account_name}')
        return matches
    return accounts_payload


def _ensure_task_payload(payload: dict[str, Any], task_name: str) -> dict[str, Any]:
    tasks_payload = payload.setdefault('tasks', {})
    task_payload = tasks_payload.setdefault(task_name, {})
    return task_payload


def _select_job_payloads(payload: dict[str, Any], settings: Settings, job_name: str | None, *, require_single: bool) -> list[dict[str, Any]]:
    scheduled_payload = _ensure_task_payload(payload, 'scheduled_start')
    jobs_payload = scheduled_payload.setdefault('jobs', [])
    if not jobs_payload and settings.tasks.scheduled_start.jobs:
        for job in settings.tasks.scheduled_start.jobs:
            jobs_payload.append(
                {
                    'instance_id': job.instance_id,
                    'name': job.name,
                    'target_time': job.target_time,
                    'advance_hours': job.advance_hours,
                    'timezone': job.timezone,
                    **({'selector': asdict(job.selector)} if job.selector is not None else {}),
                    **({'priority': [asdict(item) for item in job.priority]} if job.priority else {}),
                }
            )
    if job_name:
        matches = [item for item in jobs_payload if item.get('name') == job_name or item.get('instance_id') == job_name]
        if not matches:
            raise ValueError(f'scheduled-start job not found: {job_name}')
        return matches
    if require_single:
        if len(jobs_payload) != 1:
            raise ValueError('检测到多个 scheduled-start jobs，请使用 --scheduled-job 指定要改写的任务。')
        return jobs_payload
    return jobs_payload


def validate_settings(
    settings: Settings,
    purpose: str = 'all',
    *,
    get_enabled_accounts_fn: Callable[[Settings], list[AccountSettings]] = get_enabled_accounts,
    time_pattern=TIME_RE,
    lightweight_modes=LIGHTWEIGHT_MODES,
) -> list[str]:
    errors: list[str] = []
    enabled_accounts = get_enabled_accounts_fn(settings)
    if not enabled_accounts:
        errors.append('At least one enabled account is required.')
    for account in enabled_accounts:
        if purpose != 'test-notify' and not account.authorization and not (account.autodl_phone and account.autodl_password):
            errors.append(f'account {account.name}: Either Authorization or both AUTODL_PHONE and AUTODL_PASSWORD are required.')
        if account.cache_max_age_seconds <= 0:
            errors.append(f'account {account.name}: auth.cache_max_age_seconds must be a positive integer.')
        if account.lightweight_mode not in lightweight_modes:
            errors.append(f'account {account.name}: lightweight_mode must be one of {sorted(lightweight_modes)}.')
        if account.runtime_auth_revalidate_seconds < 0:
            errors.append(f'account {account.name}: runtime_auth_revalidate_seconds must be zero or a positive integer.')
        if account.force_refresh_min_interval_seconds < 0:
            errors.append(f'account {account.name}: force_refresh_min_interval_seconds must be zero or a positive integer.')
        if account.auth_failure_backoff_seconds < 0:
            errors.append(f'account {account.name}: auth_failure_backoff_seconds must be zero or a positive integer.')

    keeper = settings.tasks.keeper
    if purpose in {'all', 'run-daemon', 'run-all', 'run-keeper', 'validate', 'healthcheck'}:
        if keeper.shutdown_release_after_hours <= 0:
            errors.append('keeper.shutdown_release_after_hours must be a positive integer.')
        if keeper.keeper_trigger_before_hours < 0:
            errors.append('keeper.keeper_trigger_before_hours must be zero or a positive integer.')
        if keeper.start_cooldown_minutes < 0:
            errors.append('keeper.start_cooldown_minutes must be zero or a positive integer.')
        if keeper.stop_cooldown_minutes < 0:
            errors.append('keeper.stop_cooldown_minutes must be zero or a positive integer.')

    if not settings.storage.database_file:
        errors.append('storage.database_file is required.')

    scheduled = settings.tasks.scheduled_start
    if purpose in {'all', 'run-daemon', 'run-all', 'run-scheduled-start', 'validate', 'healthcheck'} and scheduled.enabled:
        if scheduled.poll_interval_seconds < 5:
            errors.append('scheduled_start.poll_interval_seconds must be at least 5.')
        if not scheduled.jobs:
            errors.append('scheduled_start.jobs must not be empty when scheduled_start is enabled.')
        for index, job in enumerate(scheduled.jobs, start=1):
            label = job.name or job.instance_id or f'job#{index}'
            if bool(job.instance_id) == bool(job.selector):
                errors.append(f'scheduled_start job {label} must provide exactly one of instance_id or selector.')
            if not time_pattern.match(job.target_time):
                errors.append(f'scheduled_start job {label} target_time must be HH:MM.')
            if job.advance_hours <= 0:
                errors.append(f'scheduled_start job {label} advance_hours must be a positive integer.')
            if job.selector is not None:
                if not job.selector.gpu_model:
                    errors.append(f'scheduled_start job {label} selector.gpu_model is required.')
                if job.selector.gpu_count <= 0:
                    errors.append(f'scheduled_start job {label} selector.gpu_count must be a positive integer.')
                if job.priority:
                    for entry in job.priority:
                        if not (entry.instance_id or entry.region or entry.machine_alias):
                            errors.append(f'scheduled_start job {label} priority entries must define at least one matcher.')

    if purpose in {'all', 'run-daemon', 'run-all', 'run-keeper', 'run-scheduled-start', 'validate', 'test-notify', 'healthcheck'} and settings.notifications.pushplus.enabled and not settings.notifications.pushplus.token:
        errors.append('pushplus is enabled but token is missing.')
    if purpose in {'all', 'run-daemon', 'run-all', 'run-keeper', 'run-scheduled-start', 'validate', 'test-notify', 'healthcheck'} and settings.notifications.serverchan.enabled and not settings.notifications.serverchan.token:
        errors.append('serverchan is enabled but token is missing.')
    if purpose in {'all', 'run-daemon', 'run-all', 'run-keeper', 'run-scheduled-start', 'validate', 'test-notify', 'healthcheck'} and settings.notifications.email.enabled:
        email = settings.notifications.email
        if not email.smtp_host or not email.username or not email.password or not email.to:
            errors.append('email is enabled but smtp_host/username/password/to are incomplete.')

    return errors


def run_scheduled_start_cycle(
    *,
    settings: Settings,
    headed: bool,
    state_file: str | Path,
    account_name: str | None = None,
    force_run_now: bool = False,
    store: SQLiteStore | None = None,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    get_enabled_accounts_fn: Callable[[Settings], list[AccountSettings]] = get_enabled_accounts,
    build_client_fn: Callable[..., object] = build_client,
    run_scheduled_start_job_fn: Callable[..., object] = run_scheduled_start_job,
    build_notifiers_fn: Callable[[NotificationSettings], list[object]] = build_notifiers,
) -> list[Any]:
    state_store = StateStore(state_file)
    notification_manager = NotificationManager(build_notifiers_fn(settings.notifications))
    store = store or create_store_fn(settings)
    results: list[Any] = []

    if not settings.tasks.scheduled_start.enabled:
        logger.info('scheduled_start.summary account=%s job=- target=- schedule=- poll=%ss status=skip reason=scheduled_disabled', account_name or 'all', settings.tasks.scheduled_start.poll_interval_seconds)
        return results

    for account in select_accounts_fn(settings, account_name):
        if not get_task_enabled(store, account.name, 'scheduled_start', default_enabled=settings.tasks.scheduled_start.enabled):
            logger.info(
                'scheduled_start.summary account=%s job=- target=- schedule=- poll=%ss status=skip reason=task_paused',
                account.name,
                settings.tasks.scheduled_start.poll_interval_seconds,
            )
            continue
        try:
            client = build_client_fn(settings, headed, account=account, store=store)
            show_prefix = len(get_enabled_accounts_fn(settings)) > 1 or account.name != 'default'
            job_name_prefix = f'{account.name}:' if show_prefix else ''
            effective_jobs = apply_runtime_controls_to_scheduled_jobs(store, account.name, list(settings.tasks.scheduled_start.jobs))
            for job in effective_jobs:
                job_identity = scheduled_job_identity(job)
                job_signature = scheduled_job_signature(job)
                runtime = ScheduledStartJobRuntime(
                    job_name=f'{job_name_prefix}{job.name or job.instance_id or (job.selector.gpu_model if job.selector else "scheduled-start")}',
                    instance_id=job.instance_id,
                    target_time=job.target_time,
                    advance_hours=job.advance_hours,
                    timezone=job.timezone,
                    poll_interval_seconds=settings.tasks.scheduled_start.poll_interval_seconds,
                    selector=job.selector,
                    priority=job.priority,
                )
                now = datetime.now()
                window_key = runtime.window_key(now)
                if store.has_scheduled_success(
                    account_name=account.name,
                    job_name=job_identity,
                    window_key=window_key,
                    job_signature=job_signature,
                    legacy_match_payload={
                        'instance_id': str(job.instance_id or ''),
                        'target_time': str(job.target_time or ''),
                        'selector': {
                            'regions': list(job.selector.regions or []),
                            'gpu_model': str(job.selector.gpu_model or ''),
                            'gpu_count': int(job.selector.gpu_count or 1),
                            'charge_types': list(job.selector.charge_types or []),
                        } if job.selector is not None else None,
                        'selector_summary': runtime.selector_summary(),
                    },
                ):
                    _log_scheduled_start_summary(
                        account_name=account.name,
                        job_name=job_identity,
                        target_time=job.target_time,
                        advance_hours=int(job.advance_hours or 0),
                        schedule_mode=str(getattr(job, 'schedule_mode', 'daily') or 'daily'),
                        poll_interval_seconds=settings.tasks.scheduled_start.poll_interval_seconds,
                        status='skip',
                        reason='window_already_succeeded',
                        now=now,
                    )
                    continue
                result = run_scheduled_start_job_fn(
                    client=client,
                    notifier=notification_manager,
                    state_store=state_store,
                    job=runtime,
                    now=now,
                    force_run_now=force_run_now,
                )
                payload = asdict(result)
                payload.update(
                    {
                        'job_signature': job_signature,
                        'job_instance_id': str(job.instance_id or ''),
                        'advance_hours': int(job.advance_hours or 0),
                        'schedule_mode': str(getattr(job, 'schedule_mode', 'daily') or 'daily'),
                        'timezone': str(getattr(job, 'timezone', 'Asia/Shanghai') or 'Asia/Shanghai'),
                        'selector': {
                            'regions': list(job.selector.regions or []),
                            'gpu_model': str(job.selector.gpu_model or ''),
                            'gpu_count': int(job.selector.gpu_count or 1),
                            'charge_types': list(job.selector.charge_types or []),
                        } if job.selector is not None else None,
                    }
                )
                store.add_scheduled_history(
                    account.name,
                    job_identity,
                    result.instance_id,
                    window_key,
                    result.result,
                    result.reason,
                    payload,
                    result.event_type,
                    result.severity,
                    result.summary,
                )
                if getattr(job, 'schedule_mode', 'daily') == 'once' and result.result in {'started', 'already_running', 'power_on_submitted', 'deadline_failed', 'instance_missing'}:
                    store.upsert_scheduled_job_control(
                        account.name,
                        job_identity,
                        enabled=False,
                        target_time_override='',
                        advance_hours_override=None,
                        source='scheduled_once_complete',
                    )
                _log_scheduled_start_summary(
                    account_name=account.name,
                    job_name=job_identity,
                    target_time=job.target_time,
                    advance_hours=int(job.advance_hours or 0),
                    schedule_mode=str(getattr(job, 'schedule_mode', 'daily') or 'daily'),
                    poll_interval_seconds=settings.tasks.scheduled_start.poll_interval_seconds,
                    status=str(result.result or ''),
                    reason=str(result.reason or ''),
                    now=now,
                    instance_id=str(result.instance_id or ''),
                    candidate_count=result.candidate_count,
                )
                results.append(result)
        except Exception as exc:
            logger.exception('scheduled-start 执行失败 账号=%s', account.name)
            store.add_event(account.name, 'scheduled_start', 'error', f'scheduled-start 执行失败: {exc}', payload={'error': str(exc)})
    return results


def run_keeper_only(
    *,
    settings: Settings,
    headed: bool,
    account_name: str | None = None,
    store: SQLiteStore | None = None,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    build_client_fn: Callable[..., object] = build_client,
    run_keeper_cycle_fn: Callable[..., list[Any]] = run_keeper_cycle,
    build_notifiers_fn: Callable[[NotificationSettings], list[object]] = build_notifiers,
) -> list[Any]:
    store = store or create_store_fn(settings)
    results: list[Any] = []
    executed_lines: list[str] = []
    failed_lines: list[str] = []
    for account in select_accounts_fn(settings, account_name):
        if not get_task_enabled(store, account.name, 'keeper', default_enabled=settings.tasks.keeper.enabled):
            logger.info('[Keeper检查] 账号=%s 结果=跳过 原因=任务已暂停 下次保活=- 释放时间=- 接管窗口=-', account.name)
            continue
        try:
            client = build_client_fn(settings, headed, account=account, store=store)
            account_results = run_keeper_cycle_fn(
                client=client,
                shutdown_release_after_hours=settings.tasks.keeper.shutdown_release_after_hours,
                keeper_trigger_before_hours=settings.tasks.keeper.keeper_trigger_before_hours,
                power_on_wait_seconds=settings.tasks.keeper.power_on_wait_seconds,
                power_off_wait_seconds=settings.tasks.keeper.power_off_wait_seconds,
                start_cooldown_minutes=settings.tasks.keeper.start_cooldown_minutes,
                stop_cooldown_minutes=settings.tasks.keeper.stop_cooldown_minutes,
                fallback_to_status_at=settings.tasks.keeper.fallback_to_status_at,
                store=store,
                account_name=account.name,
            )
            for result in account_results:
                result_label = {
                    'keeper_executed': '已执行保活',
                    'skip_not_due': '跳过',
                    'skip_running': '跳过',
                    'keeper_failed_power_on': '失败',
                    'keeper_failed_power_off': '失败',
                }.get(result.result, result.result or '-')
                reason_label = {
                    'keeper_window_reached': '到达保活窗口',
                    'before_next_keeper_time': '未到保活窗口',
                    'instance_running': '实例正在运行',
                    'power_on_failed': '开机失败',
                    'power_off_failed': '关机失败',
                }.get(result.reason, result.reason or '-')
                extra = [
                    f'下次保活={_format_local_time_label(result.next_keeper_time)}',
                    f'释放时间={_format_local_time_label(result.release_deadline)}',
                    f'接管窗口={_format_keeper_window(next_keeper_time=result.next_keeper_time, release_deadline=result.release_deadline)}',
                ]
                if result.shutdown_duration_seconds is not None:
                    extra.append(f'关机时长={format_duration_seconds(result.shutdown_duration_seconds)}')
                logger.info(
                    '[Keeper检查] 账号=%s 实例=%s 结果=%s 原因=%s %s',
                    account.name,
                    result.instance_id,
                    result_label,
                    reason_label,
                    ' '.join(extra).strip(),
                )
                if result.result == 'keeper_executed':
                    executed_lines.append(f'{account.name}:{result.instance_id}')
                if result.result in {'keeper_failed_power_on', 'keeper_failed_power_off'}:
                    failed_lines.append(f'{account.name}:{result.instance_id}')
            results.extend(account_results)
        except Exception as exc:
            logger.exception('keeper 执行失败 账号=%s', account.name)
            store.add_event(account.name, 'keeper', 'error', f'keeper 执行失败: {exc}', payload={'error': str(exc)})
    if executed_lines or failed_lines:
        lines: list[str] = []
        if executed_lines:
            lines.append('已执行: ' + ', '.join(executed_lines))
        if failed_lines:
            lines.append('失败: ' + ', '.join(failed_lines))
        NotificationManager(build_notifiers_fn(settings.notifications)).notify_task_result(
            task_type='keeper',
            title='keeper 执行结果',
            message='\n'.join(lines),
        )
    return results


def run_cycle(
    *,
    settings: Settings,
    headed: bool,
    state_file: str | Path,
    account_name: str | None = None,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    run_keeper_only_fn: Callable[..., list[Any]] = run_keeper_only,
    run_scheduled_start_cycle_fn: Callable[..., list[Any]] = run_scheduled_start_cycle,
) -> list[Any]:
    store = create_store_fn(settings)
    if settings.tasks.keeper.enabled:
        run_keeper_only_fn(settings=settings, headed=headed, account_name=account_name, store=store)
    return run_scheduled_start_cycle_fn(
        settings=settings,
        headed=headed,
        state_file=state_file,
        account_name=account_name,
        store=store,
    )


def execute_variant_cycle(
    *,
    mode: str,
    args: argparse.Namespace,
    load_settings_fn: Callable[[str], Settings],
    create_store_fn: Callable[[Settings], SQLiteStore],
    run_keeper_only_fn: Callable[..., list[Any]],
    run_scheduled_start_cycle_fn: Callable[..., list[Any]],
    run_cycle_fn: Callable[..., list[Any]],
) -> tuple[Settings, SQLiteStore, list[Any]]:
    settings = apply_cli_overrides(args, load_settings_fn(args.config))
    store = create_store_fn(settings)
    results: list[Any]
    if mode == 'keeper':
        results = run_keeper_only_fn(settings=settings, headed=args.headed, account_name=args.account, store=store)
    elif mode == 'scheduled_start':
        results = run_scheduled_start_cycle_fn(settings=settings, headed=args.headed, state_file=args.state_file, account_name=args.account, store=store)
    else:
        results = run_cycle_fn(settings=settings, headed=args.headed, state_file=args.state_file, account_name=args.account, create_store_fn=lambda _settings: store, run_keeper_only_fn=run_keeper_only_fn, run_scheduled_start_cycle_fn=run_scheduled_start_cycle_fn)
    return settings, store, results


def daemon_dispatch(
    *,
    args: argparse.Namespace,
    load_settings_fn: Callable[[str], Settings],
    create_store_fn: Callable[[Settings], SQLiteStore],
    run_keeper_only_fn: Callable[..., list[Any]],
    run_scheduled_start_cycle_fn: Callable[..., list[Any]],
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    state: dict[str, Any] | None = None,
    now_fn: Callable[[], datetime] = datetime.now,
) -> list[Any]:
    settings = state.get('settings') if state is not None else None
    if settings is None:
        settings = apply_cli_overrides(args, load_settings_fn(args.config))
        if state is not None:
            state['settings'] = settings
    store = create_store_fn(settings)
    if state is not None:
        settings = _maybe_reload_daemon_settings(
            args=args,
            store=store,
            state=state,
            load_settings_fn=load_settings_fn,
            validate_settings_fn=validate_settings_fn,
        )
    else:
        settings = apply_cli_overrides(args, load_settings_fn(args.config))
    results: list[Any] = []
    now = now_fn()
    scheduled_last_run_raw = str(store.get_runtime_value('last_run:scheduled_start', '') or '')
    keeper_due = settings.tasks.keeper.enabled and task_due(store, 'keeper', interval_seconds=max(60, settings.tasks.keeper.interval_minutes * 60), now=now)
    scheduled_due = (
        settings.tasks.scheduled_start.enabled
        and bool(settings.tasks.scheduled_start.jobs)
        and task_due(store, 'scheduled_start', interval_seconds=max(5, settings.tasks.scheduled_start.poll_interval_seconds), now=now)
    )
    if keeper_due:
        results.extend(run_keeper_only_fn(settings=settings, headed=args.headed, account_name=args.account, store=store))
        mark_task_run(store, 'keeper', now=now)
    if scheduled_due:
        results.extend(run_scheduled_start_cycle_fn(settings=settings, headed=args.headed, state_file=args.state_file, account_name=args.account, store=store))
        mark_task_run(store, 'scheduled_start', now=now)
    scheduled_gap_text = '首次执行'
    if scheduled_last_run_raw:
        try:
            previous_scheduled = datetime.fromisoformat(scheduled_last_run_raw)
            if previous_scheduled.tzinfo is None:
                previous_scheduled = previous_scheduled.replace(tzinfo=timezone.utc)
            scheduled_gap_text = f'{max(0.0, (now.astimezone(timezone.utc) - previous_scheduled.astimezone(timezone.utc)).total_seconds()):.1f}秒'
        except ValueError:
            scheduled_gap_text = '时间格式异常'
    if settings.tasks.keeper.enabled:
        keeper_log_status = '本轮执行' if keeper_due else '本轮未执行'
    else:
        keeper_log_status = '未启用'
    if settings.tasks.scheduled_start.enabled and bool(settings.tasks.scheduled_start.jobs):
        scheduled_log_status = '本轮执行' if scheduled_due else '未到下次轮询间隔'
    else:
        scheduled_log_status = '未启用'
    logger.info(
        '[后台轮询] Keeper状态=%s 抢机状态=%s 抢机任务数=%s 本轮结果数=%s 距上次抢机轮询=%s 抢机间隔阈值=%s秒',
        keeper_log_status,
        scheduled_log_status,
        len(settings.tasks.scheduled_start.jobs),
        len(results),
        scheduled_gap_text,
        max(5, settings.tasks.scheduled_start.poll_interval_seconds),
    )
    return results


def scheduled_daemon_should_exit(*, settings: Settings, store: SQLiteStore, account_name: str | None = None) -> bool:
    def account_has_enabled_jobs(name: str) -> bool:
        if not get_task_enabled(store, name, 'scheduled_start', default_enabled=settings.tasks.scheduled_start.enabled):
            return False
        effective_jobs = apply_runtime_controls_to_scheduled_jobs(store, name, list(settings.tasks.scheduled_start.jobs))
        return bool(effective_jobs)

    if account_name:
        return not account_has_enabled_jobs(account_name)

    account_names = [account.name for account in settings.accounts if account.enabled] if settings.accounts else ['default']
    return not any(account_has_enabled_jobs(name) for name in account_names)


def watch_instance(
    *,
    client,
    keeper_settings=None,
    instance_id: str,
    interval_seconds: int,
    json_output: bool,
    output: TextIO,
    sleep_fn=time.sleep,
    max_iterations: int | None = None,
    account_name: str = '',
    normalize_instance_debug_fn: Callable[..., dict[str, object]],
    extract_watch_fields_fn: Callable[..., dict[str, object]],
    format_watch_change_fn: Callable[[dict[str, object]], str],
) -> int:
    previous_snapshot: dict[str, object] | None = None
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        current = None
        for item in client.list_instances():
            if item.get('uuid') == instance_id:
                current = normalize_instance_debug_fn(item, keeper_settings=keeper_settings, account_name=account_name)
                break
        if current is None:
            missing_payload = {'account': account_name, 'instance_id': instance_id, 'missing': True} if account_name else {'instance_id': instance_id, 'missing': True}
            print(json.dumps(missing_payload, ensure_ascii=False) if json_output else f'instance_id={instance_id} missing=true', file=output)
        elif json_output:
            print(json.dumps(current, ensure_ascii=False), file=output)
        else:
            watch_fields = extract_watch_fields_fn(current, keeper_settings=keeper_settings)
            if previous_snapshot != watch_fields:
                print(format_watch_change_fn(watch_fields), file=output)
                previous_snapshot = watch_fields
        output.flush()
        iteration += 1
        if max_iterations is not None and iteration >= max_iterations:
            break
        sleep_fn(interval_seconds)
    return 0


def probe_path_writable(path: str | Path) -> bool:
    probe_path = Path(path)
    try:
        probe_path.parent.mkdir(parents=True, exist_ok=True)
        with open(probe_path, 'a', encoding='utf-8'):
            pass
        return True
    except OSError:
        return False


def collect_healthcheck_errors(
    *,
    settings: Settings,
    state_file: str | Path,
    lock_file: str | Path,
    smoke: bool,
    headed: bool,
    permission_probe: Callable[[str | Path], bool] = probe_path_writable,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    get_enabled_accounts_fn: Callable[[Settings], list[AccountSettings]] = get_enabled_accounts,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    build_client_fn: Callable[..., object] = build_client,
) -> list[str]:
    errors = validate_settings_fn(settings, purpose='healthcheck')
    for account in get_enabled_accounts_fn(settings):
        if not permission_probe(account.cache_file):
            errors.append(f'Auth cache file is not writable for account {account.name}: {account.cache_file}')
    if not permission_probe(settings.storage.database_file):
        errors.append(f'SQLite database is not writable: {settings.storage.database_file}')
    if not permission_probe(state_file):
        errors.append(f'State file is not writable: {state_file}')
    if not permission_probe(lock_file):
        errors.append(f'Lock file is not writable: {lock_file}')
    try:
        store = create_store_fn(settings)
        if store.schema_version() != SQLiteStore.SCHEMA_VERSION:
            errors.append(f'Unexpected SQLite schema version: {store.schema_version()}')
    except Exception as exc:
        errors.append(f'SQLite check failed: {exc}')
        store = None
    if smoke:
        for account in get_enabled_accounts_fn(settings):
            try:
                client = build_client_fn(settings, headed, account=account, store=store)
                client.list_instances()
            except Exception as exc:
                errors.append(f'Smoke check failed for account {account.name}: {exc}')
    return errors


def command_run_variant(
    args: argparse.Namespace,
    mode: str,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    file_lock_cls: type[FileLock] = FileLock,
    scheduler_cls: type[BlockingScheduler] = BlockingScheduler,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    run_keeper_only_fn: Callable[..., list[Any]] = run_keeper_only,
    run_scheduled_start_cycle_fn: Callable[..., list[Any]] = run_scheduled_start_cycle,
    run_cycle_fn: Callable[..., list[Any]] = run_cycle,
    compute_interval_for_mode_fn: Callable[[Settings, str], int] = compute_interval_for_mode,
    compute_dispatch_interval_seconds_fn: Callable[[Settings], int] = compute_dispatch_interval_seconds,
    alert_auth_failure_fn: Callable[[str], None] = alert_auth_failure,
    daemon_dispatch_fn: Callable[..., list[Any]] = daemon_dispatch,
) -> int:
    try:
        settings = apply_cli_overrides(args, load_settings_fn(args.config))
        validation_purpose = 'run-daemon' if mode == 'all' else 'run-keeper' if mode == 'keeper' else 'run-scheduled-start'
        errors = validate_settings_fn(settings, purpose=validation_purpose)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        with file_lock_cls(args.lock_file):
            store = create_store_fn(settings)
            daemon_state: dict[str, Any] = {'settings': settings}
            heartbeat_mode = 'all' if mode == 'all' else mode
            heartbeat_origin = os.environ.get('AUTODL_HELPER_DAEMON_ORIGIN', 'cli')
            heartbeat_account = args.account or ''
            mark_config_reload_success(
                store,
                generation=read_config_reload_status(store)['requested_generation'],
                config_mtime=_config_mtime_value(args.config),
            )
            if mode == 'all':
                daemon_dispatch_fn(
                    args=args,
                    load_settings_fn=load_settings_fn,
                    create_store_fn=create_store_fn,
                    run_keeper_only_fn=run_keeper_only_fn,
                    run_scheduled_start_cycle_fn=run_scheduled_start_cycle_fn,
                    validate_settings_fn=validate_settings_fn,
                    state=daemon_state,
                )
            elif mode == 'keeper':
                run_keeper_only_fn(settings=settings, headed=args.headed, account_name=args.account, store=store)
            else:
                run_scheduled_start_cycle_fn(settings=settings, headed=args.headed, state_file=args.state_file, account_name=args.account, store=store)
            if args.run_once:
                return 0
            if mode == 'scheduled_start':
                current_settings = apply_cli_overrides(args, load_settings_fn(args.config))
                if scheduled_daemon_should_exit(settings=current_settings, store=store, account_name=args.account):
                    clear_daemon_heartbeat(store)
                    return 0
            scheduler = scheduler_cls()
            mark_daemon_heartbeat(store, mode=heartbeat_mode, account=heartbeat_account, origin=heartbeat_origin)
            scheduler.add_job(
                mark_daemon_heartbeat,
                'interval',
                seconds=DAEMON_HEARTBEAT_INTERVAL_SECONDS,
                kwargs={
                    'store': store,
                    'mode': heartbeat_mode,
                    'account': heartbeat_account,
                    'origin': heartbeat_origin,
                },
                coalesce=True,
                max_instances=1,
                misfire_grace_time=5,
            )
            if mode == 'all':
                scheduler.add_job(
                    daemon_dispatch_fn,
                    'interval',
                    seconds=compute_dispatch_interval_seconds_fn(settings),
                    kwargs={
                        'args': args,
                        'load_settings_fn': load_settings_fn,
                        'create_store_fn': create_store_fn,
                        'run_keeper_only_fn': run_keeper_only_fn,
                        'run_scheduled_start_cycle_fn': run_scheduled_start_cycle_fn,
                        'validate_settings_fn': validate_settings_fn,
                        'state': daemon_state,
                    },
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=30,
                )
            else:
                if mode == 'keeper':
                    job_func = run_keeper_only_fn
                    kwargs: dict[str, Any] = {'settings': settings, 'headed': args.headed, 'account_name': args.account}
                else:
                    def scheduled_start_daemon_tick() -> list[Any]:
                        current_settings = apply_cli_overrides(args, load_settings_fn(args.config))
                        results = run_scheduled_start_cycle_fn(
                            settings=current_settings,
                            headed=args.headed,
                            state_file=args.state_file,
                            account_name=args.account,
                            store=store,
                        )
                        if scheduled_daemon_should_exit(settings=current_settings, store=store, account_name=args.account):
                            clear_daemon_heartbeat(store)
                            scheduler.shutdown(wait=False)
                        return results

                    job_func = scheduled_start_daemon_tick
                    kwargs = {}
                scheduler.add_job(
                    job_func,
                    'interval',
                    seconds=compute_interval_for_mode_fn(settings, mode),
                    kwargs=kwargs,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=30,
                )
            try:
                scheduler.start()
                return 0
            finally:
                clear_daemon_heartbeat(store)
                clear_daemon_launch_state(store)
    except LockAcquisitionError:
        logger.warning('Another autodl-helper process is already running.')
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except AuthError as exc:
        alert_auth_failure_fn(str(exc))
        return 1
    except (KeyboardInterrupt, SystemExit):
        logger.info('Exiting autodl-helper.')
        return 0


def command_list_instances(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    build_client_fn: Callable[..., object] = build_client,
    get_enabled_accounts_fn: Callable[[Settings], list[AccountSettings]] = get_enabled_accounts,
    normalize_instance_fn: Callable[..., dict[str, object]],
    format_instances_table_fn: Callable[[list[dict[str, object]]], str],
    normalize_instance_debug_fn: Callable[..., dict[str, object]] | None = None,
) -> int:
    settings = load_settings_fn(args.config)
    errors = validate_settings_fn(settings, purpose='list-instances')
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    store = create_store_fn(settings)
    rows: list[dict[str, object]] = []
    multi_account = len(get_enabled_accounts_fn(settings)) > 1 or bool(args.account)
    for account in select_accounts_fn(settings, args.account):
        client = build_client_fn(settings, args.headed, account=account, store=store)
        if args.json and normalize_instance_debug_fn is not None:
            rows.extend(
                normalize_instance_debug_fn(
                    item,
                    keeper_settings=settings.tasks.keeper,
                    account_name=account.name if multi_account else '',
                )
                for item in client.list_instances()
            )
        else:
            rows.extend(normalize_instance_fn(item, account_name=account.name if multi_account else '') for item in client.list_instances())
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(format_instances_table_fn(rows))
    return 0


def command_accounts(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    account_status_rows_fn: Callable[..., list[dict[str, Any]]] = account_status_rows,
) -> int:
    settings = load_settings_fn(args.config)
    store = create_store_fn(settings)
    try:
        rows = account_status_rows_fn(settings, store, account_name=getattr(args, 'account', None))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if getattr(args, 'json', False):
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print('账号状态')
    print('=' * 72)
    header = f"{'account':<16} {'enabled':<7} {'status':<18} {'source':<12} {'cache':<20} {'creds':<5} {'cfg':<5} {'mode':<10}"
    print(header)
    print('-' * len(header))
    for row in rows:
        cached_at = row.get('cached_at_iso') or '-'
        if len(cached_at) > 19:
            cached_at = cached_at[:19]
        print(
            f"{row['account_name']:<16} "
            f"{('yes' if row['enabled'] else 'no'):<7} "
            f"{row['status_label']:<18} "
            f"{row['auth_source_label']:<12} "
            f"{cached_at:<20} "
            f"{('yes' if row['has_credentials'] else 'no'):<5} "
            f"{('yes' if row['has_config_token'] else 'no'):<5} "
            f"{row['lightweight_mode']:<10}"
        )
    return 0


def command_login(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    resolve_authorization_fn: Callable[..., str] = resolve_authorization,
) -> int:
    settings = load_settings_fn(args.config)
    store = create_store_fn(settings)
    try:
        if getattr(args, 'all', False):
            accounts = select_accounts_fn(settings, None)
        elif getattr(args, 'account', None):
            accounts = select_accounts_fn(settings, args.account)
        else:
            accounts = select_accounts_fn(settings, None, require_explicit_for_multi=True)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    failed = False
    for account in accounts:
        try:
            resolve_authorization_fn(
                account.to_auth_settings(),
                headed=args.headed,
                force_refresh=True,
                store=store,
                account_name=account.name,
            )
            state = inspect_auth_state(account.to_auth_settings(), store=store, account_name=account.name)
            print(
                f"登录成功: account={account.name} status={_account_status_label(str(state['status']))} "
                f"source={_account_source_label(str(state['auth_source']))}"
            )
        except AuthError as exc:
            failed = True
            print(f'登录失败: account={account.name} {exc}', file=sys.stderr)
    return 1 if failed else 0


def command_inspect_instance(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    build_client_fn: Callable[..., object] = build_client,
    normalize_instance_debug_fn: Callable[..., dict[str, object]],
) -> int:
    settings = load_settings_fn(args.config)
    errors = validate_settings_fn(settings, purpose='inspect-instance')
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    try:
        account = select_accounts_fn(settings, args.account, require_explicit_for_multi=True)[0]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    store = create_store_fn(settings)
    client = build_client_fn(settings, args.headed, account=account, store=store)
    for item in client.list_instances():
        if item.get('uuid') == args.instance_id:
            print(json.dumps(normalize_instance_debug_fn(item, keeper_settings=settings.tasks.keeper, account_name=account.name), ensure_ascii=False, indent=2))
            return 0
    print(f'Instance {args.instance_id} not found.', file=sys.stderr)
    return 1


def command_watch_instance(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    build_client_fn: Callable[..., object] = build_client,
    watch_instance_fn: Callable[..., int] = watch_instance,
    normalize_instance_debug_fn: Callable[..., dict[str, object]],
    extract_watch_fields_fn: Callable[..., dict[str, object]],
    format_watch_change_fn: Callable[[dict[str, object]], str],
) -> int:
    settings = load_settings_fn(args.config)
    errors = validate_settings_fn(settings, purpose='watch-instance')
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    try:
        account = select_accounts_fn(settings, args.account, require_explicit_for_multi=True)[0]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    store = create_store_fn(settings)
    client = build_client_fn(settings, args.headed, account=account, store=store)
    return watch_instance_fn(
        client=client,
        keeper_settings=settings.tasks.keeper,
        instance_id=args.instance_id,
        interval_seconds=args.interval,
        json_output=args.json,
        output=sys.stdout,
        account_name=account.name,
        normalize_instance_debug_fn=normalize_instance_debug_fn,
        extract_watch_fields_fn=extract_watch_fields_fn,
        format_watch_change_fn=format_watch_change_fn,
    )


def command_keeper_probe(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    build_client_fn: Callable[..., object] = build_client,
    evaluate_keeper_instance_fn: Callable[..., Any] = evaluate_keeper_instance,
    format_keeper_probe_line_fn: Callable[..., str],
) -> int:
    settings = load_settings_fn(args.config)
    errors = validate_settings_fn(settings, purpose='run-keeper')
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    store = create_store_fn(settings)
    lines: list[str] = []
    for account in select_accounts_fn(settings, args.account, require_explicit_for_multi=False):
        client = build_client_fn(settings, args.headed, account=account, store=store)
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
            executed = bool(result.release_deadline and store.was_keeper_executed_in_cycle(account.name, result.instance_id, result.release_deadline))
            if args.only_eligible and not result.eligible:
                continue
            lines.append(format_keeper_probe_line_fn(result, account_name=account.name, executed_in_cycle=executed))
    if lines:
        print('\n'.join(lines))
    return 0


def command_history(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    history_row_to_json_fn: Callable[[Any], dict[str, object]],
    format_history_table_fn: Callable[[Sequence[Any]], str],
) -> int:
    settings = load_settings_fn(args.config)
    store = create_store_fn(settings)
    try:
        if args.account:
            select_accounts_fn(settings, args.account)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    rows = store.read_history(account_name=args.account, task_type=args.task, event_type=args.event_type, limit=args.limit)
    if not rows:
        print('No history.')
        return 0
    if args.json:
        print(json.dumps([history_row_to_json_fn(row) for row in rows], ensure_ascii=False, indent=2))
        return 0
    print(format_history_table_fn(rows))
    return 0


def command_auth_report(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    auth_report_row_to_json_fn: Callable[[Any], dict[str, object]],
    auth_report_match_label_fn: Callable[[Any], str],
    likely_auth_candidate_fn: Callable[[Any], bool],
    render_auth_signal_patch_fn: Callable[[Sequence[Any]], str],
    apply_auth_signal_patch_fn: Callable[[Sequence[Any]], tuple[int, int, str]],
    known_code_signals: Sequence[str],
    known_message_signals: Sequence[str],
) -> int:
    settings = load_settings_fn(args.config)
    store = create_store_fn(settings)
    try:
        if args.account:
            select_accounts_fn(settings, args.account)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    rows = store.summarize_auth_failures(account_name=args.account, limit=args.limit)
    if args.only_unmapped:
        rows = [row for row in rows if not row.mapped]
    if args.only_likely_auth:
        rows = [row for row in rows if likely_auth_candidate_fn(row)]
    if args.apply_suggested_patch:
        code_count, message_count, file_path = apply_auth_signal_patch_fn(rows)
        print(f'Applied suggested patch to {file_path}: codes={code_count}, messages={message_count}')
        return 0
    if args.json:
        print(json.dumps({
            'known_code_signals': sorted(known_code_signals),
            'known_message_signals': list(known_message_signals),
            'rows': [auth_report_row_to_json_fn(row) for row in rows],
            'suggested_patch': render_auth_signal_patch_fn(rows) if args.suggest_patch else '',
        }, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print('No auth failures observed.')
        return 0
    print('最近出现时间                  覆盖状态          命中来源        次数    账号                code                      msg')
    print('--------------------------  ----------------  --------------  ------  ------------------  ------------------------  ----------------------------------------')
    for row in rows:
        accounts = ','.join(row.accounts) or '-'
        print(f'{row.last_seen_at:<26}  {auth_report_match_label_fn(row):<16}  {row.matched_by:<14}  {row.count:<6}  {accounts:<18}  {row.code:<24}  {row.msg}')
    if args.suggest_patch:
        print('')
        print(render_auth_signal_patch_fn(rows), end='')
    return 0


def command_db_check(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
) -> int:
    settings = load_settings_fn(args.config)
    try:
        store = create_store_fn(settings)
        version = store.schema_version()
    except Exception as exc:
        print(f'DB check failed: {exc}', file=sys.stderr)
        return 1
    print(f'DB OK. path={settings.storage.database_file} schema_version={version}')
    return 0


def command_test_notify(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    build_named_notifiers_fn: Callable[[NotificationSettings], dict[str, object]] = build_named_notifiers,
) -> int:
    settings = load_settings_fn(args.config)
    errors = validate_settings_fn(settings, purpose='test-notify')
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    named_notifiers = build_named_notifiers_fn(settings.notifications)
    if not named_notifiers:
        print('No enabled notification channels are configured.')
        return 1

    selected = named_notifiers if args.channel == 'all' else {args.channel: named_notifiers.get(args.channel)}
    failures: list[str] = []
    successes: list[str] = []
    for name, notifier in selected.items():
        if notifier is None:
            failures.append(f'{name}: not configured')
            continue
        try:
            notifier.send('[autodl-helper] test notification', 'This is a test notification from autodl-helper.')
            successes.append(name)
        except Exception as exc:
            failures.append(f'{name}: {exc}')
    if successes:
        print('notification sent via: ' + ', '.join(successes))
    if failures:
        print('notification failures: ' + '; '.join(failures))
        return 1
    return 0


def command_init(
    args: argparse.Namespace,
    *,
    validate_config_fn: Callable[[argparse.Namespace], int] | None = None,
    launch_interactive_fn: Callable[[argparse.Namespace], int] | None = None,
    input_fn: Callable[[str], str] | None = None,
    cwd: str | Path | None = None,
) -> int:
    root = Path(cwd).resolve() if cwd is not None else Path.cwd()
    config_path = Path(args.config).expanduser()
    if not config_path.is_absolute():
        config_path = (root / config_path).resolve()

    package_root = Path(__file__).resolve().parents[1]
    env_template_path = (root / '.env.template') if (root / '.env.template').exists() else (package_root / '.env.template')
    env_path = root / '.env'
    config_template_path = (root / 'config.example.yaml') if (root / 'config.example.yaml').exists() else (package_root / 'config.example.yaml')

    if validate_config_fn is None:
        validate_config_fn = command_validate_config
    if input_fn is None:
        input_fn = input

    print('[1/4] Environment')
    print(f'Python: {sys.executable}')
    print(f'pip: {shutil.which("pip") or "not found"}')
    print(f'playwright: {shutil.which("playwright") or "not found"}')

    def _should_overwrite(dst: Path, label: str) -> bool:
        if getattr(args, 'force', False):
            return True
        if getattr(args, 'yes', False):
            return False
        answer = str(input_fn(f'{label} already exists. Overwrite from template? [y/N]: ')).strip().lower()
        return answer in {'y', 'yes'}

    def _sync_file(*, src: Path, dst: Path, label: str) -> None:
        if not src.exists():
            print(f'Missing template: {src.name}', file=sys.stderr)
            raise FileNotFoundError(src)
        if dst.exists():
            if not _should_overwrite(dst, label):
                print(f'Kept existing {label}.')
                return
            action = 'Overwrote'
        else:
            action = 'Created'
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        print(f'{action} {label} from template.')

    print('[2/4] Bootstrap files')
    try:
        _sync_file(src=env_template_path, dst=env_path, label='.env')
        _sync_file(src=config_template_path, dst=config_path, label=config_path.name)
    except FileNotFoundError:
        return 1

    print('[3/4] Validate config')
    validation_code = validate_config_fn(argparse.Namespace(config=str(config_path)))
    if validation_code != 0:
        print('Configuration validation failed.', file=sys.stderr)
        return int(validation_code or 1)

    print('[4/4] Ready')
    print('Bootstrap complete.')
    print('Next:')
    print(f'  python main.py interactive --config {config_path.name}')
    print(f'  python main.py login --config {config_path.name} --account <account-name>')
    print(f'  python main.py service-install --config {config_path.name}')

    if launch_interactive_fn is not None and not getattr(args, 'yes', False):
        answer = str(input_fn('Launch interactive now? [y/N]: ')).strip().lower()
        if answer in {'y', 'yes'}:
            interactive_args = argparse.Namespace(**vars(args))
            interactive_args.config = str(config_path)
            return int(launch_interactive_fn(interactive_args) or 0)
    return 0

def command_validate_config(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
) -> int:
    try:
        settings = apply_cli_overrides(args, load_settings_fn(args.config))
        errors = validate_settings_fn(settings, purpose='validate')
        if errors:
            print('Configuration invalid:', file=sys.stderr)
            for error in errors:
                print(f'- {error}', file=sys.stderr)
            return 1
        print('Configuration valid.')
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def command_config_show(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
) -> int:
    try:
        settings = load_settings_fn(args.config)
        print(json.dumps(serialize_settings(settings, resolved=False, account_name=getattr(args, 'account', None)), ensure_ascii=False, indent=2))
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def command_config_resolve(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
) -> int:
    try:
        settings = apply_cli_overrides(args, load_settings_fn(args.config))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    errors = validate_settings_fn(settings, purpose='validate')
    if errors:
        print('Configuration invalid:', file=sys.stderr)
        for error in errors:
            print(f'- {error}', file=sys.stderr)
        return 1
    print(json.dumps(serialize_settings(settings, resolved=True, account_name=getattr(args, 'account', None)), ensure_ascii=False, indent=2))
    return 0


def command_config_edit(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    read_raw_settings_fn: Callable[[str], dict[str, Any]] = read_raw_settings,
    write_raw_settings_fn: Callable[[str, dict[str, Any]], None] = write_raw_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    request_reload_fn: Callable[[SQLiteStore], Any] = request_config_reload,
) -> int:
    try:
        args = collect_config_edit_args(args)
        raw_payload = read_raw_settings_fn(args.config)
        settings = load_settings_fn(args.config)

        if any(getattr(args, field, None) is not None for field in (
            'lightweight_mode',
            'runtime_auth_revalidate_seconds',
            'force_refresh_min_interval_seconds',
            'auth_failure_backoff_seconds',
        )):
            for account_payload in _select_account_payloads(raw_payload, settings, getattr(args, 'account', None)):
                if getattr(args, 'lightweight_mode', None) is not None:
                    account_payload['lightweight_mode'] = args.lightweight_mode
                if getattr(args, 'runtime_auth_revalidate_seconds', None) is not None:
                    account_payload['runtime_auth_revalidate_seconds'] = args.runtime_auth_revalidate_seconds
                if getattr(args, 'force_refresh_min_interval_seconds', None) is not None:
                    account_payload['force_refresh_min_interval_seconds'] = args.force_refresh_min_interval_seconds
                if getattr(args, 'auth_failure_backoff_seconds', None) is not None:
                    account_payload['auth_failure_backoff_seconds'] = args.auth_failure_backoff_seconds

        keeper_payload = _ensure_task_payload(raw_payload, 'keeper')
        if getattr(args, 'shutdown_release_after_hours', None) is not None:
            keeper_payload['shutdown_release_after_hours'] = args.shutdown_release_after_hours
        if getattr(args, 'keeper_trigger_before_hours', None) is not None:
            keeper_payload['keeper_trigger_before_hours'] = args.keeper_trigger_before_hours
        if getattr(args, 'start_cooldown_minutes', None) is not None:
            keeper_payload['start_cooldown_minutes'] = args.start_cooldown_minutes
        if getattr(args, 'stop_cooldown_minutes', None) is not None:
            keeper_payload['stop_cooldown_minutes'] = args.stop_cooldown_minutes
        if getattr(args, 'fallback_to_status_at', None) is not None:
            keeper_payload['fallback_to_status_at'] = bool(args.fallback_to_status_at)

        scheduled_payload = _ensure_task_payload(raw_payload, 'scheduled_start')
        if getattr(args, 'scheduled_poll_interval', None) is not None:
            scheduled_payload['poll_interval_seconds'] = args.scheduled_poll_interval
        if getattr(args, 'target_time', None) is not None or getattr(args, 'advance_hours', None) is not None:
            for job_payload in _select_job_payloads(raw_payload, settings, getattr(args, 'scheduled_job', None), require_single=True):
                if getattr(args, 'target_time', None) is not None:
                    job_payload['target_time'] = args.target_time
                if getattr(args, 'advance_hours', None) is not None:
                    job_payload['advance_hours'] = args.advance_hours
        elif getattr(args, 'scheduled_job', None) is not None:
            _select_job_payloads(raw_payload, settings, getattr(args, 'scheduled_job', None), require_single=False)

        write_raw_settings_fn(args.config, raw_payload)
        effective = apply_cli_overrides(args, load_settings_fn(args.config))
        errors = validate_settings_fn(effective, purpose='validate')
        if errors:
            print('Configuration invalid after edit:', file=sys.stderr)
            for error in errors:
                print(f'- {error}', file=sys.stderr)
            return 1
        store = create_store_fn(effective)
        request_reload_fn(store)
        print(f'Updated config: {args.config}')
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _config_mtime_value(path: str | Path) -> str:
    try:
        return f'{os.path.getmtime(path):.6f}'
    except OSError:
        return ''


def _maybe_reload_daemon_settings(
    *,
    args: argparse.Namespace,
    store: SQLiteStore,
    state: dict[str, Any],
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    mtime_fn: Callable[[str | Path], float] = os.path.getmtime,
) -> Settings:
    active_settings = state.get('settings')
    if active_settings is None:
        active_settings = apply_cli_overrides(args, load_settings_fn(args.config))
        state['settings'] = active_settings
    reload_status = read_config_reload_status(store)
    try:
        current_mtime = f'{float(mtime_fn(args.config)):.6f}'
    except OSError:
        current_mtime = ''
    should_reload = (
        reload_status['requested_generation'] > reload_status['processed_generation']
        or (current_mtime and current_mtime != reload_status['last_processed_mtime'])
    )
    if not should_reload:
        return active_settings
    try:
        candidate = apply_cli_overrides(args, load_settings_fn(args.config))
        errors = validate_settings_fn(candidate, purpose='run-daemon')
        if errors:
            raise ValueError('\n'.join(errors))
    except Exception as exc:
        mark_config_reload_failure(
            store,
            generation=reload_status['requested_generation'],
            config_mtime=current_mtime,
            error=str(exc),
        )
        return active_settings
    state['settings'] = candidate
    mark_config_reload_success(
        store,
        generation=reload_status['requested_generation'],
        config_mtime=current_mtime,
    )
    return candidate


def command_healthcheck(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    collect_healthcheck_errors_fn: Callable[..., list[str]] = collect_healthcheck_errors,
) -> int:
    settings = load_settings_fn(args.config)
    errors = collect_healthcheck_errors_fn(
        settings=settings,
        state_file=args.state_file,
        lock_file=args.lock_file,
        smoke=args.smoke,
        headed=args.headed,
    )
    if errors:
        print('Healthcheck failed:', file=sys.stderr)
        for error in errors:
            print(f'- {error}', file=sys.stderr)
        return 1
    print('Healthcheck OK.')
    return 0


def _log_service_action(config_path: str, message: str) -> None:
    try:
        append_service_lifecycle_log(config_path, message)
    except Exception:
        logger.exception('写入服务管理日志失败')


def _record_service_event(
    config_path: str,
    *,
    action: str,
    message: str,
    level: str = 'info',
    detail: str = '',
    plist_path: str = '',
) -> None:
    try:
        settings = load_settings(config_path)
        store = create_store(settings)
        store.add_event(
            '',
            'service',
            level,
            message,
            payload={
                'label': '后台服务',
                'action': action,
                'detail': detail,
                'plist_path': plist_path,
            },
        )
    except Exception:
        logger.exception('写入服务事件历史失败')


def _service_event_label(payload: dict[str, Any] | None) -> str:
    data = payload or {}
    return str(data.get('label') or data.get('backend') or '后台服务')


def command_service_install(args: argparse.Namespace) -> int:
    status = install_service(config_path=args.config)
    label = _service_event_label(status)
    artifact_path = status.get('artifact_path') or ''
    _log_service_action(args.config, f'已安装后台服务 label={label} backend={status.get("backend") or "-"} artifact={artifact_path}')
    _record_service_event(args.config, action='install', message='已安装后台服务', detail=str(status.get('detail') or ''), plist_path=str(artifact_path))
    print(f'Installed background service ({status.get("backend")}): {artifact_path}')
    return 0


def command_service_start(args: argparse.Namespace) -> int:
    status = service_status(config_path=args.config)
    label = _service_event_label(status)
    if not status.get('installed'):
        print('后台服务未安装，请先执行 service-install。', file=sys.stderr)
        return 1
    if status.get('running'):
        _log_service_action(args.config, f'后台服务已在运行 label={label}')
        _record_service_event(args.config, action='start', message='后台服务已在运行')
        print(f'Background service already running: {label}')
        return 0
    result = start_service(config_path=args.config)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or 'service start failed').strip()
        _record_service_event(args.config, action='start', message='启动后台服务失败', level='error', detail=detail)
        print(detail, file=sys.stderr)
        return int(result.returncode or 1)
    _log_service_action(args.config, f'已启动后台服务 label={label}')
    _record_service_event(args.config, action='start', message='已启动后台服务')
    print(f'Started background service: {label}')
    return 0


def command_service_stop(args: argparse.Namespace) -> int:
    status = service_status(config_path=args.config)
    label = _service_event_label(status)
    if not status.get('installed'):
        _log_service_action(args.config, f'后台服务未安装 label={label}')
        _record_service_event(args.config, action='stop', message='后台服务未安装')
        print(f'Background service already absent: {label}')
        return 0
    if not status.get('running'):
        _log_service_action(args.config, f'后台服务已停止 label={label}')
        _record_service_event(args.config, action='stop', message='后台服务已停止')
        print(f'Background service already stopped: {label}')
        return 0
    result = stop_service(config_path=args.config)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or 'service stop failed').strip()
        _record_service_event(args.config, action='stop', message='停止后台服务失败', level='error', detail=detail)
        print(detail, file=sys.stderr)
        return int(result.returncode or 1)
    _log_service_action(args.config, f'已停止后台服务 label={label}')
    _record_service_event(args.config, action='stop', message='已停止后台服务')
    print(f'Stopped background service: {label}')
    return 0


def command_service_restart(args: argparse.Namespace) -> int:
    status = service_status(config_path=args.config)
    label = _service_event_label(status)
    if not status.get('installed'):
        print('后台服务未安装，请先执行 service-install。', file=sys.stderr)
        return 1
    result = restart_service(config_path=args.config)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or 'service restart failed').strip()
        _record_service_event(args.config, action='restart', message='重启后台服务失败', level='error', detail=detail)
        print(detail, file=sys.stderr)
        return int(result.returncode or 1)
    _log_service_action(args.config, f'已重启后台服务 label={label}')
    _record_service_event(args.config, action='restart', message='已重启后台服务')
    print(f'Restarted background service: {label}')
    return 0


def command_service_status(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
) -> int:
    settings = load_settings_fn(args.config)
    store = create_store_fn(settings)
    service = service_status(config_path=args.config)
    daemon_status = read_daemon_status(store)
    reload_status = read_config_reload_status(store)
    print(json.dumps({'service': service, 'daemon': daemon_status, 'reload': reload_status}, ensure_ascii=False, indent=2))
    return 0


def command_service_uninstall(args: argparse.Namespace) -> int:
    status = service_status(config_path=args.config)
    label = _service_event_label(status)
    if not status.get('installed'):
        _log_service_action(args.config, f'后台服务已不存在 label={label}')
        _record_service_event(args.config, action='uninstall', message='后台服务已不存在')
        print(f'Background service already absent: {label}')
        return 0
    removed = uninstall_service(config_path=args.config)
    artifact_path = removed.get('artifact_path') or ''
    _log_service_action(args.config, f'已卸载后台服务 label={label} artifact={artifact_path}')
    _record_service_event(args.config, action='uninstall', message='已卸载后台服务', plist_path=str(artifact_path))
    print(f'Uninstalled background service: {label}')
    return 0

def command_interactive(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    run_variant_fn: Callable[[argparse.Namespace, str], int] = command_run_variant,
    start_background_scheduled_fn: Callable[[argparse.Namespace], tuple[int, str]] | None = None,
    stop_background_polling_fn: Callable[[Settings, SQLiteStore], tuple[int, str]] | None = None,
    command_config_show_fn: Callable[..., int] = command_config_show,
    command_config_resolve_fn: Callable[..., int] = command_config_resolve,
    command_config_edit_fn: Callable[..., int] = command_config_edit,
    command_history_fn: Callable[..., int] = command_history,
    command_keeper_probe_fn: Callable[..., int] = command_keeper_probe,
    command_auth_report_fn: Callable[..., int] = command_auth_report,
    command_list_instances_fn: Callable[..., int] = command_list_instances,
    command_accounts_fn: Callable[..., int] = command_accounts,
    command_login_fn: Callable[..., int] = command_login,
    command_healthcheck_fn: Callable[..., int] = command_healthcheck,
    list_instances_panel_rows_fn: Callable[..., list[dict[str, Any]]] | None = None,
    history_panel_rows_fn: Callable[..., list[Any]] = history_panel_rows,
    auth_panel_rows_fn: Callable[..., list[Any]] = auth_panel_rows,
    build_dashboard_view_fn: Callable[..., dict[str, Any]] = build_dashboard_view,
    render_dashboard_fn: Callable[[dict[str, Any]], str] = render_dashboard,
    set_task_enabled_fn: Callable[..., None] = set_task_enabled,
    set_job_enabled_fn: Callable[..., None] = set_job_enabled,
    set_job_override_fn: Callable[..., None] = set_job_override,
    clear_runtime_controls_fn: Callable[..., None] = clear_runtime_controls,
    runtime_controls_snapshot_fn: Callable[..., dict[str, Any]] = runtime_controls_snapshot,
    request_reload_fn: Callable[..., None] = request_reload,
    run_keeper_only_fn: Callable[..., list[Any]] = run_keeper_only,
    run_scheduled_start_cycle_fn: Callable[..., list[Any]] = run_scheduled_start_cycle,
    scheduled_candidate_panel_data_fn: Callable[..., dict[str, Any] | None] = scheduled_candidate_panel_data,
    keeper_probe_rows_fn: Callable[..., list[dict[str, Any]]] = keeper_probe_rows,
    scheduled_job_status_rows_fn: Callable[..., list[dict[str, Any]]] = scheduled_job_status_rows,
    render_candidate_explanation_fn: Callable[[dict[str, Any] | None], str] = render_candidate_explanation,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    build_client_fn: Callable[..., object] = build_client,
    evaluate_keeper_instance_fn: Callable[..., Any] = evaluate_keeper_instance,
) -> int:
    try:
        if list_instances_panel_rows_fn is None:
            def list_instances_panel_rows_fn(settings: Settings, store: SQLiteStore, *, account_name: str | None = None):
                return list_instances_panel_rows(
                    settings,
                    store,
                    account_name=account_name,
                    select_accounts_fn=select_accounts,
                    build_client_fn=build_client,
                )
        return run_interactive(
            args,
            load_settings_fn=load_settings_fn,
            validate_settings_fn=validate_settings_fn,
            create_store_fn=create_store_fn,
            render_dashboard_fn=render_dashboard_fn,
            build_dashboard_view_fn=build_dashboard_view_fn,
            set_task_enabled_fn=set_task_enabled_fn,
            set_job_enabled_fn=set_job_enabled_fn,
            set_job_override_fn=set_job_override_fn,
            clear_runtime_controls_fn=clear_runtime_controls_fn,
            runtime_controls_snapshot_fn=runtime_controls_snapshot_fn,
            request_reload_fn=request_reload_fn,
            run_variant_fn=run_variant_fn,
            start_background_scheduled_fn=start_background_scheduled_fn,
            stop_background_polling_fn=stop_background_polling_fn,
            run_keeper_only_fn=run_keeper_only_fn,
            run_scheduled_start_cycle_fn=run_scheduled_start_cycle_fn,
            command_config_show_fn=command_config_show_fn,
            command_config_resolve_fn=command_config_resolve_fn,
            command_config_edit_fn=command_config_edit_fn,
            command_history_fn=command_history_fn,
            command_keeper_probe_fn=command_keeper_probe_fn,
            command_auth_report_fn=command_auth_report_fn,
            command_list_instances_fn=command_list_instances_fn,
            command_accounts_fn=command_accounts_fn,
            command_login_fn=command_login_fn,
            command_healthcheck_fn=command_healthcheck_fn,
            list_instances_panel_rows_fn=list_instances_panel_rows_fn,
            history_panel_rows_fn=history_panel_rows_fn,
            auth_panel_rows_fn=auth_panel_rows_fn,
            keeper_probe_rows_fn=lambda settings, store, *, account_name=None: keeper_probe_rows_fn(
                settings,
                store,
                account_name=account_name,
                select_accounts_fn=select_accounts_fn,
                build_client_fn=build_client_fn,
                evaluate_keeper_instance_fn=evaluate_keeper_instance_fn,
            ),
            scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
            scheduled_candidate_panel_data_fn=scheduled_candidate_panel_data_fn,
            render_candidate_explanation_fn=render_candidate_explanation_fn,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
