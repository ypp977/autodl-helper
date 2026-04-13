from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Protocol

DEFAULT_SERVICE_LABEL = 'autodl-helper'
SERVICE_STDOUT_LOG_NAME = 'service.stdout.log'
SERVICE_STDERR_LOG_NAME = 'service.stderr.log'
RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class ServiceBackend(Protocol):
    backend_name: str

    def install_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, Any]: ...
    def start_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]: ...
    def stop_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]: ...
    def restart_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]: ...
    def service_status(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, Any]: ...
    def uninstall_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, Any]: ...


def resolve_config_path(config_path: str | Path) -> Path:
    return Path(config_path).expanduser().resolve()


def working_directory_for_config(config_path: str | Path) -> Path:
    return resolve_config_path(config_path).parent


def logs_dir_for_config(config_path: str | Path) -> Path:
    return working_directory_for_config(config_path) / 'logs'


def service_stdout_log_path(config_path: str | Path) -> Path:
    return logs_dir_for_config(config_path) / SERVICE_STDOUT_LOG_NAME


def service_stderr_log_path(config_path: str | Path) -> Path:
    return logs_dir_for_config(config_path) / SERVICE_STDERR_LOG_NAME


def ensure_service_logs_dir(config_path: str | Path) -> Path:
    logs_dir = logs_dir_for_config(config_path)
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def default_main_path() -> Path:
    return Path(__file__).resolve().parents[2] / 'main.py'


def default_python_path() -> str:
    return sys.executable


def normalize_status_label(*, installed: bool, running: bool, enabled: bool, detail: str = '') -> str:
    if not installed:
        return '未安装'
    if running:
        return '运行中'
    if enabled:
        return '已停止'
    if detail:
        return '状态异常'
    return '已停止'


def build_service_status(
    *,
    platform: str,
    backend: str,
    label: str,
    config_path: str | Path,
    installed: bool,
    running: bool,
    enabled: bool,
    detail: str = '',
    status_label: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    config = resolve_config_path(config_path)
    working_directory = config.parent
    stdout_path = service_stdout_log_path(config)
    stderr_path = service_stderr_log_path(config)
    payload: dict[str, Any] = {
        'platform': platform,
        'backend': backend,
        'label': label,
        'installed': installed,
        'running': running,
        'enabled': enabled,
        'loaded': running,
        'status_label': status_label or normalize_status_label(installed=installed, running=running, enabled=enabled, detail=detail),
        'detail': detail,
        'config_path': str(config),
        'working_directory': str(working_directory),
        'stdout_path': str(stdout_path),
        'stderr_path': str(stderr_path),
    }
    payload.update(extra)
    return payload


def run_command_safe(cmd: list[str], *, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
    try:
        return run_command(cmd, capture_output=True, text=True)
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, stdout='', stderr=str(exc))
