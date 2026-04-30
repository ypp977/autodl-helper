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


from .scheduled import (
    _keeper_reason_label,
    _keeper_result_label,
    _normalize_instance_status,
    _scheduled_reason_label,
    _scheduled_result_label,
)

GPU_SPEC_RE = re.compile(r'(?P<model>.+?)\s*[*×x]\s*(?P<count>\d+)\s*(?:卡)?\s*$')

def _history_record_subject(row: HistoryRecord) -> str:
    payload = row.payload or {}
    if row.task_type == 'keeper':
        return str(payload.get('instance_id') or row.instance_id or '-')
    if row.task_type == 'service':
        return str(payload.get('label') or '后台服务')
    return str(payload.get('selected_instance_id') or payload.get('instance_id') or row.instance_id or '-')

def _history_record_summary(row: HistoryRecord) -> str:
    if row.summary:
        return _humanize_datetime_text(row.summary)
    payload = row.payload or {}
    if row.task_type == 'keeper':
        return _humanize_datetime_text(
            f"释放时间={payload.get('release_deadline') or '-'} 下次保活={payload.get('next_keeper_time') or '-'}"
        )
    if row.task_type == 'service':
        return _humanize_datetime_text(f"动作={payload.get('action') or '-'} 详情={payload.get('detail') or '-'}")
    return _humanize_datetime_text(
        f"目标时间={payload.get('target_time') or '-'} deadline={payload.get('deadline') or '-'}"
    )

def _history_task_label(task_type: str) -> str:
    return {
        'scheduled_start': '抢机器',
        'keeper': 'Keeper',
        'service': '后台服务',
    }.get(task_type, task_type or '-')

def _history_brief_line(row: HistoryRecord) -> str:
    subject = _history_record_subject(row)
    summary = _history_record_summary(row)
    if len(summary) > 42:
        summary = summary[:39] + '...'
    return f"{_history_task_label(row.task_type)} / {subject} / {summary}"

def _is_success_record(row: HistoryRecord) -> bool:
    if row.task_type == 'service':
        return row.severity not in {'error', 'fatal'}
    if row.severity == 'success':
        return True
    return row.result in {'started', 'already_running', 'keeper_executed', 'power_on_submitted'}

def _is_failure_record(row: HistoryRecord) -> bool:
    if row.task_type == 'service':
        return row.severity in {'error', 'fatal'}
    if row.severity in {'error', 'fatal'}:
        return True
    return row.result in {'deadline_failed', 'instance_missing', 'keeper_failed_power_on', 'keeper_failed_power_off'}

def _render_instance_reference(row: HistoryRecord) -> str:
    payload = row.payload or {}
    instance_id = payload.get('selected_instance_id') or payload.get('instance_id') or row.instance_id or payload.get('label') or '-'
    lines = [
        _heading('关联实例', color=CYAN),
        _separator(),
        _key_value('实例 ID', instance_id),
        _key_value('任务类型', row.task_type),
        _key_value('结果', row.result if row.task_type == 'service' else (_keeper_result_label(row.result) if row.task_type == 'keeper' else _scheduled_result_label(row.result))),
        _key_value('原因', (row.reason or '-') if row.task_type == 'service' else (_keeper_reason_label(row.reason) if row.task_type == 'keeper' else _scheduled_reason_label(row.reason))),
    ]
    if row.task_type == 'keeper':
        lines.extend([
            _key_value('释放时间', _format_human_datetime(str(payload.get('release_deadline') or '')) if payload.get('release_deadline') else '-'),
            _key_value('下次保活', _format_human_datetime(str(payload.get('next_keeper_time') or '')) if payload.get('next_keeper_time') else '-'),
        ])
    elif row.task_type == 'service':
        lines.extend([
            _key_value('服务标签', payload.get('label') or '-'),
            _key_value('动作', payload.get('action') or '-'),
        ])
    else:
        lines.extend([
            _key_value('目标时间', payload.get('target_time') or '-'),
            _key_value('截止时间', payload.get('deadline') or '-'),
        ])
    return '\n'.join(lines)

def _find_scheduled_job(settings: Settings, job_name: str):
    for job in settings.tasks.scheduled_start.jobs:
        if job.name == job_name or job.instance_id == job_name or scheduled_job_identity(job) == job_name:
            return job
    raise ValueError(f'job 不存在: {job_name}')

def _instance_gpu_summary(row: dict[str, Any]) -> str:
    spec = str(row.get('spec') or '').strip()
    match = GPU_SPEC_RE.match(spec)
    if match:
        model = str(match.group('model') or '').strip()
        count = str(match.group('count') or '').strip()
        if model and count:
            return f'{model}×{count}'
    gpu_all_num = str(row.get('gpu_all_num') or '').strip()
    if spec and gpu_all_num.isdigit():
        return f'{spec}×{gpu_all_num}'
    if spec:
        return spec
    if gpu_all_num.isdigit():
        return f'GPU×{gpu_all_num}'
    return 'GPU=-'

def _instance_idle_gpu_summary(row: dict[str, Any]) -> str:
    idle_value = row.get('gpu_idle_num')
    idle = '' if idle_value is None else str(idle_value).strip()
    return f'空闲{idle}' if idle not in {'', '-'} else '空闲-'


__all__ = [
    "_history_record_subject",
    "_history_record_summary",
    "_history_task_label",
    "_history_brief_line",
    "_is_success_record",
    "_is_failure_record",
    "_render_instance_reference",
    "_find_scheduled_job",
    "_instance_gpu_summary",
    "_instance_idle_gpu_summary",
]
