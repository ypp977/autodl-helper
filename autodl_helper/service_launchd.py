from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape


DEFAULT_SERVICE_LABEL = 'com.autodl.helper'
DEFAULT_UID = os.getuid()


def launch_agent_domain(*, uid: int = DEFAULT_UID) -> str:
    return f'gui/{int(uid)}'


def launch_agent_plist_path(*, home_dir: str | Path | None = None, label: str = DEFAULT_SERVICE_LABEL) -> Path:
    root = Path(home_dir).expanduser() if home_dir is not None else Path.home()
    return root / 'Library' / 'LaunchAgents' / f'{label}.plist'


def _logs_dir_for_config(config_path: str | Path) -> Path:
    return Path(config_path).resolve().parent / 'logs'


def service_stdout_log_path(config_path: str | Path) -> Path:
    return _logs_dir_for_config(config_path) / 'service.stdout.log'


def append_service_lifecycle_log(config_path: str | Path, message: str) -> Path:
    log_path = service_stdout_log_path(config_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    timestamp = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
    with log_path.open('a', encoding='utf-8') as handle:
        handle.write(f'{timestamp} - INFO - [服务管理] {message}\n')
    return log_path


def build_launch_agent_plist(
    *,
    label: str,
    python_path: str,
    main_path: str,
    config_path: str,
    cwd: str,
    stdout_path: str,
    stderr_path: str,
) -> str:
    args = ''.join(
        f'\n        <string>{escape(item)}</string>'
        for item in [python_path, main_path, 'run-daemon', '--config', config_path]
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
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
    python_path: str | None = None,
    main_path: str | None = None,
    home_dir: str | Path | None = None,
    label: str = DEFAULT_SERVICE_LABEL,
) -> Path:
    config = Path(config_path).resolve()
    plist_path = launch_agent_plist_path(home_dir=home_dir, label=label)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    logs_dir = _logs_dir_for_config(config)
    logs_dir.mkdir(parents=True, exist_ok=True)
    rendered = build_launch_agent_plist(
        label=label,
        python_path=python_path or sys.executable,
        main_path=main_path or str(Path(__file__).resolve().parent.parent / 'main.py'),
        config_path=str(config),
        cwd=str(config.parent),
        stdout_path=str(logs_dir / 'service.stdout.log'),
        stderr_path=str(logs_dir / 'service.stderr.log'),
    )
    plist_path.write_text(rendered, encoding='utf-8')
    return plist_path


def _run_launchctl(
    cmd: list[str],
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    try:
        return run_command(cmd, capture_output=True, text=True)
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, stdout='', stderr=str(exc))


def start_launch_agent(
    *,
    home_dir: str | Path | None = None,
    label: str = DEFAULT_SERVICE_LABEL,
    uid: int = DEFAULT_UID,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    plist_path = launch_agent_plist_path(home_dir=home_dir, label=label)
    return _run_launchctl(
        ['launchctl', 'bootstrap', launch_agent_domain(uid=uid), str(plist_path)],
        run_command=run_command,
    )


def stop_launch_agent(
    *,
    home_dir: str | Path | None = None,
    label: str = DEFAULT_SERVICE_LABEL,
    uid: int = DEFAULT_UID,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    plist_path = launch_agent_plist_path(home_dir=home_dir, label=label)
    return _run_launchctl(
        ['launchctl', 'bootout', launch_agent_domain(uid=uid), str(plist_path)],
        run_command=run_command,
    )


def read_launch_agent_status(
    *,
    home_dir: str | Path | None = None,
    label: str = DEFAULT_SERVICE_LABEL,
    uid: int = DEFAULT_UID,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    plist_path = launch_agent_plist_path(home_dir=home_dir, label=label)
    result = _run_launchctl(
        ['launchctl', 'print', f'{launch_agent_domain(uid=uid)}/{label}'],
        run_command=run_command,
    )
    return {
        'label': label,
        'domain': launch_agent_domain(uid=uid),
        'plist_path': str(plist_path),
        'installed': plist_path.exists(),
        'loaded': result.returncode == 0,
        'stdout': result.stdout,
        'stderr': result.stderr,
    }


def restart_launch_agent(
    *,
    home_dir: str | Path | None = None,
    label: str = DEFAULT_SERVICE_LABEL,
    uid: int = DEFAULT_UID,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    stop_launch_agent(home_dir=home_dir, label=label, uid=uid, run_command=run_command)
    return start_launch_agent(home_dir=home_dir, label=label, uid=uid, run_command=run_command)


def uninstall_launch_agent(
    *,
    home_dir: str | Path | None = None,
    label: str = DEFAULT_SERVICE_LABEL,
    uid: int = DEFAULT_UID,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Path:
    stop_launch_agent(home_dir=home_dir, label=label, uid=uid, run_command=run_command)
    plist_path = launch_agent_plist_path(home_dir=home_dir, label=label)
    if plist_path.exists():
        plist_path.unlink()
    return plist_path
