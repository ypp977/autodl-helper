from __future__ import annotations

import subprocess
from pathlib import Path

from autodl_helper import service_launchd


def test_build_launch_agent_plist_contains_expected_paths(tmp_path):
    plist = service_launchd.build_launch_agent_plist(
        label=service_launchd.DEFAULT_SERVICE_LABEL,
        python_path='/tmp/venv/bin/python',
        main_path='/tmp/repo/main.py',
        config_path='/tmp/repo/config.yaml',
        cwd='/tmp/repo',
        stdout_path='/tmp/repo/logs/service.stdout.log',
        stderr_path='/tmp/repo/logs/service.stderr.log',
    )

    assert service_launchd.DEFAULT_SERVICE_LABEL in plist
    assert '/tmp/venv/bin/python' in plist
    assert '/tmp/repo/main.py' in plist
    assert 'run-daemon' in plist
    assert '/tmp/repo/config.yaml' in plist
    assert '/tmp/repo/logs/service.stdout.log' in plist
    assert '/tmp/repo/logs/service.stderr.log' in plist


def test_install_launch_agent_writes_plist_without_loading(tmp_path):
    plist_path = service_launchd.install_launch_agent(
        config_path=str(tmp_path / 'config.yaml'),
        python_path='/tmp/venv/bin/python',
        main_path='/tmp/repo/main.py',
        home_dir=tmp_path,
    )

    assert plist_path == tmp_path / 'Library' / 'LaunchAgents' / f'{service_launchd.DEFAULT_SERVICE_LABEL}.plist'
    assert plist_path.exists()
    content = plist_path.read_text(encoding='utf-8')
    assert 'run-daemon' in content
    assert str(tmp_path / 'config.yaml') in content


def test_start_launch_agent_bootstraps_plist(tmp_path):
    plist_path = service_launchd.install_launch_agent(
        config_path=str(tmp_path / 'config.yaml'),
        python_path='/tmp/venv/bin/python',
        main_path='/tmp/repo/main.py',
        home_dir=tmp_path,
    )
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

    service_launchd.start_launch_agent(home_dir=tmp_path, run_command=fake_run)

    assert seen == [[
        'launchctl',
        'bootstrap',
        service_launchd.launch_agent_domain(uid=service_launchd.DEFAULT_UID),
        str(plist_path),
    ]]


def test_stop_launch_agent_boots_out_plist(tmp_path):
    plist_path = service_launchd.install_launch_agent(
        config_path=str(tmp_path / 'config.yaml'),
        python_path='/tmp/venv/bin/python',
        main_path='/tmp/repo/main.py',
        home_dir=tmp_path,
    )
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

    service_launchd.stop_launch_agent(home_dir=tmp_path, run_command=fake_run)

    assert seen == [[
        'launchctl',
        'bootout',
        service_launchd.launch_agent_domain(uid=service_launchd.DEFAULT_UID),
        str(plist_path),
    ]]


def test_read_launch_agent_status_reports_install_and_loaded(tmp_path):
    service_launchd.install_launch_agent(
        config_path=str(tmp_path / 'config.yaml'),
        python_path='/tmp/venv/bin/python',
        main_path='/tmp/repo/main.py',
        home_dir=tmp_path,
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout='state = running\n', stderr='')

    status = service_launchd.read_launch_agent_status(home_dir=tmp_path, run_command=fake_run)

    assert status['installed'] is True
    assert status['loaded'] is True
    assert status['label'] == service_launchd.DEFAULT_SERVICE_LABEL
    assert status['plist_path'] == str(tmp_path / 'Library' / 'LaunchAgents' / f'{service_launchd.DEFAULT_SERVICE_LABEL}.plist')


def test_read_launch_agent_status_reports_unloaded_when_launchctl_print_fails(tmp_path):
    service_launchd.install_launch_agent(
        config_path=str(tmp_path / 'config.yaml'),
        python_path='/tmp/venv/bin/python',
        main_path='/tmp/repo/main.py',
        home_dir=tmp_path,
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout='', stderr='not loaded')

    status = service_launchd.read_launch_agent_status(home_dir=tmp_path, run_command=fake_run)

    assert status['installed'] is True
    assert status['loaded'] is False


def test_read_launch_agent_status_handles_missing_launchctl(tmp_path):
    service_launchd.install_launch_agent(
        config_path=str(tmp_path / 'config.yaml'),
        python_path='/tmp/venv/bin/python',
        main_path='/tmp/repo/main.py',
        home_dir=tmp_path,
    )

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError('launchctl not found')

    status = service_launchd.read_launch_agent_status(home_dir=tmp_path, run_command=fake_run)

    assert status['installed'] is True
    assert status['loaded'] is False
    assert 'launchctl not found' in status['stderr']
