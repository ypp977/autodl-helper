from pathlib import Path
import os

import pytest

from autodl_helper.core import config


def test_load_settings_reads_yaml_jobs(tmp_path, monkeypatch):
    monkeypatch.delenv('MIN_DAY', raising=False)
    monkeypatch.delenv('AUTODL_PHONE', raising=False)
    monkeypatch.delenv('AUTODL_PASSWORD', raising=False)
    monkeypatch.chdir(tmp_path)
    yaml_path = tmp_path / 'config.yaml'
    yaml_path.write_text(
        '\n'.join([
            'tasks:',
            '  scheduled_start:',
            '    enabled: true',
            '    poll_interval_seconds: 120',
            '    jobs:',
            '      - instance_id: abc',
            '        name: gpu-1',
            '        target_time: "14:00"',
            '        advance_hours: 2',
            '        timezone: Asia/Shanghai',
        ])
    )
    monkeypatch.setenv('Authorization', 'Bearer token')

    settings = config.load_settings(yaml_path)

    assert settings.auth.authorization == 'Bearer token'
    assert settings.auth.cache_file == str((tmp_path / '.autodl-helper-auth.json').resolve())
    assert settings.tasks.scheduled_start.jobs[0].instance_id == 'abc'
    assert settings.tasks.scheduled_start.poll_interval_seconds == 120
    assert settings.tasks.scheduled_start.jobs[0].schedule_mode == 'daily'


def test_write_raw_settings_uses_restricted_permissions(tmp_path):
    config_path = tmp_path / 'config.yaml'

    config.write_raw_settings(config_path, {'auth': {'authorization': 'Bearer secret'}})

    assert os.stat(config_path).st_mode & 0o777 == 0o600
    assert 'Bearer secret' in config_path.read_text(encoding='utf-8')


def test_load_settings_reads_job_schedule_mode(tmp_path, monkeypatch):
    monkeypatch.delenv('MIN_DAY', raising=False)
    monkeypatch.delenv('AUTODL_PHONE', raising=False)
    monkeypatch.delenv('AUTODL_PASSWORD', raising=False)
    monkeypatch.chdir(tmp_path)
    yaml_path = tmp_path / 'config.yaml'
    yaml_path.write_text(
        '\n'.join([
            'tasks:',
            '  scheduled_start:',
            '    enabled: true',
            '    jobs:',
            '      - name: job-once',
            '        target_time: "14:00"',
            '        advance_hours: 2',
            '        schedule_mode: weekly',
            '        weekdays: [1, 3, 5]',
        ])
    )
    monkeypatch.setenv('Authorization', 'Bearer token')

    settings = config.load_settings(yaml_path)

    assert settings.tasks.scheduled_start.jobs[0].schedule_mode == 'weekly'
    assert settings.tasks.scheduled_start.jobs[0].weekdays == [1, 3, 5]


def test_load_settings_parses_string_booleans(tmp_path, monkeypatch):
    monkeypatch.delenv('Authorization', raising=False)
    yaml_path = tmp_path / 'config.yaml'
    yaml_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: "false"',
            '    authorization: Bearer token',
            'tasks:',
            '  keeper:',
            '    enabled: "0"',
            '    fallback_to_status_at: "no"',
            '  scheduled_start:',
            '    enabled: "yes"',
        ])
    )

    settings = config.load_settings(yaml_path)

    assert settings.accounts[0].enabled is False
    assert settings.tasks.keeper.enabled is False
    assert settings.tasks.keeper.fallback_to_status_at is False
    assert settings.tasks.scheduled_start.enabled is True


def test_load_settings_rejects_invalid_boolean_string(tmp_path, monkeypatch):
    monkeypatch.delenv('Authorization', raising=False)
    yaml_path = tmp_path / 'config.yaml'
    yaml_path.write_text(
        '\n'.join([
            'tasks:',
            '  keeper:',
            '    enabled: maybe',
        ])
    )

    with pytest.raises(ValueError, match='tasks.keeper.enabled'):
        config.load_settings(yaml_path)



def test_load_settings_defaults_keeper_when_yaml_missing(monkeypatch, tmp_path):
    monkeypatch.delenv('Authorization', raising=False)
    monkeypatch.delenv('MIN_DAY', raising=False)
    monkeypatch.delenv('AUTODL_PHONE', raising=False)
    monkeypatch.delenv('AUTODL_PASSWORD', raising=False)
    monkeypatch.chdir(tmp_path)
    settings = config.load_settings(tmp_path / 'missing.yaml')
    assert settings.tasks.keeper.min_day == 7
    assert settings.tasks.keeper.shutdown_release_after_hours == 360
    assert settings.tasks.keeper.keeper_trigger_before_hours == 6
    assert settings.tasks.keeper.interval_minutes == 60
    assert settings.tasks.keeper.start_cooldown_minutes == 60
    assert settings.tasks.keeper.stop_cooldown_minutes == 360
    assert settings.tasks.keeper.fallback_to_status_at is True


def test_load_settings_reads_keeper_extended_options(tmp_path, monkeypatch):
    monkeypatch.delenv('MIN_DAY', raising=False)
    yaml_path = tmp_path / 'config.yaml'
    yaml_path.write_text(
        '\n'.join([
            'tasks:',
            '  keeper:',
            '    shutdown_release_after_hours: 240',
            '    keeper_trigger_before_hours: 12',
            '    start_cooldown_minutes: 30',
            '    stop_cooldown_minutes: 180',
            '    fallback_to_status_at: false',
        ])
    )

    settings = config.load_settings(yaml_path)

    assert settings.tasks.keeper.shutdown_release_after_hours == 240
    assert settings.tasks.keeper.keeper_trigger_before_hours == 12
    assert settings.tasks.keeper.start_cooldown_minutes == 30
    assert settings.tasks.keeper.stop_cooldown_minutes == 180
    assert settings.tasks.keeper.fallback_to_status_at is False


def test_load_settings_reads_auth_lightweight_options(tmp_path, monkeypatch):
    monkeypatch.delenv('Authorization', raising=False)
    yaml_path = tmp_path / 'config.yaml'
    yaml_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    authorization: Bearer one',
            '    lightweight_mode: aggressive',
        ])
    )

    settings = config.load_settings(yaml_path)

    assert settings.accounts[0].lightweight_mode == 'aggressive'
    assert settings.accounts[0].runtime_auth_revalidate_seconds == 0
    assert settings.accounts[0].force_refresh_min_interval_seconds == 0
    assert settings.accounts[0].auth_failure_backoff_seconds == 0
    assert settings.auth.lightweight_mode == 'aggressive'


def test_load_settings_reads_selector_priority_and_auth_cache_options(tmp_path, monkeypatch):
    monkeypatch.delenv('Authorization', raising=False)
    yaml_path = tmp_path / 'config.yaml'
    yaml_path.write_text(
        '\n'.join([
            'auth:',
            '  cache_file: ".cache/auth.json"',
            '  cache_max_age_seconds: 123',
            'tasks:',
            '  scheduled_start:',
            '    enabled: true',
            '    jobs:',
            '      - name: selector-job',
            '        selector:',
            '          regions: ["北京A区"]',
            '          gpu_model: "RTX 3080 Ti"',
            '          gpu_count: 1',
            '          charge_types: ["payg"]',
            '        priority:',
            '          - region: "北京A区"',
            '            machine_alias: "351机"',
        ])
    )

    settings = config.load_settings(yaml_path)

    job = settings.tasks.scheduled_start.jobs[0]
    assert settings.auth.cache_file == str((tmp_path / '.cache/auth.json').resolve())
    assert settings.auth.cache_max_age_seconds == 123
    assert job.selector is not None
    assert job.selector.gpu_model == 'RTX 3080 Ti'
    assert job.priority[0].machine_alias == '351机'


def test_load_settings_supports_multi_accounts_and_storage(tmp_path, monkeypatch):
    monkeypatch.delenv('Authorization', raising=False)
    yaml_path = tmp_path / 'config.yaml'
    yaml_path.write_text(
        '\n'.join([
            'storage:',
            '  database_file: data/custom.db',
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer one',
            '  - name: backup',
            '    enabled: true',
            '    autodl_phone: 18200000000',
            '    autodl_password: secret',
        ])
    )

    settings = config.load_settings(yaml_path)

    assert [account.name for account in settings.accounts] == ['main', 'backup']
    assert settings.storage.database_file == str((tmp_path / 'data/custom.db').resolve())
    assert settings.accounts[0].cache_file == str((tmp_path / '.cache/main-auth.json').resolve())
    assert settings.accounts[1].cache_file == str((tmp_path / '.cache/backup-auth.json').resolve())
    assert settings.auth.authorization == 'Bearer one'
