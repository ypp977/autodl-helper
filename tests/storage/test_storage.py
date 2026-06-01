import sqlite3
import os

from autodl_helper.core.config import AccountSettings
from autodl_helper.core.store import SQLiteStore


def test_sqlite_store_initializes_schema_and_registers_accounts(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.register_accounts([
        AccountSettings(name='main', enabled=True),
        AccountSettings(name='backup', enabled=False),
    ])

    assert store.schema_version() == SQLiteStore.SCHEMA_VERSION
    rows = store.read_history(limit=5)
    assert rows == []


def test_sqlite_store_creates_database_with_restricted_permissions(tmp_path):
    db_path = tmp_path / 'private-data' / 'data.db'
    store = SQLiteStore(db_path)

    store.init_schema()

    assert os.stat(db_path).st_mode & 0o777 == 0o600
    assert os.stat(db_path.parent).st_mode & 0o777 == 0o700


def test_keeper_cycle_dedupe_uses_account_instance_and_release_deadline(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.add_keeper_history(
        'main',
        'iid-1',
        '2026-04-22T21:00:04+08:00',
        'keeper_executed',
        'keeper_window_reached',
        {'instance_id': 'iid-1'},
    )

    assert store.was_keeper_executed_in_cycle('main', 'iid-1', '2026-04-22T21:00:04+08:00') is True
    assert store.was_keeper_executed_in_cycle('backup', 'iid-1', '2026-04-22T21:00:04+08:00') is False
    assert store.was_keeper_executed_in_cycle('main', 'iid-1', '2026-04-23T21:00:04+08:00') is False


def test_read_history_can_filter_task_and_account(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.add_keeper_history('main', 'iid-1', 'deadline-1', 'keeper_executed', 'keeper_window_reached', {'k': 1})
    store.add_scheduled_history('backup', 'job-1', 'iid-2', '2026-04-08', 'started', 'started', {'s': 1})

    all_rows = store.read_history(limit=10)
    keeper_rows = store.read_history(task_type='keeper', limit=10)
    backup_rows = store.read_history(account_name='backup', limit=10)

    assert len(all_rows) == 2
    assert keeper_rows[0].task_type == 'keeper'
    assert keeper_rows[0].event_type == 'keeper.executed'
    assert backup_rows[0].account_name == 'backup'
    assert backup_rows[0].event_type.startswith('scheduled.')


def test_read_history_can_filter_event_type(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.init_schema()
    store.add_keeper_history('main', 'iid-1', 'deadline-1', 'keeper_executed', 'keeper_window_reached', {'k': 1}, event_type='keeper.executed')
    store.add_scheduled_history('main', 'job-1', 'iid-2', '2026-04-08', 'started', 'started', {'s': 1}, event_type='scheduled.started')
    store.add_scheduled_history('main', 'job-2', 'iid-3', '2026-04-08', 'waiting_for_gpu', 'gpu_idle_zero', {'s': 2}, event_type='scheduled.wait.gpu')

    rows = store.read_history(event_type='scheduled.started', limit=10)

    assert len(rows) == 1
    assert rows[0].event_type == 'scheduled.started'
    assert rows[0].instance_id == 'iid-2'


def test_read_history_filters_before_limit_for_sparse_account(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.init_schema()
    store.add_scheduled_history('backup', 'job-backup', 'iid-backup', '2026-04-08', 'started', 'started', {'s': 'backup'})
    for index in range(60):
        store.add_scheduled_history('main', f'job-main-{index}', f'iid-main-{index}', '2026-04-08', 'waiting_for_gpu', 'gpu_idle_zero', {'s': index})

    rows = store.read_history(account_name='backup', limit=1)

    assert len(rows) == 1
    assert rows[0].account_name == 'backup'
    assert rows[0].task_type == 'scheduled_start'


def test_set_runtime_values_writes_multiple_keys(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.init_schema()

    store.set_runtime_values({'daemon_state': 'running', 'daemon_mode': 'all', 'daemon_pid': '123'})

    snapshot = store.get_runtime_snapshot()
    assert snapshot['daemon_state'] == 'running'
    assert snapshot['daemon_mode'] == 'all'
    assert snapshot['daemon_pid'] == '123'


def test_summarize_auth_failures_marks_unmapped_rows(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.add_event('main', 'auth', 'warning', 'auth miss', code='WeirdCode', msg='totally unrelated', payload={})
    store.add_event('main', 'auth', 'warning', 'auth hit', code='Unauthorized', msg='token expired', payload={})

    rows = store.summarize_auth_failures(limit=10)

    assert rows[0].count >= rows[1].count
    assert any(row.code == 'WeirdCode' and row.mapped is False for row in rows)
    assert any(row.code == 'Unauthorized' and row.mapped is True for row in rows)


def test_record_auth_event_redacts_sensitive_payload(tmp_path):
    from autodl_helper.cli.shared_accounts import record_auth_event

    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()

    record_auth_event(
        store,
        'main',
        {
            'code': 'Unauthorized',
            'msg': 'token=secret-token authorization=Bearer secret',
            'token': 'secret-token',
            'nested': {'password': 'secret-password'},
        },
    )

    with store.connect() as conn:
        row = conn.execute('SELECT msg, payload FROM event_log WHERE task_type = ?', ('auth',)).fetchone()
    payload = __import__('json').loads(row['payload'])
    assert row['msg'] == 'token=<redacted> authorization=<redacted>'
    assert payload['token'] == '<redacted>'
    assert payload['nested']['password'] == '<redacted>'


def test_sqlite_store_connect_retries_transient_open_failure(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    attempts = {'value': 0}
    original_connect = sqlite3.connect

    def flaky_connect(path, *args, **kwargs):
        attempts['value'] += 1
        if attempts['value'] < 3:
            raise sqlite3.OperationalError('unable to open database file')
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', flaky_connect)

    with store.connect() as conn:
        conn.execute('select 1').fetchone()

    assert attempts['value'] == 3


def test_sqlite_store_connect_reports_db_path_on_final_open_failure(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')

    def always_fail(path, *args, **kwargs):
        raise sqlite3.OperationalError('unable to open database file')

    monkeypatch.setattr(sqlite3, 'connect', always_fail)

    try:
        store.connect()
    except sqlite3.OperationalError as exc:
        message = str(exc)
    else:
        raise AssertionError('expected sqlite open failure')

    assert 'unable to open database file' in message
    assert f'path={store.path}' in message


def test_sqlite_store_connect_context_closes_connection(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')

    with store.connect() as conn:
        conn.execute('select 1').fetchone()

    try:
        conn.execute('select 1').fetchone()
    except sqlite3.ProgrammingError as exc:
        assert 'closed' in str(exc).lower()
    else:
        raise AssertionError('expected connection to be closed after context exit')
