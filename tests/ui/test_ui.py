from __future__ import annotations

import re
import time
from datetime import datetime
from types import SimpleNamespace

from autodl_helper.core.config import load_settings
from autodl_helper.core.store import SQLiteStore
from autodl_helper.ui import app as ui_app
from autodl_helper.ui import run_ui
from autodl_helper.ui.action_menus import account_status_text, run_daemon_control_menu, run_keeper_menu
from autodl_helper.ui.render import GREEN, display_width, render_header, render_metric_row, render_rule, render_status


ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub('', text)


def test_render_dashboard_header():
    assert render_header('autodl-helper dashboard') == '== autodl-helper dashboard =='


def test_render_console_primitives_keep_plain_text_searchable():
    assert render_rule(color_enabled=False) == '─' * 72
    assert 'daemon ● 运行中' == render_status('daemon', '运行中', GREEN, color_enabled=False)
    assert '机器 7 台' in render_metric_row([('机器', '7 台', GREEN)], color_enabled=False)


def test_metric_rows_align_mixed_width_chinese_labels():
    row = render_metric_row(
        [
            ('机器', '7 台', GREEN),
            ('上次检查', '05-08 13:00', GREEN),
            ('3天内临期失败', '0 条', GREEN),
        ],
        color_enabled=False,
    )
    first, second, third = row.split('  |  ')

    assert display_width(first) == display_width(second)
    assert third == '3天内临期失败 0 条'


def test_account_status_table_keeps_columns_aligned_with_color_codes():
    args = SimpleNamespace(config='config.yaml', account=None)

    class Store:
        def __init__(self, path):
            self.path = path

        def init_schema(self):
            return None

    rows = account_status_text(
        args,
        load_settings_fn=lambda path: SimpleNamespace(storage=SimpleNamespace(database_file=':memory:')),
        store_cls=Store,
        account_status_rows_fn=lambda settings, store, account_name=None: [
            {
                'account_name': 'main',
                'enabled': True,
                'status_label': '已登录',
                'auth_source_label': '缓存',
                'cached_at_iso': '2026-05-08T13:00:00+08:00',
                'has_credentials': True,
                'has_config_token': False,
                'lightweight_mode': 'normal',
            },
            {
                'account_name': 'backup-account',
                'enabled': False,
                'status_label': '未授权',
                'auth_source_label': '配置',
                'cached_at_iso': '',
                'has_credentials': False,
                'has_config_token': True,
                'lightweight_mode': 'light',
            },
        ],
    )
    header = strip_ansi(rows[0])
    first = strip_ansi(rows[2])
    second = strip_ansi(rows[3])

    assert header.startswith('账户')
    assert first.index('已登录') == second.index('未授权')
    assert first.index('缓存') == second.index('配置')
    assert first.index('normal') == second.index('light')


def test_run_ui_prints_compact_keeper_and_scheduled_dashboard(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  scheduled_start:',
            '    enabled: true',
            '    poll_interval_seconds: 5',
            '    jobs:',
            '      - name: gpu-job',
            '        instance_id: iid-1',
            '        target_time: "14:00"',
            '        advance_hours: 2',
            '      - name: pending-job',
            '        instance_id: iid-2',
            '        target_time: "20:00"',
            '        advance_hours: 1',
            '  keeper:',
            '    enabled: true',
            '    keeper_trigger_before_hours: 72',
            '    shutdown_release_after_hours: 360',
        ]),
        encoding='utf-8',
    )
    store = SQLiteStore(db_path)
    store.init_schema()
    store.add_scheduled_history(
        'main',
        'gpu-job',
        'iid-1',
        '2026-05-08',
        'outside_window',
        'outside_window',
        {'candidate_count': 3, 'gpu_idle_num': 1, 'selected_instance_label': 'A100'},
    )
    store.add_keeper_history(
        'main',
        'iid-1',
        '2026-05-09T00:00:00+08:00',
        'keeper_executed',
        'eligible',
        {'release_deadline': '2026-05-09T00:00:00+08:00', 'next_keeper_time': '2026-05-08T16:00:00+08:00'},
    )

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2026, 5, 8, 13, 0, tzinfo=tz)
            return base

    monkeypatch.setattr('autodl_helper.ui.app.datetime', FixedDateTime)
    monkeypatch.setattr(
        'autodl_helper.ui.app.service_status',
        lambda config_path: {'status_label': '未安装', 'running': False, 'detail': 'state=spawn scheduled'},
    )
    monkeypatch.setattr(
        'autodl_helper.ui.app.build_client',
        lambda settings, headed, account=None: SimpleNamespace(
            list_instances=lambda: [
                {
                    'uuid': 'iid-1',
                    'status': 'shutdown',
                    'release_at': '2026-05-09T00:00:00+08:00',
                }
            ]
        ),
    )

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)))
    captured = capsys.readouterr()

    assert code == 0
    assert '●' in captured.out
    assert '守护进程' in captured.out
    assert '服务' in captured.out
    assert '重载' in captured.out
    assert '心跳' in captured.out
    assert 'reload' not in captured.out
    assert 'heartbeat' not in captured.out
    assert '─' * 12 in captured.out
    assert '[Keeper]' in captured.out
    assert '机器 1 台' in captured.out
    assert '上次检查' in captured.out
    assert '3天内临期 1 台' in captured.out
    assert '下次检查' in captured.out
    assert '3天内临期失败 0 条' in captured.out
    assert '即将临期: iid-1' in captured.out
    assert '[抢机]' in captured.out
    assert '成功 0' in captured.out
    assert '3天内临期失败 0' in captured.out
    assert '进行中 1' in captured.out
    assert '待运行 1' in captured.out
    assert '进行中任务:' in captured.out
    assert '待运行任务:' in captured.out
    assert 'pending-job' in captured.out
    assert '刷新 1 次' in captured.out
    assert '05-08 19:00~05-08 20:00' in captured.out
    assert '刷新 1 次' in captured.out
    assert '上次' in captured.out
    assert '候选 3' not in captured.out
    assert 'service详情' not in captured.out
    assert 'state=spawn scheduled' not in captured.out


def test_interactive_ui_first_screen_does_not_block_on_service_status(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
        ]),
        encoding='utf-8',
    )

    def slow_service_status(config_path):
        time.sleep(1)
        return {'status_label': '运行中', 'running': True}

    monkeypatch.setattr('autodl_helper.ui.app.service_status', slow_service_status)
    inputs = iter(['0'])
    started_at = time.monotonic()

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    elapsed = time.monotonic() - started_at
    captured = capsys.readouterr()

    assert code == 0
    assert elapsed < 0.5
    assert '服务' in captured.out
    assert '刷新中' in captured.out


def test_dashboard_marks_running_service_with_stale_heartbeat(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
        ]),
        encoding='utf-8',
    )
    store = SQLiteStore(db_path)
    store.init_schema()
    store.set_runtime_value('daemon_state', 'running')
    store.set_runtime_value('daemon_last_seen_at', '2026-05-08T00:00:00+00:00')
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '运行中', 'running': True})

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)))
    captured = capsys.readouterr()

    assert code == 0
    assert '守护进程' in captured.out
    assert '心跳过期' in captured.out
    assert '服务' in captured.out
    assert '运行中' in captured.out


def test_dashboard_summarizes_launchd_config_error(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
        ]),
        encoding='utf-8',
    )
    monkeypatch.setattr(
        'autodl_helper.ui.app.service_status',
        lambda config_path: {'status_label': '状态异常', 'running': False, 'detail': 'state=spawn scheduled | last_exit=78: EX_CONFIG'},
    )

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)))
    captured = capsys.readouterr()

    assert code == 0
    assert '状态异常(EX_CONFIG)' in captured.out
    assert 'state=spawn scheduled' not in captured.out


def test_keeper_dashboard_uses_history_without_live_probe(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  keeper:',
            '    enabled: true',
            '    keeper_trigger_before_hours: 72',
            '    shutdown_release_after_hours: 360',
        ]),
        encoding='utf-8',
    )
    store = SQLiteStore(db_path)
    store.init_schema()
    store.add_keeper_history(
        'main',
        'iid-live',
        '2026-05-09T00:00:00+08:00',
        'skip_not_due',
        'before_next_keeper_time',
        {'instance_id': 'iid-live', 'release_deadline': '2026-05-09T00:00:00+08:00', 'next_keeper_time': '2026-05-08T16:00:00+08:00'},
    )

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 5, 8, 13, 0, tzinfo=tz)

    monkeypatch.setattr('autodl_helper.ui.app.datetime', FixedDateTime)
    calls = []

    def fail_build_client(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('passive dashboard render must not create a client')

    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr('autodl_helper.ui.app.build_client', fail_build_client)

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)))
    captured = capsys.readouterr()

    assert code == 0
    assert calls == []
    assert '3天内临期 1 台' in captured.out
    assert '即将临期: iid-live' in captured.out


def test_interactive_ui_initial_render_does_not_live_probe(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )
    calls = []

    def fail_build_client(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('initial UI render must not perform live API probe')

    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr('autodl_helper.ui.app.build_client', fail_build_client)

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': '0')
    captured = capsys.readouterr()

    assert code == 0
    assert calls == []
    assert 'autodl-helper dashboard' in captured.out


def test_run_ui_refresh_fetches_latest_keeper_dashboard_state(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  keeper:',
            '    enabled: true',
            '    keeper_trigger_before_hours: 72',
            '    shutdown_release_after_hours: 360',
        ]),
        encoding='utf-8',
    )
    store = SQLiteStore(db_path)
    store.init_schema()
    store.add_keeper_history(
        'main',
        'iid-stale',
        '2026-05-09T00:00:00+08:00',
        'skip_not_due',
        'before_next_keeper_time',
        {'instance_id': 'iid-stale', 'release_deadline': '2026-05-09T00:00:00+08:00', 'next_keeper_time': '2026-05-08T16:00:00+08:00'},
    )

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 5, 8, 13, 0, tzinfo=tz)

    class DummyClient:
        def list_instances(self):
            return [
                    {
                        'uuid': 'iid-fresh',
                        'status': 'shutdown',
                        'stopped_at': {'Valid': True, 'Time': '2026-04-24T00:00:00+08:00'},
                        'status_at': '2026-04-24T00:00:00+08:00',
                    }
                ]

    monkeypatch.setattr('autodl_helper.ui.app.datetime', FixedDateTime)
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr('autodl_helper.ui.app.build_client', lambda settings, headed, account=None, store=None: DummyClient())

    class CompletedTask:
        def __init__(self, value):
            self.value = value

        def done(self):
            return True

        def result(self):
            return self.value

    def fake_start_refresh_task(args):
        return CompletedTask(ui_app._refresh_keeper_dashboard(args))

    monkeypatch.setattr('autodl_helper.ui.app._start_refresh_task', fake_start_refresh_task)
    inputs = iter(['1', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()
    latest_screen = captured.out.split('\x1b[2J\x1b[H')[-1]

    assert code == 0
    assert '已刷新最新状态: Keeper 1 台' in latest_screen
    assert 'iid-fresh' in latest_screen
    assert 'iid-stale' not in latest_screen
    assert '3天内临期 1 台' in latest_screen


def test_run_ui_refresh_updates_keeper_and_service_from_same_snapshot(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  keeper:',
            '    enabled: true',
            '    keeper_trigger_before_hours: 72',
            '    shutdown_release_after_hours: 360',
        ]),
        encoding='utf-8',
    )

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 5, 8, 13, 0, tzinfo=tz)

    class DummyClient:
        def list_instances(self):
            return [
                {
                    'uuid': 'iid-fresh',
                    'status': 'shutdown',
                    'release_at': '2026-05-09T00:00:00+08:00',
                }
            ]

    class NeverDoneTask:
        def done(self):
            return False

    class CompletedTask:
        def __init__(self, value):
            self.value = value

        def done(self):
            return True

        def result(self):
            return self.value

    monkeypatch.setattr('autodl_helper.ui.app.datetime', FixedDateTime)
    monkeypatch.setattr('autodl_helper.ui.app.build_client', lambda settings, headed, account=None, store=None: DummyClient())
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '运行中', 'running': True})
    monkeypatch.setattr('autodl_helper.ui.app._start_service_status_task', lambda args: NeverDoneTask())
    monkeypatch.setattr(
        'autodl_helper.ui.app._start_refresh_task',
        lambda args: CompletedTask(ui_app._refresh_dashboard_snapshot(args)),
    )
    inputs = iter(['1', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()
    latest_screen = strip_ansi(captured.out.split('\x1b[2J\x1b[H')[-1])

    assert code == 0
    assert '已刷新最新状态: Keeper 1 台 | 服务 运行中' in latest_screen
    assert '服务 ● 运行中' in latest_screen
    assert 'iid-fresh' in latest_screen


def test_refresh_dashboard_snapshot_loads_settings_once(tmp_path, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
        ]),
        encoding='utf-8',
    )
    real_load_settings = ui_app.load_settings
    load_calls = []

    def counting_load_settings(path):
        load_calls.append(path)
        return real_load_settings(path)

    monkeypatch.setattr('autodl_helper.ui.app.load_settings', counting_load_settings)
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr(
        'autodl_helper.ui.app.build_client',
        lambda settings, headed, account=None, store=None: SimpleNamespace(list_instances=lambda: []),
    )

    snapshot = ui_app._refresh_dashboard_snapshot(SimpleNamespace(config=str(config_path)))

    assert snapshot.keeper_live_rows == []
    assert snapshot.service_snapshot == {'status_label': '未安装', 'running': False}
    assert load_calls == [str(config_path)]


def test_run_ui_refresh_submits_background_task_without_blocking_input(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    calls = []

    class PendingTask:
        def done(self):
            return False

        def result(self):
            raise AssertionError('pending refresh task must not be consumed')

    def fake_start_refresh_task(args):
        calls.append(args)
        return PendingTask()

    monkeypatch.setattr('autodl_helper.ui.app._start_refresh_task', fake_start_refresh_task)
    inputs = iter(['1', '1', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert len(calls) == 1
    assert '刷新中' in captured.out
    assert '面板' in captured.out


def test_dashboard_does_not_build_client_for_password_only_account(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: ""',
            '    autodl_phone: "13800000000"',
            '    autodl_password: "secret"',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )

    calls = []

    def fail_build_client(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('passive dashboard render must not trigger login/client creation')

    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr('autodl_helper.ui.app.build_client', fail_build_client)

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)))
    captured = capsys.readouterr()

    assert code == 0
    assert calls == []
    assert '0 台' in captured.out


def test_run_ui_rejects_unknown_top_level_choice(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
        ]),
        encoding='utf-8',
    )
    inputs = iter(['x', '0'])
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert '无效选择，请输入 1/2/3/4/0' in captured.out


def test_run_ui_main_menu_uses_grouped_actions(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
        ]),
        encoding='utf-8',
    )
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': '0')
    captured = capsys.readouterr()

    assert code == 0
    assert '[状态看板]' in captured.out
    assert '[业务操作]' in captured.out
    assert '[设置管理]' in captured.out
    assert '[后台服务]' in captured.out
    assert '刷新状态' in captured.out
    assert '进入业务操作' in captured.out
    assert '进入设置管理' in captured.out
    assert '执行 Keeper' not in captured.out
    assert '查看 Keeper 详情' not in captured.out
    assert 'Keeper 管理' not in captured.out
    assert '抢机管理' not in captured.out
    assert '账户管理' not in captured.out
    assert 'daemon 管理' in captured.out
    assert '配置管理' not in captured.out


def test_run_ui_can_start_daemon_service(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
        ]),
        encoding='utf-8',
    )
    calls = []

    monkeypatch.setattr(
        'autodl_helper.ui.app.service_status',
        lambda config_path: {'installed': True, 'running': False, 'backend': 'test-service'},
    )
    monkeypatch.setattr(
        'autodl_helper.ui.app.start_service',
        lambda config_path: calls.append(('start', config_path)) or SimpleNamespace(returncode=0, stdout='', stderr=''),
    )
    inputs = iter(['4', '1', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert calls == [('start', str(config_path))]
    assert 'daemon 管理' in captured.out
    assert '服务 ● 已停止: test-service' in strip_ansi(captured.out)
    assert '服务入口: service install/start/stop/restart/status' in captured.out
    assert '启动服务' in captured.out
    assert '停止服务' in captured.out
    assert '重启服务' in captured.out
    assert '已启动 daemon 服务: test-service' in captured.out


def test_run_ui_can_execute_keeper_once(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )

    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr(
        'autodl_helper.ui.app.run_keeper_only',
        lambda **kwargs: [
            SimpleNamespace(result='keeper_executed'),
            SimpleNamespace(result='skip_not_due'),
            SimpleNamespace(result='keeper_failed_power_on'),
        ],
    )
    inputs = iter(['2', '1', '1', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert '立即执行' in captured.out
    assert 'Keeper 已执行: 3 台 | 保活 1 | 跳过 1 | 失败 1' in captured.out
    assert '进度 [########--------!!!!!!!!] 100%' in captured.out
    assert '#保活 -跳过 !失败' not in captured.out


def test_run_ui_keeper_once_shows_failure_reason_summary(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )

    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr(
        'autodl_helper.ui.app.run_keeper_only',
        lambda **kwargs: [
            SimpleNamespace(result='keeper_failed_power_on', reason='power_on_failed', instance_id='iid-1'),
            SimpleNamespace(result='keeper_failed_power_on', reason='power_on_failed', instance_id='iid-2'),
            SimpleNamespace(result='keeper_executed', reason='keeper_window_reached', instance_id='iid-3'),
        ],
    )
    inputs = iter(['2', '1', '1', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert '失败 开机失败 x2' in captured.out
    assert '详情见执行详情' in captured.out
    assert '失败示例' not in captured.out
    assert 'iid-1:power_on_failed' not in captured.out


def test_run_ui_reports_paused_keeper_when_execute_once_is_blocked(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )
    store = SQLiteStore(db_path)
    store.init_schema()
    store.set_task_control('main', 'keeper', enabled=False, source='keeper_guard')

    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    inputs = iter(['2', '1', '1', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert '运行时暂停: main (keeper_guard)' in captured.out
    assert 'Keeper 当前已暂停，未执行: main(keeper_guard)' in captured.out


def test_run_ui_keeper_once_precheck_blocks_disabled_keeper(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  keeper:',
            '    enabled: false',
        ]),
        encoding='utf-8',
    )

    def fail_run_keeper(**kwargs):
        raise AssertionError('run_keeper_only should not be called when keeper is disabled')

    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr('autodl_helper.ui.app.run_keeper_only', fail_run_keeper)
    inputs = iter(['2', '1', '1', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert 'Keeper 预检失败: Keeper 未启用' in captured.out


def test_run_ui_keeper_once_does_not_block_when_execution_is_slow(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )

    def slow_run_keeper_only(**kwargs):
        time.sleep(1)
        return [SimpleNamespace(result='keeper_executed')]

    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr('autodl_helper.ui.app.run_keeper_only', slow_run_keeper_only)
    inputs = iter(['2', '1', '1', '0', '0'])
    started_at = time.monotonic()

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    elapsed = time.monotonic() - started_at
    captured = capsys.readouterr()

    assert code == 0
    assert elapsed < 0.5
    assert 'Keeper 执行任务已提交' in captured.out


def test_run_ui_keeper_details_uses_history_without_live_probe(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: ""',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )
    store = SQLiteStore(db_path)
    store.init_schema()
    store.add_keeper_history(
        'main',
        'iid-skip',
        '2026-05-26T12:00:00+08:00',
        'skip_not_due',
        'before_next_keeper_time',
        {
            'next_keeper_time': '2026-05-26T09:00:00+08:00',
            'release_deadline': '2026-05-26T12:00:00+08:00',
        },
    )
    store.add_keeper_history(
        'main',
        'iid-fail',
        '2026-05-26T12:00:00+08:00',
        'keeper_failed_power_on',
        'auth_failed',
        {
            'next_keeper_time': '2026-05-26T09:00:00+08:00',
            'release_deadline': '2026-05-26T12:00:00+08:00',
        },
    )
    build_calls = []

    def fail_build_client(*args, **kwargs):
        build_calls.append((args, kwargs))
        raise AssertionError('keeper details must not perform live API probe')

    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr('autodl_helper.ui.app.build_client', fail_build_client)
    inputs = iter(['2', '1', '3', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert build_calls == []
    assert '执行详情' in captured.out
    assert 'iid-fail' in captured.out
    assert '失败' in captured.out
    assert '授权失效或接口拒绝登录态' in captured.out
    assert 'iid-skip' in captured.out
    assert '未到保活窗口' in captured.out
    assert '05-26 09:00' in captured.out
    assert '05-26 12:00' in captured.out


def test_daemon_and_keeper_menus_use_page_style_notice(capsys):
    daemon_inputs = iter(['x', '0'])
    daemon_notice = run_daemon_control_menu(
        SimpleNamespace(config='config.yaml'),
        input_fn=lambda prompt='': next(daemon_inputs),
        service_status_fn=lambda **kwargs: {'installed': True, 'running': False, 'backend': 'test-service'},
        start_service_fn=lambda **kwargs: SimpleNamespace(returncode=0, stdout='', stderr=''),
        stop_service_fn=lambda **kwargs: SimpleNamespace(returncode=0, stdout='', stderr=''),
        restart_service_fn=lambda **kwargs: SimpleNamespace(returncode=0, stdout='', stderr=''),
    )

    keeper_inputs = iter(['x', '0'])
    keeper_notice = run_keeper_menu(
        SimpleNamespace(config='config.yaml'),
        input_fn=lambda prompt='': next(keeper_inputs),
        load_settings_fn=lambda path: SimpleNamespace(storage=SimpleNamespace(database_file=':memory:'), tasks=SimpleNamespace(keeper=SimpleNamespace(enabled=True))),
        store_cls=SQLiteStore,
        select_accounts_fn=lambda settings, account_name=None: [],
        run_keeper_only_fn=lambda **kwargs: [],
        result_label_fn=str,
    )
    captured = capsys.readouterr()

    assert daemon_notice == ''
    assert keeper_notice == ''
    assert captured.out.count('\x1b[2J\x1b[H') >= 2
    assert captured.out.count('提示: 无效选择，请输入 1/2/3/0') >= 2


def test_run_ui_can_show_account_management_and_login(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: ""',
            '    autodl_phone: "13800000000"',
            '    autodl_password: "secret"',
            'storage:',
            f'  database_file: {db_path}',
        ]),
        encoding='utf-8',
    )
    calls = []

    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr(
        'autodl_helper.ui.app.resolve_authorization',
        lambda auth_settings, **kwargs: calls.append(kwargs['account_name']) or 'Bearer fresh',
    )
    inputs = iter(['3', '1', '3', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert calls == ['main']
    assert '账户管理' in captured.out
    assert 'main' in captured.out
    assert '可密码登录' in captured.out
    assert '账户登录成功: main' in captured.out
    assert '登录全部账户' not in captured.out


def test_run_ui_account_menu_can_add_account_and_request_reload(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
        ]),
        encoding='utf-8',
    )
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    inputs = iter(['3', '1', '2', '13900000000', 'secret', '', 'backup', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()
    settings = load_settings(config_path)
    store = SQLiteStore(db_path)

    assert code == 0
    assert '账户已添加并已请求重载: backup' in captured.out
    assert [account.name for account in settings.accounts] == ['main', 'backup']
    assert settings.accounts[1].autodl_phone == '13900000000'
    assert settings.accounts[1].cache_file.endswith('/.cache/backup-auth.json')
    assert store.get_runtime_value('config_generation', '0') == '1'


def test_run_ui_account_menu_rejects_numeric_account_name(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
        ]),
        encoding='utf-8',
    )
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    inputs = iter(['3', '1', '2', '13900000000', 'secret', '', '2', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()
    settings = load_settings(config_path)

    assert code == 0
    assert [account.name for account in settings.accounts] == ['main']
    assert '账户名不能是纯数字' in captured.out


def test_run_ui_account_menu_can_select_account_to_login(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: ""',
            '    autodl_phone: "13800000000"',
            '    autodl_password: "secret"',
            '  - name: backup',
            '    enabled: true',
            '    authorization: ""',
            '    autodl_phone: "13900000000"',
            '    autodl_password: "secret"',
            'storage:',
            f'  database_file: {db_path}',
        ]),
        encoding='utf-8',
    )
    calls = []

    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr(
        'autodl_helper.ui.app.resolve_authorization',
        lambda auth_settings, **kwargs: calls.append(kwargs['account_name']) or 'Bearer fresh',
    )
    inputs = iter(['3', '1', '3', '2', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert calls == ['backup']
    assert '选择要登录的账户' in captured.out
    assert '账户登录成功: backup' in captured.out


def test_run_ui_account_menu_can_toggle_edit_and_delete_account(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            '  - name: backup',
            '    enabled: true',
            '    authorization: ""',
            '    autodl_phone: "13900000000"',
            '    autodl_password: "old"',
            'storage:',
            f'  database_file: {db_path}',
        ]),
        encoding='utf-8',
    )
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    inputs = iter([
        '3',
        '1',
        '6', '2',  # 停用 backup
        '5', '2', '', 'new-secret', '',  # 更新 backup 密码
        '7', '2', 'yes',  # 删除 backup
        '0',
        '0',
    ])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()
    settings = load_settings(config_path)

    assert code == 0
    assert [account.name for account in settings.accounts] == ['main']
    assert '账户已停用并已请求重载: backup' in captured.out
    assert '账户凭据已更新并已请求重载: backup' in captured.out
    assert '账户已删除并已请求重载: backup' in captured.out


def test_run_ui_shows_config_save_notice_and_refreshes_dashboard(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  keeper:',
            '    enabled: true',
            '    keeper_trigger_before_hours: 72',
            '    shutdown_release_after_hours: 360',
        ]),
        encoding='utf-8',
    )
    inputs = iter([
        '3',  # 设置管理
        '2',  # 配置管理
        '2',  # Keeper 配置
        '0',  # 先不进子菜单
        '0',  # 返回配置管理
        '0',  # 退出时自动保存
        '0',
    ])

    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert '配置已保存并已请求重载' not in captured.out or '配置未变更' in captured.out
    assert 'autodl-helper dashboard' in captured.out


def test_run_ui_account_menu_uses_page_style_notice_and_clear(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: ""',
            '    autodl_phone: "13800000000"',
            '    autodl_password: "secret"',
            'storage:',
            f'  database_file: {db_path}',
        ]),
        encoding='utf-8',
    )
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr(
        'autodl_helper.ui.app.resolve_authorization',
        lambda auth_settings, **kwargs: 'Bearer fresh',
    )
    inputs = iter(['3', '1', '3', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert '\x1b[2J\x1b[H' in captured.out
    assert '账户管理' in captured.out


def test_run_ui_account_menu_can_run_health_check(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
        ]),
        encoding='utf-8',
    )

    class DummyClient:
        def list_instances(self):
            return [{'uuid': 'iid-1'}, {'uuid': 'iid-2'}]

    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr('autodl_helper.ui.app.build_client', lambda *args, **kwargs: DummyClient())
    inputs = iter(['3', '1', '4', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert '账户健康检查' in captured.out
    assert '账户健康检查: 正常 1 个 | 异常 0 个 | 正常 main(2 台)' in captured.out


def test_run_ui_account_health_check_does_not_block_input(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
        ]),
        encoding='utf-8',
    )

    class SlowClient:
        def list_instances(self):
            time.sleep(1)
            return [{'uuid': 'iid-1'}]

    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr('autodl_helper.ui.app.build_client', lambda *args, **kwargs: SlowClient())
    inputs = iter(['3', '1', '4', '0', '0'])
    started_at = time.monotonic()

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    elapsed = time.monotonic() - started_at
    captured = capsys.readouterr()

    assert code == 0
    assert elapsed < 0.5
    assert '账户健康检查已提交' in captured.out


def test_run_ui_scheduled_management_is_read_only_business_page(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  scheduled_start:',
            '    enabled: true',
            '    poll_interval_seconds: 5',
            '    jobs:',
            '      - name: fixed-job',
            '        enabled: true',
            '        instance_id: iid-1',
            '        target_time: "13:00"',
            '        advance_hours: 2',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    before = config_path.read_text(encoding='utf-8')
    inputs = iter(['2', '2', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert '抢机管理' in captured.out
    assert '抢机' in captured.out
    assert 'fixed-job' in captured.out
    assert '暂停单个任务' in captured.out
    assert '恢复单个任务' in captured.out
    assert '暂停全部抢机' in captured.out
    assert '恢复全部抢机' in captured.out
    assert '新增任务' not in captured.out
    assert '编辑任务' not in captured.out
    assert '修改轮询' not in captured.out
    assert '配置已保存并已请求重载' not in captured.out
    assert config_path.read_text(encoding='utf-8') == before


def test_dashboard_scheduled_counts_respect_runtime_pause(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  scheduled_start:',
            '    enabled: true',
            '    poll_interval_seconds: 5',
            '    jobs:',
            '      - name: fixed-job',
            '        enabled: true',
            '        instance_id: iid-1',
            '        target_time: "20:00"',
            '        advance_hours: 1',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )
    store = SQLiteStore(db_path)
    store.init_schema()
    store.set_task_control('main', 'scheduled_start', enabled=False, source='ui_scheduled_control')

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2026, 5, 8, 19, 30, tzinfo=tz)
            return base

    monkeypatch.setattr('autodl_helper.ui.app.datetime', FixedDateTime)
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)))
    captured = capsys.readouterr()

    assert code == 0
    assert '进行中 0' in captured.out
    assert '待运行 0' in captured.out
    assert 'fixed-job' not in captured.out


def test_scheduled_dashboard_batches_window_history_queries(tmp_path, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  scheduled_start:',
            '    enabled: true',
            '    poll_interval_seconds: 5',
            '    jobs:',
            '      - name: job-1',
            '        enabled: true',
            '        instance_id: iid-1',
            '        target_time: "20:00"',
            '        advance_hours: 1',
            '      - name: job-2',
            '        enabled: true',
            '        instance_id: iid-2',
            '        target_time: "20:00"',
            '        advance_hours: 1',
            '      - name: job-3',
            '        enabled: true',
            '        instance_id: iid-3',
            '        target_time: "20:00"',
            '        advance_hours: 1',
        ]),
        encoding='utf-8',
    )
    settings = load_settings(config_path)
    store = SQLiteStore(db_path)
    store.init_schema()

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2026, 5, 8, 19, 30, tzinfo=tz)
            return base

    query_count = 0
    original_connect = store.connect

    class CountingConnection:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._conn.__exit__(exc_type, exc, tb)

        def execute(self, sql, parameters=()):
            nonlocal query_count
            if 'FROM scheduled_history' in sql:
                query_count += 1
            return self._conn.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr('autodl_helper.ui.app.datetime', FixedDateTime)
    monkeypatch.setattr(store, 'connect', lambda: CountingConnection(original_connect()))

    lines = ui_app._scheduled_lines(settings, store)

    assert any('进行中 3' in strip_ansi(line) for line in lines)
    assert query_count == 1


def test_run_ui_can_pause_and_resume_single_scheduled_job(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  scheduled_start:',
            '    enabled: true',
            '    jobs:',
            '      - name: fixed-job',
            '        enabled: true',
            '        instance_id: iid-1',
            '        target_time: "13:00"',
            '        advance_hours: 2',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    before = config_path.read_text(encoding='utf-8')
    pause_inputs = iter(['2', '2', '2', '1', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(pause_inputs))
    paused = SQLiteStore(db_path).get_scheduled_job_control('main', 'fixed-job')
    captured = capsys.readouterr()

    assert code == 0
    assert paused is not None
    assert paused['enabled'] is False
    assert paused['source'] == 'ui_scheduled_control'
    assert '抢机任务已暂停: main/fixed-job' in captured.out
    assert '已暂停' in captured.out
    assert config_path.read_text(encoding='utf-8') == before

    resume_inputs = iter(['2', '2', '3', '1', '0', '0'])
    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(resume_inputs))
    resumed = SQLiteStore(db_path).get_scheduled_job_control('main', 'fixed-job')
    captured = capsys.readouterr()

    assert code == 0
    assert resumed is not None
    assert resumed['enabled'] is True
    assert resumed['source'] == 'ui_scheduled_control'
    assert '抢机任务已恢复: main/fixed-job' in captured.out
    assert config_path.read_text(encoding='utf-8') == before


def test_run_ui_can_pause_and_resume_all_scheduled_jobs(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            '  - name: backup',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  scheduled_start:',
            '    enabled: true',
            '    jobs:',
            '      - name: fixed-job',
            '        enabled: true',
            '        instance_id: iid-1',
            '        target_time: "13:00"',
            '        advance_hours: 2',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    pause_inputs = iter(['2', '2', '4', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(pause_inputs))
    captured = capsys.readouterr()

    assert code == 0
    store = SQLiteStore(db_path)
    assert store.get_task_control('backup', 'scheduled_start') is False
    assert store.get_task_control('main', 'scheduled_start') is False
    assert '抢机已全部暂停: main, backup' in captured.out

    resume_inputs = iter(['2', '2', '5', '0', '0'])
    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(resume_inputs))
    captured = capsys.readouterr()

    assert code == 0
    store = SQLiteStore(db_path)
    assert store.get_task_control('backup', 'scheduled_start') is True
    assert store.get_task_control('main', 'scheduled_start') is True
    assert '抢机已全部恢复: main, backup' in captured.out


def test_run_ui_does_not_resume_config_disabled_scheduled_job(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  scheduled_start:',
            '    enabled: true',
            '    jobs:',
            '      - name: fixed-job',
            '        enabled: false',
            '        instance_id: iid-1',
            '        target_time: "13:00"',
            '        advance_hours: 2',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    inputs = iter(['2', '2', '3', '1', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert SQLiteStore(db_path).get_scheduled_job_control('main', 'fixed-job') is None
    assert '配置停用' in captured.out
    assert '配置停用任务不能通过运行时恢复' in captured.out


def test_run_ui_scheduled_management_does_not_pause_when_no_jobs(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  scheduled_start:',
            '    enabled: true',
            '    jobs: []',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    inputs = iter(['2', '2', '4', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert SQLiteStore(db_path).get_task_control('main', 'scheduled_start') is None
    assert '暂无抢机任务' in captured.out


def test_run_ui_scheduled_management_marks_disabled_scheduler_as_config_disabled(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  scheduled_start:',
            '    enabled: false',
            '    jobs:',
            '      - name: fixed-job',
            '        enabled: true',
            '        instance_id: iid-1',
            '        target_time: "13:00"',
            '        advance_hours: 2',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    inputs = iter(['2', '2', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert 'fixed-job' in captured.out
    assert '配置停用' in captured.out
    assert '运行中' not in strip_ansi(captured.out)


def test_run_ui_can_resume_paused_keeper(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        '\n'.join([
            'accounts:',
            '  - name: main',
            '    enabled: true',
            '    authorization: Bearer token',
            'storage:',
            f'  database_file: {db_path}',
            'tasks:',
            '  keeper:',
            '    enabled: true',
        ]),
        encoding='utf-8',
    )
    store = SQLiteStore(db_path)
    store.init_schema()
    store.set_task_control('main', 'keeper', enabled=False, source='keeper_guard')

    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    inputs = iter(['2', '1', '2', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert 'Keeper 已恢复: main' in captured.out
    assert SQLiteStore(db_path).get_task_control('main', 'keeper') is True
