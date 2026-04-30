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
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta
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

from . import dialogs as _dialogs
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
    _show_cursor,
    _split_csv,
    _supports_arrow_menu,
    _update_menu_selection,
    _update_menu_title,
)

# Backward-compatible monkeypatch anchors used by older tests and integrations.
# Keep these as aliases to dialogs' platform-aware imports; on Windows termios/tty
# are intentionally None so importing interactive.app still works.
select = _dialogs.select
termios = _dialogs.termios
tty = _dialogs.tty

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
from .service_ops import *  # noqa: F401,F403
from .config_ops import *  # noqa: F401,F403
from .screens import *  # noqa: F401,F403

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


def _patch_compat_support_exports() -> None:
    try:
        from .support import keeper as _keeper_support
        from .support import scheduled as _scheduled_support
        from .features.instances.browse import _browse_keeper_probe as _browse_keeper_probe_impl
        from .features.keeper import probe as _keeper_probe_module
    except Exception:
        return
    for name in ('_persist_keeper_changes', '_store_snapshot'):
        if hasattr(_keeper_support, name):
            continue
        if hasattr(_scheduled_support, name):
            setattr(_keeper_support, name, getattr(_scheduled_support, name))
    if not hasattr(_keeper_probe_module, '_browse_keeper_probe'):
        setattr(_keeper_probe_module, '_browse_keeper_probe', _browse_keeper_probe_impl)


_patch_compat_support_exports()


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


def _scheduled_menu(*args, **kwargs):
    from .menu_scheduled import _scheduled_menu as _impl

    return _impl(*args, **kwargs)



def _keeper_menu(*args, **kwargs):
    from .menu_keeper import _keeper_menu as _impl

    return _impl(*args, **kwargs)



def _account_menu(*args, **kwargs):
    from .menu_account import _account_menu as _impl

    return _impl(*args, **kwargs)



def _records_menu(*args, **kwargs):
    from .menu_records import _records_menu as _impl

    return _impl(*args, **kwargs)



def _diagnostics_page_status(*args, **kwargs):
    from .menu_diagnostics import _diagnostics_page_status as _impl

    return _impl(*args, **kwargs)



def _diagnostics_menu(*args, **kwargs):
    from .menu_diagnostics import _diagnostics_menu as _impl

    return _impl(*args, **kwargs)



def run_interactive(*args, **kwargs):
    from .app_runtime import run_interactive as _impl

    if args:
        runtime_args = args[0]
        try:
            global _SERVICE_CONFIG_PATH
            _SERVICE_CONFIG_PATH = runtime_args.config
        except Exception:
            pass
    return _impl(*args, **kwargs)
