from datetime import datetime

from autodl_helper.tasks import keeper


class DummyClient:
    def __init__(self, instances):
        self.instances = instances
        self.opened = []
        self.closed = []

    def list_instances(self, page=1, page_size=100):
        return self.instances

    def running_days(self, status_at, now=None):
        return 8

    def days_until_release(self, release_at, now=None):
        return 8

    def open_machine(self, instance_id):
        self.opened.append(instance_id)
        return True

    def close_machine(self, instance_id):
        self.closed.append(instance_id)
        return True



def test_run_keeper_cycle_powers_instances_past_threshold(monkeypatch):
    client = DummyClient([
        {
            'uuid': 'iid',
            'machine_alias': 'gpu-1',
            'region_name': '北京A区',
            'status': 'stopped',
            'status_at': '2026-04-01T10:00:00+08:00',
            'stopped_at': {'Time': '2026-04-01T10:00:00+08:00', 'Valid': True},
            'phone': '13800000000',
        }
    ])
    monkeypatch.setattr(keeper.time, 'sleep', lambda *_args, **_kwargs: None)

    processed = keeper.run_keeper_cycle(
        client=client,
        shutdown_release_after_hours=24 * 15,
        keeper_trigger_before_hours=6,
        now=datetime(2026, 4, 16, 20, 0, 0),
    )

    assert [item.instance_id for item in processed if item.result == 'keeper_executed'] == ['iid']
    assert client.opened == ['iid']
    assert client.closed == ['iid']


def test_run_keeper_cycle_only_processes_instances_with_release_within_threshold(monkeypatch):
    client = DummyClient([
        {
            'uuid': 'iid-near',
            'status': 'shutdown',
            'stopped_at': {'Time': '2026-04-01T10:00:00+08:00', 'Valid': True},
        },
        {
            'uuid': 'iid-far',
            'status': 'shutdown',
            'stopped_at': {'Time': '2026-04-08T10:00:00+08:00', 'Valid': True},
        },
    ])
    monkeypatch.setattr(keeper.time, 'sleep', lambda *_args, **_kwargs: None)

    processed = keeper.run_keeper_cycle(
        client=client,
        shutdown_release_after_hours=24 * 15,
        keeper_trigger_before_hours=6,
        now=datetime(2026, 4, 16, 20, 0, 0),
    )

    assert [item.instance_id for item in processed if item.result == 'keeper_executed'] == ['iid-near']
    assert client.opened == ['iid-near']
    assert client.closed == ['iid-near']


def test_run_keeper_cycle_skips_instances_before_next_keeper_time(monkeypatch):
    client = DummyClient([
        {
            'uuid': 'iid-recent-stop',
            'status': 'shutdown',
            'stopped_at': {'Time': '2026-04-07T21:00:04+08:00', 'Valid': True},
            'started_at': {'Time': '2026-04-07T13:43:34+08:00', 'Valid': True},
        },
    ])
    monkeypatch.setattr(keeper.time, 'sleep', lambda *_args, **_kwargs: None)

    processed = keeper.run_keeper_cycle(
        client=client,
        shutdown_release_after_hours=24 * 15,
        keeper_trigger_before_hours=6,
        stop_cooldown_minutes=360,
        now=datetime(2026, 4, 22, 12, 13, 29),
    )

    assert len(processed) == 1
    assert processed[0].result == 'skip_not_due'
    assert processed[0].reason == 'before_next_keeper_time'
    assert processed[0].release_source == 'stopped_at'
    assert processed[0].shutdown_duration_seconds == 14 * 24 * 60 * 60 + 15 * 60 * 60 + 13 * 60 + 25
    assert client.opened == []
    assert client.closed == []


def test_run_keeper_cycle_uses_started_at_and_stopped_at_for_durations(monkeypatch):
    client = DummyClient([
        {
            'uuid': 'iid-observe',
            'status': 'shutdown',
            'started_at': {'Time': '2026-04-07T13:43:34+08:00', 'Valid': True},
            'stopped_at': {'Time': '2026-04-08T00:13:29+08:00', 'Valid': True},
        },
    ])
    monkeypatch.setattr(keeper.time, 'sleep', lambda *_args, **_kwargs: None)

    processed = keeper.run_keeper_cycle(
        client=client,
        shutdown_release_after_hours=24 * 15,
        keeper_trigger_before_hours=6,
        stop_cooldown_minutes=1,
        now=datetime(2026, 4, 22, 20, 13, 29),
    )

    assert len(processed) == 1
    assert processed[0].result == 'keeper_executed'
    assert processed[0].shutdown_duration_seconds == 14 * 24 * 60 * 60 + 20 * 60 * 60
    assert processed[0].started_at == '2026-04-07T13:43:34+08:00'
    assert processed[0].stopped_at == '2026-04-08T00:13:29+08:00'
    assert processed[0].release_source == 'stopped_at'
    assert processed[0].release_deadline == '2026-04-23T00:13:29+08:00'
    assert processed[0].next_keeper_time == '2026-04-22T18:13:29+08:00'


def test_run_keeper_cycle_can_fallback_to_status_at(monkeypatch):
    client = DummyClient([
        {
            'uuid': 'iid-fallback',
            'status': 'shutdown',
            'status_at': '2026-04-07T18:00:00+08:00',
        },
    ])
    monkeypatch.setattr(keeper.time, 'sleep', lambda *_args, **_kwargs: None)

    processed = keeper.run_keeper_cycle(
        client=client,
        shutdown_release_after_hours=24 * 15,
        keeper_trigger_before_hours=6,
        fallback_to_status_at=True,
        stop_cooldown_minutes=12 * 60,
        now=datetime(2026, 4, 22, 4, 30, 0),
    )

    assert len(processed) == 1
    assert processed[0].result == 'skip_not_due'
    assert processed[0].reason == 'before_next_keeper_time'
    assert processed[0].release_source == 'fallback_status_at'
    assert processed[0].shutdown_duration_seconds == 14 * 24 * 60 * 60 + 10 * 60 * 60 + 30 * 60


def test_run_keeper_cycle_marks_ready_via_fallback_status_at(monkeypatch):
    client = DummyClient([
        {
            'uuid': 'iid-fallback-ready',
            'status': 'shutdown',
            'status_at': '2026-04-07T10:00:00+08:00',
        },
    ])
    monkeypatch.setattr(keeper.time, 'sleep', lambda *_args, **_kwargs: None)

    processed = keeper.run_keeper_cycle(
        client=client,
        shutdown_release_after_hours=24 * 15,
        keeper_trigger_before_hours=6,
        fallback_to_status_at=True,
        stop_cooldown_minutes=60,
        now=datetime(2026, 4, 22, 20, 30, 0),
    )

    assert len(processed) == 1
    assert processed[0].result == 'keeper_executed'
    assert processed[0].reason == 'fallback_status_at_ready'
    assert processed[0].release_source == 'fallback_status_at'


def test_run_keeper_cycle_marks_ready_without_release_time(monkeypatch):
    client = DummyClient([
        {
            'uuid': 'iid-no-release',
            'status': 'shutdown',
            'started_at': {'Time': '2026-04-06T13:56:16+08:00', 'Valid': True},
            'stopped_at': {'Time': '2026-04-06T13:56:28+08:00', 'Valid': True},
        },
    ])
    monkeypatch.setattr(keeper.time, 'sleep', lambda *_args, **_kwargs: None)

    processed = keeper.run_keeper_cycle(
        client=client,
        shutdown_release_after_hours=24 * 15,
        keeper_trigger_before_hours=6,
        stop_cooldown_minutes=60,
        now=datetime(2026, 4, 22, 20, 30, 0),
    )

    assert len(processed) == 1
    assert processed[0].result == 'keeper_executed'
    assert processed[0].reason == 'keeper_window_reached'
    assert processed[0].release_source == 'stopped_at'


def test_run_keeper_cycle_waits_until_next_keeper_time(monkeypatch):
    client = DummyClient([
        {
            'uuid': 'iid-wait',
            'status': 'shutdown',
            'stopped_at': {'Time': '2026-04-07T21:00:04+08:00', 'Valid': True},
        },
    ])
    monkeypatch.setattr(keeper.time, 'sleep', lambda *_args, **_kwargs: None)

    processed = keeper.run_keeper_cycle(
        client=client,
        shutdown_release_after_hours=24 * 15,
        keeper_trigger_before_hours=6,
        now=datetime(2026, 4, 20, 12, 0, 0),
    )

    assert processed[0].result == 'skip_not_due'
    assert processed[0].reason == 'before_next_keeper_time'
    assert processed[0].release_source == 'stopped_at'


class DummyStore:
    def __init__(self, executed=False):
        self.executed = executed
        self.history = []

    def was_keeper_executed_in_cycle(self, account_name, instance_id, release_deadline):
        return self.executed

    def add_keeper_history(self, account_name, instance_id, release_deadline, result, reason, payload):
        self.history.append((account_name, instance_id, release_deadline, result, reason, payload))


def test_run_keeper_cycle_skips_already_executed_release_cycle(monkeypatch):
    client = DummyClient([
        {
            'uuid': 'iid',
            'status': 'shutdown',
            'stopped_at': {'Time': '2026-04-01T10:00:00+08:00', 'Valid': True},
        }
    ])
    store = DummyStore(executed=True)
    monkeypatch.setattr(keeper.time, 'sleep', lambda *_args, **_kwargs: None)

    processed = keeper.run_keeper_cycle(
        client=client,
        shutdown_release_after_hours=24 * 15,
        keeper_trigger_before_hours=6,
        now=datetime(2026, 4, 16, 20, 0, 0),
        store=store,
        account_name='main',
    )

    assert processed[0].result == 'skip_already_executed_in_cycle'
    assert processed[0].reason == 'already_executed_in_release_cycle'
    assert client.opened == []
    assert store.history[0][0] == 'main'


def test_run_keeper_cycle_records_failed_attempt_but_allows_retry(monkeypatch):
    client = DummyClient([
        {
            'uuid': 'iid',
            'status': 'shutdown',
            'stopped_at': {'Time': '2026-04-01T10:00:00+08:00', 'Valid': True},
        }
    ])
    client.open_machine = lambda instance_id: False
    store = DummyStore(executed=False)
    monkeypatch.setattr(keeper.time, 'sleep', lambda *_args, **_kwargs: None)

    processed = keeper.run_keeper_cycle(
        client=client,
        shutdown_release_after_hours=24 * 15,
        keeper_trigger_before_hours=6,
        now=datetime(2026, 4, 16, 20, 0, 0),
        store=store,
        account_name='main',
    )

    assert processed[0].result == 'keeper_failed_power_on'
    assert store.history[0][3] == 'keeper_failed_power_on'


def test_run_keeper_cycle_writes_same_batch_id_for_single_execution(monkeypatch):
    client = DummyClient([
        {
            'uuid': 'iid-1',
            'status': 'shutdown',
            'stopped_at': {'Time': '2026-04-01T10:00:00+08:00', 'Valid': True},
        },
        {
            'uuid': 'iid-2',
            'status': 'shutdown',
            'stopped_at': {'Time': '2026-04-01T11:00:00+08:00', 'Valid': True},
        },
    ])
    store = DummyStore(executed=False)
    monkeypatch.setattr(keeper.time, 'sleep', lambda *_args, **_kwargs: None)

    keeper.run_keeper_cycle(
        client=client,
        shutdown_release_after_hours=24 * 15,
        keeper_trigger_before_hours=6,
        now=datetime(2026, 4, 16, 20, 0, 0),
        store=store,
        account_name='main',
    )

    batch_ids = {payload.get('batch_id') for *_prefix, payload in store.history}
    assert len(batch_ids) == 1
    assert next(iter(batch_ids))
