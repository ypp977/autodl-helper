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

def command_history(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    history_row_to_json_fn: Callable[[Any], dict[str, object]],
    format_history_table_fn: Callable[[Sequence[Any]], str],
) -> int:
    settings = load_settings_fn(args.config)
    store = create_store_fn(settings)
    try:
        if args.account:
            select_accounts_fn(settings, args.account)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    rows = store.read_history(account_name=args.account, task_type=args.task, event_type=args.event_type, limit=args.limit)
    if not rows:
        print('No history.')
        return 0
    if args.json:
        print(json.dumps([history_row_to_json_fn(row) for row in rows], ensure_ascii=False, indent=2))
        return 0
    print(format_history_table_fn(rows))
    return 0


def command_auth_report(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    auth_report_row_to_json_fn: Callable[[Any], dict[str, object]],
    auth_report_match_label_fn: Callable[[Any], str],
    likely_auth_candidate_fn: Callable[[Any], bool],
    render_auth_signal_patch_fn: Callable[[Sequence[Any]], str],
    apply_auth_signal_patch_fn: Callable[[Sequence[Any]], tuple[int, int, str]],
    known_code_signals: Sequence[str],
    known_message_signals: Sequence[str],
) -> int:
    settings = load_settings_fn(args.config)
    store = create_store_fn(settings)
    try:
        if args.account:
            select_accounts_fn(settings, args.account)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    rows = store.summarize_auth_failures(account_name=args.account, limit=args.limit)
    if args.only_unmapped:
        rows = [row for row in rows if not row.mapped]
    if args.only_likely_auth:
        rows = [row for row in rows if likely_auth_candidate_fn(row)]
    if args.apply_suggested_patch:
        code_count, message_count, file_path = apply_auth_signal_patch_fn(rows)
        print(f'Applied suggested patch to {file_path}: codes={code_count}, messages={message_count}')
        return 0
    if args.json:
        print(json.dumps({
            'known_code_signals': sorted(known_code_signals),
            'known_message_signals': list(known_message_signals),
            'rows': [auth_report_row_to_json_fn(row) for row in rows],
            'suggested_patch': render_auth_signal_patch_fn(rows) if args.suggest_patch else '',
        }, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print('No auth failures observed.')
        return 0
    print('最近出现时间                  覆盖状态          命中来源        次数    账号                code                      msg')
    print('--------------------------  ----------------  --------------  ------  ------------------  ------------------------  ----------------------------------------')
    for row in rows:
        accounts = ','.join(row.accounts) or '-'
        print(f'{row.last_seen_at:<26}  {auth_report_match_label_fn(row):<16}  {row.matched_by:<14}  {row.count:<6}  {accounts:<18}  {row.code:<24}  {row.msg}')
    if args.suggest_patch:
        print('')
        print(render_auth_signal_patch_fn(rows), end='')
    return 0


def command_db_check(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
) -> int:
    settings = load_settings_fn(args.config)
    try:
        store = create_store_fn(settings)
        version = store.schema_version()
    except Exception as exc:
        print(f'DB check failed: {exc}', file=sys.stderr)
        return 1
    print(f'DB OK. path={settings.storage.database_file} schema_version={version}')
    return 0


def command_test_notify(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    build_named_notifiers_fn: Callable[[NotificationSettings], dict[str, object]] = build_named_notifiers,
) -> int:
    settings = load_settings_fn(args.config)
    errors = validate_settings_fn(settings, purpose='test-notify')
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    named_notifiers = build_named_notifiers_fn(settings.notifications)
    if not named_notifiers:
        print('No enabled notification channels are configured.')
        return 1

    selected = named_notifiers if args.channel == 'all' else {args.channel: named_notifiers.get(args.channel)}
    failures: list[str] = []
    successes: list[str] = []
    for name, notifier in selected.items():
        if notifier is None:
            failures.append(f'{name}: not configured')
            continue
        try:
            notifier.send('[autodl-helper] test notification', 'This is a test notification from autodl-helper.')
            successes.append(name)
        except Exception as exc:
            failures.append(f'{name}: {exc}')
    if successes:
        print('notification sent via: ' + ', '.join(successes))
    if failures:
        print('notification failures: ' + '; '.join(failures))
        return 1
    return 0


__all__ = [
    "command_history",
    "command_auth_report",
    "command_db_check",
    "command_test_notify",
]
