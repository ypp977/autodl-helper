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

def _delegate(name: str, fallback):
    class _Proxy:
        def _target(self):
            import sys
            module = sys.modules.get("autodl_helper.cli.handlers")
            if module is not None:
                target = getattr(module, name, None)
                if target is not None and target is not self:
                    return target
            return fallback

        def __call__(self, *args, **kwargs):
            return self._target()(*args, **kwargs)

        def __getattr__(self, attr):
            return getattr(self._target(), attr)

    return _Proxy()

service_status = _delegate('service_status', service_status)
start_service = _delegate('start_service', start_service)


def _log_service_action(config_path: str, message: str) -> None:
    try:
        append_service_lifecycle_log(config_path, message)
    except Exception:
        logger.exception('写入服务管理日志失败')


def _record_service_event(
    config_path: str,
    *,
    action: str,
    message: str,
    level: str = 'info',
    detail: str = '',
    plist_path: str = '',
) -> None:
    try:
        settings = load_settings(config_path)
        store = create_store(settings)
        store.add_event(
            '',
            'service',
            level,
            message,
            payload={
                'label': '后台服务',
                'action': action,
                'detail': detail,
                'plist_path': plist_path,
            },
        )
    except Exception:
        logger.exception('写入服务事件历史失败')


def _service_event_label(payload: dict[str, Any] | None) -> str:
    data = payload or {}
    return str(data.get('label') or data.get('backend') or '后台服务')


def command_service_install(args: argparse.Namespace) -> int:
    status = install_service(config_path=args.config)
    label = _service_event_label(status)
    artifact_path = status.get('artifact_path') or ''
    _log_service_action(args.config, f'已安装后台服务 label={label} backend={status.get("backend") or "-"} artifact={artifact_path}')
    _record_service_event(args.config, action='install', message='已安装后台服务', detail=str(status.get('detail') or ''), plist_path=str(artifact_path))
    print(f'Installed background service ({status.get("backend")}): {artifact_path}')
    return 0


def command_service_start(args: argparse.Namespace) -> int:
    status = service_status(config_path=args.config)
    label = _service_event_label(status)
    if not status.get('installed'):
        print('后台服务未安装，请先执行 service-install。', file=sys.stderr)
        return 1
    if status.get('running'):
        _log_service_action(args.config, f'后台服务已在运行 label={label}')
        _record_service_event(args.config, action='start', message='后台服务已在运行')
        print(f'Background service already running: {label}')
        return 0
    result = start_service(config_path=args.config)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or 'service start failed').strip()
        _record_service_event(args.config, action='start', message='启动后台服务失败', level='error', detail=detail)
        print(detail, file=sys.stderr)
        return int(result.returncode or 1)
    _log_service_action(args.config, f'已启动后台服务 label={label}')
    _record_service_event(args.config, action='start', message='已启动后台服务')
    print(f'Started background service: {label}')
    return 0


def command_service_stop(args: argparse.Namespace) -> int:
    status = service_status(config_path=args.config)
    label = _service_event_label(status)
    if not status.get('installed'):
        _log_service_action(args.config, f'后台服务未安装 label={label}')
        _record_service_event(args.config, action='stop', message='后台服务未安装')
        print(f'Background service already absent: {label}')
        return 0
    if not status.get('running'):
        _log_service_action(args.config, f'后台服务已停止 label={label}')
        _record_service_event(args.config, action='stop', message='后台服务已停止')
        print(f'Background service already stopped: {label}')
        return 0
    result = stop_service(config_path=args.config)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or 'service stop failed').strip()
        _record_service_event(args.config, action='stop', message='停止后台服务失败', level='error', detail=detail)
        print(detail, file=sys.stderr)
        return int(result.returncode or 1)
    _log_service_action(args.config, f'已停止后台服务 label={label}')
    _record_service_event(args.config, action='stop', message='已停止后台服务')
    print(f'Stopped background service: {label}')
    return 0


def command_service_restart(args: argparse.Namespace) -> int:
    status = service_status(config_path=args.config)
    label = _service_event_label(status)
    if not status.get('installed'):
        print('后台服务未安装，请先执行 service-install。', file=sys.stderr)
        return 1
    result = restart_service(config_path=args.config)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or 'service restart failed').strip()
        _record_service_event(args.config, action='restart', message='重启后台服务失败', level='error', detail=detail)
        print(detail, file=sys.stderr)
        return int(result.returncode or 1)
    _log_service_action(args.config, f'已重启后台服务 label={label}')
    _record_service_event(args.config, action='restart', message='已重启后台服务')
    print(f'Restarted background service: {label}')
    return 0


def command_service_status(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
) -> int:
    settings = load_settings_fn(args.config)
    store = create_store_fn(settings)
    service = service_status(config_path=args.config)
    daemon_status = read_daemon_status(store)
    reload_status = read_config_reload_status(store)
    print(json.dumps({'service': service, 'daemon': daemon_status, 'reload': reload_status}, ensure_ascii=False, indent=2))
    return 0


def command_service_uninstall(args: argparse.Namespace) -> int:
    status = service_status(config_path=args.config)
    label = _service_event_label(status)
    if not status.get('installed'):
        _log_service_action(args.config, f'后台服务已不存在 label={label}')
        _record_service_event(args.config, action='uninstall', message='后台服务已不存在')
        print(f'Background service already absent: {label}')
        return 0
    removed = uninstall_service(config_path=args.config)
    artifact_path = removed.get('artifact_path') or ''
    _log_service_action(args.config, f'已卸载后台服务 label={label} artifact={artifact_path}')
    _record_service_event(args.config, action='uninstall', message='已卸载后台服务', plist_path=str(artifact_path))
    print(f'Uninstalled background service: {label}')
    return 0


__all__ = [
    "service_status",
    "start_service",
    "_log_service_action",
    "_record_service_event",
    "_service_event_label",
    "command_service_install",
    "command_service_start",
    "command_service_stop",
    "command_service_restart",
    "command_service_status",
    "command_service_uninstall",
]
