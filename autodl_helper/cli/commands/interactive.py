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

from .runtime import daemon_dispatch, run_cycle, run_keeper_only, run_scheduled_start_cycle
from .accounts import command_accounts, command_login
from .instances import command_keeper_probe, command_list_instances
from .history import command_auth_report, command_history
from .config import _config_mtime_value, command_config_edit, command_config_resolve, command_config_show, command_healthcheck

def command_run_variant(
    args: argparse.Namespace,
    mode: str,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    file_lock_cls: type[FileLock] = FileLock,
    scheduler_cls: type[BlockingScheduler] = BlockingScheduler,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    run_keeper_only_fn: Callable[..., list[Any]] = run_keeper_only,
    run_scheduled_start_cycle_fn: Callable[..., list[Any]] = run_scheduled_start_cycle,
    run_cycle_fn: Callable[..., list[Any]] = run_cycle,
    compute_interval_for_mode_fn: Callable[[Settings, str], int] = compute_interval_for_mode,
    compute_dispatch_interval_seconds_fn: Callable[[Settings], int] = compute_dispatch_interval_seconds,
    alert_auth_failure_fn: Callable[[str], None] = alert_auth_failure,
    daemon_dispatch_fn: Callable[..., list[Any]] = daemon_dispatch,
) -> int:
    try:
        settings = apply_cli_overrides(args, load_settings_fn(args.config))
        validation_purpose = 'run-daemon' if mode == 'all' else 'run-keeper' if mode == 'keeper' else 'run-scheduled-start'
        errors = validate_settings_fn(settings, purpose=validation_purpose)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        with file_lock_cls(args.lock_file):
            store = create_store_fn(settings)
            daemon_state: dict[str, Any] = {'settings': settings}
            heartbeat_mode = 'all' if mode == 'all' else mode
            heartbeat_origin = os.environ.get('AUTODL_HELPER_DAEMON_ORIGIN', 'cli')
            heartbeat_account = args.account or ''
            mark_config_reload_success(
                store,
                generation=read_config_reload_status(store)['requested_generation'],
                config_mtime=_config_mtime_value(args.config),
            )
            if mode == 'all':
                daemon_dispatch_fn(
                    args=args,
                    load_settings_fn=load_settings_fn,
                    create_store_fn=create_store_fn,
                    run_keeper_only_fn=run_keeper_only_fn,
                    run_scheduled_start_cycle_fn=run_scheduled_start_cycle_fn,
                    validate_settings_fn=validate_settings_fn,
                    state=daemon_state,
                )
            elif mode == 'keeper':
                run_keeper_only_fn(settings=settings, headed=args.headed, account_name=args.account, store=store)
            else:
                run_scheduled_start_cycle_fn(settings=settings, headed=args.headed, state_file=args.state_file, account_name=args.account, store=store)
            if args.run_once:
                return 0
            if mode == 'scheduled_start':
                current_settings = apply_cli_overrides(args, load_settings_fn(args.config))
                if scheduled_daemon_should_exit(settings=current_settings, store=store, account_name=args.account):
                    clear_daemon_heartbeat(store)
                    return 0
            scheduler = scheduler_cls()
            mark_daemon_heartbeat(store, mode=heartbeat_mode, account=heartbeat_account, origin=heartbeat_origin)
            scheduler.add_job(
                mark_daemon_heartbeat,
                'interval',
                seconds=DAEMON_HEARTBEAT_INTERVAL_SECONDS,
                kwargs={
                    'store': store,
                    'mode': heartbeat_mode,
                    'account': heartbeat_account,
                    'origin': heartbeat_origin,
                },
                coalesce=True,
                max_instances=1,
                misfire_grace_time=5,
            )
            if mode == 'all':
                scheduler.add_job(
                    daemon_dispatch_fn,
                    'interval',
                    seconds=compute_dispatch_interval_seconds_fn(settings),
                    kwargs={
                        'args': args,
                        'load_settings_fn': load_settings_fn,
                        'create_store_fn': create_store_fn,
                        'run_keeper_only_fn': run_keeper_only_fn,
                        'run_scheduled_start_cycle_fn': run_scheduled_start_cycle_fn,
                        'validate_settings_fn': validate_settings_fn,
                        'state': daemon_state,
                    },
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=30,
                )
            else:
                if mode == 'keeper':
                    job_func = run_keeper_only_fn
                    kwargs: dict[str, Any] = {'settings': settings, 'headed': args.headed, 'account_name': args.account}
                else:
                    def scheduled_start_daemon_tick() -> list[Any]:
                        current_settings = apply_cli_overrides(args, load_settings_fn(args.config))
                        results = run_scheduled_start_cycle_fn(
                            settings=current_settings,
                            headed=args.headed,
                            state_file=args.state_file,
                            account_name=args.account,
                            store=store,
                        )
                        if scheduled_daemon_should_exit(settings=current_settings, store=store, account_name=args.account):
                            clear_daemon_heartbeat(store)
                            scheduler.shutdown(wait=False)
                        return results

                    job_func = scheduled_start_daemon_tick
                    kwargs = {}
                scheduler.add_job(
                    job_func,
                    'interval',
                    seconds=compute_interval_for_mode_fn(settings, mode),
                    kwargs=kwargs,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=30,
                )
            try:
                scheduler.start()
                return 0
            finally:
                clear_daemon_heartbeat(store)
                clear_daemon_launch_state(store)
    except LockAcquisitionError:
        logger.warning('Another autodl-helper process is already running.')
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except AuthError as exc:
        alert_auth_failure_fn(str(exc))
        return 1
    except (KeyboardInterrupt, SystemExit):
        logger.info('Exiting autodl-helper.')
        return 0


def command_interactive(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    run_variant_fn: Callable[[argparse.Namespace, str], int] = command_run_variant,
    start_background_scheduled_fn: Callable[[argparse.Namespace], tuple[int, str]] | None = None,
    stop_background_polling_fn: Callable[[Settings, SQLiteStore], tuple[int, str]] | None = None,
    command_config_show_fn: Callable[..., int] = command_config_show,
    command_config_resolve_fn: Callable[..., int] = command_config_resolve,
    command_config_edit_fn: Callable[..., int] = command_config_edit,
    command_history_fn: Callable[..., int] = command_history,
    command_keeper_probe_fn: Callable[..., int] = command_keeper_probe,
    command_auth_report_fn: Callable[..., int] = command_auth_report,
    command_list_instances_fn: Callable[..., int] = command_list_instances,
    command_accounts_fn: Callable[..., int] = command_accounts,
    command_login_fn: Callable[..., int] = command_login,
    command_healthcheck_fn: Callable[..., int] = command_healthcheck,
    list_instances_panel_rows_fn: Callable[..., list[dict[str, Any]]] | None = None,
    history_panel_rows_fn: Callable[..., list[Any]] = history_panel_rows,
    auth_panel_rows_fn: Callable[..., list[Any]] = auth_panel_rows,
    build_dashboard_view_fn: Callable[..., dict[str, Any]] = build_dashboard_view,
    render_dashboard_fn: Callable[[dict[str, Any]], str] = render_dashboard,
    set_task_enabled_fn: Callable[..., None] = set_task_enabled,
    set_job_enabled_fn: Callable[..., None] = set_job_enabled,
    set_job_override_fn: Callable[..., None] = set_job_override,
    clear_runtime_controls_fn: Callable[..., None] = clear_runtime_controls,
    runtime_controls_snapshot_fn: Callable[..., dict[str, Any]] = runtime_controls_snapshot,
    request_reload_fn: Callable[..., None] = request_reload,
    run_keeper_only_fn: Callable[..., list[Any]] = run_keeper_only,
    run_scheduled_start_cycle_fn: Callable[..., list[Any]] = run_scheduled_start_cycle,
    scheduled_candidate_panel_data_fn: Callable[..., dict[str, Any] | None] = scheduled_candidate_panel_data,
    keeper_probe_rows_fn: Callable[..., list[dict[str, Any]]] = keeper_probe_rows,
    scheduled_job_status_rows_fn: Callable[..., list[dict[str, Any]]] = scheduled_job_status_rows,
    render_candidate_explanation_fn: Callable[[dict[str, Any] | None], str] = render_candidate_explanation,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
    build_client_fn: Callable[..., object] = build_client,
    evaluate_keeper_instance_fn: Callable[..., Any] = evaluate_keeper_instance,
) -> int:
    try:
        if list_instances_panel_rows_fn is None:
            def list_instances_panel_rows_fn(settings: Settings, store: SQLiteStore, *, account_name: str | None = None):
                return list_instances_panel_rows(
                    settings,
                    store,
                    account_name=account_name,
                    select_accounts_fn=select_accounts,
                    build_client_fn=build_client,
                )
        return run_interactive(
            args,
            load_settings_fn=load_settings_fn,
            validate_settings_fn=validate_settings_fn,
            create_store_fn=create_store_fn,
            render_dashboard_fn=render_dashboard_fn,
            build_dashboard_view_fn=build_dashboard_view_fn,
            set_task_enabled_fn=set_task_enabled_fn,
            set_job_enabled_fn=set_job_enabled_fn,
            set_job_override_fn=set_job_override_fn,
            clear_runtime_controls_fn=clear_runtime_controls_fn,
            runtime_controls_snapshot_fn=runtime_controls_snapshot_fn,
            request_reload_fn=request_reload_fn,
            run_variant_fn=run_variant_fn,
            start_background_scheduled_fn=start_background_scheduled_fn,
            stop_background_polling_fn=stop_background_polling_fn,
            run_keeper_only_fn=run_keeper_only_fn,
            run_scheduled_start_cycle_fn=run_scheduled_start_cycle_fn,
            command_config_show_fn=command_config_show_fn,
            command_config_resolve_fn=command_config_resolve_fn,
            command_config_edit_fn=command_config_edit_fn,
            command_history_fn=command_history_fn,
            command_keeper_probe_fn=command_keeper_probe_fn,
            command_auth_report_fn=command_auth_report_fn,
            command_list_instances_fn=command_list_instances_fn,
            command_accounts_fn=command_accounts_fn,
            command_login_fn=command_login_fn,
            command_healthcheck_fn=command_healthcheck_fn,
            list_instances_panel_rows_fn=list_instances_panel_rows_fn,
            history_panel_rows_fn=history_panel_rows_fn,
            auth_panel_rows_fn=auth_panel_rows_fn,
            keeper_probe_rows_fn=lambda settings, store, *, account_name=None: keeper_probe_rows_fn(
                settings,
                store,
                account_name=account_name,
                select_accounts_fn=select_accounts_fn,
                build_client_fn=build_client_fn,
                evaluate_keeper_instance_fn=evaluate_keeper_instance_fn,
            ),
            scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
            scheduled_candidate_panel_data_fn=scheduled_candidate_panel_data_fn,
            render_candidate_explanation_fn=render_candidate_explanation_fn,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1


__all__ = [
    "command_run_variant",
    "command_interactive",
]
