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

from autodl_helper.api import AutoDLClient
from autodl_helper.auth import AuthError, alert_auth_failure, inspect_auth_state, resolve_authorization
from autodl_helper.auth_policy import resolve_auth_runtime_policy
from autodl_helper.config import AccountSettings, LIGHTWEIGHT_MODES, NotificationSettings, Settings, load_settings, read_raw_settings, write_raw_settings
from autodl_helper.interactive_actions import (
    auth_panel_rows,
    build_dashboard_view,
    clear_runtime_controls,
    history_panel_rows,
    keeper_probe_rows,
    list_instances_panel_rows,
    request_reload,
    scheduled_candidate_panel_data,
    scheduled_job_status_rows,
    runtime_controls_snapshot,
    set_job_enabled,
    set_job_override,
    set_task_enabled,
)
from autodl_helper.interactive_app import run_interactive
from autodl_helper.interactive_views import render_candidate_explanation, render_dashboard
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
from autodl_helper.service_launchd import append_service_lifecycle_log
from autodl_helper.services.manager import (
    install_service,
    restart_service,
    service_status,
    start_service,
    stop_service,
    uninstall_service,
)
from autodl_helper.state import StateStore
from autodl_helper.storage import SQLiteStore
from autodl_helper.tasks.keeper import evaluate_keeper_instance, format_duration_seconds, run_keeper_cycle
from autodl_helper.tasks.scheduled_start import ScheduledStartJobRuntime, run_scheduled_start_job

logger = logging.getLogger(__name__)
TIME_RE = re.compile(r'^(?:[01]\d|2[0-3]):[0-5]\d$')
DAEMON_HEARTBEAT_INTERVAL_SECONDS = 30



from ..shared import *  # noqa: F401,F403

def watch_instance(
    *,
    client,
    keeper_settings=None,
    instance_id: str,
    interval_seconds: int,
    json_output: bool,
    output: TextIO,
    sleep_fn=time.sleep,
    max_iterations: int | None = None,
    account_name: str = '',
    normalize_instance_debug_fn: Callable[..., dict[str, object]],
    extract_watch_fields_fn: Callable[..., dict[str, object]],
    format_watch_change_fn: Callable[[dict[str, object]], str],
) -> int:
    previous_snapshot: dict[str, object] | None = None
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        current = None
        for item in client.list_instances():
            if item.get('uuid') == instance_id:
                current = normalize_instance_debug_fn(item, keeper_settings=keeper_settings, account_name=account_name)
                break
        if current is None:
            missing_payload = {'account': account_name, 'instance_id': instance_id, 'missing': True} if account_name else {'instance_id': instance_id, 'missing': True}
            print(json.dumps(missing_payload, ensure_ascii=False) if json_output else f'instance_id={instance_id} missing=true', file=output)
        elif json_output:
            print(json.dumps(current, ensure_ascii=False), file=output)
        else:
            watch_fields = extract_watch_fields_fn(current, keeper_settings=keeper_settings)
            if previous_snapshot != watch_fields:
                print(format_watch_change_fn(watch_fields), file=output)
                previous_snapshot = watch_fields
        output.flush()
        iteration += 1
        if max_iterations is not None and iteration >= max_iterations:
            break
        sleep_fn(interval_seconds)
    return 0


def command_list_instances(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    build_client_fn: Callable[..., object] = build_client,
    get_enabled_accounts_fn: Callable[[Settings], list[AccountSettings]] = get_enabled_accounts,
    normalize_instance_fn: Callable[..., dict[str, object]],
    format_instances_table_fn: Callable[[list[dict[str, object]]], str],
    normalize_instance_debug_fn: Callable[..., dict[str, object]] | None = None,
) -> int:
    settings = load_settings_fn(args.config)
    errors = validate_settings_fn(settings, purpose='list-instances')
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    store = create_store_fn(settings)
    rows: list[dict[str, object]] = []
    multi_account = len(get_enabled_accounts_fn(settings)) > 1 or bool(args.account)
    for account in select_accounts_fn(settings, args.account):
        client = build_client_fn(settings, args.headed, account=account, store=store)
        if args.json and normalize_instance_debug_fn is not None:
            rows.extend(
                normalize_instance_debug_fn(
                    item,
                    keeper_settings=settings.tasks.keeper,
                    account_name=account.name if multi_account else '',
                )
                for item in client.list_instances()
            )
        else:
            rows.extend(normalize_instance_fn(item, account_name=account.name if multi_account else '') for item in client.list_instances())
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(format_instances_table_fn(rows))
    return 0


def command_inspect_instance(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    build_client_fn: Callable[..., object] = build_client,
    normalize_instance_debug_fn: Callable[..., dict[str, object]],
) -> int:
    settings = load_settings_fn(args.config)
    errors = validate_settings_fn(settings, purpose='inspect-instance')
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    try:
        account = select_accounts_fn(settings, args.account, require_explicit_for_multi=True)[0]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    store = create_store_fn(settings)
    client = build_client_fn(settings, args.headed, account=account, store=store)
    for item in client.list_instances():
        if item.get('uuid') == args.instance_id:
            print(json.dumps(normalize_instance_debug_fn(item, keeper_settings=settings.tasks.keeper, account_name=account.name), ensure_ascii=False, indent=2))
            return 0
    print(f'Instance {args.instance_id} not found.', file=sys.stderr)
    return 1


def command_watch_instance(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    build_client_fn: Callable[..., object] = build_client,
    watch_instance_fn: Callable[..., int] = watch_instance,
    normalize_instance_debug_fn: Callable[..., dict[str, object]],
    extract_watch_fields_fn: Callable[..., dict[str, object]],
    format_watch_change_fn: Callable[[dict[str, object]], str],
) -> int:
    settings = load_settings_fn(args.config)
    errors = validate_settings_fn(settings, purpose='watch-instance')
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    try:
        account = select_accounts_fn(settings, args.account, require_explicit_for_multi=True)[0]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    store = create_store_fn(settings)
    client = build_client_fn(settings, args.headed, account=account, store=store)
    return watch_instance_fn(
        client=client,
        keeper_settings=settings.tasks.keeper,
        instance_id=args.instance_id,
        interval_seconds=args.interval,
        json_output=args.json,
        output=sys.stdout,
        account_name=account.name,
        normalize_instance_debug_fn=normalize_instance_debug_fn,
        extract_watch_fields_fn=extract_watch_fields_fn,
        format_watch_change_fn=format_watch_change_fn,
    )


def command_keeper_probe(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    build_client_fn: Callable[..., object] = build_client,
    evaluate_keeper_instance_fn: Callable[..., Any] = evaluate_keeper_instance,
    format_keeper_probe_line_fn: Callable[..., str],
) -> int:
    settings = load_settings_fn(args.config)
    errors = validate_settings_fn(settings, purpose='run-keeper')
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    store = create_store_fn(settings)
    lines: list[str] = []
    for account in select_accounts_fn(settings, args.account, require_explicit_for_multi=False):
        client = build_client_fn(settings, args.headed, account=account, store=store)
        for item in client.list_instances():
            result = evaluate_keeper_instance_fn(
                client=client,
                item=item,
                shutdown_release_after_hours=settings.tasks.keeper.shutdown_release_after_hours,
                keeper_trigger_before_hours=settings.tasks.keeper.keeper_trigger_before_hours,
                start_cooldown_minutes=settings.tasks.keeper.start_cooldown_minutes,
                stop_cooldown_minutes=settings.tasks.keeper.stop_cooldown_minutes,
                fallback_to_status_at=settings.tasks.keeper.fallback_to_status_at,
                now=datetime.now(),
            )
            executed = bool(result.release_deadline and store.was_keeper_executed_in_cycle(account.name, result.instance_id, result.release_deadline))
            if args.only_eligible and not result.eligible:
                continue
            lines.append(format_keeper_probe_line_fn(result, account_name=account.name, executed_in_cycle=executed))
    if lines:
        print('\n'.join(lines))
    return 0


__all__ = [
    "watch_instance",
    "command_list_instances",
    "command_inspect_instance",
    "command_watch_instance",
    "command_keeper_probe",
]
