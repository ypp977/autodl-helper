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

from autodl_helper.core.api import AutoDLClient
from autodl_helper.core.auth import AuthError, alert_auth_failure, inspect_auth_state, resolve_authorization
from autodl_helper.core.auth import resolve_auth_runtime_policy
from autodl_helper.core.config import AccountSettings, LIGHTWEIGHT_MODES, NotificationSettings, Settings, load_settings, read_raw_settings, write_raw_settings
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
from autodl_helper.services.launchd import append_service_lifecycle_log
from autodl_helper.services.manager import (
    install_service,
    restart_service,
    service_status,
    start_service,
    stop_service,
    uninstall_service,
)
from autodl_helper.state import StateStore
from autodl_helper.core.store import SQLiteStore
from autodl_helper.tasks.keeper import evaluate_keeper_instance, format_duration_seconds, run_keeper_cycle
from autodl_helper.tasks.scheduled_start import ScheduledStartJobRuntime, run_scheduled_start_job
from autodl_helper.cli.output import print_json_error

logger = logging.getLogger(__name__)
TIME_RE = re.compile(r'^(?:[01]\d|2[0-3]):[0-5]\d$')
DAEMON_HEARTBEAT_INTERVAL_SECONDS = 30



from ..shared import (
    get_enabled_accounts,
    select_accounts,
    create_store,
    _account_status_label,
    _account_source_label,
    account_status_rows,
    record_auth_event,
    create_client,
    build_client,
    _has_config_edit_args,
    _prompt_optional_text,
    _prompt_optional_int,
    _prompt_optional_bool,
    collect_config_edit_args,
    _ensure_account_payloads,
    _select_account_payloads,
    _ensure_task_payload,
    _select_job_payloads,
    compute_cycle_interval_seconds,
    compute_dispatch_interval_seconds,
    compute_interval_for_mode,
    _sync_primary_auth,
    _resolve_account_override_targets,
    _resolve_job_override_targets,
    apply_cli_overrides,
    serialize_settings,
    validate_settings,
    build_named_notifiers,
    build_notifiers,
    probe_path_writable,
    collect_healthcheck_errors,
    _scheduled_start_reason_label,
    _format_scheduled_window,
    _format_next_check,
    _format_local_time_label,
    _format_keeper_window,
    _log_scheduled_start_summary,
)

def command_accounts(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    account_status_rows_fn: Callable[..., list[dict[str, Any]]] = account_status_rows,
) -> int:
    settings = load_settings_fn(args.config)
    store = create_store_fn(settings)
    try:
        rows = account_status_rows_fn(settings, store, account_name=getattr(args, 'account', None))
    except ValueError as exc:
        if getattr(args, 'json', False):
            print_json_error('account_error', str(exc))
            return 1
        print(str(exc), file=sys.stderr)
        return 1
    if getattr(args, 'json', False):
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print('账号状态')
    print('=' * 72)
    header = f"{'account':<16} {'enabled':<7} {'status':<18} {'source':<12} {'cache':<20} {'creds':<5} {'cfg':<5} {'mode':<10}"
    print(header)
    print('-' * len(header))
    for row in rows:
        cached_at = row.get('cached_at_iso') or '-'
        if len(cached_at) > 19:
            cached_at = cached_at[:19]
        print(
            f"{row['account_name']:<16} "
            f"{('yes' if row['enabled'] else 'no'):<7} "
            f"{row['status_label']:<18} "
            f"{row['auth_source_label']:<12} "
            f"{cached_at:<20} "
            f"{('yes' if row['has_credentials'] else 'no'):<5} "
            f"{('yes' if row['has_config_token'] else 'no'):<5} "
            f"{row['lightweight_mode']:<10}"
        )
    return 0


def command_login(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    resolve_authorization_fn: Callable[..., str] = resolve_authorization,
) -> int:
    settings = load_settings_fn(args.config)
    store = create_store_fn(settings)
    try:
        if getattr(args, 'all', False):
            accounts = select_accounts_fn(settings, None)
        elif getattr(args, 'account', None):
            accounts = select_accounts_fn(settings, args.account)
        else:
            accounts = select_accounts_fn(settings, None, require_explicit_for_multi=True)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    failed = False
    for account in accounts:
        try:
            resolve_authorization_fn(
                account.to_auth_settings(),
                headed=args.headed,
                force_refresh=True,
                store=store,
                account_name=account.name,
            )
            state = inspect_auth_state(account.to_auth_settings(), store=store, account_name=account.name)
            print(
                f"登录成功: account={account.name} status={_account_status_label(str(state['status']))} "
                f"source={_account_source_label(str(state['auth_source']))}"
            )
        except AuthError as exc:
            failed = True
            print(f'登录失败: account={account.name} {exc}', file=sys.stderr)
    return 1 if failed else 0


__all__ = [
    "command_accounts",
    "command_login",
]
