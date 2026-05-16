from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from autodl_helper.core.store import SQLiteStore
from autodl_helper.ui import run_ui
from autodl_helper.ui.render import render_header


def test_render_dashboard_header():
    assert render_header('autodl-helper dashboard') == '== autodl-helper dashboard =='


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


def test_keeper_dashboard_prefers_live_release_at_over_history(tmp_path, capsys, monkeypatch):
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
    monkeypatch.setattr('autodl_helper.ui.app.service_status', lambda config_path: {'status_label': '未安装', 'running': False})
    monkeypatch.setattr(
        'autodl_helper.ui.app.build_client',
        lambda settings, headed, account=None: SimpleNamespace(
            list_instances=lambda: [
                {
                    'uuid': 'iid-live',
                    'status': 'shutdown',
                    'release_at': '2026-05-21T05:41:00+08:00',
                }
            ]
        ),
    )

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)))
    captured = capsys.readouterr()

    assert code == 0
    assert '3天内临期 0 台' in captured.out
    assert '即将临期: iid-live' not in captured.out


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
    assert '无效选择，请输入 1/2/3/4/5/0' in captured.out


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
    inputs = iter(['5', '1', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert calls == [('start', str(config_path))]
    assert 'daemon 控制' in captured.out
    assert '启动 daemon' in captured.out
    assert '停止 daemon' in captured.out
    assert '重启 daemon' in captured.out
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
    inputs = iter(['4', '1', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert '立即执行一次 Keeper' in captured.out
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
    inputs = iter(['4', '1', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert '失败 开机失败 x2' in captured.out
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
    inputs = iter(['4', '1', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert '运行时暂停: main (keeper_guard)' in captured.out
    assert 'Keeper 当前已暂停，未执行: main(keeper_guard)' in captured.out


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
    inputs = iter(['3', '2', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert calls == ['main']
    assert '账号管理' in captured.out
    assert 'main' in captured.out
    assert '可密码登录' in captured.out
    assert '账号登录完成: 成功 1 个 | 失败 0 个' in captured.out


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
        '2',  # 配置管理
        '2',  # Keeper 参数
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
    inputs = iter(['3', '2', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert '\x1b[2J\x1b[H' in captured.out
    assert '账号管理' in captured.out


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
    inputs = iter(['4', '2', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert 'Keeper 已恢复: main' in captured.out
    assert SQLiteStore(db_path).get_task_control('main', 'keeper') is True
