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
from .status_task import _bump_subprocess_task_stat

if TYPE_CHECKING:
    from autodl_helper.models import HistoryRecord

DEFAULT_SERVICE_LABEL = 'autodl-helper'
_SERVICE_CONFIG_PATH = 'config.yaml'

def _copy_args(args: argparse.Namespace, **updates: Any) -> SimpleNamespace:
    payload = dict(vars(args))
    payload.update(updates)
    return SimpleNamespace(**payload)

def _mask_phone(phone: str | None) -> str:
    raw = str(phone or '').strip()
    if len(raw) < 7:
        return '-'
    return f'{raw[:3]}****{raw[-4:]}'

def _account_display_name(settings: Settings, account_name: str | None) -> str:
    if not account_name:
        return '-'
    account = next((item for item in settings.accounts if item.name == account_name), None)
    if account is None:
        return account_name
    phone = _mask_phone(account.autodl_phone) if account.autodl_phone else ''
    if phone and phone != '-':
        return f'{account.name} ({phone})'
    return account.name

def _auth_rank(status: str) -> int:
    order = {
        'logged_in': 5,
        'cached': 4,
        'token_configured': 3,
        'login_ready': 2,
        'not_configured': 1,
    }
    return order.get(status, 0)

def _pick_default_account(settings: Settings, preferred_account: str | None, store) -> str | None:
    enabled_accounts = [account for account in settings.accounts if account.enabled] if settings.accounts else []
    if preferred_account and any(account.name == preferred_account for account in enabled_accounts):
        return preferred_account
    if not enabled_accounts:
        return preferred_account or 'default'
    ranked = []
    for index, account in enumerate(enabled_accounts):
        state = inspect_auth_state(account.to_auth_settings(), store=store, account_name=account.name)
        ranked.append((_auth_rank(str(state.get('status') or '')), index, account.name))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked[0][2] if ranked else enabled_accounts[0].name

def _capture_action_output(action: Callable[[], Any]) -> tuple[Any, str]:
    result, output = capture_callable_output(action)
    output = output or '无输出。'
    return result, output

def _run_captured_action(title: str, action: Callable[[], int | None]) -> tuple[int | None, str]:
    del title
    result, output = _capture_action_output(action)
    code = result if isinstance(result, int) or result is None else 0
    return code, output

def _background_command_entry(command_fn, args_payload: dict[str, Any], result_queue) -> None:
    try:
        code, output = _run_captured_action(
            '后台命令',
            lambda: command_fn(SimpleNamespace(**args_payload)),
        )
        result_queue.put(
            {
                'ok': True,
                'code': code,
                'output': output,
                'summary': '',
                'timed_out': False,
            }
        )
    except BaseException as exc:
        result_queue.put(
            {
                'ok': False,
                'code': 1,
                'output': '',
                'summary': str(exc),
                'timed_out': False,
            }
        )

def _run_command_with_timeout(
    *,
    command_fn,
    args: SimpleNamespace,
    timeout_seconds: float,
    title: str,
    timeout_summary: str,
) -> dict[str, Any]:
    started_at = time.time()
    try:
        _bump_subprocess_task_stat('started')
        code, output = _run_captured_action(title, lambda: command_fn(args))
        elapsed_seconds = round(max(0.0, time.time() - started_at), 3)
        long_running = elapsed_seconds > max(0.0, timeout_seconds)
        if long_running:
            _bump_subprocess_task_stat('long_running')
        _bump_subprocess_task_stat('completed')
        return {
            'ok': True,
            'code': code,
            'output': output,
            'summary': timeout_summary if long_running else '',
            'timed_out': False,
            'long_running': long_running,
            'elapsed_seconds': elapsed_seconds,
        }
    except Exception:
        _bump_subprocess_task_stat('failed')
        raise


__all__ = [
    "_copy_args",
    "_mask_phone",
    "_account_display_name",
    "_auth_rank",
    "_pick_default_account",
    "_capture_action_output",
    "_run_captured_action",
    "_background_command_entry",
    "_run_command_with_timeout",
]
