from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Callable

from autodl_helper.core.config import Settings, load_settings
from autodl_helper.core.store import SQLiteStore

from ..output import print_json, print_json_error, json_ok
from ..shared_healthcheck import collect_healthcheck_errors
from ..shared_settings import apply_cli_overrides, validate_settings
from autodl_helper.runtime_control import mark_config_reload_failure, mark_config_reload_success, read_config_reload_status


def _config_mtime_value(path: str | Path) -> str:
    try:
        return f'{os.path.getmtime(path):.6f}'
    except OSError:
        return ''


def _maybe_reload_daemon_settings(
    *,
    args: argparse.Namespace,
    store: SQLiteStore,
    state: dict[str, Any],
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    mtime_fn: Callable[[str | Path], float] = os.path.getmtime,
) -> Settings:
    active_settings = state.get('settings')
    if active_settings is None:
        active_settings = apply_cli_overrides(args, load_settings_fn(args.config))
        state['settings'] = active_settings
    reload_status = read_config_reload_status(store)
    try:
        current_mtime = f'{float(mtime_fn(args.config)):.6f}'
    except OSError:
        current_mtime = ''
    should_reload = (
        reload_status['requested_generation'] > reload_status['processed_generation']
        or (current_mtime and current_mtime != reload_status['last_processed_mtime'])
    )
    if not should_reload:
        return active_settings
    try:
        candidate = apply_cli_overrides(args, load_settings_fn(args.config))
        errors = validate_settings_fn(candidate, purpose='run_daemon')
        if errors:
            raise ValueError('\n'.join(errors))
    except Exception as exc:
        mark_config_reload_failure(
            store,
            generation=reload_status['requested_generation'],
            config_mtime=current_mtime,
            error=str(exc),
        )
        return active_settings
    state['settings'] = candidate
    mark_config_reload_success(
        store,
        generation=reload_status['requested_generation'],
        config_mtime=current_mtime,
    )
    return candidate


def command_healthcheck(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    collect_healthcheck_errors_fn: Callable[..., list[str]] = collect_healthcheck_errors,
) -> int:
    settings = load_settings_fn(args.config)
    errors = collect_healthcheck_errors_fn(
        settings=settings,
        state_file=args.state_file,
        lock_file=args.lock_file,
        smoke=args.smoke,
        headed=args.headed,
    )
    if errors:
        if getattr(args, 'json', False):
            print_json_error('healthcheck_failed', 'Healthcheck failed.', details={'errors': errors})
            return 1
        print('Healthcheck failed:', file=sys.stderr)
        for error in errors:
            print(f'- {error}', file=sys.stderr)
        return 1
    if getattr(args, 'json', False):
        print_json(json_ok({'status': 'ok'}))
        return 0
    print('Healthcheck OK.')
    return 0


__all__ = ["_config_mtime_value", "_maybe_reload_daemon_settings", "command_healthcheck"]
