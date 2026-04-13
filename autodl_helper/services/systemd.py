from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

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

backend_name = 'systemd'
UNIT_NAME = f'{DEFAULT_SERVICE_LABEL}.service'


def systemd_unit_path() -> Path:
    return Path.home() / '.config' / 'systemd' / 'user' / UNIT_NAME


def build_systemd_unit(*, config_path: str | Path, label: str = DEFAULT_SERVICE_LABEL) -> str:
    config = resolve_config_path(config_path)
    logs_dir = ensure_service_logs_dir(config)
    stdout_path = logs_dir / 'service.stdout.log'
    stderr_path = logs_dir / 'service.stderr.log'
    return '\n'.join([
        '[Unit]',
        f'Description={label} daemon',
        'After=network-online.target',
        '',
        '[Service]',
        'Type=simple',
        f'WorkingDirectory={config.parent}',
        f'ExecStart={default_python_path()} {default_main_path()} run-daemon --config {config}',
        'Restart=always',
        'RestartSec=10',
        f'StandardOutput=append:{stdout_path}',
        f'StandardError=append:{stderr_path}',
        '',
        '[Install]',
        'WantedBy=default.target',
        '',
    ])


def _is_active(*, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
    return run_command_safe(['systemctl', '--user', 'is-active', DEFAULT_SERVICE_LABEL], run_command=run_command)


def _is_enabled(*, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
    return run_command_safe(['systemctl', '--user', 'is-enabled', DEFAULT_SERVICE_LABEL], run_command=run_command)


def read_systemd_status(*, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, Any]:
    unit_path = systemd_unit_path()
    active = _is_active(run_command=run_command)
    enabled = _is_enabled(run_command=run_command)
    installed = unit_path.exists()
    running = active.returncode == 0 and active.stdout.strip() == 'active'
    enabled_flag = enabled.returncode == 0 and enabled.stdout.strip() == 'enabled'
    detail_parts = []
    if active.stdout.strip() or active.stderr.strip():
        detail_parts.append(f"is-active={active.stdout.strip() or active.stderr.strip()}")
    if enabled.stdout.strip() or enabled.stderr.strip():
        detail_parts.append(f"is-enabled={enabled.stdout.strip() or enabled.stderr.strip()}")
    if active.returncode == 127 or enabled.returncode == 127:
        detail_parts.append('systemctl --user 不可用')
    status_label = '未安装' if not installed else ('运行中' if running else ('已停止' if enabled_flag else '状态异常'))
    return build_service_status(
        platform='Linux',
        backend=backend_name,
        label=DEFAULT_SERVICE_LABEL,
        config_path=config_path,
        installed=installed,
        running=running,
        enabled=enabled_flag,
        detail=' | '.join(part for part in detail_parts if part),
        status_label=status_label,
        unit_path=str(unit_path),
        raw_stdout=active.stdout or enabled.stdout,
        raw_stderr='\n'.join(filter(None, [active.stderr.strip(), enabled.stderr.strip()])),
    )


class SystemdBackend:
    backend_name = backend_name

    def install_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, Any]:
        unit_path = systemd_unit_path()
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(build_systemd_unit(config_path=config_path), encoding='utf-8')
        run_command_safe(['systemctl', '--user', 'daemon-reload'], run_command=run_command)
        run_command_safe(['systemctl', '--user', 'enable', DEFAULT_SERVICE_LABEL], run_command=run_command)
        status = self.service_status(config_path=config_path, run_command=run_command)
        status['artifact_path'] = str(unit_path)
        return status

    def start_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
        return run_command_safe(['systemctl', '--user', 'start', DEFAULT_SERVICE_LABEL], run_command=run_command)

    def stop_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
        return run_command_safe(['systemctl', '--user', 'stop', DEFAULT_SERVICE_LABEL], run_command=run_command)

    def restart_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
        return run_command_safe(['systemctl', '--user', 'restart', DEFAULT_SERVICE_LABEL], run_command=run_command)

    def service_status(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, Any]:
        return read_systemd_status(config_path=config_path, run_command=run_command)

    def uninstall_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, Any]:
        unit_path = systemd_unit_path()
        run_command_safe(['systemctl', '--user', 'disable', DEFAULT_SERVICE_LABEL], run_command=run_command)
        if unit_path.exists():
            unit_path.unlink()
        run_command_safe(['systemctl', '--user', 'daemon-reload'], run_command=run_command)
        status = self.service_status(config_path=config_path, run_command=run_command)
        status['artifact_path'] = str(unit_path)
        return status
