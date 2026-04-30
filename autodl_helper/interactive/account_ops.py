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

def _enabled_account_names(settings: Settings) -> list[str]:
    if settings.accounts:
        return [account.name for account in settings.accounts if account.enabled]
    return ['default']


def _ensure_account_payloads(raw_payload: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    accounts_payload = raw_payload.get('accounts')
    if isinstance(accounts_payload, list) and accounts_payload:
        return accounts_payload
    payload_accounts: list[dict[str, Any]] = []
    for account in settings.accounts:
        payload_accounts.append(
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
        )
    if not payload_accounts:
        payload_accounts.append({'name': 'default', 'enabled': True})
    raw_payload['accounts'] = payload_accounts
    return payload_accounts


def _resolve_current_account_slot(settings: Settings, current_account: str | None) -> str:
    if current_account:
        return current_account
    if settings.accounts:
        enabled = [account.name for account in settings.accounts if account.enabled]
        if enabled:
            return enabled[0]
        return settings.accounts[0].name
    return 'default'


def _clear_persisted_auth_state(*, store, account_name: str, cache_file: str) -> None:
    clear_runtime_authorization(account_name)
    if store is not None:
        store.set_auth_cache(account_name, '', 0)
    write_auth_cache(cache_file, '', cached_at=0)


def _persist_account_credentials(
    *,
    config_path: str,
    settings: Settings,
    current_account: str | None,
    mode: str,
    authorization: str = '',
    phone: str = '',
    password: str = '',
    load_settings_fn: Callable[[str], Settings],
    validate_settings_fn: Callable[[Settings, str], list[str]],
    store,
) -> tuple[Settings, str]:
    account_name = _resolve_current_account_slot(settings, current_account)
    raw_payload = read_raw_settings(config_path)
    original_payload = copy.deepcopy(raw_payload)
    accounts_payload = _ensure_account_payloads(raw_payload, settings)
    target_payload = next((item for item in accounts_payload if str(item.get('name') or '') == account_name), None)
    if target_payload is None:
        target_payload = {'name': account_name, 'enabled': True}
        accounts_payload.insert(0, target_payload)
    auth_payload = raw_payload.setdefault('auth', {})
    if mode == 'authorization':
        token = authorization.strip()
        if not token:
            raise ValueError('Authorization 不能为空。')
        target_payload['authorization'] = token
        target_payload['autodl_phone'] = ''
        target_payload['autodl_password'] = ''
        auth_payload['authorization'] = token
        auth_payload['autodl_phone'] = ''
        auth_payload['autodl_password'] = ''
    elif mode == 'password':
        phone = phone.strip()
        password = password.strip()
        if not phone or not password:
            raise ValueError('手机号和密码都不能为空。')
        target_payload['authorization'] = ''
        target_payload['autodl_phone'] = phone
        target_payload['autodl_password'] = password
        auth_payload['authorization'] = ''
        auth_payload['autodl_phone'] = phone
        auth_payload['autodl_password'] = password
    else:
        raise ValueError(f'未知账号切换方式: {mode}')
    target_payload['enabled'] = True
    write_raw_settings(config_path, raw_payload)
    try:
        updated_settings = load_settings_fn(config_path)
        errors = validate_settings_fn(updated_settings, purpose='validate')
        if errors:
            raise ValueError('; '.join(errors))
    except Exception:
        write_raw_settings(config_path, original_payload)
        raise
    account = next((item for item in updated_settings.accounts if item.name == account_name), None)
    auth_settings = account.to_auth_settings() if isinstance(account, AccountSettings) else updated_settings.auth
    _clear_persisted_auth_state(
        store=store,
        account_name=account_name,
        cache_file=auth_settings.cache_file,
    )
    return updated_settings, account_name


def _switch_to_new_account(
    *,
    args: argparse.Namespace,
    settings: Settings,
    store,
    current_account: str | None,
    command_login_fn,
    load_settings_fn: Callable[[str], Settings],
    validate_settings_fn: Callable[[Settings, str], list[str]],
) -> tuple[Settings, str | None]:
    choice = _choose_menu(
        '切换到新账号',
        [
            MenuItem('1', '粘贴 Authorization Token'),
            MenuItem('2', '浏览器登录（手机号+密码）'),
            MenuItem('0', '取消'),
        ],
        default_key='1',
    )
    if choice == '0':
        return settings, current_account
    account_name = _resolve_current_account_slot(settings, current_account)
    try:
        if choice == '1':
            token = _prompt_with_default('Authorization')
            settings, account_name = _persist_account_credentials(
                config_path=args.config,
                settings=settings,
                current_account=account_name,
                mode='authorization',
                authorization=token,
                load_settings_fn=load_settings_fn,
                validate_settings_fn=validate_settings_fn,
                store=store,
            )
        elif choice == '2':
            phone = _prompt_with_default('AutoDL 手机号')
            password = _prompt_with_default('AutoDL 密码')
            settings, account_name = _persist_account_credentials(
                config_path=args.config,
                settings=settings,
                current_account=account_name,
                mode='password',
                phone=phone,
                password=password,
                load_settings_fn=load_settings_fn,
                validate_settings_fn=validate_settings_fn,
                store=store,
            )
        else:
            print('无效选择。')
            return settings, current_account
    except _InteractiveCancel:
        return settings, current_account
    except ValueError as exc:
        _print_execution_summary('切换新账号失败', detail=str(exc))
        return settings, current_account
    _show_login_refresh_progress(
        args=args,
        account_name=account_name,
        command_login_fn=command_login_fn,
        title='切换到新账号',
        headed_override=True if choice == '2' else None,
    )
    return settings, account_name


def _account_runtime_snapshot(
    settings: Settings,
    store,
    *,
    account_name: str,
    keeper_probe_rows_fn: Callable[..., list[dict[str, Any]]],
    scheduled_job_status_rows_fn: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    account = next((item for item in settings.accounts if item.name == account_name), None)
    auth_status = '未配置'
    auth_source = '-'
    account_enabled = True
    if account is not None:
        state = inspect_auth_state(account.to_auth_settings(), store=store, account_name=account.name)
        auth_status = str(state.get('status') or '未配置')
        auth_source = str(state.get('auth_source') or '未配置')
        cached_at_iso = str(state.get('cached_at_iso') or '')
        account_enabled = bool(account.enabled)
    else:
        cached_at_iso = ''
    probe_rows = keeper_probe_rows_fn(settings, store, account_name=account_name)
    now = datetime.now().astimezone()
    deadline_cutoff = now + timedelta(days=7)
    running_instances = sum(1 for row in probe_rows if str(row.get('status') or '').lower() == 'running')
    expiring_soon = 0
    for row in probe_rows:
        deadline = _parse_iso_datetime(str(row.get('release_deadline') or ''))
        if deadline is None:
            continue
        if deadline.tzinfo is None:
            deadline = deadline.astimezone()
        if now <= deadline <= deadline_cutoff:
            expiring_soon += 1
    scheduled_rows = scheduled_job_status_rows_fn(settings, store, account_name=account_name)
    paused_jobs = sum(1 for row in scheduled_rows if not bool(row.get('enabled', True)))
    return {
        'account_name': account_name,
        'account_enabled': account_enabled,
        'auth_status': auth_status,
        'auth_source': auth_source,
        'cached_at_iso': cached_at_iso,
        'running_instances': running_instances,
        'expiring_soon': expiring_soon,
        'scheduled_jobs': len(scheduled_rows),
        'paused_jobs': paused_jobs,
        'keeper_enabled': get_task_enabled(store, account_name, 'keeper', default_enabled=settings.tasks.keeper.enabled),
    }


def _login_verify_snapshot(
    *,
    args: argparse.Namespace,
    account_name: str,
    command_login_fn,
    settings: Settings,
    store,
    keeper_probe_rows_fn: Callable[..., list[dict[str, Any]]],
    scheduled_job_status_rows_fn: Callable[..., list[dict[str, Any]]],
    timeout_seconds: float = LOGIN_VERIFY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    result = _run_command_with_timeout(
        command_fn=command_login_fn,
        args=_copy_args(args, account=account_name, headed=False, all=False),
        timeout_seconds=timeout_seconds,
        title='登录状态验证',
        timeout_summary='登录状态验证超时，已终止本次后台验证',
    )
    if not bool(result.get('ok')):
        raise ValueError(str(result.get('summary') or '登录状态验证失败'))
    code = result.get('code')
    if code not in {0, None}:
        raise ValueError(f'登录状态验证失败（code={code}）')
    return _account_runtime_snapshot(
        settings,
        store,
        account_name=account_name,
        keeper_probe_rows_fn=keeper_probe_rows_fn,
        scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
    )
datetime = _delegate('datetime', datetime)
def _account_show_login_refresh_progress_fallback(*args, **kwargs):
    from .service_ops import _show_login_refresh_progress as _impl
    return _impl(*args, **kwargs)

_show_login_refresh_progress = _delegate('_show_login_refresh_progress', _account_show_login_refresh_progress_fallback)

__all__ = [
    "_enabled_account_names",
    "_ensure_account_payloads",
    "_resolve_current_account_slot",
    "_clear_persisted_auth_state",
    "_persist_account_credentials",
    "_switch_to_new_account",
    "_account_runtime_snapshot",
    "_login_verify_snapshot",
]
