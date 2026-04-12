from __future__ import annotations

from pathlib import Path

import pytest

from autodl_helper.lock import FileLock, LockAcquisitionError


def test_file_lock_recovers_stale_empty_lock(tmp_path):
    lock_path = tmp_path / 'worker.lock'
    lock_path.write_text('', encoding='utf-8')

    lock = FileLock(lock_path)
    lock.acquire()
    try:
        content = lock_path.read_text(encoding='utf-8').strip()
        assert content.isdigit()
    finally:
        lock.release()


def test_file_lock_recovers_stale_dead_pid(monkeypatch, tmp_path):
    lock_path = tmp_path / 'worker.lock'
    lock_path.write_text('999999', encoding='utf-8')
    monkeypatch.setattr('autodl_helper.lock._pid_is_running', lambda pid: False)

    lock = FileLock(lock_path)
    lock.acquire()
    try:
        content = lock_path.read_text(encoding='utf-8').strip()
        assert content.isdigit()
        assert content != '999999'
    finally:
        lock.release()


def test_file_lock_preserves_live_lock(monkeypatch, tmp_path):
    lock_path = tmp_path / 'worker.lock'
    lock_path.write_text('12345', encoding='utf-8')
    monkeypatch.setattr('autodl_helper.lock._pid_is_running', lambda pid: True)

    lock = FileLock(lock_path)
    with pytest.raises(LockAcquisitionError):
        lock.acquire()
