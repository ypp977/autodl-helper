import io
import subprocess
import sys
import time
import threading
from pathlib import Path

from autodl_helper import interactive_runtime


def test_interactive_task_manager_deduplicates_active_tasks():
    snapshots = interactive_runtime.InteractiveSnapshotStore()
    manager = interactive_runtime.InteractiveTaskManager(snapshot_store=snapshots)
    calls = []
    gate = threading.Event()

    def runner():
        calls.append('run')
        gate.wait(timeout=1.0)
        return {'ok': True}

    try:
        first = manager.submit(
            'dashboard_refresh',
            scope='main',
            runner=runner,
        )
        second = manager.submit(
            'dashboard_refresh',
            scope='main',
            runner=runner,
        )

        assert first.task_key == second.task_key
        assert first.status in {'queued', 'running'}
        assert second.status in {'queued', 'running'}
        assert calls == []

        manager.start_pending()
        gate.set()
        deadline = time.time() + 1.0
        while time.time() < deadline:
            manager.drain_completed()
            latest = manager.get_task('dashboard_refresh', 'main')
            if latest and latest.status == 'succeeded':
                break
            time.sleep(0.01)

        latest = manager.get_task('dashboard_refresh', 'main')
        assert latest is not None
        assert latest.status == 'succeeded'
        assert latest.payload == {'ok': True}
        assert calls == ['run']
    finally:
        manager.shutdown(wait=False)


def test_snapshot_store_exposes_failed_refresh_with_last_success():
    snapshots = interactive_runtime.InteractiveSnapshotStore()
    snapshots.set_snapshot('dashboard:main', {'value': 1}, status_message='ready')
    snapshots.record_failure('dashboard:main', 'boom')

    page_status = snapshots.page_status('dashboard:main')

    assert page_status.state == 'failed'
    assert page_status.updated_at
    assert page_status.message == '刷新失败（保留上次结果）'
    assert page_status.error_message == 'boom'


def test_snapshot_store_entry_revision_increments_on_updates():
    snapshots = interactive_runtime.InteractiveSnapshotStore()

    before = snapshots.entry_revision('dashboard:main')
    snapshots.set_snapshot('dashboard:main', {'value': 1})
    mid = snapshots.entry_revision('dashboard:main')
    snapshots.record_failure('dashboard:main', 'boom')
    after = snapshots.entry_revision('dashboard:main')

    assert before == 0
    assert mid > before
    assert after > mid


def test_snapshot_store_can_clear_prefix():
    snapshots = interactive_runtime.InteractiveSnapshotStore()
    snapshots.set_snapshot('scheduled_progress:job:main:job-1', {'value': 1})
    snapshots.set_snapshot('scheduled_progress:all:main', {'value': 2})
    snapshots.set_snapshot('dashboard:main', {'value': 3})

    snapshots.clear_prefix('scheduled_progress:')

    assert snapshots.get_snapshot('scheduled_progress:job:main:job-1') is None
    assert snapshots.get_snapshot('scheduled_progress:all:main') is None
    assert snapshots.get_snapshot('dashboard:main') == {'value': 3}


def test_interactive_task_manager_can_replace_queued_task():
    snapshots = interactive_runtime.InteractiveSnapshotStore()
    manager = interactive_runtime.InteractiveTaskManager(snapshot_store=snapshots, max_workers=1)
    gate = threading.Event()
    calls = []

    def running_task():
        gate.wait(timeout=1.0)
        return 'running'

    def stale_task():
        calls.append('stale')
        return 'stale'

    def replacement_task():
        calls.append('replacement')
        return 'replacement'

    try:
        manager.submit('refresh', scope='running', runner=running_task)
        manager.start_pending()
        manager.submit('refresh', scope='same', runner=stale_task)
        manager.submit('refresh', scope='same', runner=replacement_task, replace_queued=True)
        gate.set()
        deadline = time.time() + 1.0
        while time.time() < deadline:
            manager.drain_completed()
            latest = manager.get_task('refresh', 'same')
            if latest and latest.status == 'succeeded':
                break
            time.sleep(0.01)
        latest = manager.get_task('refresh', 'same')
        assert latest is not None
        assert latest.payload == 'replacement'
        assert calls == ['replacement']
    finally:
        manager.shutdown(wait=False)


def test_snapshot_store_enforces_namespace_limit():
    snapshots = interactive_runtime.InteractiveSnapshotStore(namespace_limits={'diagnostics': 2})

    snapshots.set_snapshot('diagnostics:main', {'value': 1})
    snapshots.set_snapshot('diagnostics:backup', {'value': 2})
    snapshots.set_snapshot('diagnostics:third', {'value': 3})

    assert snapshots.get_snapshot('diagnostics:main') is None
    assert snapshots.get_snapshot('diagnostics:backup') == {'value': 2}
    assert snapshots.get_snapshot('diagnostics:third') == {'value': 3}


def test_interactive_task_manager_runtime_stats_include_running_and_queued(monkeypatch):
    monkeypatch.setattr(
        interactive_runtime,
        '_fd_usage_snapshot',
        lambda: {'current': 12, 'soft_limit': 128, 'usage_percent': 9.4},
    )
    snapshots = interactive_runtime.InteractiveSnapshotStore()
    manager = interactive_runtime.InteractiveTaskManager(snapshot_store=snapshots, max_workers=1)
    gate = threading.Event()

    try:
        manager.submit('healthcheck_run', scope='main', runner=lambda: gate.wait(timeout=1.0) or {'ok': True})
        manager.submit('account_refresh', scope='main', runner=lambda: {'ok': True})
        manager.start_pending()
        time.sleep(0.05)

        stats = manager.runtime_stats()

        assert stats['max_workers'] == 1
        assert stats['running_count'] == 1
        assert stats['queued_count'] == 1
        assert stats['running_by_type']['healthcheck_run'] == 1
        assert stats['queued_by_type']['account_refresh'] == 1
        assert stats['fd_current'] == 12
        assert stats['fd_soft_limit'] == 128
        assert stats['fd_usage_percent'] == 9.4
    finally:
        gate.set()
        manager.shutdown(wait=False)


def test_interactive_task_manager_task_revision_changes_with_state(monkeypatch):
    monkeypatch.setattr(
        interactive_runtime,
        '_fd_usage_snapshot',
        lambda: {'current': 12, 'soft_limit': 128, 'usage_percent': 9.4},
    )
    snapshots = interactive_runtime.InteractiveSnapshotStore()
    manager = interactive_runtime.InteractiveTaskManager(snapshot_store=snapshots, max_workers=1)

    try:
        initial = manager.task_revision('healthcheck_run:main')
        manager.submit('healthcheck_run', scope='main', runner=lambda: {'ok': True})
        queued = manager.task_revision('healthcheck_run:main')
        manager.start_pending()
        deadline = time.time() + 1.0
        while time.time() < deadline:
            manager.drain_completed()
            current = manager.get_task('healthcheck_run', 'main')
            if current and current.status == 'succeeded':
                break
            time.sleep(0.01)
        finished = manager.task_revision('healthcheck_run:main')

        assert initial == 0
        assert queued > initial
        assert finished > queued
    finally:
        manager.shutdown(wait=False)


def test_interactive_task_manager_opens_circuit_and_rejects_high_risk_tasks(monkeypatch):
    monkeypatch.setattr(
        interactive_runtime,
        '_fd_usage_snapshot',
        lambda: {'current': 90, 'soft_limit': 100, 'usage_percent': 90.0},
    )
    snapshots = interactive_runtime.InteractiveSnapshotStore()
    manager = interactive_runtime.InteractiveTaskManager(snapshot_store=snapshots, circuit_cooldown_seconds=0.01)

    try:
        task = manager.submit('healthcheck_run', scope='main', runner=lambda: {'ok': True})
        circuit = manager.circuit_state()

        assert task.status == 'failed'
        assert '资源熔断' in task.error_message
        assert circuit['circuit_open'] is True
        assert '文件描述符' in circuit['circuit_reason']
    finally:
        manager.shutdown(wait=False)


def test_interactive_task_manager_rejects_refresh_tasks_when_circuit_open(monkeypatch):
    monkeypatch.setattr(
        interactive_runtime,
        '_fd_usage_snapshot',
        lambda: {'current': 90, 'soft_limit': 100, 'usage_percent': 90.0},
    )
    snapshots = interactive_runtime.InteractiveSnapshotStore()
    manager = interactive_runtime.InteractiveTaskManager(snapshot_store=snapshots, circuit_cooldown_seconds=0.01)

    try:
        task = manager.submit('instances_refresh', scope='main', runner=lambda: [{'instance_id': 'iid-1'}])

        assert task.status == 'failed'
        assert '资源熔断' in task.error_message
    finally:
        manager.shutdown(wait=False)


def test_interactive_task_manager_half_open_allows_single_probe_after_cooldown(monkeypatch):
    usage = {'current': 90, 'soft_limit': 100, 'usage_percent': 90.0}
    monkeypatch.setattr(interactive_runtime, '_fd_usage_snapshot', lambda: dict(usage))
    snapshots = interactive_runtime.InteractiveSnapshotStore()
    manager = interactive_runtime.InteractiveTaskManager(snapshot_store=snapshots, circuit_cooldown_seconds=0.01)

    try:
        rejected = manager.submit('healthcheck_run', scope='main', runner=lambda: {'ok': True})
        assert rejected.status == 'failed'

        usage.update({'current': 10, 'soft_limit': 100, 'usage_percent': 10.0})
        time.sleep(0.02)

        first_probe = manager.submit('healthcheck_run', scope='main', runner=lambda: {'ok': True})
        second_probe = manager.submit('login_verify_run', scope='main', runner=lambda: {'ok': True})

        assert first_probe.status == 'queued'
        assert second_probe.status == 'failed'
        assert '半开' in second_probe.error_message
    finally:
        manager.shutdown(wait=False)


def test_thread_capturing_print_ignores_closed_stdout(monkeypatch):
    class ClosedStdout(io.StringIO):
        @property
        def closed(self):  # type: ignore[override]
            return True

    monkeypatch.setattr(interactive_runtime.sys, 'stdout', ClosedStdout())
    monkeypatch.setattr(
        interactive_runtime,
        '_original_print',
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('I/O operation on closed file.')),
    )
    interactive_runtime._print_buffers.clear()

    interactive_runtime._thread_capturing_print('hello', end='')


def test_release_thread_capture_streams_does_not_restore_stale_closed_stdout(monkeypatch):
    class ClosedStdout(io.StringIO):
        @property
        def closed(self):  # type: ignore[override]
            return True

    current_stdout = io.StringIO()
    current_stderr = io.StringIO()
    stdout_proxy = interactive_runtime._ThreadCaptureStream(current_stdout)
    stderr_proxy = interactive_runtime._ThreadCaptureStream(current_stderr)

    monkeypatch.setattr(interactive_runtime, '_stdout_proxy', stdout_proxy)
    monkeypatch.setattr(interactive_runtime, '_stderr_proxy', stderr_proxy)
    monkeypatch.setattr(interactive_runtime, '_stream_capture_count', 1)
    monkeypatch.setattr(interactive_runtime, '_original_stdout', ClosedStdout())
    monkeypatch.setattr(interactive_runtime, '_original_stderr', ClosedStdout())
    monkeypatch.setattr(interactive_runtime.sys, 'stdout', current_stdout)
    monkeypatch.setattr(interactive_runtime.sys, 'stderr', current_stderr)

    interactive_runtime._release_thread_capture_streams()

    assert interactive_runtime.sys.stdout is current_stdout
    assert interactive_runtime.sys.stderr is current_stderr


def test_interactive_task_manager_shutdown_wait_false_does_not_block_process():
    script = """
import threading
from autodl_helper.interactive_runtime import InteractiveSnapshotStore, InteractiveTaskManager

snapshots = InteractiveSnapshotStore()
manager = InteractiveTaskManager(snapshot_store=snapshots, max_workers=1)
manager.submit(
    'healthcheck_run',
    scope='main',
    runner=lambda: threading.Event().wait(timeout=5.0) or {'ok': True},
)
manager.start_pending()
manager.shutdown(wait=False)
print('after-shutdown')
"""
    repo_root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [sys.executable, '-c', script],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=1.0,
    )

    assert completed.returncode == 0
    assert 'after-shutdown' in completed.stdout


def test_reset_thread_capture_state_clears_leaked_capture_globals(monkeypatch):
    stdout_proxy = interactive_runtime._ThreadCaptureStream(io.StringIO())
    stderr_proxy = interactive_runtime._ThreadCaptureStream(io.StringIO())
    monkeypatch.setattr(interactive_runtime, '_stdout_proxy', stdout_proxy)
    monkeypatch.setattr(interactive_runtime, '_stderr_proxy', stderr_proxy)
    monkeypatch.setattr(interactive_runtime, '_stream_capture_count', 1)
    monkeypatch.setattr(interactive_runtime, '_print_capture_count', 1)
    monkeypatch.setattr(interactive_runtime, '_original_stdout', io.StringIO())
    monkeypatch.setattr(interactive_runtime, '_original_stderr', io.StringIO())
    monkeypatch.setattr(interactive_runtime, '_original_print', lambda *args, **kwargs: None)
    monkeypatch.setattr(interactive_runtime.sys, 'stdout', stdout_proxy)
    monkeypatch.setattr(interactive_runtime.sys, 'stderr', stderr_proxy)
    interactive_runtime._print_buffers[123] = io.StringIO()

    interactive_runtime.reset_thread_capture_state()

    assert interactive_runtime._stream_capture_count == 0
    assert interactive_runtime._print_capture_count == 0
    assert interactive_runtime._stdout_proxy is None
    assert interactive_runtime._stderr_proxy is None
    assert interactive_runtime._original_stdout is None
    assert interactive_runtime._original_stderr is None
    assert interactive_runtime._original_print is None
    assert interactive_runtime._print_buffers == {}
