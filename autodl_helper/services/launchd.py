from __future__ import annotations

import os
import subprocess
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .base import (
    RunCommand,
    build_service_status,
    build_daemon_command_args,
    ensure_service_logs_dir,
    logs_dir_for_config,
    resolve_config_path,
    run_command_safe,
)

DEFAULT_SERVICE_LABEL = 'com.autodl.helper'
DEFAULT_UID = getattr(os, 'getuid', lambda: 0)()
backend_name = 'launchd'
_STATE_RE = re.compile(r'^\s*state\s*=\s*(.+?)\s*$', re.MULTILINE)
_LAST_EXIT_RE = re.compile(r'^\s*last exit code\s*=\s*(.+?)\s*$', re.MULTILINE)


def launch_agent_domain(*, uid: int = DEFAULT_UID) -> str:
    return f'gui/{int(uid)}'


def launch_agent_plist_path(*, home_dir: str | Path | None = None, label: str = DEFAULT_SERVICE_LABEL) -> Path:
    root = Path(home_dir).expanduser() if home_dir is not None else Path.home()
    return root / 'Library' / 'LaunchAgents' / f'{label}.plist'


def service_stdout_log_path(config_path: str | Path) -> Path:
    return logs_dir_for_config(config_path) / 'service.stdout.log'


def append_service_lifecycle_log(config_path: str | Path, message: str) -> Path:
    log_path = service_stdout_log_path(config_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
    with log_path.open('a', encoding='utf-8') as handle:
        handle.write(f'{timestamp} - INFO - [服务管理] {message}\n')
    return log_path


def build_launch_agent_plist(
    *,
    label: str,
    command_path: str | None = None,
    config_path: str,
    cwd: str,
    stdout_path: str,
    stderr_path: str,
) -> str:
    args = ''.join(
        f'\n        <string>{escape(item)}</string>'
        for item in build_daemon_command_args(config_path, command=command_path)
    )
    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
    <key>Label</key>
    <string>{escape(label)}</string>
    <key>ProgramArguments</key>
    <array>{args}
    </array>
    <key>WorkingDirectory</key>
    <string>{escape(cwd)}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{escape(stdout_path)}</string>
    <key>StandardErrorPath</key>
    <string>{escape(stderr_path)}</string>
</dict>
</plist>
"""


def install_launch_agent(
    *,
    config_path: str,
    command_path: str | None = None,
    home_dir: str | Path | None = None,
    label: str = DEFAULT_SERVICE_LABEL,
) -> Path:
    config = resolve_config_path(config_path)
    plist_path = launch_agent_plist_path(home_dir=home_dir, label=label)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    logs_dir = ensure_service_logs_dir(config)
    rendered = build_launch_agent_plist(
        label=label,
        command_path=command_path,
        config_path=str(config),
        cwd=str(config.parent),
        stdout_path=str(logs_dir / 'service.stdout.log'),
        stderr_path=str(logs_dir / 'service.stderr.log'),
    )
    plist_path.write_text(rendered, encoding='utf-8')
    return plist_path


def start_launch_agent(
    *,
    home_dir: str | Path | None = None,
    label: str = DEFAULT_SERVICE_LABEL,
    uid: int = DEFAULT_UID,
    run_command: RunCommand = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    plist_path = launch_agent_plist_path(home_dir=home_dir, label=label)
    return run_command_safe(
        ['launchctl', 'bootstrap', launch_agent_domain(uid=uid), str(plist_path)],
        run_command=run_command,
    )


def stop_launch_agent(
    *,
    home_dir: str | Path | None = None,
    label: str = DEFAULT_SERVICE_LABEL,
    uid: int = DEFAULT_UID,
    run_command: RunCommand = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    plist_path = launch_agent_plist_path(home_dir=home_dir, label=label)
    return run_command_safe(
        ['launchctl', 'bootout', launch_agent_domain(uid=uid), str(plist_path)],
        run_command=run_command,
    )


def read_launch_agent_status(
    *,
    config_path: str | Path,
    home_dir: str | Path | None = None,
    label: str = DEFAULT_SERVICE_LABEL,
    uid: int = DEFAULT_UID,
    run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    config = resolve_config_path(config_path)
    plist_path = launch_agent_plist_path(home_dir=home_dir, label=label)
    result = run_command_safe(
        ['launchctl', 'print', f'{launch_agent_domain(uid=uid)}/{label}'],
        run_command=run_command,
    )
    state_match = _STATE_RE.search(result.stdout or '')
    launch_state = state_match.group(1).strip() if state_match else ''
    last_exit_match = _LAST_EXIT_RE.search(result.stdout or '')
    last_exit = last_exit_match.group(1).strip() if last_exit_match else ''
    detail_parts = []
    if result.stderr.strip():
        detail_parts.append(result.stderr.strip())
    if launch_state:
        detail_parts.append(f'state={launch_state}')
    if last_exit:
        detail_parts.append(f'last_exit={last_exit}')
    elif result.stdout.strip() and result.returncode != 0:
        detail_parts.append(result.stdout.strip())
    running = result.returncode == 0 and launch_state == 'running'
    return build_service_status(
        platform='macOS',
        backend=backend_name,
        label=label,
        config_path=config,
        installed=plist_path.exists(),
        running=running,
        enabled=plist_path.exists(),
        detail=' | '.join(detail_parts),
        status_label='未安装' if not plist_path.exists() else ('运行中' if running else '状态异常'),
        plist_path=str(plist_path),
        domain=launch_agent_domain(uid=uid),
        stdout=result.stdout,
        stderr=result.stderr,
    )


def restart_launch_agent(
    *,
    home_dir: str | Path | None = None,
    label: str = DEFAULT_SERVICE_LABEL,
    uid: int = DEFAULT_UID,
    run_command: RunCommand = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    stop_launch_agent(home_dir=home_dir, label=label, uid=uid, run_command=run_command)
    return start_launch_agent(home_dir=home_dir, label=label, uid=uid, run_command=run_command)


def uninstall_launch_agent(
    *,
    config_path: str | Path,
    home_dir: str | Path | None = None,
    label: str = DEFAULT_SERVICE_LABEL,
    uid: int = DEFAULT_UID,
    run_command: RunCommand = subprocess.run,
) -> Path:
    stop_launch_agent(home_dir=home_dir, label=label, uid=uid, run_command=run_command)
    plist_path = launch_agent_plist_path(home_dir=home_dir, label=label)
    if plist_path.exists():
        plist_path.unlink()
    return plist_path


class LaunchdBackend:
    backend_name = backend_name

    def install_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, Any]:
        plist_path = install_launch_agent(config_path=str(config_path))
        status = self.service_status(config_path=config_path, run_command=run_command)
        status['artifact_path'] = str(plist_path)
        return status

    def start_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
        return start_launch_agent(run_command=run_command)

    def stop_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
        return stop_launch_agent(run_command=run_command)

    def restart_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> subprocess.CompletedProcess[str]:
        return restart_launch_agent(run_command=run_command)

    def service_status(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, Any]:
        return read_launch_agent_status(config_path=config_path, run_command=run_command)

    def uninstall_service(self, *, config_path: str | Path, run_command: RunCommand = subprocess.run) -> dict[str, Any]:
        plist_path = uninstall_launch_agent(config_path=config_path, run_command=run_command)
        status = self.service_status(config_path=config_path, run_command=run_command)
        status['artifact_path'] = str(plist_path)
        return status
