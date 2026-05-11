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

from .runtime import daemon_dispatch, run_cycle, run_keeper_only, run_scheduled_start_cycle, scheduled_daemon_should_exit
from .accounts import command_accounts, command_login
from .instances import command_keeper_probe, command_list_instances
from .history import command_auth_report, command_history
from .config_basic import command_config_resolve, command_config_show
from .config_edit import command_config_edit
from .config_runtime import _config_mtime_value, command_healthcheck

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
        validation_purpose = 'run_daemon' if mode == 'all' else 'run_keeper' if mode == 'keeper' else 'run_scheduled'
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
    **_kwargs,
) -> int:
    from autodl_helper.ui import run_ui

    return run_ui(args)


__all__ = [
    "command_run_variant",
    "command_interactive",
]
