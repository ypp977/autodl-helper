from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from dataclasses import asdict, dataclass, is_dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

from autodl_helper.auth import clear_runtime_authorization, inspect_auth_state
from autodl_helper.auth_cache import write_auth_cache
from .dialogs import (
    MenuItem,
    _InteractiveCancel,
    _choose_menu,
    _choose_menu_with_refresh,
    _clear_screen,
    _confirm_action,
    _decode_arrow_escape_sequence,
    _hide_cursor,
    _menu_default_key,
    _prompt,
    _prompt_int_with_default,
    _prompt_keeper_settings,
    _prompt_scheduled_job,
    _prompt_scheduled_time_settings,
    _prompt_with_default,
    _read_escape_sequence_blocking,
    _read_escape_sequence_with_deadline,
    _read_fd_char,
    _read_key_with_timeout,
    _repaint_screen,
    _render_menu,
    _supports_arrow_menu,
    _show_cursor,
    _split_csv,
    _update_menu_selection,
    _update_menu_title,
)
from .presentation import (
    BLUE,
    CYAN,
    DIM,
    GREEN,
    RED,
    YELLOW,
    _boxed_lines,
    _display_width,
    _format_hours_brief,
    _format_human_datetime,
    _format_minutes_brief,
    _format_relative_deadline,
    _heading,
    _humanize_datetime_text,
    _key_value,
    _pad_display,
    _parse_iso_datetime,
    _render_two_columns,
    _section,
    _separator,
    _strip_ansi,
    _style_text,
    _tone_chip,
)
from .runtime import (
    InteractivePageStatus,
    InteractiveSnapshotStore,
    InteractiveTaskManager,
    InteractiveTaskResult,
    capture_callable_output,
    reset_thread_capture_state,
)
from autodl_helper.config import (
    AccountSettings,
    KeeperSettings,
    ScheduledStartJob,
    ScheduledStartPriority,
    ScheduledStartSelector,
    Settings,
    read_raw_settings,
    write_raw_settings,
)
from autodl_helper.runtime_control import (
    get_task_enabled,
    read_config_reload_status,
    read_daemon_launch_status,
    read_daemon_status,
    scheduled_job_identity,
)
from autodl_helper.service_launchd import append_service_lifecycle_log
from autodl_helper.services.manager import service_status as _service_status
from autodl_helper.services.manager import start_service as _start_service
from autodl_helper.services.manager import stop_service as _stop_service

if TYPE_CHECKING:
    from autodl_helper.models import HistoryRecord

DEFAULT_SERVICE_LABEL = 'autodl-helper'
_SERVICE_CONFIG_PATH = 'config.yaml'

GPU_SPEC_RE = re.compile(r'(?P<model>.+?)\s*[*×x]\s*(?P<count>\d+)\s*(?:卡)?\s*$')
SNAPSHOT_TEXT_LIMIT = 512
SNAPSHOT_BODY_LIMIT = 2048
SERVICE_HEARTBEAT_OK_SECONDS = 75
LOGIN_VERIFY_TIMEOUT_SECONDS = 12.0
HEALTHCHECK_TIMEOUT_SECONDS = 8.0
KEEPER_EXECUTE_LONG_RUNNING_SECONDS = 12.0
_SUBPROCESS_TASK_STATS_LOCK = threading.Lock()
_SUBPROCESS_TASK_STATS: dict[str, int] = {}

def _delegate(name: str, fallback):
    class _Proxy:
        def _target(self):
            app_module = sys.modules.get("autodl_helper.interactive.app")
            if app_module is not None:
                target = getattr(app_module, name, None)
                if target is not None and target is not self:
                    return target
            return fallback

        def __call__(self, *args, **kwargs):
            return self._target()(*args, **kwargs)

        def __getattr__(self, attr):
            return getattr(self._target(), attr)

    return _Proxy()

def read_launch_agent_status(config_path: str | None = None) -> dict[str, Any]:
    return _service_status(config_path=config_path or _SERVICE_CONFIG_PATH)

def start_launch_agent(config_path: str | None = None):
    return _start_service(config_path=config_path or _SERVICE_CONFIG_PATH)

def stop_launch_agent(config_path: str | None = None):
    return _stop_service(config_path=config_path or _SERVICE_CONFIG_PATH)

def _interactive_max_workers(settings: Settings | None) -> int:
    try:
        return max(1, int(getattr(getattr(settings, 'interactive', None), 'max_workers', 6) or 6))
    except Exception:
        return 6

def _is_scheduled_once_complete_result(result: Any) -> bool:
    return str(result or '') in {'started', 'already_running', 'power_on_submitted'}

def _is_scheduled_once_terminal_result(result: Any) -> bool:
    return str(result or '') in {'started', 'already_running', 'power_on_submitted', 'deadline_failed', 'instance_missing'}

def _nudge_background_tasks(task_manager: InteractiveTaskManager, *, settle_seconds: float = 0.01) -> None:
    task_manager.start_pending()
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    task_manager.drain_completed()

def _snapshot_key(namespace: str, scope: str | None) -> str:
    return f'{namespace}:{scope or "default"}'

def _bump_subprocess_task_stat(name: str, amount: int = 1) -> None:
    with _SUBPROCESS_TASK_STATS_LOCK:
        _SUBPROCESS_TASK_STATS[name] = int(_SUBPROCESS_TASK_STATS.get(name, 0)) + amount

def _subprocess_task_stats_snapshot() -> dict[str, int]:
    with _SUBPROCESS_TASK_STATS_LOCK:
        return {key: int(value) for key, value in _SUBPROCESS_TASK_STATS.items()}

def _friendly_resource_error_message(error_message: str) -> str:
    message = str(error_message or '').strip()
    lowered = message.lower()
    if 'unable to open database file' in lowered:
        path_match = re.search(r'(path=[^;]+)$', message)
        suffix = f' / {path_match.group(1)}' if path_match else ''
        return f'数据库打开失败（可能为文件描述符耗尽或资源熔断）{suffix}'
    if 'too many open files' in lowered:
        return f'资源不足：文件描述符耗尽 ({message})'
    if 'resource temporarily unavailable' in lowered:
        return f'资源不足：系统暂时不可用 ({message})'
    return message

def _truncate_text(value: Any, *, limit: int = SNAPSHOT_TEXT_LIMIT) -> str:
    text = str(value or '')
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + '...'

def _trim_snapshot_payload(snapshot_key: str, payload: Any) -> Any:
    namespace = str(snapshot_key).split(':', 1)[0]
    if namespace == 'account_runtime' and isinstance(payload, dict):
        return {
            'account_name': payload.get('account_name'),
            'account_enabled': bool(payload.get('account_enabled', True)),
            'auth_status': _truncate_text(payload.get('auth_status')),
            'auth_source': _truncate_text(payload.get('auth_source')),
            'cached_at_iso': payload.get('cached_at_iso'),
            'running_instances': int(payload.get('running_instances') or 0),
            'expiring_soon': int(payload.get('expiring_soon') or 0),
            'scheduled_jobs': int(payload.get('scheduled_jobs') or 0),
            'paused_jobs': int(payload.get('paused_jobs') or 0),
            'keeper_enabled': bool(payload.get('keeper_enabled', False)),
        }
    if namespace == 'diagnostics' and isinstance(payload, dict):
        return {
            'instance_total': int(payload.get('instance_total') or 0),
            'instance_running': int(payload.get('instance_running') or 0),
            'instance_shutdown': int(payload.get('instance_shutdown') or 0),
            'keeper_total': int(payload.get('keeper_total') or 0),
            'keeper_eligible': int(payload.get('keeper_eligible') or 0),
            'healthcheck_status': _truncate_text(payload.get('healthcheck_status')),
            'healthcheck_summary': _truncate_text(payload.get('healthcheck_summary')),
            'config_status': _truncate_text(payload.get('config_status')),
            'config_summary': _truncate_text(payload.get('config_summary')),
            'fd_current': payload.get('fd_current'),
            'fd_soft_limit': payload.get('fd_soft_limit'),
            'fd_usage_percent': payload.get('fd_usage_percent'),
            'interactive_workers_max': int(payload.get('interactive_workers_max') or 0),
            'interactive_running_count': int(payload.get('interactive_running_count') or 0),
            'interactive_queued_count': int(payload.get('interactive_queued_count') or 0),
            'interactive_running_by_type': dict(payload.get('interactive_running_by_type') or {}),
            'daemon_launch_state': _truncate_text(payload.get('daemon_launch_state')),
            'daemon_pid': payload.get('daemon_pid'),
            'daemon_error_count': int(payload.get('daemon_error_count') or 0),
            'daemon_last_error': _truncate_text(payload.get('daemon_last_error')),
            'daemon_fused_until': payload.get('daemon_fused_until'),
            'interactive_circuit_open': bool(payload.get('interactive_circuit_open', False)),
            'interactive_circuit_reason': _truncate_text(payload.get('interactive_circuit_reason')),
            'interactive_circuit_until': payload.get('interactive_circuit_until'),
        }
    if namespace == 'healthcheck' and isinstance(payload, dict):
        return {
            'status': _truncate_text(payload.get('status')),
            'summary': _truncate_text(payload.get('summary')),
            'code': payload.get('code'),
            'body': _truncate_text(payload.get('body') or '无输出。', limit=SNAPSHOT_BODY_LIMIT),
        }
    if namespace == 'config_diagnostics' and isinstance(payload, dict):
        return {
            'status': _truncate_text(payload.get('status')),
            'summary': _truncate_text(payload.get('summary')),
            'body': _truncate_text(payload.get('body') or '', limit=SNAPSHOT_BODY_LIMIT),
        }
    if namespace == 'dashboard' and isinstance(payload, dict):
        scheduled_jobs = []
        for job in list(payload.get('scheduled_jobs') or [])[:6]:
            if not isinstance(job, dict):
                continue
            scheduled_jobs.append(
                {
                    'job_name': job.get('job_name'),
                    'enabled': bool(job.get('enabled', False)),
                    'target_time': job.get('target_time'),
                    'advance_hours': job.get('advance_hours'),
                    'latest_result': job.get('latest_result'),
                    'latest_created_at': job.get('latest_created_at'),
                    'task_status_label': _truncate_text(job.get('task_status_label')),
                    'task_status_tone': job.get('task_status_tone'),
                }
            )
        candidate_summary = payload.get('candidate_summary') if isinstance(payload.get('candidate_summary'), dict) else {}
        runtime_status = payload.get('runtime_status') if isinstance(payload.get('runtime_status'), dict) else {}
        current_account_row = payload.get('current_account_row') if isinstance(payload.get('current_account_row'), dict) else {}
        keeper_summary = payload.get('keeper_summary') if isinstance(payload.get('keeper_summary'), dict) else {}
        return {
            'runtime_status': {
                'running': bool(runtime_status.get('running', False)),
                'pid': runtime_status.get('pid'),
                'heartbeat_age_seconds': runtime_status.get('heartbeat_age_seconds'),
            },
            'current_account': payload.get('current_account'),
            'current_account_row': {
                key: value
                for key, value in {
                    'status': _truncate_text(current_account_row.get('status')),
                    'auth_source': _truncate_text(current_account_row.get('auth_source')),
                    'cached_at_iso': current_account_row.get('cached_at_iso'),
                }.items()
                if value not in {None, ''}
            },
            'enabled_accounts': int(payload.get('enabled_accounts') or 0),
            'effective_keeper_enabled': bool(payload.get('effective_keeper_enabled', False)),
            'effective_scheduled_enabled': bool(payload.get('effective_scheduled_enabled', False)),
            'paused_job_count': int(payload.get('paused_job_count') or 0),
            'scheduled_job_count': len(list(payload.get('scheduled_jobs') or [])),
            'scheduled_jobs': scheduled_jobs,
            'keeper_summary': {
                'pending': int(keeper_summary.get('pending') or 0),
                'not_due': int(keeper_summary.get('not_due') or 0),
                'abnormal': int(keeper_summary.get('abnormal') or 0),
                'expiring_soon': int(keeper_summary.get('expiring_soon') or 0),
                'failed': int(keeper_summary.get('failed') or 0),
            },
            'candidate_summary': {
                'job_name': candidate_summary.get('job_name'),
                'selected_instance_id': candidate_summary.get('selected_instance_id'),
                'candidate_count': int(candidate_summary.get('candidate_count') or 0),
                'top_reasons': list(candidate_summary.get('top_reasons') or [])[:3],
            },
            'service_state_label': _truncate_text(payload.get('service_state_label')),
            'service_state_tone': payload.get('service_state_tone'),
            'service_last_seen_at': payload.get('service_last_seen_at'),
            'service_pid': payload.get('service_pid'),
        }
    if namespace == 'scheduled_progress' and isinstance(payload, list):
        kept_rows: list[dict[str, Any]] = []
        allowed_row_keys = {
            'job_name', 'enabled', 'target_mode', 'target_summary', 'target_time', 'advance_hours',
            'schedule_mode', 'timezone', 'latest_created_at', 'latest_result', 'latest_summary',
            'latest_matching_created_at', 'latest_matches_current_rule', 'has_history',
            'daemon_running',
            'latest_payload', '_live_stage_label', '_live_stage_tone', '_live_execution_label',
            '_live_execution_tone', '_live_next_action', '_live_poll_text', '_live_target_text',
            '_live_missing_reason_label', '_live_missing_reason_tone',
        }
        for item in payload:
            if not isinstance(item, dict):
                continue
            row = {key: item.get(key) for key in allowed_row_keys if key in item}
            latest_payload = item.get('latest_payload')
            if isinstance(latest_payload, dict):
                row['latest_payload'] = {
                    'hit_count': latest_payload.get('hit_count'),
                    'waiting_count': latest_payload.get('waiting_count'),
                    'dropped_count': latest_payload.get('dropped_count'),
                }
            else:
                row['latest_payload'] = {}
            row['latest_summary'] = _truncate_text(item.get('latest_summary'))
            row['target_summary'] = _truncate_text(item.get('target_summary'))
            row['_live_next_action'] = _truncate_text(item.get('_live_next_action'))
            kept_rows.append(row)
        return kept_rows
    if namespace == 'scheduled_status' and isinstance(payload, list):
        kept_rows: list[dict[str, Any]] = []
        allowed_row_keys = {
            'job_name', 'enabled', 'target_time', 'advance_hours', 'schedule_mode', 'timezone',
            'latest_result', 'latest_reason', 'latest_summary', 'latest_created_at', 'latest_instance_id',
            'has_history', 'latest_matches_current_rule', 'task_status_label', 'task_status_tone',
            'daemon_running', 'last_run_trigger', 'last_run_label', 'last_run_summary',
        }
        for item in payload:
            if not isinstance(item, dict):
                continue
            row = {key: item.get(key) for key in allowed_row_keys if key in item}
            latest_payload = item.get('latest_payload')
            if isinstance(latest_payload, dict):
                row['latest_payload'] = {
                    'candidate_count': latest_payload.get('candidate_count'),
                    'selected_instance_id': latest_payload.get('selected_instance_id'),
                    'selected_instance_label': _truncate_text(latest_payload.get('selected_instance_label')),
                    'selector_summary': _truncate_text(latest_payload.get('selector_summary')),
                    'status': latest_payload.get('status'),
                }
            else:
                row['latest_payload'] = {}
            row['latest_summary'] = _truncate_text(item.get('latest_summary'))
            row['last_run_summary'] = _truncate_text(item.get('last_run_summary'))
            kept_rows.append(row)
        return kept_rows
    return payload

def _store_snapshot(
    snapshot_store: InteractiveSnapshotStore,
    snapshot_key: str,
    payload: Any,
    *,
    status_message: str = '最近更新',
) -> None:
    snapshot_store.set_snapshot(snapshot_key, _trim_snapshot_payload(snapshot_key, payload), status_message=status_message)

def _page_status_tone(status: InteractivePageStatus, active_task: InteractiveTaskResult | None = None) -> str:
    if active_task is not None and active_task.status in {'queued', 'running'}:
        age_seconds = _task_running_age_seconds(active_task)
        threshold = _task_long_running_threshold_seconds(active_task)
        if threshold > 0 and age_seconds >= threshold:
            return 'warn'
        return 'info'
    return {
        'ready': 'ok',
        'failed': 'bad',
        'refreshing': 'info',
        'loading': 'info',
        'idle': 'muted',
    }.get(status.state, 'muted')

def _task_activity_label(task: InteractiveTaskResult) -> tuple[str, str]:
    if task.status == 'queued':
        return '排队中', 'info'
    age_seconds = _task_running_age_seconds(task)
    threshold = _task_long_running_threshold_seconds(task)
    if threshold > 0 and age_seconds >= threshold:
        return '耗时较长', 'warn'
    return '运行中', 'info'

def _render_task_progress_bar(
    *,
    task: InteractiveTaskResult,
    width: int = 10,
) -> str:
    frame = int(max(0.0, time.monotonic()) * 4)
    pulse_width = min(2, width)
    if task.status == 'queued':
        fill = 0
    else:
        threshold = max(1.0, float(_task_long_running_threshold_seconds(task) or 10.0))
        fill_ratio = min(1.0, _task_running_age_seconds(task) / threshold)
        fill = max(1, int(round(fill_ratio * width)))
    fill = max(0, min(width, fill))
    label, tone = _task_activity_label(task)
    tone_color = {'info': CYAN, 'warn': YELLOW}.get(tone, CYAN)
    cells = ['░'] * width
    for index in range(fill):
        cells[index] = '█'
    if width > 0:
        if task.status == 'queued':
            pulse_start = frame % width
            pulse_indexes = [(pulse_start + offset) % width for offset in range(pulse_width)]
        else:
            animation_width = max(fill, pulse_width)
            pulse_start = frame % animation_width
            pulse_indexes = [(pulse_start + offset) % animation_width for offset in range(pulse_width)]
        pulse_indexes = [index for index in pulse_indexes if 0 <= index < width]
    else:
        pulse_indexes = []
    rendered_cells: list[str] = []
    for index, cell in enumerate(cells):
        if index in pulse_indexes:
            rendered_cells.append(_style_text('▓', tone_color, bold=True))
        elif cell == '█':
            rendered_cells.append(_style_text(cell, tone_color, bold=True))
        else:
            rendered_cells.append(_style_text(cell, DIM))
    suffix = '排队中' if task.status == 'queued' else f'{_task_running_age_seconds(task)}s'
    return f"[{''.join(rendered_cells)}] {suffix}"

def _page_status_lines(
    status: InteractivePageStatus,
    *,
    prefix: str = '数据状态',
    active_task: InteractiveTaskResult | None = None,
    progress_label: str = '任务进度',
    show_task_stage: bool = True,
    show_progress: bool = False,
    show_hint: bool = True,
) -> list[str]:
    message = str(status.message or '').strip() or '首次加载中'
    if status.updated_at:
        message = f'{message} / 最近更新于 {_format_human_datetime(status.updated_at)}'
    lines = [_key_value(prefix, _tone_chip(message, _page_status_tone(status, active_task)))]
    if active_task is not None and active_task.status in {'queued', 'running'}:
        if show_task_stage:
            activity_label, activity_tone = _task_activity_label(active_task)
            lines.append(_key_value('任务阶段', _tone_chip(activity_label, activity_tone)))
        if show_progress:
            lines.append(_key_value(progress_label, _render_task_progress_bar(task=active_task)))
        age_seconds = _task_running_age_seconds(active_task)
        threshold = _task_long_running_threshold_seconds(active_task)
        if show_hint and active_task.status == 'running' and threshold > 0 and age_seconds >= threshold:
            lines.append(_key_value('提示', _style_text('可按 q 返回，后台继续执行', DIM)))
    if status.error_message:
        lines.append(_key_value('错误信息', _tone_chip(status.error_message, 'bad')))
    return lines

def _page_status_from_snapshot_keys(
    *,
    snapshot_store: InteractiveSnapshotStore,
    snapshot_keys: list[str],
    primary_task: InteractiveTaskResult | None = None,
    secondary_tasks: list[InteractiveTaskResult | None] | None = None,
) -> InteractivePageStatus:
    active_task = primary_task
    for task in secondary_tasks or []:
        if task is not None and task.status in {'queued', 'running'}:
            active_task = task
            break

    latest_ready_entry = None
    latest_failed_entry = None
    for key in snapshot_keys:
        entry = snapshot_store.get_entry(key)
        if entry is None:
            continue
        if entry.updated_at:
            if latest_ready_entry is None or str(entry.updated_at) >= str(latest_ready_entry.updated_at):
                latest_ready_entry = entry
        if entry.error_message:
            if latest_failed_entry is None:
                latest_failed_entry = entry
            elif entry.updated_at and (not latest_failed_entry.updated_at or str(entry.updated_at) >= str(latest_failed_entry.updated_at)):
                latest_failed_entry = entry

    if active_task is not None and active_task.status in {'queued', 'running'}:
        if latest_ready_entry is not None and latest_ready_entry.updated_at:
            status = InteractivePageStatus(
                state='refreshing',
                message='正在刷新',
                updated_at=latest_ready_entry.updated_at,
                error_message='',
            )
        else:
            status = InteractivePageStatus(
                state='loading',
                message='首次加载中',
                updated_at='',
                error_message='',
            )
        age_seconds = _task_running_age_seconds(active_task)
        threshold = _task_long_running_threshold_seconds(active_task)
        if active_task.status == 'running' and threshold > 0 and age_seconds >= threshold:
            return InteractivePageStatus(
                state=status.state,
                message=f'{active_task.status_message or status.message}（已持续 {age_seconds}s，超时风险）',
                updated_at=status.updated_at,
                error_message=status.error_message,
            )
        if active_task.status_message:
            return InteractivePageStatus(
                state=status.state,
                message=active_task.status_message,
                updated_at=status.updated_at,
                error_message=status.error_message,
            )
        return status

    if latest_failed_entry is not None and not latest_ready_entry:
        return InteractivePageStatus(
            state='failed',
            message='刷新失败',
            updated_at='',
            error_message=latest_failed_entry.error_message,
        )
    if latest_failed_entry is not None and latest_failed_entry.updated_at and (
        latest_ready_entry is None or str(latest_failed_entry.updated_at) >= str(latest_ready_entry.updated_at)
    ):
        return InteractivePageStatus(
            state='failed',
            message='刷新失败（保留上次结果）',
            updated_at=latest_failed_entry.updated_at,
            error_message=latest_failed_entry.error_message,
        )
    if latest_ready_entry is not None and latest_ready_entry.updated_at:
        return InteractivePageStatus(
            state='ready',
            message=latest_ready_entry.status_message or '最近更新',
            updated_at=latest_ready_entry.updated_at,
            error_message='',
        )
    return InteractivePageStatus(
        state='idle',
        message='首次加载中',
        updated_at='',
        error_message='',
    )

def _task_long_running_threshold_seconds(task: InteractiveTaskResult | None) -> float:
    if task is None:
        return 0.0
    if task.task_type == 'login_verify_run':
        return LOGIN_VERIFY_TIMEOUT_SECONDS
    if task.task_type == 'healthcheck_run':
        return HEALTHCHECK_TIMEOUT_SECONDS
    if task.task_type == 'keeper_execute_run':
        return KEEPER_EXECUTE_LONG_RUNNING_SECONDS
    return 0.0

def _task_running_age_seconds(task: InteractiveTaskResult | None) -> int:
    if task is None or not task.started_at:
        return 0
    try:
        started_at = datetime.fromisoformat(task.started_at)
    except ValueError:
        return 0
    return int(max(0.0, (datetime.now().astimezone() - started_at).total_seconds()))

def _page_status_from_tasks(
    *,
    snapshot_store: InteractiveSnapshotStore,
    snapshot_key: str,
    primary_task: InteractiveTaskResult | None = None,
    secondary_tasks: list[InteractiveTaskResult | None] | None = None,
) -> InteractivePageStatus:
    active_task = primary_task
    for task in secondary_tasks or []:
        if task is not None and task.status in {'queued', 'running'}:
            active_task = task
            break
    status = snapshot_store.page_status(snapshot_key, active_task)
    if active_task is not None and active_task.status in {'queued', 'running'}:
        age_seconds = _task_running_age_seconds(active_task)
        threshold = _task_long_running_threshold_seconds(active_task)
        if active_task.status == 'running' and threshold > 0 and age_seconds >= threshold:
            return InteractivePageStatus(
                state=status.state,
                message=f'{active_task.status_message or status.message}（已持续 {age_seconds}s，超时风险）',
                updated_at=status.updated_at,
                error_message=status.error_message,
            )
        if active_task.status_message:
            return InteractivePageStatus(
                state=status.state,
                message=active_task.status_message,
                updated_at=status.updated_at,
                error_message=status.error_message,
            )
    return status

def _page_status_from_task_result(
    task: InteractiveTaskResult | None,
    *,
    success_message: str,
    idle_message: str,
) -> InteractivePageStatus:
    if task is None:
        return InteractivePageStatus(state='idle', message=idle_message)
    if task.status in {'queued', 'running'}:
        return InteractivePageStatus(state='refreshing', message=task.status_message or idle_message)
    if task.status == 'succeeded':
        return InteractivePageStatus(
            state='ready',
            message=success_message,
            updated_at=task.finished_at or task.started_at,
        )
    if task.status == 'failed':
        return InteractivePageStatus(
            state='failed',
            message='执行失败',
            updated_at=task.finished_at or task.started_at,
            error_message=_friendly_resource_error_message(task.error_message),
        )
    return InteractivePageStatus(state='idle', message=idle_message)

def _menu_refresh_revision(
    *,
    snapshot_store: InteractiveSnapshotStore | None = None,
    snapshot_keys: list[str] | None = None,
    task_manager: InteractiveTaskManager | None = None,
    task_keys: list[str] | None = None,
) -> tuple[Any, ...]:
    snapshot_token = tuple(
        snapshot_store.entry_revision(key) for key in (snapshot_keys or [])
    ) if snapshot_store is not None else ()
    task_token = tuple(
        task_manager.task_revision(key) for key in (task_keys or [])
    ) if task_manager is not None else ()
    return (snapshot_token, task_token)

def _print_execution_summary(title: str, *, code: int | None = None, detail: str | None = None) -> None:
    body = _humanize_datetime_text(detail or '无额外信息。')
    _show_result_screen(f'执行完成: {title}', body, code=code)


datetime = _delegate('datetime', datetime)

def _shared_show_result_screen_fallback(*args, **kwargs):
    from .screens import _show_result_screen as _screen_show_result_screen
    return _screen_show_result_screen(*args, **kwargs)

_show_result_screen = _delegate('_show_result_screen', _shared_show_result_screen_fallback)

__all__ = [
    "read_launch_agent_status",
    "start_launch_agent",
    "stop_launch_agent",
    "_interactive_max_workers",
    "_is_scheduled_once_complete_result",
    "_is_scheduled_once_terminal_result",
    "_nudge_background_tasks",
    "_snapshot_key",
    "_bump_subprocess_task_stat",
    "_subprocess_task_stats_snapshot",
    "_friendly_resource_error_message",
    "_truncate_text",
    "_trim_snapshot_payload",
    "_store_snapshot",
    "_page_status_tone",
    "_task_activity_label",
    "_render_task_progress_bar",
    "_page_status_lines",
    "_page_status_from_snapshot_keys",
    "_task_long_running_threshold_seconds",
    "_task_running_age_seconds",
    "_page_status_from_tasks",
    "_page_status_from_task_result",
    "_menu_refresh_revision",
    "_print_execution_summary",
]
