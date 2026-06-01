import json

import autodl_helper.cli.app as cli
from autodl_helper.core.store import AuthEventSummary


class DummyAuthStore:
    def __init__(self, rows):
        self.rows = rows

    def summarize_auth_failures(self, **kwargs):
        return self.rows


def test_auth_report_outputs_json(monkeypatch, capsys):
    rows = [
        AuthEventSummary(
            code='Unauthorized',
            msg='token expired',
            count=3,
            last_seen_at='2026-04-08T01:00:00+08:00',
            accounts=['main'],
            mapped=True,
            matched_by='message',
        )
    ]
    monkeypatch.setattr(cli, 'load_settings', lambda path: object())
    monkeypatch.setattr(cli, 'create_store', lambda settings: DummyAuthStore(rows))

    code = cli.main(['debug', 'auth', '--config', 'config.yaml', '--json'])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert payload['rows'][0]['code'] == 'Unauthorized'
    assert 'known_code_signals' in payload


def test_auth_report_outputs_table(monkeypatch, capsys):
    rows = [
        AuthEventSummary(
            code='WeirdCode',
            msg='custom failure',
            count=2,
            last_seen_at='2026-04-08T01:00:00+08:00',
            accounts=['main', 'backup'],
            mapped=False,
            matched_by='unmapped',
        )
    ]
    monkeypatch.setattr(cli, 'load_settings', lambda path: object())
    monkeypatch.setattr(cli, 'create_store', lambda settings: DummyAuthStore(rows))

    code = cli.main(['debug', 'auth', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert '未覆盖' in captured.out
    assert 'WeirdCode' in captured.out
    assert 'main,backup' in captured.out


def test_auth_report_only_unmapped_filters_rows(monkeypatch, capsys):
    rows = [
        AuthEventSummary(
            code='Unauthorized',
            msg='token expired',
            count=3,
            last_seen_at='2026-04-08T01:00:00+08:00',
            accounts=['main'],
            mapped=True,
            matched_by='message',
        ),
        AuthEventSummary(
            code='WeirdAuthCode',
            msg='credential rejected',
            count=1,
            last_seen_at='2026-04-08T02:00:00+08:00',
            accounts=['main'],
            mapped=False,
            matched_by='unmapped',
        ),
    ]
    monkeypatch.setattr(cli, 'load_settings', lambda path: object())
    monkeypatch.setattr(cli, 'create_store', lambda settings: DummyAuthStore(rows))

    code = cli.main(['debug', 'auth', '--config', 'config.yaml', '--only-unmapped', '--json'])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert len(payload['rows']) == 1
    assert payload['rows'][0]['code'] == 'WeirdAuthCode'


def test_auth_report_suggest_patch_outputs_candidates(monkeypatch, capsys):
    rows = [
        AuthEventSummary(
            code='SessionRevoked',
            msg='session revoked',
            count=2,
            last_seen_at='2026-04-08T01:00:00+08:00',
            accounts=['main'],
            mapped=False,
            matched_by='unmapped',
        )
    ]
    monkeypatch.setattr(cli, 'load_settings', lambda path: object())
    monkeypatch.setattr(cli, 'create_store', lambda settings: DummyAuthStore(rows))

    code = cli.main(['debug', 'auth', '--config', 'config.yaml', '--json', '--suggest-patch'])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert 'sessionrevoked' in payload['suggested_patch']
    assert 'session revoked' in payload['suggested_patch']


def test_auth_report_only_likely_auth_filters_noise(monkeypatch, capsys):
    rows = [
        AuthEventSummary(
            code='Unauthorized',
            msg='token expired',
            count=3,
            last_seen_at='2026-04-08T01:00:00+08:00',
            accounts=['main'],
            mapped=True,
            matched_by='message',
        ),
        AuthEventSummary(
            code='InsufficientBalance',
            msg='balance not enough',
            count=9,
            last_seen_at='2026-04-08T03:00:00+08:00',
            accounts=['main'],
            mapped=False,
            matched_by='unmapped',
        ),
    ]
    monkeypatch.setattr(cli, 'load_settings', lambda path: object())
    monkeypatch.setattr(cli, 'create_store', lambda settings: DummyAuthStore(rows))

    code = cli.main(['debug', 'auth', '--config', 'config.yaml', '--only-likely-auth', '--json'])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert len(payload['rows']) == 1
    assert payload['rows'][0]['code'] == 'Unauthorized'


def test_auth_report_apply_suggested_patch_is_disabled(monkeypatch, capsys):
    rows = [
        AuthEventSummary(
            code='SessionRevoked',
            msg='session revoked',
            count=2,
            last_seen_at='2026-04-08T01:00:00+08:00',
            accounts=['main'],
            mapped=False,
            matched_by='unmapped',
        )
    ]
    monkeypatch.setattr(cli, 'load_settings', lambda path: object())
    monkeypatch.setattr(cli, 'create_store', lambda settings: DummyAuthStore(rows))

    code = cli.main(['debug', 'auth', '--config', 'config.yaml', '--apply-suggested-patch'])
    captured = capsys.readouterr()

    assert code == 2
    assert '已禁用' in captured.err


def test_apply_auth_signal_patch_updates_target_file(tmp_path, monkeypatch):
    target = tmp_path / 'auth_error_signals.py'
    target.write_text(
        'AUTH_CODE_SIGNALS = {\n'
        '    "unauthorized",\n'
        '}\n\n'
        'AUTH_MESSAGE_SIGNALS = (\n'
        '    "login",\n'
        ')\n'
    )
    rows = [
        AuthEventSummary(
            code='SessionRevoked',
            msg='session revoked',
            count=2,
            last_seen_at='2026-04-08T01:00:00+08:00',
            accounts=['main'],
            mapped=False,
            matched_by='unmapped',
        )
    ]
    monkeypatch.setattr(cli, 'AUTH_ERROR_SIGNALS_FILE', target, raising=False)

    code_count, message_count, file_path = cli._apply_auth_signal_patch(rows)

    content = target.read_text()
    assert code_count == 1
    assert message_count == 1
    assert file_path == str(target)
    assert '"sessionrevoked"' in content
    assert '"session revoked"' in content
