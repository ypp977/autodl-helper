from __future__ import annotations

import subprocess
from pathlib import Path

from .base import (
    DEFAULT_SERVICE_LABEL,
    RunCommand,
    build_service_status,
    default_main_path,
    default_python_path,
    ensure_service_logs_dir,
    resolve_config_path,
    run_command_safe,
)

backend_name = 'windows_task'
TASK_NAME = DEFAULT_SERVICE_LABEL


def build_task_command(*, config_path: str | Path) -> str:
    config = resolve_config_path(config_path)
    ensure_service_logs_dir(config)
    return f'\"{default_python_path()}\" \"{default_main_path()}\" run-daemon --config \"{config}\"'


def _query_task(*, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
    return run_command_safe(['schtasks', '/Query', '/TN', TASK_NAME, '/V', '/FO', 'LIST'], run_command=run_command)


def _parse_task_query_output(text: str) -> dict[str, str]:
    payload: dict[str, str] = {}
    for line in text.splitlines():
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        payload[key.strip().lower()] = value.strip()
    return payload


def read_windows_task_status(*, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, object]:
    result = _query_task(run_command=run_command)
    installed = result.returncode == 0
    parsed = _parse_task_query_output(result.stdout)
    status_value = (parsed.get('status') or parsed.get('scheduled task state') or '').lower()
    enabled_value = (parsed.get('scheduled task state') or parsed.get('status') or '').lower()
    running = 'running' in status_value
    enabled = 'disabled' not in enabled_value if installed else False
    detail_parts = []
    if installed:
        for key in ('status', 'scheduled task state', 'last run time', 'next run time'):
            if parsed.get(key):
                detail_parts.append(f'{key}={parsed[key]}')
    elif result.stderr.strip() or result.stdout.strip():
        detail_parts.append(result.stderr.strip() or result.stdout.strip())
    status_label = '未安装' if not installed else ('运行中' if running else ('已停止' if enabled else '状态异常'))
    return build_service_status(
        platform='Windows',
        backend=backend_name,
        label=TASK_NAME,
        config_path=config_path,
        installed=installed,
        running=running,
        enabled=enabled,
        detail=' | '.join(part for part in detail_parts if part),
        status_label=status_label,
        task_name=TASK_NAME,
        raw_stdout=result.stdout,
        raw_stderr=result.stderr,
    )


class WindowsTaskBackend:
    backend_name = backend_name

    def install_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, object]:
        command = build_task_command(config_path=config_path)
        run_command_safe(
            ['schtasks', '/Create', '/TN', TASK_NAME, '/SC', 'ONLOGON', '/TR', command, '/RL', 'LIMITED', '/F'],
            run_command=run_command,
        )
        status = self.service_status(config_path=config_path, run_command=run_command)
        status['artifact_path'] = TASK_NAME
        return status

    def start_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
        return run_command_safe(['schtasks', '/Run', '/TN', TASK_NAME], run_command=run_command)

    def stop_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
        return run_command_safe(['schtasks', '/End', '/TN', TASK_NAME], run_command=run_command)

    def restart_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
        self.stop_service(config_path=config_path, run_command=run_command)
        return self.start_service(config_path=config_path, run_command=run_command)

    def service_status(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, object]:
        return read_windows_task_status(config_path=config_path, run_command=run_command)

    def uninstall_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, object]:
        run_command_safe(['schtasks', '/Delete', '/TN', TASK_NAME, '/F'], run_command=run_command)
        status = self.service_status(config_path=config_path, run_command=run_command)
        status['artifact_path'] = TASK_NAME
        return status
