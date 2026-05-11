from __future__ import annotations

import subprocess
import sys

from autodl_helper.services import systemd


def test_build_systemd_unit_contains_expected_fields(tmp_path):
    config_path = tmp_path / 'config.yaml'
    unit = systemd.build_systemd_unit(config_path=config_path)

    assert 'Type=simple' in unit
    assert f'ExecStart={sys.executable} -m autodl_helper run daemon --config' in unit
    assert str(config_path) in unit
    assert 'Restart=always' in unit
    assert 'StandardOutput=append:' in unit
    assert 'StandardError=append:' in unit


def test_read_systemd_status_reports_running(monkeypatch, tmp_path):
    unit_path = tmp_path / '.config' / 'systemd' / 'user' / 'autodl-helper.service'
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text('unit', encoding='utf-8')
    monkeypatch.setattr(systemd, 'systemd_unit_path', lambda: unit_path)

    def fake_run(cmd, **kwargs):
        if cmd[-2:] == ['is-active', 'autodl-helper']:
            return subprocess.CompletedProcess(cmd, 0, stdout='active\n', stderr='')
        if cmd[-2:] == ['is-enabled', 'autodl-helper']:
            return subprocess.CompletedProcess(cmd, 0, stdout='enabled\n', stderr='')
        raise AssertionError(cmd)

    status = systemd.read_systemd_status(config_path=tmp_path / 'config.yaml', run_command=fake_run)

    assert status['installed'] is True
    assert status['running'] is True
    assert status['enabled'] is True
    assert status['backend'] == 'systemd'


def test_systemd_backend_install_writes_unit(tmp_path, monkeypatch):
    unit_path = tmp_path / '.config' / 'systemd' / 'user' / 'autodl-helper.service'
    monkeypatch.setattr(systemd, 'systemd_unit_path', lambda: unit_path)
    backend = systemd.SystemdBackend()
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        if cmd[-2:] == ['is-active', 'autodl-helper']:
            return subprocess.CompletedProcess(cmd, 3, stdout='inactive\n', stderr='')
        if cmd[-2:] == ['is-enabled', 'autodl-helper']:
            return subprocess.CompletedProcess(cmd, 0, stdout='enabled\n', stderr='')
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

    status = backend.install_service(config_path=tmp_path / 'config.yaml', run_command=fake_run)

    assert unit_path.exists()
    assert any(cmd[:3] == ['systemctl', '--user', 'daemon-reload'] for cmd in seen)
    assert any(cmd[:3] == ['systemctl', '--user', 'enable'] for cmd in seen)
    assert status['artifact_path'] == str(unit_path)
