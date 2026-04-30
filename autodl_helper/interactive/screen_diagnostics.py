
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
from .shared import *  # noqa: F401,F403
from .account_ops import *  # noqa: F401,F403
from .account_common import *  # noqa: F401,F403
from .service_ops import *  # noqa: F401,F403
from .config_ops import *  # noqa: F401,F403
from .screen_support import *  # noqa: F401,F403
from .screen_scheduled import _show_result_screen

from .screen_support import _delegate

def _diagnostics_snapshot_payload(
    *,
    snapshot_store: InteractiveSnapshotStore,
    account_name: str,
    task_manager: InteractiveTaskManager | None = None,
    store=None,
) -> dict[str, Any]:
    instance_rows = snapshot_store.get_snapshot(_snapshot_key('instances', account_name))
    keeper_rows = snapshot_store.get_snapshot(_snapshot_key('keeper_probe', account_name))
    healthcheck_snapshot = snapshot_store.get_snapshot(_snapshot_key('healthcheck', account_name))
    config_snapshot = snapshot_store.get_snapshot(_snapshot_key('config_diagnostics', account_name))
    instances = list(instance_rows) if isinstance(instance_rows, list) else []
    keeper = list(keeper_rows) if isinstance(keeper_rows, list) else []
    health = healthcheck_snapshot if isinstance(healthcheck_snapshot, dict) else {}
    config_diag = config_snapshot if isinstance(config_snapshot, dict) else {}
    runtime_stats = task_manager.runtime_stats() if task_manager is not None else {}
    circuit_state = task_manager.circuit_state() if task_manager is not None else {}
    daemon_launch = read_daemon_launch_status(store) if store is not None else {}
    launch_agent = read_launch_agent_status() if store is not None else {}
    reload_status = read_config_reload_status(store) if store is not None else {}
    daemon_status = read_daemon_status(store) if store is not None else {}
    return {
        'instance_total': len(instances),
        'instance_running': sum(1 for row in instances if str(row.get('status') or '').lower() == 'running'),
        'instance_shutdown': sum(1 for row in instances if str(row.get('status') or '').lower() == 'shutdown'),
        'keeper_total': len(keeper),
        'keeper_eligible': sum(1 for row in keeper if bool(row.get('eligible'))),
        'healthcheck_status': str(health.get('status') or '尚未执行'),
        'healthcheck_summary': str(health.get('summary') or '暂无结果'),
        'config_status': str(config_diag.get('status') or '尚未执行'),
        'config_summary': str(config_diag.get('summary') or '暂无结果'),
        'fd_current': runtime_stats.get('fd_current'),
        'fd_soft_limit': runtime_stats.get('fd_soft_limit'),
        'fd_usage_percent': runtime_stats.get('fd_usage_percent'),
        'interactive_workers_max': runtime_stats.get('max_workers') or 0,
        'interactive_running_count': runtime_stats.get('running_count') or 0,
        'interactive_queued_count': runtime_stats.get('queued_count') or 0,
        'interactive_running_by_type': dict(runtime_stats.get('running_by_type') or {}),
        'daemon_launch_state': str(daemon_launch.get('launch_state') or 'idle'),
        'daemon_pid': daemon_launch.get('launch_pid'),
        'daemon_error_count': int(daemon_launch.get('launch_error_count') or 0),
        'daemon_last_error': str(daemon_launch.get('launch_last_error') or ''),
        'daemon_fused_until': str(daemon_launch.get('launch_fused_until') or ''),
        'daemon_running': bool(daemon_status.get('running', False)),
        'daemon_last_seen_at': str(daemon_status.get('last_seen_at') or ''),
        'interactive_circuit_open': bool(circuit_state.get('circuit_open', False)),
        'interactive_circuit_reason': str(circuit_state.get('circuit_reason') or ''),
        'interactive_circuit_until': str(circuit_state.get('circuit_until') or ''),
        'service_installed': bool(launch_agent.get('installed', False)),
        'service_loaded': bool(launch_agent.get('loaded', False)),
        'service_label': str(launch_agent.get('label') or '未安装'),
        'reload_status': str(reload_status.get('last_reload_status') or '尚未执行'),
        'reload_error': str(reload_status.get('last_reload_error') or ''),
    }

def _render_diagnostics_page(
    account_label: str,
    snapshot: dict[str, Any] | None,
    *,
    page_status_lines: list[str] | None = None,
) -> str:
    default_data = {
        'instance_total': 0,
        'instance_running': 0,
        'instance_shutdown': 0,
        'keeper_total': 0,
        'keeper_eligible': 0,
        'healthcheck_status': '尚未执行',
        'healthcheck_summary': '暂无结果',
        'config_status': '尚未执行',
        'config_summary': '暂无结果',
        'fd_current': '未知',
        'fd_soft_limit': '未知',
        'fd_usage_percent': 0.0,
        'interactive_workers_max': 0,
        'interactive_running_count': 0,
        'interactive_queued_count': 0,
        'interactive_running_by_type': {},
        'daemon_launch_state': 'idle',
        'daemon_pid': None,
        'daemon_error_count': 0,
        'daemon_last_error': '',
        'daemon_fused_until': '',
        'daemon_running': False,
        'daemon_last_seen_at': '',
        'interactive_circuit_open': False,
        'interactive_circuit_reason': '',
        'interactive_circuit_until': '',
        'service_installed': False,
        'service_loaded': False,
        'service_label': '未安装',
        'reload_status': '尚未执行',
        'reload_error': '',
    }
    data = {**default_data, **(snapshot or {})}
    heartbeat_age_seconds: float | None = None
    if data.get('daemon_last_seen_at'):
        heartbeat_dt = _parse_iso_datetime(str(data.get('daemon_last_seen_at') or ''))
        if heartbeat_dt is not None:
            heartbeat_age_seconds = max(0.0, (datetime.now().astimezone() - heartbeat_dt.astimezone()).total_seconds())
    if not data['service_installed']:
        service_state = '未安装'
    elif data['daemon_launch_state'] == 'starting':
        service_state = '启动中'
    elif data['service_loaded'] and data['daemon_running'] and heartbeat_age_seconds is not None and heartbeat_age_seconds <= SERVICE_HEARTBEAT_OK_SECONDS:
        service_state = '运行中'
    elif data['service_loaded'] and (data['daemon_last_error'] or data['daemon_launch_state'] == 'fused' or (heartbeat_age_seconds is not None and heartbeat_age_seconds > SERVICE_HEARTBEAT_OK_SECONDS)):
        service_state = '状态异常'
    elif data['service_loaded'] and not data['daemon_running']:
        service_state = '状态异常'
    else:
        service_state = '已停止'
    lines = [
        _heading('诊断', color=CYAN),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        _key_value('查看账号', account_label),
        '',
    ])
    left_column = [
        _section('[实例摘要]'),
        _key_value('实例总数', data['instance_total']),
        _key_value('运行中', data['instance_running']),
        _key_value('已关机', data['instance_shutdown']),
        '',
        _section('[Keeper 摘要]'),
        _key_value('实例总数', data['keeper_total']),
        _key_value('本次可执行', data['keeper_eligible']),
        '',
        _section('[最近检查]'),
        _key_value('健康自检', data['healthcheck_status']),
        _key_value('配置诊断', data['config_status']),
    ]
    right_column = [
        _section('[后台服务]'),
        _key_value('服务状态', service_state),
        _key_value('服务标签', data['service_label'] or '未安装'),
        _key_value('最近心跳', _format_human_datetime(data['daemon_last_seen_at']) if data.get('daemon_last_seen_at') else '暂无结果'),
        _key_value('服务说明', '后台运行正常' if service_state == '运行中' else ('后台正在启动，请稍后刷新' if service_state == '启动中' else ('最近心跳延迟或超时，建议重启' if service_state == '状态异常' else '可去诊断页启动或重启服务'))),
        '',
        _section('[任务状态]'),
        _key_value('交互任务池', f"运行中 {data['interactive_running_count']} / 排队 {data['interactive_queued_count']} / 并发上限 {data['interactive_workers_max']}"),
        _key_value(
            '交互轮询任务',
            '当前空闲'
            if str(data['daemon_launch_state'] or '') == 'idle' and not data.get('daemon_pid')
            else f"{data['daemon_launch_state']} / pid={data['daemon_pid'] or '未运行'}",
        ),
        _key_value('最近错误', data['daemon_last_error'] or '暂无错误'),
        '',
        _section('[热重载状态]'),
        _key_value('配置热重载', data['reload_status'] or '尚未执行'),
    ]
    lines.extend(_render_two_columns(left_column, right_column))
    if data.get('reload_error'):
        lines.append(_key_value('重载错误', data['reload_error']))
    return '\n'.join(lines)

def _healthcheck_snapshot_payload(*, code: int | None, output: str) -> dict[str, Any]:
    lines = [line.strip() for line in str(output or '').splitlines() if line.strip()]
    summary = lines[-1] if lines else ('健康自检成功' if code in {0, None} else '健康自检失败')
    return {
        'status': '成功' if code in {0, None} else '失败',
        'summary': summary,
        'code': 0 if code is None else int(code),
        'body': output or '无输出。',
    }

def _healthcheck_snapshot_payload_from_result(result: dict[str, Any]) -> dict[str, Any]:
    if result.get('timed_out'):
        return {
            'status': '超时',
            'summary': _truncate_text(result.get('summary') or '健康自检超时'),
            'code': result.get('code', 124),
            'body': _truncate_text(result.get('summary') or '健康自检超时，已终止本次检查', limit=SNAPSHOT_BODY_LIMIT),
        }
    if result.get('long_running'):
        return {
            'status': '耗时较长',
            'summary': _truncate_text(result.get('summary') or '健康自检耗时较长，但已完成'),
            'code': result.get('code', 0),
            'body': _truncate_text(result.get('output') or '无输出。', limit=SNAPSHOT_BODY_LIMIT),
        }
    return _healthcheck_snapshot_payload(
        code=result.get('code'),
        output=str(result.get('output') or ''),
    )

def _render_healthcheck_detail(snapshot: dict[str, Any] | None, *, page_status_lines: list[str] | None = None) -> str:
    data = snapshot or {'status': '未执行', 'summary': '首次加载中', 'body': '尚未执行健康检查。'}
    lines = [
        _heading('健康自检', color=CYAN),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        _key_value('最近状态', data.get('status') or '未执行'),
        _key_value('结果摘要', data.get('summary') or '-'),
        '',
        _section('[详情]'),
        str(data.get('body') or '无输出。'),
    ])
    return '\n'.join(lines)

def _browse_healthcheck_detail(
    *,
    args: argparse.Namespace,
    current_account: str | None,
    command_healthcheck_fn,
    task_manager: InteractiveTaskManager,
    snapshot_store: InteractiveSnapshotStore,
    timeout_seconds: float = HEALTHCHECK_TIMEOUT_SECONDS,
) -> None:
    account_scope = current_account or 'default'
    snapshot_key = _snapshot_key('healthcheck', account_scope)

    def _queue_healthcheck() -> None:
        task_manager.submit(
            'healthcheck_run',
            scope=account_scope,
            runner=lambda: _run_command_with_timeout(
                command_fn=command_healthcheck_fn,
                args=_copy_args(args, account=current_account, smoke=True),
                timeout_seconds=timeout_seconds,
                title='健康自检',
                timeout_summary='健康自检超时，已终止本次检查',
            ),
            status_message='正在执行健康自检',
            on_success=lambda task_result: _store_snapshot(
                snapshot_store,
                snapshot_key,
                _healthcheck_snapshot_payload_from_result(task_result.payload if isinstance(task_result.payload, dict) else {}),
                status_message='最近更新',
            ),
            on_error=lambda task_result: (
                task_manager.record_resource_error(task_result.error_message),
                snapshot_store.record_failure(snapshot_key, _friendly_resource_error_message(task_result.error_message)),
            ),
            replace_queued=True,
        )
        task_manager.start_pending()

    selected_key = '1'
    while True:
        task_manager.drain_completed()
        healthcheck_task = task_manager.get_task('healthcheck_run', account_scope)
        status = _page_status_from_tasks(
            snapshot_store=snapshot_store,
            snapshot_key=snapshot_key,
            primary_task=healthcheck_task,
        )
        payload = snapshot_store.get_snapshot(snapshot_key)
        items = [MenuItem('1', '重新运行检查'), MenuItem('0', '返回诊断')]
        action = _choose_menu_with_refresh(
            _render_healthcheck_detail(
                payload if isinstance(payload, dict) else None,
                page_status_lines=_page_status_lines(
                    status,
                    active_task=healthcheck_task,
                    progress_label='检查进度',
                    show_progress=False,
                ),
            ),
            items,
            default_key=_menu_default_key(items, selected_key),
            refresh_fn=lambda preferred_key: (
                _render_healthcheck_detail(
                    snapshot_store.get_snapshot(snapshot_key) if isinstance(snapshot_store.get_snapshot(snapshot_key), dict) else None,
                    page_status_lines=_page_status_lines(
                        _page_status_from_tasks(
                            snapshot_store=snapshot_store,
                            snapshot_key=snapshot_key,
                            primary_task=task_manager.get_task('healthcheck_run', account_scope),
                        ),
                        active_task=task_manager.get_task('healthcheck_run', account_scope),
                        progress_label='检查进度',
                        show_progress=False,
                    ),
                ),
                items,
                preferred_key or selected_key,
            ),
            refresh_revision_fn=lambda: _menu_refresh_revision(
                snapshot_store=snapshot_store,
                snapshot_keys=[snapshot_key],
                task_manager=task_manager,
                task_keys=[task_manager.task_key('healthcheck_run', account_scope)],
            ),
            refresh_interval_seconds=1.0,
            on_rendered_fn=task_manager.start_pending,
            refresh_policy='always',
            pre_refresh_fn=task_manager.drain_completed,
        )
        selected_key = action
        if action == '1':
            _queue_healthcheck()
        elif action == '0':
            return

_choose_menu_with_refresh = _delegate('_choose_menu_with_refresh', _choose_menu_with_refresh)
_menu_default_key = _delegate('_menu_default_key', _menu_default_key)
_show_result_screen = _delegate('_show_result_screen', _show_result_screen)
read_launch_agent_status = _delegate('read_launch_agent_status', read_launch_agent_status)
start_launch_agent = _delegate('start_launch_agent', start_launch_agent)
stop_launch_agent = _delegate('stop_launch_agent', stop_launch_agent)
read_daemon_status = _delegate('read_daemon_status', read_daemon_status)
read_daemon_launch_status = _delegate('read_daemon_launch_status', read_daemon_launch_status)
read_config_reload_status = _delegate('read_config_reload_status', read_config_reload_status)

__all__ = [
    '_diagnostics_snapshot_payload',
    '_render_diagnostics_page',
    '_healthcheck_snapshot_payload',
    '_healthcheck_snapshot_payload_from_result',
    '_render_healthcheck_detail',
    '_browse_healthcheck_detail',
]

_diagnostics_snapshot_payload = _delegate('_diagnostics_snapshot_payload', _diagnostics_snapshot_payload)
_render_diagnostics_page = _delegate('_render_diagnostics_page', _render_diagnostics_page)
_healthcheck_snapshot_payload = _delegate('_healthcheck_snapshot_payload', _healthcheck_snapshot_payload)
_healthcheck_snapshot_payload_from_result = _delegate('_healthcheck_snapshot_payload_from_result', _healthcheck_snapshot_payload_from_result)
_render_healthcheck_detail = _delegate('_render_healthcheck_detail', _render_healthcheck_detail)
_browse_healthcheck_detail = _delegate('_browse_healthcheck_detail', _browse_healthcheck_detail)
