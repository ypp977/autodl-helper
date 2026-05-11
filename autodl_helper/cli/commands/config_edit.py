from __future__ import annotations

import argparse
import sys
from typing import Any, Callable

from autodl_helper.core.config import Settings, load_settings, read_raw_settings, write_raw_settings
from autodl_helper.core.store import SQLiteStore

from ..shared_accounts import create_store
from ..shared_edit import collect_config_edit_args, _ensure_task_payload, _select_account_payloads, _select_job_payloads
from ..shared_settings import apply_cli_overrides, validate_settings
from autodl_helper.runtime_control import request_config_reload


def command_config_edit(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    read_raw_settings_fn: Callable[[str], dict[str, Any]] = read_raw_settings,
    write_raw_settings_fn: Callable[[str, dict[str, Any]], None] = write_raw_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    request_reload_fn: Callable[[SQLiteStore], Any] = request_config_reload,
) -> int:
    try:
        args = collect_config_edit_args(args)
        raw_payload = read_raw_settings_fn(args.config)
        settings = load_settings_fn(args.config)

        if any(getattr(args, field, None) is not None for field in (
            'lightweight_mode',
            'runtime_auth_revalidate_seconds',
            'force_refresh_min_interval_seconds',
            'auth_failure_backoff_seconds',
        )):
            for account_payload in _select_account_payloads(raw_payload, settings, getattr(args, 'account', None)):
                if getattr(args, 'lightweight_mode', None) is not None:
                    account_payload['lightweight_mode'] = args.lightweight_mode
                if getattr(args, 'runtime_auth_revalidate_seconds', None) is not None:
                    account_payload['runtime_auth_revalidate_seconds'] = args.runtime_auth_revalidate_seconds
                if getattr(args, 'force_refresh_min_interval_seconds', None) is not None:
                    account_payload['force_refresh_min_interval_seconds'] = args.force_refresh_min_interval_seconds
                if getattr(args, 'auth_failure_backoff_seconds', None) is not None:
                    account_payload['auth_failure_backoff_seconds'] = args.auth_failure_backoff_seconds

        keeper_payload = _ensure_task_payload(raw_payload, 'keeper')
        if getattr(args, 'shutdown_release_after_hours', None) is not None:
            keeper_payload['shutdown_release_after_hours'] = args.shutdown_release_after_hours
        if getattr(args, 'keeper_trigger_before_hours', None) is not None:
            keeper_payload['keeper_trigger_before_hours'] = args.keeper_trigger_before_hours
        if getattr(args, 'start_cooldown_minutes', None) is not None:
            keeper_payload['start_cooldown_minutes'] = args.start_cooldown_minutes
        if getattr(args, 'stop_cooldown_minutes', None) is not None:
            keeper_payload['stop_cooldown_minutes'] = args.stop_cooldown_minutes
        if getattr(args, 'fallback_to_status_at', None) is not None:
            keeper_payload['fallback_to_status_at'] = bool(args.fallback_to_status_at)

        scheduled_payload = _ensure_task_payload(raw_payload, 'scheduled_start')
        if getattr(args, 'scheduled_poll_interval', None) is not None:
            scheduled_payload['poll_interval_seconds'] = args.scheduled_poll_interval
        if getattr(args, 'target_time', None) is not None or getattr(args, 'advance_hours', None) is not None:
            for job_payload in _select_job_payloads(raw_payload, settings, getattr(args, 'scheduled_job', None), require_single=True):
                if getattr(args, 'target_time', None) is not None:
                    job_payload['target_time'] = args.target_time
                if getattr(args, 'advance_hours', None) is not None:
                    job_payload['advance_hours'] = args.advance_hours
        elif getattr(args, 'scheduled_job', None) is not None:
            _select_job_payloads(raw_payload, settings, getattr(args, 'scheduled_job', None), require_single=False)

        write_raw_settings_fn(args.config, raw_payload)
        effective = apply_cli_overrides(args, load_settings_fn(args.config))
        errors = validate_settings_fn(effective, purpose='validate')
        if errors:
            print('Configuration invalid after edit:', file=sys.stderr)
            for error in errors:
                print(f'- {error}', file=sys.stderr)
            return 1
        store = create_store_fn(effective)
        request_reload_fn(store)
        print(f'Updated config: {args.config}')
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1


__all__ = ["command_config_edit"]
