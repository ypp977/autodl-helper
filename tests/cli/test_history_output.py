import json

import autodl_helper.cli.app as cli
from autodl_helper.core.store import HistoryRecord


class DummyStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def read_history(self, **kwargs):
        self.calls.append(kwargs)
        return self.rows


def test_command_history_outputs_json(monkeypatch, capsys):
    rows = [
        HistoryRecord(
            created_at='2026-04-08T01:00:00+08:00',
            account_name='main',
            task_type='keeper',
            result='keeper_executed',
            reason='keeper_window_reached',
            instance_id='iid-1',
            payload={'instance_id': 'iid-1', 'release_deadline': '2026-04-22T21:00:04+08:00', 'next_keeper_time': '2026-04-22T15:00:04+08:00'},
            event_type='keeper.executed',
            severity='success',
            summary='已执行 keeper；状态=shutdown；释放时间=2026-04-22T21:00:04+08:00；下次keeper=2026-04-22T15:00:04+08:00',
        )
    ]
    monkeypatch.setattr(cli, 'load_settings', lambda path: object())
    monkeypatch.setattr(cli, 'create_store', lambda settings: DummyStore(rows))

    code = cli.main(['debug', 'history', '--config', 'config.yaml', '--json'])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert payload[0]['account'] == 'main'
    assert payload[0]['event_type'] == 'keeper.executed'
    assert payload[0]['severity'] == 'success'
    assert '已执行 keeper' in payload[0]['summary']


def test_command_history_outputs_troubleshooting_table(monkeypatch, capsys):
    rows = [
        HistoryRecord(
            created_at='2026-04-08T01:00:00+08:00',
            account_name='main',
            task_type='scheduled_start',
            result='started',
            reason='selected_candidate',
            instance_id='iid-2',
            payload={'selected_instance_id': 'iid-2', 'target_time': '14:00', 'deadline': '2026-04-08T14:00:00+08:00'},
            event_type='scheduled.started',
            severity='success',
            summary='已发起 GPU 开机；实例=iid-2；目标时间=14:00；deadline=2026-04-08T14:00:00+08:00',
        )
    ]
    monkeypatch.setattr(cli, 'load_settings', lambda path: object())
    monkeypatch.setattr(cli, 'create_store', lambda settings: DummyStore(rows))

    code = cli.main(['debug', 'history', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert 'created_at' in captured.out
    assert 'scheduled_start' in captured.out
    assert 'event_type' in captured.out
    assert 'scheduled.started' in captured.out
    assert 'subject' in captured.out
    assert '已发起 GPU 开机' in captured.out


def test_command_history_passes_event_type_filter(monkeypatch, capsys):
    store = DummyStore([])
    monkeypatch.setattr(cli, 'load_settings', lambda path: object())
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)

    code = cli.main(['debug', 'history', '--config', 'config.yaml', '--event-type', 'scheduled.started'])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.strip() == 'No history.'
    assert store.calls == [{'account_name': None, 'task_type': None, 'event_type': 'scheduled.started', 'limit': 20}]
