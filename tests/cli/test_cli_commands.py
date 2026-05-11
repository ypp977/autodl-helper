from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import pytest

import autodl_helper.cli.app as cli
import autodl_helper.cli.commands as cli_backend
from autodl_helper.core.config import (
    AccountSettings,
    AuthSettings,
    EmailSettings,
    KeeperSettings,
    NotificationChannelSettings,
    NotificationSettings,
    ScheduledStartJob,
    ScheduledStartSelector,
    ScheduledStartSettings,
    Settings,
    TaskSettings,
)
from autodl_helper.core.models import HistoryRecord


class DummyClient:
    def __init__(self, instances=None):
        self._instances = instances or []

    def list_instances(self, page=1, page_size=100):
        return self._instances


BASE_SETTINGS = Settings(
    auth=AuthSettings(authorization='Bearer token'),
    tasks=TaskSettings(
        keeper=KeeperSettings(enabled=True, interval_minutes=30),
        scheduled_start=ScheduledStartSettings(
            enabled=True,
            poll_interval_seconds=300,
            jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='14:00', advance_hours=2)],
        ),
    ),
)


def test_main_requires_subcommand(capsys):
    code = cli.main([])
    captured = capsys.readouterr()

    assert code == 2
    assert 'usage:' in captured.err.lower()


def test_unknown_flat_command_is_not_registered():
    assert cli.main(['legacy-flat-command']) == 2



def test_run_all_subcommand_invokes_variant_runner(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_command_run_variant', lambda args, mode: calls.append((args.command, mode)) or 0)

    code = cli.main(['run', 'daemon', '--run-once', '--config', 'config.yaml'])

    assert code == 0
    assert calls == [('run', 'all')]


def test_run_keeper_subcommand_invokes_variant_runner(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_command_run_variant', lambda args, mode: calls.append((args.command, mode)) or 0)

    code = cli.main(['run', 'keeper', '--run-once', '--config', 'config.yaml'])

    assert code == 0
    assert calls == [('run', 'keeper')]


def test_run_scheduled_start_subcommand_invokes_variant_runner(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_command_run_variant', lambda args, mode: calls.append((args.command, mode)) or 0)

    code = cli.main(['run', 'scheduled', '--run-once', '--config', 'config.yaml'])

    assert code == 0
    assert calls == [('run', 'scheduled_start')]


def test_login_command_delegates_to_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_command_login', lambda args: calls.append((args.command, args.account, args.all)) or 0)

    code = cli.main(['login', '--config', 'config.yaml', '--account', 'main'])

    assert code == 0
    assert calls == [('login', 'main', False)]


def test_accounts_command_delegates_to_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_command_accounts', lambda args: calls.append((args.command, args.account, args.json)) or 0)

    code = cli.main(['accounts', '--config', 'config.yaml', '--account', 'main', '--json'])

    assert code == 0
    assert calls == [('accounts', 'main', True)]


def test_init_command_delegates_to_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_command_init', lambda args: calls.append((args.command, args.config, args.force, args.yes)) or 0)

    code = cli.main(['init', '--config', 'config.yaml'])

    assert code == 0
    assert calls == [('init', 'config.yaml', False, False)]



def test_healthcheck_command_delegates_to_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_command_healthcheck', lambda args: calls.append(args.command) or 0)

    code = cli.main(['debug', 'health', '--config', 'config.yaml'])

    assert code == 0
    assert calls == ['debug']


def test_service_install_command_delegates_to_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_command_service_install', lambda args: calls.append(args.command) or 0)

    code = cli.main(['service', 'install', '--config', 'config.yaml'])

    assert code == 0
    assert calls == ['service']


def test_service_status_command_delegates_to_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_command_service_status', lambda args: calls.append(args.command) or 0)

    code = cli.main(['service', 'status', '--config', 'config.yaml'])

    assert code == 0
    assert calls == ['service']


def test_main_configures_logging_to_stdout(monkeypatch):
    seen = {}

    def fake_basic_config(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(cli.logging, 'basicConfig', fake_basic_config)
    monkeypatch.setattr(cli, '_command_service_status', lambda args: 0)

    code = cli.main(['service', 'status', '--config', 'config.yaml'])

    assert code == 0
    assert seen['stream'] is sys.stdout


def test_main_sets_apscheduler_logger_to_warning(monkeypatch):
    seen = []

    monkeypatch.setattr(cli, '_command_service_status', lambda args: 0)

    def fake_set_level(level):
        seen.append(level)

    monkeypatch.setattr(cli.logging.getLogger('apscheduler'), 'setLevel', fake_set_level)

    code = cli.main(['service', 'status', '--config', 'config.yaml'])

    assert code == 0
    assert logging.WARNING in seen



def test_list_instances_outputs_json(monkeypatch, capsys):
    instances = [
        {
            'uuid': 'iid-1',
            'instance_name': 'alpha',
            'region_name': '北京A区',
            'status': 'running',
            'machine_alias': '926机',
            'snapshot_gpu_alias_name': 'RTX 3080 Ti',
            'gpu_all_num': 8,
            'charge_type': 'payg',
            'release_at': '2026-04-08 10:00:00',
            'status_at': '2026-04-07T10:00:00+08:00',
        }
    ]
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_client', lambda settings, headed: DummyClient(instances))

    code = cli.main(['list', '--config', 'config.yaml', '--json'])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert payload[0]['instance_id'] == 'iid-1'
    assert payload[0]['name'] == 'alpha'
    assert payload[0]['spec'] == 'RTX 3080 Ti * 8卡'


def test_login_command_refreshes_selected_account(monkeypatch, capsys, tmp_path):
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        accounts=[AccountSettings(name='main', enabled=True, authorization='', autodl_phone='1', autodl_password='2')],
        tasks=BASE_SETTINGS.tasks,
    )
    monkeypatch.setattr(cli, 'load_settings', lambda path: settings)
    monkeypatch.setattr(cli, 'create_store', lambda settings: cli.SQLiteStore(tmp_path / 'data.db'))
    seen = []
    monkeypatch.setattr(
        cli,
        'resolve_authorization',
        lambda auth_settings, headed=False, force_refresh=False, store=None, account_name='default': seen.append((account_name, force_refresh)) or 'Bearer fresh',
    )

    code = cli.main(['login', '--config', 'config.yaml', '--account', 'main'])
    captured = capsys.readouterr()

    assert code == 0
    assert seen == [('main', True)]
    assert '登录成功' in captured.out



def test_list_instances_outputs_table(monkeypatch, capsys):
    instances = [
        {
            'uuid': 'iid-1',
            'instance_name': 'alpha',
            'region_name': '北京A区',
            'status': 'running',
            'machine_alias': 'RTX 2080 Ti * 1卡',
            'charge_type': 'payg',
            'release_at': '2026-04-08 10:00:00',
            'status_at': '2026-04-07T10:00:00+08:00',
        }
    ]
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_client', lambda settings, headed: DummyClient(instances))

    code = cli.main(['list', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert 'instance_id' in captured.out
    assert 'iid-1' in captured.out
    assert 'alpha' in captured.out


def test_history_outputs_compact_table(monkeypatch, capsys):
    class DummyStore:
        def read_history(self, **kwargs):
            return [
                HistoryRecord(
                    created_at='2026-04-08T10:00:00+08:00',
                    account_name='main',
                    task_type='scheduled_start',
                    event_type='scheduled.wait.gpu',
                    severity='warning',
                    result='waiting_for_gpu',
                    reason='no_eligible_candidate',
                    instance_id='iid-1',
                    payload={'target_time': '14:00', 'deadline': '12:00'},
                    summary='candidate summary',
                )
            ]

    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: DummyStore())

    code = cli.main(['debug', 'history', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert 'created_at' in captured.out
    assert 'account' in captured.out
    assert 'subject' in captured.out
    assert 'summary' in captured.out
    assert 'result' not in captured.out
    assert 'reason' not in captured.out
    assert 'candidate summary' in captured.out


def test_validate_config_rejects_missing_jobs():
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(enabled=True, poll_interval_seconds=300, jobs=[]),
        ),
    )

    errors = cli.validate_settings(settings)

    assert any('jobs' in err for err in errors)



def test_validate_config_rejects_missing_auth_and_password_combo():
    settings = Settings(
        auth=AuthSettings(authorization='', autodl_phone='', autodl_password=''),
        tasks=TaskSettings(),
    )

    errors = cli.validate_settings(settings)

    assert any('AUTODL_PHONE' in err for err in errors)



def test_validate_config_rejects_invalid_job_time_and_notification_fields():
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        notifications=NotificationSettings(
            pushplus=NotificationChannelSettings(enabled=True, token=''),
            email=EmailSettings(enabled=True, smtp_host='', username='user@example.com', password='', to=[]),
        ),
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=4,
                jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='25:61', advance_hours=0)],
            ),
        ),
    )

    errors = cli.validate_settings(settings)

    assert any('HH:MM' in err for err in errors)
    assert any('advance_hours' in err for err in errors)
    assert any('poll_interval_seconds' in err for err in errors)
    assert any('pushplus' in err.lower() for err in errors)
    assert any('email' in err.lower() for err in errors)


def test_validate_config_rejects_job_with_both_instance_id_and_selector():
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=5,
                jobs=[
                    ScheduledStartJob(
                        instance_id='iid-1',
                        name='job-1',
                        selector=ScheduledStartSelector(gpu_model='RTX 3080 Ti', gpu_count=1),
                    )
                ],
            ),
        ),
    )

    errors = cli.validate_settings(settings)

    assert any('exactly one' in err for err in errors)



def test_validate_config_command_prints_errors(monkeypatch, capsys):
    settings = Settings(auth=AuthSettings())
    monkeypatch.setattr(cli, 'load_settings', lambda path: settings)

    code = cli.main(['config', 'validate', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 1
    assert 'configuration invalid' in captured.err.lower()


def test_validate_config_allows_five_second_scheduled_polling():
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=5,
                jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='14:00', advance_hours=1)],
            ),
        ),
    )

    errors = cli.validate_settings(settings)

    assert not any('poll_interval_seconds' in err for err in errors)


def test_validate_config_rejects_polling_below_five_seconds():
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=4,
                jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='14:00', advance_hours=1)],
            ),
        ),
    )

    errors = cli.validate_settings(settings)

    assert any('poll_interval_seconds' in err for err in errors)


def test_validate_config_rejects_invalid_lightweight_mode():
    settings = Settings(
        accounts=[
            AccountSettings(
                name='main',
                enabled=True,
                authorization='Bearer token',
                lightweight_mode='turbo',
            )
        ],
        auth=AuthSettings(authorization='Bearer token'),
    )

    errors = cli.validate_settings(settings)

    assert any('lightweight_mode' in err for err in errors)


def test_validate_config_rejects_keeper_window_misconfiguration():
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        tasks=TaskSettings(
            keeper=KeeperSettings(
                enabled=True,
                shutdown_release_after_hours=360,
                keeper_trigger_before_hours=360,
            ),
        ),
    )

    errors = cli.validate_settings(settings)

    assert any('smaller than shutdown_release_after_hours' in err for err in errors)


def test_run_daemon_subcommand_invokes_variant_runner(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_command_run_variant', lambda args, mode: calls.append((args.command, mode)) or 0)

    code = cli.main(['run', 'daemon', '--run-once', '--config', 'config.yaml'])

    assert code == 0
    assert calls == [('run', 'all')]


def test_start_background_scheduled_polling_returns_stderr_reason_on_failure(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('auth:\n  authorization: Bearer token\n', encoding='utf-8')
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()

    class FakeProc:
        pid = 4321
        returncode = 1

        def poll(self):
            return self.returncode

    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'read_daemon_status', lambda store: {'running': False, 'pid': None})

    def fake_popen(cmd, **kwargs):
        Path(kwargs['stderr'].name).write_text('validation failed\nmissing config\n', encoding='utf-8')
        return FakeProc()

    monkeypatch.setattr(cli.subprocess, 'Popen', fake_popen)

    args = argparse.Namespace(
        config=str(config_path),
        lock_file=str(tmp_path / '.autodl-helper.lock'),
        state_file=str(tmp_path / '.autodl-helper-state.json'),
        account='main',
        headed=False,
    )

    code, detail = cli.start_background_scheduled_polling(args)

    assert code == 1
    assert 'missing config' in detail


def test_start_background_scheduled_polling_resolves_relative_paths_from_config_dir(monkeypatch, tmp_path):
    config_dir = tmp_path / 'cfg'
    config_dir.mkdir()
    config_path = config_dir / 'config.yaml'
    config_path.write_text('auth:\n  authorization: Bearer token\n', encoding='utf-8')
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()

    captured = {}

    class FakeProc:
        pid = 4321

        def poll(self):
            return None

    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'read_daemon_status', lambda store: {'running': True, 'pid': 4321})

    def fake_popen(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['cwd'] = kwargs['cwd']
        return FakeProc()

    monkeypatch.setattr(cli.subprocess, 'Popen', fake_popen)

    args = argparse.Namespace(
        config=str(config_path),
        lock_file='.autodl-helper.lock',
        state_file='.autodl-helper-state.json',
        account='main',
        headed=False,
    )

    code, detail = cli.start_background_scheduled_polling(args)

    assert code == 0
    assert 'pid=4321' in detail
    assert str(config_dir) == captured['cwd']
    assert str(config_dir / '.autodl-helper.lock') in captured['cmd']
    assert str(config_dir / '.autodl-helper-state.json') in captured['cmd']


def test_start_background_scheduled_polling_reuses_running_launch(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('auth:\n  authorization: Bearer token\n', encoding='utf-8')
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    pid = os.getpid()
    cli.mark_daemon_heartbeat(store, mode='scheduled_start', pid=pid, account='main', origin='interactive-auto')
    cli.mark_daemon_launch_running(store, account='main', pid=pid)
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli.subprocess, 'Popen', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('should not spawn')))

    args = argparse.Namespace(
        config=str(config_path),
        lock_file=str(tmp_path / '.autodl-helper.lock'),
        state_file=str(tmp_path / '.autodl-helper-state.json'),
        account='main',
        headed=False,
    )

    code, detail = cli.start_background_scheduled_polling(args)

    assert code == 0
    assert 'already running' in detail


def test_start_background_scheduled_polling_reuses_starting_launch(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('auth:\n  authorization: Bearer token\n', encoding='utf-8')
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    cli.claim_daemon_launch(store, account='main', starting_ttl_seconds=10)
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli.subprocess, 'Popen', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('should not spawn')))

    args = argparse.Namespace(
        config=str(config_path),
        lock_file=str(tmp_path / '.autodl-helper.lock'),
        state_file=str(tmp_path / '.autodl-helper-state.json'),
        account='main',
        headed=False,
    )

    code, detail = cli.start_background_scheduled_polling(args)

    assert code == 0
    assert '启动中' in detail


def test_start_background_scheduled_polling_enters_fused_state_after_repeated_failures(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('auth:\n  authorization: Bearer token\n', encoding='utf-8')
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'read_daemon_status', lambda store: {'running': False, 'pid': None})
    popen_calls = []

    class FakeProc:
        pid = 4321
        returncode = 1

        def poll(self):
            return self.returncode

    def fake_popen(cmd, **kwargs):
        popen_calls.append(cmd)
        Path(kwargs['stderr'].name).write_text('boom\n', encoding='utf-8')
        return FakeProc()

    monkeypatch.setattr(cli.subprocess, 'Popen', fake_popen)

    args = argparse.Namespace(
        config=str(config_path),
        lock_file=str(tmp_path / '.autodl-helper.lock'),
        state_file=str(tmp_path / '.autodl-helper-state.json'),
        account='main',
        headed=False,
    )

    first = cli.start_background_scheduled_polling(args)
    second = cli.start_background_scheduled_polling(args)
    third = cli.start_background_scheduled_polling(args)
    fourth = cli.start_background_scheduled_polling(args)

    assert first[0] == 1
    assert second[0] == 1
    assert third[0] == 1
    assert '熔断' in fourth[1]
    assert len(popen_calls) == 3


def test_db_check_command_delegates_to_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_command_db_check', lambda args: calls.append(args.command) or 0)

    code = cli.main(['debug', 'db', '--config', 'config.yaml'])

    assert code == 0
    assert calls == ['debug']


def test_history_command_delegates_to_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_command_history', lambda args: calls.append(args.command) or 0)

    code = cli.main(['debug', 'history', '--config', 'config.yaml'])

    assert code == 0
    assert calls == ['debug']


def test_history_command_accepts_json_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_command_history', lambda args: calls.append((args.command, args.json)) or 0)

    code = cli.main(['debug', 'history', '--config', 'config.yaml', '--json'])

    assert code == 0
    assert calls == [('debug', True)]


def test_auth_report_command_delegates_to_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        '_command_auth_report',
        lambda args: calls.append((args.command, args.json, args.only_unmapped, args.only_likely_auth, args.suggest_patch, args.apply_suggested_patch)) or 0,
    )

    code = cli.main(['debug', 'auth', '--config', 'config.yaml', '--json', '--only-unmapped', '--only-likely-auth', '--suggest-patch'])

    assert code == 0
    assert calls == [('debug', True, True, True, True, False)]


def test_run_scheduled_start_accepts_cli_overrides(monkeypatch):
    captured = {}

    class DummyLock:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token', lightweight_mode='off')],
        tasks=TaskSettings(
            keeper=KeeperSettings(enabled=True),
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=300,
                jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='14:00', advance_hours=2)],
            ),
        ),
    )

    monkeypatch.setattr(cli, 'load_settings', lambda path: settings)
    monkeypatch.setattr(cli, 'FileLock', DummyLock)
    monkeypatch.setattr(cli, 'validate_settings', lambda settings, purpose='all': [])
    monkeypatch.setattr(cli, 'run_scheduled_start_cycle', lambda **kwargs: captured.update(kwargs) or [])

    code = cli.main([
        'run',
        'scheduled',
        '--run-once',
        '--config',
        'config.yaml',
        '--scheduled-poll-interval',
        '5',
        '--scheduled-job',
        'job-1',
        '--target-time',
        '15:30',
        '--advance-hours',
        '1',
        '--lightweight-mode',
        'normal',
    ])

    assert code == 0
    effective = captured['settings']
    assert effective.tasks.scheduled_start.poll_interval_seconds == 5
    assert len(effective.tasks.scheduled_start.jobs) == 1
    assert effective.tasks.scheduled_start.jobs[0].name == 'job-1'
    assert effective.tasks.scheduled_start.jobs[0].target_time == '15:30'
    assert effective.tasks.scheduled_start.jobs[0].advance_hours == 1
    assert effective.accounts[0].lightweight_mode == 'normal'


def test_run_keeper_accepts_keeper_cli_overrides(monkeypatch):
    captured = {}

    class DummyLock:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token', lightweight_mode='off')],
        tasks=TaskSettings(keeper=KeeperSettings(enabled=True)),
    )

    monkeypatch.setattr(cli, 'load_settings', lambda path: settings)
    monkeypatch.setattr(cli, 'FileLock', DummyLock)
    monkeypatch.setattr(cli, 'validate_settings', lambda settings, purpose='all': [])
    monkeypatch.setattr(cli, 'run_keeper_only', lambda **kwargs: captured.update(kwargs) or [])

    code = cli.main([
        'run',
        'keeper',
        '--run-once',
        '--config',
        'config.yaml',
        '--shutdown-release-after-hours',
        '240',
        '--keeper-trigger-before-hours',
        '12',
        '--start-cooldown-minutes',
        '30',
        '--stop-cooldown-minutes',
        '180',
        '--no-fallback-to-status-at',
    ])

    assert code == 0
    effective = captured['settings']
    assert effective.tasks.keeper.shutdown_release_after_hours == 240
    assert effective.tasks.keeper.keeper_trigger_before_hours == 12
    assert effective.tasks.keeper.start_cooldown_minutes == 30
    assert effective.tasks.keeper.stop_cooldown_minutes == 180
    assert effective.tasks.keeper.fallback_to_status_at is False


def test_config_show_outputs_loaded_config(monkeypatch, capsys):
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token', autodl_password='secret'),
        accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token', autodl_password='secret', lightweight_mode='off')],
        tasks=TaskSettings(
            keeper=KeeperSettings(enabled=True, shutdown_release_after_hours=360),
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=300,
                jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='14:00', advance_hours=2)],
            ),
        ),
    )
    monkeypatch.setattr(cli, 'load_settings', lambda path: settings)

    code = cli.main(['config', 'show', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert payload['tasks']['keeper']['shutdown_release_after_hours'] == 360
    assert payload['accounts'][0]['lightweight_mode'] == 'off'
    assert payload['auth']['authorization'] == '<redacted>'
    assert payload['auth']['autodl_password'] == '<redacted>'


def test_command_config_edit_requests_reload_after_success(tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: "main"',
            '    enabled: true',
            '    authorization: "Bearer token"',
            'tasks:',
            '  keeper:',
            '    shutdown_release_after_hours: 360',
            '    keeper_trigger_before_hours: 6',
            '    start_cooldown_minutes: 60',
            '    stop_cooldown_minutes: 360',
            '    fallback_to_status_at: true',
            '  scheduled_start:',
            '    enabled: false',
            '    poll_interval_seconds: 300',
            '    jobs: []',
        ]),
        encoding='utf-8',
    )
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    args = argparse.Namespace(
        command='config-edit',
        config=str(config_path),
        account=None,
        scheduled_job=None,
        target_time=None,
        advance_hours=None,
        scheduled_poll_interval=5,
        shutdown_release_after_hours=None,
        keeper_trigger_before_hours=None,
        start_cooldown_minutes=None,
        stop_cooldown_minutes=None,
        fallback_to_status_at=None,
        lightweight_mode=None,
        runtime_auth_revalidate_seconds=None,
        force_refresh_min_interval_seconds=None,
        auth_failure_backoff_seconds=None,
    )

    code = cli_backend.command_config_edit(
        args,
        load_settings_fn=cli.load_settings,
        create_store_fn=lambda settings: store,
        request_reload_fn=cli.request_reload,
    )

    assert code == 0
    assert store.get_runtime_value('config_generation') == '1'
