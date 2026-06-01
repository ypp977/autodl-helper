from types import SimpleNamespace
from io import StringIO
from datetime import datetime, timezone

import autodl_helper.cli.app as cli
import autodl_helper.cli.commands.runtime as runtime_commands
import autodl_helper.cli.commands.service as service_commands
import autodl_helper.cli.commands as cli_backend
from autodl_helper.runtime_control import mark_task_run
from autodl_helper.core.config import EmailSettings, KeeperSettings, NotificationChannelSettings, NotificationSettings, ScheduledStartJob, ScheduledStartSettings, Settings, TaskSettings


class DummyClient:
    def __init__(self, instances=None):
        self.instances = instances or []
        self.calls = 0

    def list_instances(self, page=1, page_size=100):
        self.calls += 1
        if self.instances and isinstance(self.instances[0], list):
            index = min(self.calls - 1, len(self.instances) - 1)
            return self.instances[index]
        return self.instances


def test_build_notifiers_creates_enabled_backends():
    notifications = NotificationSettings(
        pushplus=NotificationChannelSettings(enabled=True, token='pp-token'),
        email=EmailSettings(enabled=True, smtp_host='smtp.qq.com', smtp_port=465, username='a@example.com', password='pwd', to=['b@example.com']),
    )
    built = build_notifiers(notifications)
    assert len(built) == 2
    assert built[0].__class__.__name__ == 'PushPlusNotifier'
    assert built[1].__class__.__name__ == 'EmailNotifier'



def test_compute_cycle_interval_prefers_faster_scheduled_polling():
    settings = Settings(
        tasks=TaskSettings(
            keeper=KeeperSettings(enabled=True, interval_minutes=60),
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=300,
                jobs=[ScheduledStartJob(instance_id='iid')],
            ),
        )
    )
    assert compute_cycle_interval_seconds(settings) == 300


def test_compute_cycle_interval_allows_five_second_scheduled_polling():
    settings = Settings(
        tasks=TaskSettings(
            keeper=KeeperSettings(enabled=True, interval_minutes=60),
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=5,
                jobs=[ScheduledStartJob(instance_id='iid')],
            ),
        )
    )
    assert compute_cycle_interval_seconds(settings) == 5



def test_compute_cycle_interval_uses_keeper_when_scheduled_disabled():
    settings = Settings(tasks=TaskSettings(keeper=KeeperSettings(enabled=True, interval_minutes=15), scheduled_start=ScheduledStartSettings(enabled=False)))
    assert compute_cycle_interval_seconds(settings) == 900


build_notifiers = cli.build_notifiers
compute_cycle_interval_seconds = cli.compute_cycle_interval_seconds
compute_dispatch_interval_seconds = cli.compute_dispatch_interval_seconds


def test_compute_dispatch_interval_uses_sixty_seconds_for_keeper_only_idle_loop():
    settings = Settings(
        tasks=TaskSettings(
            keeper=KeeperSettings(enabled=True, interval_minutes=15),
            scheduled_start=ScheduledStartSettings(enabled=False),
        )
    )
    assert compute_dispatch_interval_seconds(settings) == 60


def test_compute_dispatch_interval_keeps_fast_scheduled_polling_when_configured():
    settings = Settings(
        tasks=TaskSettings(
            keeper=KeeperSettings(enabled=True, interval_minutes=60),
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=5,
                jobs=[ScheduledStartJob(instance_id='iid')],
            ),
        )
    )
    assert compute_dispatch_interval_seconds(settings) == 5


def test_compute_dispatch_interval_caps_slow_scheduled_polling_at_one_minute():
    settings = Settings(
        tasks=TaskSettings(
            keeper=KeeperSettings(enabled=True, interval_minutes=60),
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=300,
                jobs=[ScheduledStartJob(instance_id='iid')],
            ),
        )
    )
    assert compute_dispatch_interval_seconds(settings) == 60


def test_run_cycle_executes_all_scheduled_jobs(monkeypatch, tmp_path):
    keeper_calls = []
    scheduled_jobs = []

    def fake_create_client(settings, headed):
        return DummyClient()

    def fake_keeper_cycle(**kwargs):
        keeper_calls.append(kwargs)
        return []

    def fake_scheduled_job(**kwargs):
        scheduled_jobs.append(kwargs['job'].job_name)
        return cli.ScheduledStartResult(
            result='outside_window',
            reason='outside_window',
            instance_id=kwargs['job'].instance_id,
            status='',
            gpu_idle_num=None,
            start_mode='',
            target_time=kwargs['job'].target_time,
            deadline='2026-04-07T14:00:00+08:00',
        )

    monkeypatch.setattr(cli, 'create_client', fake_create_client)
    monkeypatch.setattr(cli, 'run_keeper_cycle', fake_keeper_cycle)
    monkeypatch.setattr(cli, 'run_scheduled_start_job', fake_scheduled_job)

    settings = Settings(
        tasks=TaskSettings(
            keeper=KeeperSettings(enabled=True),
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=300,
                jobs=[
                    ScheduledStartJob(instance_id='iid-1', name='job-1'),
                    ScheduledStartJob(instance_id='iid-2', name='job-2'),
                ],
            ),
        )
    )

    cli.run_cycle(settings=settings, headed=False, state_file=tmp_path / 'state.json')

    assert len(keeper_calls) == 1
    assert scheduled_jobs == ['job-1', 'job-2']


def test_run_cycle_respects_runtime_task_pause(monkeypatch, tmp_path):
    keeper_calls = []
    scheduled_jobs = []

    def fake_create_client(settings, headed, account=None, store=None):
        return DummyClient()

    def fake_keeper_cycle(**kwargs):
        keeper_calls.append(kwargs)
        return []

    def fake_scheduled_job(**kwargs):
        scheduled_jobs.append(kwargs['job'].job_name)
        return cli.ScheduledStartResult(
            result='outside_window',
            reason='outside_window',
            instance_id=kwargs['job'].instance_id,
            status='',
            gpu_idle_num=None,
            start_mode='',
            target_time=kwargs['job'].target_time,
            deadline='2026-04-07T14:00:00+08:00',
        )

    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.set_task_control('default', 'scheduled_start', enabled=False, source='interactive')

    monkeypatch.setattr(cli, 'create_client', fake_create_client)
    monkeypatch.setattr(cli, 'run_keeper_cycle', fake_keeper_cycle)
    monkeypatch.setattr(cli, 'run_scheduled_start_job', fake_scheduled_job)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)

    settings = Settings(
        tasks=TaskSettings(
            keeper=KeeperSettings(enabled=True),
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=300,
                jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1')],
            ),
        )
    )

    cli.run_cycle(settings=settings, headed=False, state_file=tmp_path / 'state.json')

    assert len(keeper_calls) == 1
    assert scheduled_jobs == []


def test_run_scheduled_start_cycle_skips_job_after_success_in_same_window(monkeypatch, tmp_path):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2026, 4, 9, 0, 20)
            return base if tz is None else base.astimezone(tz)

    scheduled_jobs = []
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.add_scheduled_history(
        'main',
        'job-1',
        'iid-1',
        '2026-04-09',
        'already_running',
        'already_running',
        {'instance_id': 'iid-1', 'target_time': '00:30'},
        'scheduled.already_running',
        'success',
        '实例已在运行',
    )

    def fake_build_client(settings, headed, account=None, store=None):
        return DummyClient()

    def fake_scheduled_job(**kwargs):
        scheduled_jobs.append(kwargs['job'].job_name)
        return cli.ScheduledStartResult(
            result='outside_window',
            reason='outside_window',
            instance_id=kwargs['job'].instance_id,
            status='',
            gpu_idle_num=None,
            start_mode='',
            target_time=kwargs['job'].target_time,
            deadline='2026-04-09T00:30:00+08:00',
        )

    monkeypatch.setattr(runtime_commands, 'datetime', FixedDateTime)
    monkeypatch.setattr(cli, 'build_client', fake_build_client)
    monkeypatch.setattr(cli, 'run_scheduled_start_job', fake_scheduled_job)

    settings = Settings(
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=5,
                jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='00:30', advance_hours=1)],
            ),
        ),
        accounts=[cli.AccountSettings(name='main', enabled=True, authorization='Bearer token')],
    )

    cli.run_scheduled_start_cycle(
        settings=settings,
        headed=False,
        state_file=tmp_path / 'state.json',
        account_name='main',
        store=store,
    )

    assert scheduled_jobs == []


def test_run_scheduled_start_cycle_disables_once_job_after_success(monkeypatch, tmp_path):
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()

    def fake_build_client(settings, headed, account=None, store=None):
        return DummyClient()

    def fake_scheduled_job(**kwargs):
        return cli.ScheduledStartResult(
            result='already_running',
            reason='already_running',
            instance_id='iid-1',
            status='running',
            gpu_idle_num=1,
            start_mode='gpu',
            target_time=kwargs['job'].target_time,
            deadline='2026-04-09T00:30:00+08:00',
        )

    monkeypatch.setattr(cli, 'build_client', fake_build_client)
    monkeypatch.setattr(cli, 'run_scheduled_start_job', fake_scheduled_job)

    settings = Settings(
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=5,
                jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='00:30', advance_hours=1, schedule_mode='once')],
            ),
        ),
        accounts=[cli.AccountSettings(name='main', enabled=True, authorization='Bearer token')],
    )

    cli.run_scheduled_start_cycle(
        settings=settings,
        headed=False,
        state_file=tmp_path / 'state.json',
        account_name='main',
        store=store,
    )

    control = store.get_scheduled_job_control('main', 'job-1')
    assert control is not None
    assert control['enabled'] is False


def test_run_scheduled_start_cycle_skips_disabled_task_before_building_client(tmp_path):
    calls = []
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()

    def fail_build_client(**kwargs):
        calls.append(kwargs)
        raise AssertionError('disabled scheduled-start must not build client')

    settings = Settings(
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=False,
                poll_interval_seconds=5,
                jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1')],
            ),
        ),
        accounts=[cli.AccountSettings(name='main', enabled=True, authorization='Bearer token')],
    )

    results = runtime_commands.run_scheduled_start_cycle(
        settings=settings,
        headed=False,
        state_file=tmp_path / 'state.json',
        account_name='main',
        store=store,
        build_client_fn=fail_build_client,
    )

    assert results == []
    assert calls == []


def test_run_scheduled_start_cycle_passes_force_run_now(monkeypatch, tmp_path):
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    seen = []

    def fake_build_client(settings, headed, account=None, store=None):
        return DummyClient()

    def fake_scheduled_job(**kwargs):
        seen.append(kwargs.get('force_run_now'))
        return cli.ScheduledStartResult(
            result='waiting_for_instance',
            reason='selector_no_match',
            instance_id='',
            status='shutdown',
            gpu_idle_num=0,
            start_mode='',
            target_time=kwargs['job'].target_time,
            deadline='2026-04-09T00:30:00+08:00',
        )

    monkeypatch.setattr(cli, 'build_client', fake_build_client)
    monkeypatch.setattr(cli, 'run_scheduled_start_job', fake_scheduled_job)

    settings = Settings(
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=5,
                jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='00:30', advance_hours=1)],
            ),
        ),
        accounts=[cli.AccountSettings(name='main', enabled=True, authorization='Bearer token')],
    )

    cli.run_scheduled_start_cycle(
        settings=settings,
        headed=False,
        state_file=tmp_path / 'state.json',
        account_name='main',
        force_run_now=True,
        store=store,
    )

    assert seen == [True]


def test_run_scheduled_start_cycle_emits_key_logs(monkeypatch, tmp_path, caplog):
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()

    def fake_build_client(settings, headed, account=None, store=None):
        return DummyClient()

    def fake_scheduled_job(**kwargs):
        return cli.ScheduledStartResult(
            result='waiting_for_instance',
            reason='selector_no_match',
            instance_id='',
            status='shutdown',
            gpu_idle_num=0,
            start_mode='',
            target_time=kwargs['job'].target_time,
            deadline='2026-04-09T00:30:00+08:00',
        )

    monkeypatch.setattr(cli, 'build_client', fake_build_client)
    monkeypatch.setattr(cli, 'run_scheduled_start_job', fake_scheduled_job)

    settings = Settings(
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=5,
                jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='00:30', advance_hours=1)],
            ),
        ),
        accounts=[cli.AccountSettings(name='main', enabled=True, authorization='Bearer token')],
    )

    with caplog.at_level('INFO'):
        cli.run_scheduled_start_cycle(
            settings=settings,
            headed=False,
            state_file=tmp_path / 'state.json',
            account_name='main',
            store=store,
        )

    joined = '\n'.join(caplog.messages)
    assert '[抢机检查]' in joined
    assert '账号=main' in joined
    assert '任务=job-1' in joined
    assert '结果=等待' in joined
    assert '原因=暂无可用目标' in joined
    assert '当前窗口=23:30-00:30' in joined
    assert '下次检查=' in joined




def test_run_keeper_only_emits_window_fields(monkeypatch, tmp_path, caplog):
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()

    def fake_build_client(settings, headed, account=None, store=None):
        return DummyClient()

    monkeypatch.setattr(cli, 'build_client', fake_build_client)
    monkeypatch.setattr(
        cli,
        'run_keeper_cycle',
        lambda **kwargs: [
            cli.KeeperResult(
                instance_id='iid-keeper',
                status='shutdown',
                release_at='',
                release_source='stopped_at',
                started_at='',
                stopped_at='2026-04-09T00:00:00+08:00',
                status_at='',
                release_deadline='2026-04-24T00:00:00+08:00',
                next_keeper_time='2026-04-23T04:00:00+08:00',
                seconds_until_release=3600,
                seconds_until_keeper=1800,
                started_duration_seconds=None,
                shutdown_duration_seconds=100,
                eligible=False,
                result='skip_not_due',
                reason='before_next_keeper_time',
                response_code='',
                response_msg='',
                summary='',
            )
        ],
    )

    settings = Settings(
        tasks=TaskSettings(keeper=KeeperSettings(enabled=True, interval_minutes=720)),
        accounts=[cli.AccountSettings(name='main', enabled=True, authorization='Bearer token')],
    )

    with caplog.at_level('INFO'):
        cli.run_keeper_only(
            settings=settings,
            headed=False,
            account_name='main',
            store=store,
        )

    joined = '\n'.join(caplog.messages)
    assert '[Keeper检查]' in joined
    assert '结果=跳过' in joined
    assert '原因=未到保活窗口' in joined
    assert '下次保活=04-23 04:00:00' in joined
    assert '释放时间=04-24 00:00:00' in joined
    assert '接管窗口=04-23 04:00:00 ~ 04-24 00:00:00' in joined


def test_run_keeper_only_skips_disabled_task_before_building_client(tmp_path):
    calls = []
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()

    def fail_build_client(**kwargs):
        calls.append(kwargs)
        raise AssertionError('disabled keeper must not build client')

    settings = Settings(
        tasks=TaskSettings(keeper=KeeperSettings(enabled=False)),
        accounts=[cli.AccountSettings(name='main', enabled=True, authorization='Bearer token')],
    )

    results = runtime_commands.run_keeper_only(
        settings=settings,
        headed=False,
        account_name='main',
        store=store,
        build_client_fn=fail_build_client,
    )

    assert results == []
    assert calls == []


def test_run_keeper_only_skips_paused_task_before_building_client(tmp_path):
    calls = []
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.set_task_control('main', 'keeper', enabled=False, source='test')

    def fail_build_client(**kwargs):
        calls.append(kwargs)
        raise AssertionError('paused keeper must not build client')

    settings = Settings(
        tasks=TaskSettings(keeper=KeeperSettings(enabled=True)),
        accounts=[cli.AccountSettings(name='main', enabled=True, authorization='Bearer token')],
    )

    results = runtime_commands.run_keeper_only(
        settings=settings,
        headed=False,
        account_name='main',
        store=store,
        build_client_fn=fail_build_client,
    )

    assert results == []
    assert calls == []



def test_command_service_start_writes_lifecycle_log(tmp_path, monkeypatch):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('tasks: {}\n')
    monkeypatch.setattr(service_commands, 'service_status', lambda config_path: {'installed': True, 'running': False, 'label': 'autodl-helper'})
    monkeypatch.setattr(
        service_commands,
        'start_service',
        lambda config_path: SimpleNamespace(returncode=0, stdout='', stderr=''),
    )

    code = cli_backend.command_service_start(SimpleNamespace(config=str(config_path)))

    assert code == 0
    log_text = (tmp_path / 'logs' / 'service.stdout.log').read_text()
    assert '[服务管理]' in log_text
    assert '已启动后台服务' in log_text


def test_command_init_bootstraps_local_files(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / 'config.yaml'
    env_template = tmp_path / '.env.template'
    config_template = tmp_path / 'config.example.yaml'
    env_template.write_text('TOKEN=\n', encoding='utf-8')
    config_template.write_text('auth:\n  authorization: Bearer token\n', encoding='utf-8')

    code = cli_backend.command_init(
        SimpleNamespace(config=str(config_path), force=False, yes=False),
        validate_config_fn=lambda args: 0,
        cwd=tmp_path,
        input_fn=lambda prompt: 'n',
    )
    captured = capsys.readouterr()

    assert code == 0
    assert (tmp_path / '.env').exists()
    assert config_path.exists()
    assert 'Created .env from template.' in captured.out
    assert 'Created config.yaml from template.' in captured.out
    assert 'Next:' in captured.out


def test_command_init_preserves_existing_files_without_force(tmp_path, capsys):
    config_path = tmp_path / 'config.yaml'
    env_path = tmp_path / '.env'
    (tmp_path / '.env.template').write_text('TOKEN=\n', encoding='utf-8')
    (tmp_path / 'config.example.yaml').write_text('auth:\n  authorization: Bearer token\n', encoding='utf-8')
    env_path.write_text('OLD=1\n', encoding='utf-8')
    config_path.write_text('custom: true\n', encoding='utf-8')

    code = cli_backend.command_init(
        SimpleNamespace(config=str(config_path), force=False, yes=False),
        validate_config_fn=lambda args: 0,
        cwd=tmp_path,
        input_fn=lambda prompt: 'n',
    )
    captured = capsys.readouterr()

    assert code == 0
    assert env_path.read_text(encoding='utf-8') == 'OLD=1\n'
    assert config_path.read_text(encoding='utf-8') == 'custom: true\n'
    assert 'Kept existing .env.' in captured.out
    assert 'Kept existing config.yaml.' in captured.out


def test_command_init_can_overwrite_existing_file_via_prompt(tmp_path, capsys):
    config_path = tmp_path / 'config.yaml'
    env_path = tmp_path / '.env'
    (tmp_path / '.env.template').write_text('TOKEN=\n', encoding='utf-8')
    (tmp_path / 'config.example.yaml').write_text('auth:\n  authorization: Bearer token\n', encoding='utf-8')
    env_path.write_text('OLD=1\n', encoding='utf-8')
    config_path.write_text('custom: true\n', encoding='utf-8')
    answers = iter(['y', 'n'])

    code = cli_backend.command_init(
        SimpleNamespace(config=str(config_path), force=False, yes=False),
        validate_config_fn=lambda args: 0,
        cwd=tmp_path,
        input_fn=lambda prompt: next(answers),
    )
    captured = capsys.readouterr()

    assert code == 0
    assert env_path.read_text(encoding='utf-8') == 'TOKEN=\n'
    assert config_path.read_text(encoding='utf-8') == 'custom: true\n'
    assert 'Overwrote .env from template.' in captured.out
    assert 'Kept existing config.yaml.' in captured.out


def test_command_init_can_launch_interactive_after_bootstrap(tmp_path):
    config_path = tmp_path / 'config.yaml'
    (tmp_path / '.env.template').write_text('TOKEN=\n', encoding='utf-8')
    (tmp_path / 'config.example.yaml').write_text('auth:\n  authorization: Bearer token\n', encoding='utf-8')
    seen = []

    code = cli_backend.command_init(
        SimpleNamespace(config=str(config_path), force=False, yes=False),
        validate_config_fn=lambda args: 0,
        launch_interactive_fn=lambda args: seen.append(args.config) or 0,
        cwd=tmp_path,
        input_fn=lambda prompt: 'y',
    )

    assert code == 0
    assert seen == [str(config_path)]


def test_command_init_returns_validation_failure(tmp_path, capsys):
    config_path = tmp_path / 'config.yaml'
    (tmp_path / '.env.template').write_text('TOKEN=\n', encoding='utf-8')
    (tmp_path / 'config.example.yaml').write_text('auth:\n  authorization: Bearer token\n', encoding='utf-8')

    code = cli_backend.command_init(
        SimpleNamespace(config=str(config_path), force=False, yes=False),
        validate_config_fn=lambda args: 1,
        cwd=tmp_path,
        input_fn=lambda prompt: 'n',
    )
    captured = capsys.readouterr()

    assert code == 1
    assert 'Configuration validation failed.' in captured.err


def test_command_service_stop_writes_lifecycle_log_when_stopped(tmp_path, monkeypatch):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('tasks: {}\n')
    monkeypatch.setattr(service_commands, 'service_status', lambda config_path: {'installed': True, 'running': False, 'label': 'autodl-helper'})

    code = cli_backend.command_service_stop(SimpleNamespace(config=str(config_path)))

    assert code == 0
    log_text = (tmp_path / 'logs' / 'service.stdout.log').read_text()
    assert '[服务管理]' in log_text
    assert '后台服务已停止' in log_text

def test_daemon_dispatch_emits_chinese_summary(monkeypatch, tmp_path, caplog):
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = Settings(
        tasks=TaskSettings(
            keeper=KeeperSettings(enabled=False),
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=5,
                jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1')],
            ),
        ),
        accounts=[cli.AccountSettings(name='main', enabled=True, authorization='Bearer token')],
    )
    args = SimpleNamespace(config='config.yaml', headed=False, state_file=tmp_path / 'state.json', account='main')

    with caplog.at_level('INFO'):
        cli_backend.daemon_dispatch(
            args=args,
            load_settings_fn=lambda path: settings,
            create_store_fn=lambda settings: store,
            run_keeper_only_fn=lambda **kwargs: [],
            run_scheduled_start_cycle_fn=lambda **kwargs: [],
            state={'settings': settings},
            now_fn=lambda: datetime.now(timezone.utc),
        )

    joined = '\n'.join(caplog.messages)
    assert '[后台轮询]' in joined
    assert 'Keeper状态=未启用' in joined
    assert '抢机状态=本轮执行' in joined
    assert '抢机间隔阈值=5秒' in joined


def test_daemon_dispatch_skips_tasks_before_interval_due(tmp_path):
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    mark_task_run(store, 'keeper', now=now)
    mark_task_run(store, 'scheduled_start', now=now)
    settings = Settings(
        tasks=TaskSettings(
            keeper=KeeperSettings(enabled=True, interval_minutes=60),
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=300,
                jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1')],
            ),
        ),
        accounts=[cli.AccountSettings(name='main', enabled=True, authorization='Bearer token')],
    )
    args = SimpleNamespace(config='config.yaml', headed=False, state_file=tmp_path / 'state.json', account='main')
    keeper_calls = []
    scheduled_calls = []

    results = cli_backend.daemon_dispatch(
        args=args,
        load_settings_fn=lambda path: settings,
        create_store_fn=lambda settings: store,
        run_keeper_only_fn=lambda **kwargs: keeper_calls.append(kwargs) or [],
        run_scheduled_start_cycle_fn=lambda **kwargs: scheduled_calls.append(kwargs) or [],
        state={'settings': settings},
        now_fn=lambda: now,
    )

    assert results == []
    assert keeper_calls == []
    assert scheduled_calls == []


def test_scheduled_daemon_should_exit_when_account_has_no_enabled_jobs(tmp_path):
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = Settings(
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1')],
            ),
        ),
        accounts=[cli.AccountSettings(name='main', enabled=True, authorization='Bearer token')],
    )
    store.upsert_scheduled_job_control('main', 'job-1', enabled=False, source='interactive')

    assert cli_backend.scheduled_daemon_should_exit(settings=settings, store=store, account_name='main') is True


def test_read_daemon_status_includes_account_and_origin(tmp_path):
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()

    cli.mark_daemon_heartbeat(store, mode='scheduled_start', pid=4321, account='main', origin='interactive-auto')
    status = cli.read_daemon_status(store)

    assert status['running'] is True
    assert status['mode'] == 'scheduled_start'
    assert status['pid'] == 4321
    assert status['account'] == 'main'
    assert status['origin'] == 'interactive-auto'


def test_daemon_run_marks_heartbeat_before_initial_dispatch(tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('storage:\n  database_file: data.db\n', encoding='utf-8')
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = Settings(
        tasks=TaskSettings(
            keeper=KeeperSettings(enabled=True, interval_minutes=60),
            scheduled_start=ScheduledStartSettings(enabled=False),
        ),
        accounts=[cli.AccountSettings(name='main', enabled=True, authorization='Bearer token')],
    )
    seen = {}

    class FakeLock:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeScheduler:
        def add_job(self, *args, **kwargs):
            return None

        def start(self):
            raise KeyboardInterrupt

    def fake_dispatch(**kwargs):
        status = cli.read_daemon_status(store)
        seen['running_before_dispatch'] = status['running']
        seen['mode_before_dispatch'] = status['mode']
        return []

    args = SimpleNamespace(
        config=str(config_path),
        lock_file=str(tmp_path / 'autodl.lock'),
        headed=False,
        account=None,
        state_file=str(tmp_path / 'state.json'),
        run_once=False,
    )

    code = cli_backend.command_run_variant(
        args,
        'all',
        load_settings_fn=lambda path: settings,
        validate_settings_fn=lambda settings, purpose='run_daemon': [],
        file_lock_cls=FakeLock,
        scheduler_cls=FakeScheduler,
        create_store_fn=lambda settings: store,
        daemon_dispatch_fn=fake_dispatch,
    )

    assert code == 0
    assert seen == {'running_before_dispatch': True, 'mode_before_dispatch': 'all'}


def test_maybe_reload_daemon_settings_consumes_reload_request(tmp_path):
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    cli.request_reload(store)
    old_settings = Settings(tasks=TaskSettings(keeper=KeeperSettings(enabled=True, interval_minutes=30)))
    new_settings = Settings(tasks=TaskSettings(keeper=KeeperSettings(enabled=False, interval_minutes=30)))
    state = {'settings': old_settings}
    args = SimpleNamespace(
        config=str(tmp_path / 'config.yaml'),
        scheduled_poll_interval=None,
        scheduled_job=None,
        target_time=None,
        advance_hours=None,
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

    effective = cli_backend._maybe_reload_daemon_settings(
        args=args,
        store=store,
        state=state,
        load_settings_fn=lambda path: new_settings,
        validate_settings_fn=lambda settings, purpose='run_daemon': [],
        mtime_fn=lambda path: 123.5,
    )

    status = cli.read_config_reload_status(store)
    assert effective.tasks.keeper.enabled is False
    assert state['settings'].tasks.keeper.enabled is False
    assert status['applied_generation'] == 1
    assert status['last_reload_status'] == 'success'


def test_maybe_reload_daemon_settings_keeps_last_known_good_on_invalid_config(tmp_path):
    store = cli.SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    cli.request_reload(store)
    old_settings = Settings(tasks=TaskSettings(keeper=KeeperSettings(enabled=True, interval_minutes=30)))
    bad_settings = Settings(tasks=TaskSettings(keeper=KeeperSettings(enabled=False, interval_minutes=30)))
    state = {'settings': old_settings}
    args = SimpleNamespace(
        config=str(tmp_path / 'config.yaml'),
        scheduled_poll_interval=None,
        scheduled_job=None,
        target_time=None,
        advance_hours=None,
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

    effective = cli_backend._maybe_reload_daemon_settings(
        args=args,
        store=store,
        state=state,
        load_settings_fn=lambda path: bad_settings,
        validate_settings_fn=lambda settings, purpose='run_daemon': ['boom'],
        mtime_fn=lambda path: 124.0,
    )

    status = cli.read_config_reload_status(store)
    assert effective is old_settings
    assert state['settings'] is old_settings
    assert status['applied_generation'] == 0
    assert status['processed_generation'] == 1
    assert status['last_reload_status'] == 'failed'
    assert status['last_reload_error'] == 'boom'


def test_watch_instance_outputs_only_changes_by_default(monkeypatch):
    client = DummyClient(
        instances=[
            [{'uuid': 'iid', 'status': 'shutdown', 'gpu_idle_num': 1, 'gpu_all_num': 1, 'start_mode': 'gpu', 'release_at': '', 'status_at': '2026-04-07T10:00:00+08:00', 'started_at': {'Time': '2026-04-07T09:00:00+08:00', 'Valid': True}, 'stopped_at': {'Time': '2026-04-07T10:00:00+08:00', 'Valid': True}}],
            [{'uuid': 'iid', 'status': 'shutdown', 'gpu_idle_num': 1, 'gpu_all_num': 1, 'start_mode': 'gpu', 'release_at': '', 'status_at': '2026-04-07T10:00:00+08:00', 'started_at': {'Time': '2026-04-07T09:00:00+08:00', 'Valid': True}, 'stopped_at': {'Time': '2026-04-07T10:00:00+08:00', 'Valid': True}}],
            [{'uuid': 'iid', 'status': 'running', 'gpu_idle_num': 0, 'gpu_all_num': 1, 'start_mode': 'gpu', 'release_at': '', 'status_at': '2026-04-07T11:00:00+08:00', 'started_at': {'Time': '2026-04-07T11:00:00+08:00', 'Valid': True}, 'stopped_at': {'Time': '2026-04-07T10:00:00+08:00', 'Valid': True}}],
        ]
    )
    output = StringIO()

    cli.watch_instance(
        client=client,
        instance_id='iid',
        interval_seconds=5,
        json_output=False,
        output=output,
        sleep_fn=lambda *_args, **_kwargs: None,
        max_iterations=3,
    )

    lines = [line for line in output.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2
    assert 'status=shutdown' in lines[0]
    assert 'started_at=2026-04-07T09:00:00+08:00' in lines[0]
    assert 'stopped_at=2026-04-07T10:00:00+08:00' in lines[0]
    assert 'release_deadline=2026-04-22T10:00:00+08:00' in lines[0]
    assert 'next_keeper_time=2026-04-22T04:00:00+08:00' in lines[0]
    assert 'status=running' in lines[1]
    assert 'started_at=2026-04-07T11:00:00+08:00' in lines[1]


def test_watch_instance_json_outputs_each_snapshot(monkeypatch):
    client = DummyClient(
        instances=[
            [{'uuid': 'iid', 'status': 'shutdown', 'gpu_idle_num': 1, 'gpu_all_num': 1, 'start_mode': 'gpu', 'release_at': '', 'status_at': '2026-04-07T10:00:00+08:00', 'started_at': {'Time': '2026-04-07T09:00:00+08:00', 'Valid': True}, 'stopped_at': {'Time': '2026-04-07T10:00:00+08:00', 'Valid': True}}],
            [{'uuid': 'iid', 'status': 'shutdown', 'gpu_idle_num': 1, 'gpu_all_num': 1, 'start_mode': 'gpu', 'release_at': '', 'status_at': '2026-04-07T10:00:00+08:00', 'started_at': {'Time': '2026-04-07T09:00:00+08:00', 'Valid': True}, 'stopped_at': {'Time': '2026-04-07T10:00:00+08:00', 'Valid': True}}],
        ]
    )
    output = StringIO()

    cli.watch_instance(
        client=client,
        instance_id='iid',
        interval_seconds=5,
        json_output=True,
        output=output,
        sleep_fn=lambda *_args, **_kwargs: None,
        max_iterations=2,
    )

    lines = [line for line in output.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2
    assert '"instance_id": "iid"' in lines[0]
    assert '"started_at": "2026-04-07T09:00:00+08:00"' in lines[0]
    assert '"release_deadline": "2026-04-22T10:00:00+08:00"' in lines[0]
    assert '"next_keeper_time": "2026-04-22T04:00:00+08:00"' in lines[0]


def test_collect_healthcheck_errors_detects_missing_auth_and_unwritable_paths(tmp_path):
    settings = Settings()
    errors = cli.collect_healthcheck_errors(
        settings=settings,
        state_file=tmp_path / 'missing' / 'state.json',
        lock_file=tmp_path / 'missing' / 'run.lock',
        smoke=False,
        headed=False,
        permission_probe=lambda path: False,
    )

    assert any('AUTODL_PHONE' in err for err in errors)
    assert any('auth cache' in err.lower() for err in errors)
    assert any('state file' in err.lower() for err in errors)
    assert any('lock file' in err.lower() for err in errors)
