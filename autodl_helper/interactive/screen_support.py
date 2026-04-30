
from __future__ import annotations

import re
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable

from autodl_helper.interactive.account_common import _run_captured_action
from autodl_helper.interactive.status_task import _bump_subprocess_task_stat, _subprocess_task_stats_snapshot
from autodl_helper.services.manager import service_status as _service_status
from autodl_helper.services.manager import start_service as _start_service
from autodl_helper.services.manager import stop_service as _stop_service

DEFAULT_SERVICE_LABEL = 'autodl-helper'
_SERVICE_CONFIG_PATH = 'config.yaml'
GPU_SPEC_RE = re.compile(r'(?P<model>.+?)\s*[*×x]\s*(?P<count>\d+)\s*(?:卡)?\s*$')
SNAPSHOT_TEXT_LIMIT = 512
SNAPSHOT_BODY_LIMIT = 2048
SERVICE_HEARTBEAT_OK_SECONDS = 75
LOGIN_VERIFY_TIMEOUT_SECONDS = 12.0
HEALTHCHECK_TIMEOUT_SECONDS = 8.0
KEEPER_EXECUTE_LONG_RUNNING_SECONDS = 12.0
_SUBPROCESS_TASK_STATS_LOCK = threading.Lock()

def _resolve_app_target(name: str, fallback):
    for module_name in ("autodl_helper.interactive_app", "autodl_helper.interactive.app"):
        app_module = sys.modules.get(module_name)
        if app_module is None:
            continue
        target = getattr(app_module, name, None)
        if target is None or target is fallback:
            continue
        if type(target).__name__ == '_Proxy':
            continue
        return target
    return fallback


def _delegate(name: str, fallback):
    class _Proxy:
        def _target(self):
            return _resolve_app_target(name, fallback)

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

def _interactive_max_workers(settings) -> int:
    try:
        return max(1, int(getattr(getattr(settings, 'interactive', None), 'max_workers', 6) or 6))
    except Exception:
        return 6

def _run_command_with_timeout(*, command_fn, args: SimpleNamespace, timeout_seconds: float, title: str, timeout_summary: str) -> dict[str, Any]:
    started_at = time.time()
    try:
        _bump_subprocess_task_stat('started')
        code, output = _run_captured_action(title, lambda: command_fn(args))
        elapsed_seconds = round(max(0.0, time.time() - started_at), 3)
        long_running = elapsed_seconds > max(0.0, timeout_seconds)
        if long_running:
            _bump_subprocess_task_stat('long_running')
        _bump_subprocess_task_stat('completed')
        return {
            'ok': True,
            'code': code,
            'output': output,
            'summary': timeout_summary if long_running else '',
            'timed_out': False,
            'long_running': long_running,
            'elapsed_seconds': elapsed_seconds,
        }
    except Exception:
        _bump_subprocess_task_stat('failed')
        raise


read_launch_agent_status = _delegate('read_launch_agent_status', read_launch_agent_status)
start_launch_agent = _delegate('start_launch_agent', start_launch_agent)
stop_launch_agent = _delegate('stop_launch_agent', stop_launch_agent)
_interactive_max_workers = _delegate('_interactive_max_workers', _interactive_max_workers)
_run_command_with_timeout = _delegate('_run_command_with_timeout', _run_command_with_timeout)

__all__ = [
    'DEFAULT_SERVICE_LABEL',
    '_SERVICE_CONFIG_PATH',
    'GPU_SPEC_RE',
    'SNAPSHOT_TEXT_LIMIT',
    'SNAPSHOT_BODY_LIMIT',
    'SERVICE_HEARTBEAT_OK_SECONDS',
    'LOGIN_VERIFY_TIMEOUT_SECONDS',
    'HEALTHCHECK_TIMEOUT_SECONDS',
    'KEEPER_EXECUTE_LONG_RUNNING_SECONDS',
    '_SUBPROCESS_TASK_STATS_LOCK',
    '_resolve_app_target',
    'read_launch_agent_status',
    'start_launch_agent',
    'stop_launch_agent',
    '_interactive_max_workers',
]
