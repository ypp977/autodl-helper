from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .base import RunCommand


def resolve_backend():
    if sys.platform == 'darwin':
        from .launchd import LaunchdBackend
        return LaunchdBackend()
    if sys.platform.startswith('linux'):
        from .systemd import SystemdBackend
        return SystemdBackend()
    if os.name == 'nt':
        from .windows_task import WindowsTaskBackend
        return WindowsTaskBackend()
    raise RuntimeError(f'unsupported platform: {sys.platform}')


def install_service(*, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, Any]:
    return resolve_backend().install_service(config_path=config_path, run_command=run_command)


def start_service(*, config_path: str | Path, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
    return resolve_backend().start_service(config_path=config_path, run_command=run_command)


def stop_service(*, config_path: str | Path, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
    return resolve_backend().stop_service(config_path=config_path, run_command=run_command)


def restart_service(*, config_path: str | Path, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
    return resolve_backend().restart_service(config_path=config_path, run_command=run_command)


def service_status(*, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, Any]:
    return resolve_backend().service_status(config_path=config_path, run_command=run_command)


def uninstall_service(*, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, Any]:
    return resolve_backend().uninstall_service(config_path=config_path, run_command=run_command)
