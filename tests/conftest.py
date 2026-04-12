from __future__ import annotations

import os
import signal
import subprocess

import pytest


def _cleanup_pytest_scheduled_daemons() -> None:
    try:
        output = subprocess.check_output(
            ['ps', 'axo', 'pid=,command='],
            text=True,
        )
    except Exception:
        return
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if 'main.py run-scheduled-start' not in line:
            continue
        if 'pytest-of-' not in line:
            continue
        try:
            pid_text, _command = line.split(' ', 1)
            os.kill(int(pid_text), signal.SIGTERM)
        except Exception:
            continue


@pytest.fixture(scope='session', autouse=True)
def _cleanup_leaked_test_daemons():
    _cleanup_pytest_scheduled_daemons()
    yield
    _cleanup_pytest_scheduled_daemons()
