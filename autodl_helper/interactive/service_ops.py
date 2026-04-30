from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import logging
import os
import re
import select
import sys
import termios
import threading
import time
import tty
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

GPU_SPEC_RE = re.compile(r'(?P<model>.+?)\s*[*×x]\s*(?P<count>\d+)\s*(?:卡)?\s*$')
SNAPSHOT_TEXT_LIMIT = 512
SNAPSHOT_BODY_LIMIT = 2048
SERVICE_HEARTBEAT_OK_SECONDS = 75


def _interactive_max_workers(settings: Settings | None) -> int:
    try:
        return max(1, int(getattr(getattr(settings, 'interactive', None), 'max_workers', 6) or 6))
    except Exception:
        return 6
LOGIN_VERIFY_TIMEOUT_SECONDS = 12.0
HEALTHCHECK_TIMEOUT_SECONDS = 8.0
KEEPER_EXECUTE_LONG_RUNNING_SECONDS = 12.0
_SUBPROCESS_TASK_STATS_LOCK = threading.Lock()

from .shared import *  # noqa: F401,F403
from .account_ops import *  # noqa: F401,F403

def read_launch_agent_status(config_path: str | None = None) -> dict[str, Any]:
    return _service_status(config_path=config_path or _SERVICE_CONFIG_PATH)


def start_launch_agent(config_path: str | None = None):
    return _start_service(config_path=config_path or _SERVICE_CONFIG_PATH)


def stop_launch_agent(config_path: str | None = None):
    return _stop_service(config_path=config_path or _SERVICE_CONFIG_PATH)


def _service_state_snapshot(store) -> dict[str, Any]:
    runtime_status = read_daemon_status(store) if store is not None else {}
    launch_agent = read_launch_agent_status()
    service_installed = bool(launch_agent.get('installed'))
    service_loaded = bool(launch_agent.get('loaded'))
    daemon_running = bool(runtime_status.get('running'))
    launch_status = read_daemon_launch_status(store) if store is not None else {}
    launch_state = str(launch_status.get('state') or '')
    last_error = str(runtime_status.get('last_error') or '')
    last_seen_raw = str(runtime_status.get('last_seen_at') or '')
    last_seen = _parse_iso_datetime(last_seen_raw)
    heartbeat_age_seconds: float | None = None
    if last_seen is not None:
        heartbeat_age_seconds = max(0.0, (datetime.now().astimezone() - last_seen.astimezone()).total_seconds())
    if not service_installed:
        label, tone = '未安装', 'warn'
    elif launch_state == 'starting':
        label, tone = '启动中', 'info'
    elif service_loaded and daemon_running and heartbeat_age_seconds is not None and heartbeat_age_seconds <= SERVICE_HEARTBEAT_OK_SECONDS:
        label, tone = '运行中', 'ok'
    elif service_loaded and (launch_state == 'fused' or last_error or (heartbeat_age_seconds is not None and heartbeat_age_seconds > SERVICE_HEARTBEAT_OK_SECONDS)):
        label, tone = '状态异常', 'bad'
    elif service_loaded and not daemon_running:
        label, tone = '状态异常', 'bad'
    else:
        label, tone = '已停止', 'warn'
    return {
        'label': label,
        'tone': tone,
        'last_seen_at': runtime_status.get('last_seen_at', ''),
        'pid': runtime_status.get('pid'),
    }


def _append_interactive_service_log(config_path: str, message: str) -> None:
    try:
        append_service_lifecycle_log(config_path, message)
    except Exception:
        logging.getLogger(__name__).exception('写入交互式服务管理日志失败')


def _record_interactive_service_event(
    store,
    *,
    action: str,
    message: str,
    level: str = 'info',
    detail: str = '',
) -> None:
    try:
        store.add_event(
            '',
            'service',
            level,
            message,
            payload={
                'label': DEFAULT_SERVICE_LABEL,
                'action': action,
                'detail': detail,
                'plist_path': '',
            },
        )
    except Exception:
        logging.getLogger(__name__).exception('写入交互式服务事件历史失败')


def _submit_snapshot_task(
    *,
    task_manager: InteractiveTaskManager,
    snapshot_store: InteractiveSnapshotStore,
    task_type: str,
    scope: str,
    snapshot_key: str,
    runner: Callable[[], Any],
    status_message: str,
    replace_queued: bool = True,
) -> None:
    task_manager.submit(
        task_type,
        scope=scope,
        runner=runner,
        status_message=status_message,
        on_success=lambda task_result: _store_snapshot(snapshot_store, snapshot_key, task_result.payload, status_message='最近更新'),
        on_error=lambda task_result: (
            task_manager.record_resource_error(task_result.error_message),
            snapshot_store.record_failure(snapshot_key, _friendly_resource_error_message(task_result.error_message)),
        ),
        replace_queued=replace_queued,
    )


def _render_login_refresh_progress(account_name: str, *, code: int | None, output: str) -> str:
    success = code == 0
    normalized = output.strip()
    result_line = '登录状态已更新'
    if normalized:
        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        if lines:
            result_line = lines[-1]
    progress_lines = [
        _key_value('账号', account_name),
        _key_value('步骤 1', '已读取当前账号配置'),
        _key_value('步骤 2', '已发起登录校验与凭据刷新'),
        _key_value('步骤 3', '已检查刷新后的登录状态' if success else '登录校验失败'),
        _key_value('结果', result_line),
    ]
    return '\n'.join(progress_lines)


def _show_login_refresh_progress(
    *,
    args: argparse.Namespace,
    account_name: str,
    command_login_fn,
    title: str,
    headed_override: bool | None = None,
) -> None:
    code, output = _run_captured_action(
        title,
        lambda: command_login_fn(
            _copy_args(
                args,
                account=account_name,
                all=False,
                **({'headed': headed_override} if headed_override is not None else {}),
            )
        ),
    )
    _show_result_screen(title, _render_login_refresh_progress(account_name, code=code, output=output), code=code)


def _poll_live_action(timeout_seconds: float | None) -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raw = _prompt('回车刷新，输入 q/0 返回: ').strip()
        if raw in {'q', 'Q', '0', 'ESC'}:
            return 'back'
        return 'refresh'
    key = _read_key_with_timeout(timeout_seconds if timeout_seconds is not None else 86400.0)
    if key is None:
        return 'refresh'
    if key in {'q', 'Q', 'ESC'}:
        return 'back'
    if key == 'ENTER':
        return 'refresh'
    return 'stay'


def _scheduled_row_needs_live_refresh(row: dict[str, Any]) -> bool:
    if not row.get('enabled', True):
        return False
    result = str(row.get('latest_result') or '')
    if row.get('schedule_mode') == 'once' and _is_scheduled_once_complete_result(result):
        return False
    if bool(row.get('daemon_running')):
        return True
    if result in {'deadline_failed', 'instance_missing', 'already_running'}:
        return False
    if result in {'waiting_for_gpu', 'waiting_for_instance', 'no_eligible_candidate', 'selector_no_match', 'started', 'power_on_submitted', 'started_without_gpu'}:
        return True
    phase_label, _, _ = _scheduled_window_phase(row)
    return phase_label == '正在轮询候选'


def _freeze_scheduled_live_row(row: dict[str, Any]) -> dict[str, Any]:
    frozen = dict(row)
    stage_label, stage_tone = _scheduled_stage_label(frozen)
    execution_label, execution_tone = _scheduled_execution_status(frozen)
    missing_reason_label, missing_reason_tone = _scheduled_missing_check_reason(frozen)
    poll_text, target_text = _scheduled_window_countdowns(frozen)
    frozen['_live_stage_label'] = stage_label
    frozen['_live_stage_tone'] = stage_tone
    frozen['_live_execution_label'] = execution_label
    frozen['_live_execution_tone'] = execution_tone
    frozen['_live_missing_reason_label'] = missing_reason_label
    frozen['_live_missing_reason_tone'] = missing_reason_tone
    frozen['_live_next_action'] = _scheduled_next_action(frozen)
    frozen['_live_poll_text'] = poll_text
    frozen['_live_target_text'] = target_text
    return frozen


def _prepare_live_scheduled_rows(
    rows: list[dict[str, Any]],
    *,
    previous_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for row in rows:
        prepared.append(row if _scheduled_row_needs_live_refresh(row) else _freeze_scheduled_live_row(row))
    return prepared


def _scheduled_live_footer(rows: list[dict[str, Any]], *, refresh_interval_seconds: float = 3.0) -> tuple[str, float | None]:
    if any(_scheduled_row_needs_live_refresh(row) for row in rows):
        return f'{int(refresh_interval_seconds)}秒轻量自动刷新 / Enter 立即刷新 / q 返回', refresh_interval_seconds
    return '当前无运行中任务 / Enter 手动刷新 / q 返回', None


def _coordinate_scheduled_background(
    *,
    args: argparse.Namespace,
    settings: Settings,
    store,
    account_name: str,
    start_background_scheduled_fn,
    stop_background_polling_fn,
    service_status_fn: Callable[[], dict[str, Any]] = read_launch_agent_status,
    service_start_fn: Callable[[], Any] = start_launch_agent,
) -> tuple[int, str]:
    enabled_jobs_exist = _account_has_enabled_scheduled_jobs(settings, store, account_name=account_name)
    daemon_status = read_daemon_status(store)
    daemon_mode = str(daemon_status.get('mode') or '')
    daemon_account = str(daemon_status.get('account') or '')
    daemon_running = bool(daemon_status.get('running'))

    if enabled_jobs_exist:
        if daemon_running and daemon_mode == 'all':
            return 0, '后台已在运行，新规则已生效'
        if daemon_running and daemon_mode == 'scheduled_start' and (daemon_account == account_name or not daemon_account):
            return 0, '后台已在运行，新规则已生效'
        if daemon_running:
            return 0, '检测到其他后台在运行，未自动接管'
        service_status = service_status_fn() if callable(service_status_fn) else {}
        if bool(service_status.get('installed')):
            result = service_start_fn()
            if isinstance(result, tuple):
                code, _detail = result
            else:
                code, _detail = 0, ''
            return code, '已启动后台服务' if code == 0 else '启动后台服务失败'
        code, _detail = start_background_scheduled_fn(_copy_args(args, account=account_name, run_once=False, daemon_origin='interactive-auto'))
        return code, '已自动启动后台（fallback 模式）' if code == 0 else '自动启动后台失败（fallback 模式）'

    if daemon_running and daemon_mode == 'scheduled_start' and daemon_account == account_name:
        code, _detail = stop_background_polling_fn(settings, store)
        return code, '已自动停止后台（当前无启用任务）' if code == 0 else '自动停止后台失败'

    return 0, '已保存，任务已暂停，不启动后台'


def _normalize_service_action_result(result: Any) -> tuple[int, str]:
    if isinstance(result, tuple):
        try:
            code = int(result[0] or 0)
        except Exception:
            code = 1
        detail = str(result[1] or '') if len(result) > 1 else ''
        return code, detail
    if hasattr(result, 'returncode'):
        code = int(getattr(result, 'returncode', 0) or 0)
        detail = str(getattr(result, 'stderr', '') or getattr(result, 'stdout', '') or '')
        return code, detail.strip()
    if result is None:
        return 0, ''
    return 0, str(result)


def _refresh_scheduled_transient_state(
    transient_state: dict[str, dict[str, Any]],
    task_manager: InteractiveTaskManager,
) -> None:
    for job_name, overlay in list(transient_state.items()):
        task_type = str(overlay.get('_task_type') or '').strip()
        task_scope = str(overlay.get('_task_scope') or '').strip()
        if not task_type or not task_scope:
            continue
        task = task_manager.get_task(task_type, task_scope)
        if task is None:
            continue
        if task.status == 'queued':
            overlay['last_run_label'] = '排队中'
            overlay['last_run_summary'] = '后台任务排队中'
            continue
        if task.status == 'running':
            overlay['last_run_label'] = '正在执行'
            overlay['last_run_summary'] = '后台任务正在执行'
            continue
        base_row = dict(overlay.get('_base_row') or {'job_name': job_name})
        trigger_label = str(overlay.get('_trigger_label') or overlay.get('last_run_trigger') or '后台执行')
        if task.status == 'failed':
            error_message = str(task.error_message or '后台任务执行失败')
            transient_state[job_name] = {
                'last_run_trigger': trigger_label,
                'last_run_label': '执行失败',
                'last_run_summary': error_message,
                'task_status_label': base_row.get('task_status_label') or '等待执行',
                'task_status_tone': base_row.get('task_status_tone') or 'info',
            }
            continue
        results = task.payload if isinstance(task.payload, list) else []
        transient_state[job_name] = _scheduled_run_result_state(base_row, results, trigger_label=trigger_label)


def _merge_scheduled_transient_state(
    rows: list[dict[str, Any]],
    transient_state: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    merged_rows: list[dict[str, Any]] = []
    for row in rows:
        overlay = transient_state.get(str(row.get('job_name') or ''))
        if not overlay:
            merged_rows.append(row)
            continue
        merged = dict(row)
        merged.update(overlay)
        merged_rows.append(merged)
    return merged_rows


def _clear_scheduled_progress_scope_snapshots(
    snapshot_store: InteractiveSnapshotStore,
    *,
    current_account: str | None,
    job_name: str | None,
) -> None:
    account_scope = current_account or 'default'
    scope = f'job:{account_scope}:{job_name}' if job_name else f'all:{account_scope}'
    snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', scope))


def _clear_diagnostics_scope_snapshots(
    snapshot_store: InteractiveSnapshotStore,
    *,
    current_account: str | None,
) -> None:
    account_scope = current_account or 'default'
    for namespace in ('diagnostics', 'healthcheck', 'instances', 'keeper_probe', 'config_diagnostics'):
        snapshot_store.clear_prefix(_snapshot_key(namespace, account_scope))


def _load_instance_rows_via_command(
    *,
    args: argparse.Namespace,
    current_account: str | None,
    command_list_instances_fn,
) -> list[dict[str, Any]]:
    code, output = _run_captured_action(
        '实例列表(JSON)',
        lambda: command_list_instances_fn(_copy_args(args, account=current_account, headed=False, json=True)),
    )
    if code != 0:
        raise ValueError(output)
    payload = json.loads(output or '[]')
    if not isinstance(payload, list):
        raise ValueError('实例列表返回格式无效。')
    return [item for item in payload if isinstance(item, dict)]


def _run_healthcheck_diagnostics(
    *,
    args: argparse.Namespace,
    current_account: str | None,
    settings: Settings,
    command_healthcheck_fn,
) -> None:
    code, output = _run_captured_action(
        '健康自检',
        lambda: command_healthcheck_fn(_copy_args(args, account=current_account, smoke=True)),
    )
    lines = [
        _heading('健康自检', color=CYAN),
        _separator(),
        _key_value('查看账号', _account_display_name(settings, current_account)),
        _key_value('检查范围', '配置解析 / 认证状态 / 本地存储 / AutoDL 连通性'),
        _key_value('检查结果', _tone_chip('通过' if code == 0 else '失败', 'ok' if code == 0 else 'bad')),
        '',
        _section('[详情]'),
    ]
    if code == 0:
        lines.extend([
            '- 配置可读且可解析',
            '- 当前账号认证状态可判定',
            '- 本地存储与 SQLite 可访问',
            '- AutoDL 登录与实例查询可执行',
        ])
    else:
        error_lines = [line for line in output.splitlines() if line.strip()]
        lines.extend(error_lines[:20] or ['- 自检失败，但没有返回详细信息'])
    _show_result_screen('健康自检', '\n'.join(lines), code=code)
datetime = _delegate('datetime', datetime)
read_launch_agent_status = _delegate('read_launch_agent_status', read_launch_agent_status)
start_launch_agent = _delegate('start_launch_agent', start_launch_agent)
stop_launch_agent = _delegate('stop_launch_agent', stop_launch_agent)
_read_key_with_timeout = _delegate('_read_key_with_timeout', _read_key_with_timeout)
def _service_show_result_screen_fallback(*args, **kwargs):
    from .screens import _show_result_screen as _impl
    return _impl(*args, **kwargs)

_show_result_screen = _delegate('_show_result_screen', _service_show_result_screen_fallback)

__all__ = [
    "read_launch_agent_status",
    "start_launch_agent",
    "stop_launch_agent",
    "_service_state_snapshot",
    "_append_interactive_service_log",
    "_record_interactive_service_event",
    "_submit_snapshot_task",
    "_render_login_refresh_progress",
    "_show_login_refresh_progress",
    "_poll_live_action",
    "_scheduled_row_needs_live_refresh",
    "_freeze_scheduled_live_row",
    "_prepare_live_scheduled_rows",
    "_scheduled_live_footer",
    "_coordinate_scheduled_background",
    "_normalize_service_action_result",
    "_refresh_scheduled_transient_state",
    "_merge_scheduled_transient_state",
    "_clear_scheduled_progress_scope_snapshots",
    "_clear_diagnostics_scope_snapshots",
    "_load_instance_rows_via_command",
    "_run_healthcheck_diagnostics",
]
