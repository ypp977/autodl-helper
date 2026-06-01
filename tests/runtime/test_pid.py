from __future__ import annotations

import os

from autodl_helper.runtime.pid import pid_exists


def test_pid_exists_rejects_empty_values():
    assert pid_exists(None) is False
    assert pid_exists(0) is False
    assert pid_exists(-1) is False


def test_pid_exists_reports_current_process():
    assert pid_exists(os.getpid()) is True
