from datetime import datetime, timedelta, timezone
import threading
from types import SimpleNamespace

from autodl_helper.core.config import (
    AccountSettings,
    KeeperSettings,
    ScheduledStartJob,
    ScheduledStartSelector,
    ScheduledStartSettings,
    Settings,
    TaskSettings,
)
from autodl_helper.runtime_control import (
    _ensure_aware_utc,
    apply_runtime_controls_to_scheduled_jobs,
    claim_daemon_launch,
    clear_daemon_launch_state,
    get_task_enabled,
    mark_config_reload_failure,
    mark_config_reload_success,
    mark_daemon_launch_failure,
    mark_daemon_launch_running,
    mark_daemon_heartbeat,
    read_config_reload_status,
    read_daemon_launch_status,
    read_daemon_status,
    request_config_reload,
    task_due,
)
from autodl_helper.core.store import SQLiteStore


def test_runtime_control_tables_and_task_overrides(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.set_task_control('main', 'keeper', enabled=False, source='interactive')
    store.upsert_scheduled_job_control('main', 'job-1', enabled=False, source='interactive')

    assert store.get_task_control('main', 'keeper') is False
    assert store.get_scheduled_job_control('main', 'job-1')['enabled'] is False


def test_runtime_control_applies_job_overrides(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    job = ScheduledStartJob(
        instance_id='iid-1',
        name='job-1',
        target_time='14:00',
        advance_hours=2,
        selector=ScheduledStartSelector(gpu_model='RTX 3080 Ti', gpu_count=1),
    )
    store.upsert_scheduled_job_control(
        'main',
        'job-1',
        enabled=True,
        target_time_override='15:30',
        advance_hours_override=1,
        source='interactive',
    )

    effective = apply_runtime_controls_to_scheduled_jobs(store, 'main', [job])

    assert len(effective) == 1
    assert effective[0].target_time == '15:30'
    assert effective[0].advance_hours == 1


def test_runtime_control_can_disable_task_even_when_config_enabled(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.set_task_control('main', 'scheduled_start', enabled=False, source='interactive')

    assert get_task_enabled(store, 'main', 'scheduled_start', default_enabled=True) is False
    assert get_task_enabled(store, 'main', 'keeper', default_enabled=True) is True


def test_daemon_heartbeat_status(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    mark_daemon_heartbeat(store, mode='foreground', pid=12345)

    snapshot = read_daemon_status(store)

    assert snapshot['running'] is True
    assert snapshot['pid'] == 12345
    assert snapshot['mode'] == 'foreground'


def test_daemon_heartbeat_ttl_exceeds_heartbeat_interval(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.set_runtime_value('daemon_state', 'running')
    store.set_runtime_value('daemon_mode', 'all')
    store.set_runtime_value('daemon_pid', '12345')
    store.set_runtime_value(
        'daemon_last_seen_at',
        (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
    )

    snapshot = read_daemon_status(store)

    assert snapshot['running'] is True


def test_task_due_accepts_naive_now_against_aware_last_run(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.set_runtime_value('last_run:scheduled_start', '2026-04-10T13:54:00+08:00')

    due = task_due(
        store,
        'scheduled_start',
        interval_seconds=5,
        now=datetime(2026, 4, 10, 13, 54, 6),
    )

    assert due is True


def test_task_due_tolerates_small_scheduler_jitter(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.set_runtime_value('last_run:scheduled_start', '2026-04-10T13:54:00.700000+08:00')

    due = task_due(
        store,
        'scheduled_start',
        interval_seconds=5,
        now=datetime(2026, 4, 10, 13, 54, 5, 534000),
    )

    assert due is True


def test_ensure_aware_utc_normalizes_naive_datetime():
    value = _ensure_aware_utc(datetime(2026, 4, 10, 13, 54, 6))

    assert value.tzinfo is not None


def test_daemon_launch_claim_respects_starting_state(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()

    claimed = claim_daemon_launch(store, account='main', starting_ttl_seconds=10)
    reused = claim_daemon_launch(store, account='main', starting_ttl_seconds=10)

    assert claimed['claimed'] is True
    assert reused['claimed'] is False
    assert reused['launch_state'] == 'starting'


def test_daemon_launch_claim_is_atomic_for_concurrent_callers(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    barrier = threading.Barrier(2)
    results: list[dict[str, object]] = []

    def claim_once(account: str) -> None:
        barrier.wait(timeout=2)
        results.append(claim_daemon_launch(store, account=account, starting_ttl_seconds=10))

    threads = [
        threading.Thread(target=claim_once, args=('main',)),
        threading.Thread(target=claim_once, args=('backup',)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert len(results) == 2
    assert sum(1 for result in results if result['claimed'] is True) == 1
    assert sum(1 for result in results if result['claimed'] is False) == 1


def test_daemon_launch_claim_writes_launch_snapshot_in_one_batch():
    class RuntimeOnlyStore:
        def __init__(self):
            self.snapshot: dict[str, str] = {}
            self.batch_writes: list[dict[str, str]] = []

        def get_runtime_snapshot(self):
            return dict(self.snapshot)

        def set_runtime_values(self, values):
            self.batch_writes.append(dict(values))
            self.snapshot.update({str(key): str(value) for key, value in values.items()})

        def set_runtime_value(self, key, value):
            raise AssertionError('claim_daemon_launch should use set_runtime_values')

    store = RuntimeOnlyStore()

    claimed = claim_daemon_launch(store, account='main', starting_ttl_seconds=10)  # type: ignore[arg-type]

    assert claimed['claimed'] is True
    assert len(store.batch_writes) == 1
    assert store.snapshot['daemon_launch_state'] == 'starting'
    assert store.snapshot['daemon_launch_account'] == 'main'


def test_daemon_launch_running_status_reuses_existing_pid(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    mark_daemon_launch_running(store, account='main', pid=4321)

    status = read_daemon_launch_status(store, pid_exists_fn=lambda pid: pid == 4321)

    assert status['launch_state'] == 'running'
    assert status['launch_pid'] == 4321


def test_daemon_launch_failure_enters_fused_state_after_threshold(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()

    for _ in range(3):
        mark_daemon_launch_failure(store, account='main', error='boom', fuse_after_failures=3, cooldown_seconds=30)

    status = read_daemon_launch_status(store)

    assert status['launch_state'] == 'fused'
    assert status['launch_error_count'] == 3
    assert status['launch_last_error'] == 'boom'
    assert status['launch_fused_until']


def test_clear_daemon_launch_state_resets_launch_snapshot(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    mark_daemon_launch_running(store, account='main', pid=4321)

    clear_daemon_launch_state(store)
    status = read_daemon_launch_status(store)

    assert status['launch_state'] == 'idle'
    assert status['launch_pid'] is None


def test_request_config_reload_increments_generation(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()

    request_config_reload(store)
    status = read_config_reload_status(store)

    assert status['requested_generation'] == 1
    assert status['processed_generation'] == 0
    assert status['applied_generation'] == 0
    assert status['requested_at']


def test_mark_config_reload_success_records_loaded_state(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    request_config_reload(store)

    mark_config_reload_success(store, generation=1, config_mtime=123.5)
    status = read_config_reload_status(store)

    assert status['requested_generation'] == 1
    assert status['processed_generation'] == 1
    assert status['applied_generation'] == 1
    assert status['last_loaded_mtime'] == '123.500000'
    assert status['last_processed_mtime'] == '123.500000'
    assert status['last_reload_status'] == 'success'
    assert status['last_reload_error'] == ''


def test_mark_config_reload_failure_records_error_without_applying(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    request_config_reload(store)

    mark_config_reload_failure(store, generation=1, config_mtime=222.0, error='invalid config')
    status = read_config_reload_status(store)

    assert status['requested_generation'] == 1
    assert status['processed_generation'] == 1
    assert status['applied_generation'] == 0
    assert status['last_processed_mtime'] == '222.000000'
    assert status['last_loaded_mtime'] == ''
    assert status['last_reload_status'] == 'failed'
    assert status['last_reload_error'] == 'invalid config'
