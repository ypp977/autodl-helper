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



from ..shared import *  # noqa: F401,F403
from .config import _maybe_reload_daemon_settings

def _delegate(name: str, fallback):
    class _Proxy:
        def _target(self):
            import sys
            module = sys.modules.get("autodl_helper.cli.handlers")
            if module is not None:
                target = getattr(module, name, None)
                if target is not None and target is not self:
                    return target
            return fallback

        def __call__(self, *args, **kwargs):
            return self._target()(*args, **kwargs)

        def __getattr__(self, attr):
            return getattr(self._target(), attr)

    return _Proxy()

datetime = _delegate('datetime', datetime)



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


__all__ = [
    "datetime",
    "run_scheduled_start_cycle",
    "run_keeper_only",
    "run_cycle",
    "execute_variant_cycle",
    "daemon_dispatch",
    "scheduled_daemon_should_exit",
]
