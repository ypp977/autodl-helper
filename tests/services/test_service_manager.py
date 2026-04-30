from __future__ import annotations

import os
import sys

from autodl_helper.services import manager


def test_resolve_backend_returns_launchd_on_macos(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    backend = manager.resolve_backend()
    assert backend.backend_name == 'launchd'


def test_resolve_backend_returns_systemd_on_linux(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    backend = manager.resolve_backend()
    assert backend.backend_name == 'systemd'


def test_resolve_backend_returns_windows_task_on_windows(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(manager.os, 'name', 'nt', raising=False)
    backend = manager.resolve_backend()
    assert backend.backend_name == 'windows_task'
