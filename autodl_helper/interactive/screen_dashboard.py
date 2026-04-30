
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

from .screen_support import _delegate

def _diagnostics_page_status(
    *,
    snapshot_store: InteractiveSnapshotStore,
    account_scope: str,
    instance_task: InteractiveTaskResult | None,
    keeper_task: InteractiveTaskResult | None,
    healthcheck_task: InteractiveTaskResult | None,
) -> InteractivePageStatus:
    sources = [
        (
            _snapshot_key('instances', account_scope),
            '最近实例更新',
            '实例刷新失败（保留上次结果）',
            '实例刷新失败',
        ),
        (
            _snapshot_key('keeper_probe', account_scope),
            '最近 Keeper 更新',
            'Keeper 刷新失败（保留上次结果）',
            'Keeper 刷新失败',
        ),
        (
            _snapshot_key('healthcheck', account_scope),
            '最近健康自检更新',
            '健康自检刷新失败（保留上次结果）',
            '健康自检刷新失败',
        ),
        (
            _snapshot_key('config_diagnostics', account_scope),
            '最近配置诊断更新',
            '配置诊断刷新失败（保留上次结果）',
            '配置诊断刷新失败',
        ),
    ]
    status = _page_status_from_snapshot_keys(
        snapshot_store=snapshot_store,
        snapshot_keys=[key for key, *_ in sources],
        primary_task=instance_task,
        secondary_tasks=[keeper_task, healthcheck_task],
    )
    if any(task is not None and task.status in {'queued', 'running'} for task in (instance_task, keeper_task, healthcheck_task)):
        return status

    latest_ready: tuple[str, Any] | None = None
    latest_failed: tuple[str, Any] | None = None
    for key, ready_message, failed_keep_message, failed_message in sources:
        entry = snapshot_store.get_entry(key)
        if entry is None:
            continue
        if entry.updated_at:
            if latest_ready is None or str(entry.updated_at) >= str(latest_ready[1].updated_at):
                latest_ready = (ready_message, entry)
        if entry.error_message:
            failed_label = failed_keep_message if entry.updated_at else failed_message
            if latest_failed is None:
                latest_failed = (failed_label, entry)
            elif entry.updated_at and (not latest_failed[1].updated_at or str(entry.updated_at) >= str(latest_failed[1].updated_at)):
                latest_failed = (failed_label, entry)

    if latest_failed is not None and (latest_ready is None or str(latest_failed[1].updated_at or '') >= str(latest_ready[1].updated_at or '')):
        return InteractivePageStatus(
            state='failed',
            message=latest_failed[0],
            updated_at=str(latest_failed[1].updated_at or ''),
            error_message=str(latest_failed[1].error_message or ''),
        )
    if latest_ready is not None:
        return InteractivePageStatus(
            state='ready',
            message=latest_ready[0],
            updated_at=str(latest_ready[1].updated_at or ''),
            error_message='',
        )
    return status

def _dashboard_placeholder_view(
    *,
    settings: Settings,
    store,
    current_account: str | None,
    scheduled_job_status_rows_fn,
) -> dict[str, Any]:
    account_name = current_account or _pick_default_account(settings, None, store) or 'default'
    account = next((item for item in settings.accounts if item.name == account_name), None)
    current_account_row = None
    if isinstance(account, AccountSettings):
        current_account_row = inspect_auth_state(account.to_auth_settings(), store=store, account_name=account.name)
    scheduled_rows = scheduled_job_status_rows_fn(settings, store, account_name=account_name)
    service_state = _service_state_snapshot(store)
    return {
        'runtime_status': read_daemon_status(store),
        'current_account': account_name,
        'current_account_row': current_account_row or {},
        'account_rows': [current_account_row] if current_account_row else [],
        'enabled_accounts': len([item for item in settings.accounts if item.enabled]) if settings.accounts else 1,
        'keeper_enabled': settings.tasks.keeper.enabled,
        'scheduled_enabled': settings.tasks.scheduled_start.enabled,
        'effective_keeper_enabled': get_task_enabled(store, account_name, 'keeper', default_enabled=settings.tasks.keeper.enabled),
        'effective_scheduled_enabled': get_task_enabled(store, account_name, 'scheduled_start', default_enabled=settings.tasks.scheduled_start.enabled),
        'paused_task_count': 0,
        'paused_job_count': sum(1 for row in scheduled_rows if not row.get('enabled')),
        'scheduled_jobs': scheduled_rows,
        'instance_rows': [],
        'recent_history': [],
        'recent_failures': [],
        'failure_account_summary': [],
        'recent_auth_rows': [],
        'candidate_summary': {'job_name': '', 'selected_instance_id': '', 'candidate_count': 0, 'top_reasons': []},
        'keeper_summary': {'pending': 0, 'expiring_soon': 0, 'failed': 0},
        'service_state_label': service_state['label'],
        'service_state_tone': service_state['tone'],
        'service_last_seen_at': service_state['last_seen_at'],
        'service_pid': service_state['pid'],
    }

def _dashboard_apply_account_snapshot(view: dict[str, Any], account_snapshot: dict[str, Any] | None) -> None:
    if isinstance(account_snapshot, dict):
        view['current_account_row'] = {
            'status': account_snapshot.get('auth_status') or view.get('current_account_row', {}).get('status'),
            'auth_source': account_snapshot.get('auth_source') or view.get('current_account_row', {}).get('auth_source'),
            'cached_at_iso': account_snapshot.get('cached_at_iso') or view.get('current_account_row', {}).get('cached_at_iso'),
        }


def _dashboard_keeper_summary(keeper_rows) -> dict[str, int]:
    expiring_cutoff = datetime.now().astimezone() + timedelta(days=7)
    expiring_soon = 0
    failed = 0
    abnormal = 0
    pending = 0
    not_due = 0
    for row in keeper_rows:
        deadline = _parse_iso_datetime(str(row.get('release_deadline') or ''))
        if deadline is not None:
            if deadline.tzinfo is None:
                deadline = deadline.astimezone()
            if datetime.now().astimezone() <= deadline <= expiring_cutoff:
                expiring_soon += 1
        result = str(row.get('result') or '')
        if result in {'skip_missing_shutdown_time', 'skip_missing_instance_id'}:
            abnormal += 1
        if result in {'keeper_failed_power_on', 'keeper_failed_power_off'}:
            failed += 1
        if bool(row.get('eligible')):
            pending += 1
        if result == 'skip_not_due':
            not_due += 1
    return {
        'pending': pending,
        'not_due': not_due,
        'abnormal': abnormal,
        'expiring_soon': expiring_soon,
        'failed': failed,
    }


def _dashboard_apply_keeper_snapshot(view: dict[str, Any], keeper_rows) -> None:
    if isinstance(keeper_rows, list):
        view['keeper_summary'] = _dashboard_keeper_summary(keeper_rows)


def _dashboard_snapshot_view(
    *,
    settings: Settings,
    store,
    current_account: str | None,
    scheduled_job_status_rows_fn,
    snapshot_store: InteractiveSnapshotStore,
) -> dict[str, Any]:
    view = _dashboard_placeholder_view(
        settings=settings,
        store=store,
        current_account=current_account,
        scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
    )
    account_name = current_account or 'default'
    account_snapshot = snapshot_store.get_snapshot(_snapshot_key('account_runtime', account_name))
    keeper_rows = snapshot_store.get_snapshot(_snapshot_key('keeper_probe', account_name))
    _dashboard_apply_account_snapshot(view, account_snapshot)
    _dashboard_apply_keeper_snapshot(view, keeper_rows)
    return view

__all__ = [
    '_diagnostics_page_status',
    '_dashboard_placeholder_view',
    '_dashboard_snapshot_view',
]

_diagnostics_page_status = _delegate('_diagnostics_page_status', _diagnostics_page_status)
_dashboard_placeholder_view = _delegate('_dashboard_placeholder_view', _dashboard_placeholder_view)
_dashboard_snapshot_view = _delegate('_dashboard_snapshot_view', _dashboard_snapshot_view)
