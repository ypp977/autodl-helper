from __future__ import annotations

import subprocess

from autodl_helper.services import windows_task


def test_build_task_command_contains_run_daemon(tmp_path):
    command = windows_task.build_task_command(config_path=tmp_path / 'config.yaml')
    assert 'run-daemon --config' in command
    assert str((tmp_path / 'config.yaml').resolve()) in command


def test_read_windows_task_status_reports_running(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='Status: Running\nScheduled Task State: Enabled\nLast Run Time: 2026/04/13 10:00:00\nNext Run Time: N/A\n',
            stderr='',
        )

    status = windows_task.read_windows_task_status(config_path=tmp_path / 'config.yaml', run_command=fake_run)

    assert status['installed'] is True
    assert status['running'] is True
    assert status['enabled'] is True
    assert status['backend'] == 'windows_task'


def test_windows_backend_install_creates_task(tmp_path):
    backend = windows_task.WindowsTaskBackend()
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        if '/Query' in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout='Status: Ready\nScheduled Task State: Enabled\n', stderr='')
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

    status = backend.install_service(config_path=tmp_path / 'config.yaml', run_command=fake_run)

    assert any(cmd[:2] == ['schtasks', '/Create'] for cmd in seen)
    assert status['artifact_path'] == 'autodl-helper'
    assert status['enabled'] is True
