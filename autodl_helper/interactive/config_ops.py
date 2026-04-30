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

def _stable_keeper_payload(settings: Settings, raw_payload: dict[str, Any]) -> dict[str, Any]:
    current = copy.deepcopy(((raw_payload.get('tasks') or {}).get('keeper') or {}))
    current.update(
        {
            'enabled': settings.tasks.keeper.enabled,
            'shutdown_release_after_hours': settings.tasks.keeper.shutdown_release_after_hours,
            'keeper_trigger_before_hours': settings.tasks.keeper.keeper_trigger_before_hours,
            'interval_minutes': settings.tasks.keeper.interval_minutes,
            'power_on_wait_seconds': settings.tasks.keeper.power_on_wait_seconds,
            'power_off_wait_seconds': settings.tasks.keeper.power_off_wait_seconds,
            'start_cooldown_minutes': settings.tasks.keeper.start_cooldown_minutes,
            'stop_cooldown_minutes': settings.tasks.keeper.stop_cooldown_minutes,
            'fallback_to_status_at': settings.tasks.keeper.fallback_to_status_at,
        }
    )
    return current


def _stable_scheduled_payload(settings: Settings, raw_payload: dict[str, Any]) -> dict[str, Any]:
    current = copy.deepcopy(((raw_payload.get('tasks') or {}).get('scheduled_start') or {}))
    current.update(
        {
            'enabled': settings.tasks.scheduled_start.enabled,
            'poll_interval_seconds': settings.tasks.scheduled_start.poll_interval_seconds,
            'jobs': [_job_to_payload(job) for job in settings.tasks.scheduled_start.jobs],
        }
    )
    return current


def _normalized_stable_tasks_payload_from_raw(raw_payload: dict[str, Any]) -> dict[str, Any]:
    tasks_payload = raw_payload.get('tasks') or {}
    keeper_payload = copy.deepcopy(tasks_payload.get('keeper') or {})
    scheduled_payload = copy.deepcopy(tasks_payload.get('scheduled_start') or {})
    jobs = []
    for item in scheduled_payload.get('jobs') or []:
        if not isinstance(item, dict):
            continue
        normalized_job = {
            'instance_id': item.get('instance_id', ''),
            'name': item.get('name', ''),
            'target_time': item.get('target_time', '14:00'),
            'advance_hours': item.get('advance_hours', 2),
            'schedule_mode': str(item.get('schedule_mode', 'daily') or 'daily'),
            'timezone': item.get('timezone', 'Asia/Shanghai') or 'Asia/Shanghai',
        }
        selector_payload = item.get('selector')
        if isinstance(selector_payload, dict):
            normalized_job['selector'] = {
                'regions': list(selector_payload.get('regions') or []),
                'gpu_model': selector_payload.get('gpu_model', ''),
                'gpu_count': selector_payload.get('gpu_count', 1),
                'charge_types': list(selector_payload.get('charge_types') or []),
            }
        jobs.append(normalized_job)
    return {
        'keeper': {
            'enabled': keeper_payload.get('enabled', True),
            'shutdown_release_after_hours': keeper_payload.get('shutdown_release_after_hours', 360),
            'keeper_trigger_before_hours': keeper_payload.get('keeper_trigger_before_hours', 6),
            'interval_minutes': keeper_payload.get('interval_minutes', 60),
            'power_on_wait_seconds': keeper_payload.get('power_on_wait_seconds', 60),
            'power_off_wait_seconds': keeper_payload.get('power_off_wait_seconds', 5),
            'start_cooldown_minutes': keeper_payload.get('start_cooldown_minutes', 60),
            'stop_cooldown_minutes': keeper_payload.get('stop_cooldown_minutes', 360),
            'fallback_to_status_at': keeper_payload.get('fallback_to_status_at', True),
        },
        'scheduled_start': {
            'enabled': scheduled_payload.get('enabled', False),
            'poll_interval_seconds': scheduled_payload.get('poll_interval_seconds', 300),
            'jobs': jobs,
        },
    }


def _normalized_stable_tasks_payload_from_settings(settings: Settings, raw_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        'keeper': _stable_keeper_payload(settings, raw_payload),
        'scheduled_start': _stable_scheduled_payload(settings, raw_payload),
    }


def _diff_structures(before: Any, after: Any, *, prefix: str = '') -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        keys = sorted(set(before) | set(after))
        diffs: list[str] = []
        for key in keys:
            next_prefix = f'{prefix}.{key}' if prefix else str(key)
            diffs.extend(_diff_structures(before.get(key), after.get(key), prefix=next_prefix))
        return diffs
    if isinstance(before, list) and isinstance(after, list):
        diffs: list[str] = []
        length = max(len(before), len(after))
        for index in range(length):
            next_prefix = f'{prefix}[{index}]'
            before_item = before[index] if index < len(before) else None
            after_item = after[index] if index < len(after) else None
            diffs.extend(_diff_structures(before_item, after_item, prefix=next_prefix))
        return diffs
    if before != after:
        return [prefix]
    return []


def _collect_legacy_config_fields(raw_payload: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    jobs = (((raw_payload.get('tasks') or {}).get('scheduled_start') or {}).get('jobs') or [])
    for index, item in enumerate(jobs, start=1):
        if isinstance(item, dict) and item.get('priority'):
            label = item.get('name') or item.get('instance_id') or f'job-{index}'
            fields.append(f'{label}.priority')
    return fields


def _sync_stable_config_payload(
    *,
    config_path: str,
    raw_payload: dict[str, Any],
    settings: Settings,
    load_settings_fn: Callable[[str], Settings],
    validate_settings_fn: Callable[[Settings, str], list[str]],
) -> tuple[list[str], list[str]]:
    before = _normalized_stable_tasks_payload_from_raw(raw_payload)
    after = _normalized_stable_tasks_payload_from_settings(settings, raw_payload)
    diff_paths = _diff_structures(before, after, prefix='tasks')
    legacy_fields = _collect_legacy_config_fields(raw_payload)
    if not diff_paths and not legacy_fields:
        return [], legacy_fields
    original_payload = copy.deepcopy(raw_payload)
    tasks_payload = raw_payload.setdefault('tasks', {})
    tasks_payload['keeper'] = _stable_keeper_payload(settings, raw_payload)
    tasks_payload['scheduled_start'] = _stable_scheduled_payload(settings, raw_payload)
    write_raw_settings(config_path, raw_payload)
    updated_settings = load_settings_fn(config_path)
    errors = validate_settings_fn(updated_settings, purpose='validate')
    if errors:
        write_raw_settings(config_path, original_payload)
        raise ValueError('配置同步失败: ' + '; '.join(errors))
    return diff_paths, legacy_fields


def _runtime_override_rows() -> list[str]:
    mapping = [
        ('Authorization', 'auth.authorization'),
        ('AUTODL_PHONE', 'auth.autodl_phone'),
        ('AUTODL_PASSWORD', 'auth.autodl_password'),
        ('AUTODL_AUTH_CACHE_FILE', 'auth.cache_file'),
        ('AUTODL_LOGIN_RETRIES', 'auth.login_retries'),
        ('AUTODL_LOGIN_TIMEOUT_MS', 'auth.login_timeout_ms'),
        ('AUTODL_POST_LOGIN_WAIT_SECONDS', 'auth.post_login_wait_seconds'),
        ('AUTODL_AUTH_CACHE_MAX_AGE_SECONDS', 'auth.cache_max_age_seconds'),
        ('AUTODL_DB_PATH', 'storage.database_file'),
        ('MIN_DAY', 'tasks.keeper.min_day'),
        ('PUSHPLUS_TOKEN', 'notifications.pushplus.token'),
        ('SERVERCHAN_SENDKEY', 'notifications.serverchan.token'),
        ('SMTP_HOST', 'notifications.email.smtp_host'),
        ('SMTP_PORT', 'notifications.email.smtp_port'),
        ('SMTP_USERNAME', 'notifications.email.username'),
        ('SMTP_PASSWORD', 'notifications.email.password'),
    ]
    rows = []
    for env_name, config_key in mapping:
        if os.getenv(env_name, '').strip():
            rows.append(f'{config_key} ← 环境变量/.env ({env_name})')
    return rows


def _render_config_diagnostics(
    *,
    settings: Settings,
    current_account: str | None,
    config_path: str,
    load_settings_fn: Callable[[str], Settings],
    validate_settings_fn: Callable[[Settings, str], list[str]],
) -> str:
    raw_payload = read_raw_settings(config_path)
    diff_paths, legacy_fields = _sync_stable_config_payload(
        config_path=config_path,
        raw_payload=raw_payload,
        settings=settings,
        load_settings_fn=load_settings_fn,
        validate_settings_fn=validate_settings_fn,
    )
    override_rows = _runtime_override_rows()
    lines = [
        _heading('配置诊断', color=CYAN),
        _separator(),
        _key_value('当前账号', _account_display_name(settings, current_account)),
        _key_value('稳定配置同步', '已同步' if diff_paths or legacy_fields else '无需同步'),
        '',
        _section('[同步结果]'),
    ]
    if diff_paths:
        lines.append(_key_value('已同步字段数', len(diff_paths)))
        for path in diff_paths[:10]:
            lines.append(f'- {path}')
    else:
        lines.append('- 没有需要同步的稳定字段')
    if legacy_fields:
        lines.append('')
        lines.append(_section('[历史字段]'))
        for field in legacy_fields:
            lines.append(f'- {field}（已清理历史 priority 字段）')
    else:
        lines.append('')
        lines.append(_section('[历史字段]'))
        lines.append('- 没有历史字段')
    lines.append('')
    lines.append(_section('[运行时覆盖]'))
    if override_rows:
        for row in override_rows:
            lines.append(f'- {row}')
    else:
        lines.append('- 当前没有环境变量/.env 覆盖')
    lines.extend([
        '',
        _section('[说明]'),
        '- 只会自动同步 YAML 可持久化的稳定字段。',
        '- 环境变量/.env、敏感字段和 CLI 临时覆盖不会写回配置文件。',
    ])
    return '\n'.join(lines)
datetime = _delegate('datetime', datetime)

__all__ = [
    "_stable_keeper_payload",
    "_stable_scheduled_payload",
    "_normalized_stable_tasks_payload_from_raw",
    "_normalized_stable_tasks_payload_from_settings",
    "_diff_structures",
    "_collect_legacy_config_fields",
    "_sync_stable_config_payload",
    "_runtime_override_rows",
    "_render_config_diagnostics",
]
