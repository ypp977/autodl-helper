import argparse
import io
import logging
import os
import pty
import sqlite3
import time
import threading
from types import SimpleNamespace
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest
import yaml

from autodl_helper import cli
from autodl_helper import interactive_app
from autodl_helper import interactive_views
from autodl_helper.interactive_runtime import InteractiveTaskResult
from autodl_helper.config import (
    AccountSettings,
    AuthSettings,
    KeeperSettings,
    ScheduledStartJob,
    ScheduledStartPriority,
    ScheduledStartSettings,
    ScheduledStartSelector,
    Settings,
    TaskSettings,
    load_settings,
    write_raw_settings,
)
from autodl_helper.models import HistoryRecord, KeeperResult, ScheduledStartResult
from autodl_helper.runtime_control import clear_daemon_heartbeat, mark_daemon_heartbeat
from autodl_helper.storage import SQLiteStore


BASE_SETTINGS = Settings(
    auth=AuthSettings(authorization='Bearer token'),
    accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token')],
    tasks=TaskSettings(
        keeper=KeeperSettings(enabled=True),
        scheduled_start=ScheduledStartSettings(
            enabled=True,
            poll_interval_seconds=300,
            jobs=[ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='14:00', advance_hours=2)],
        ),
    ),
)


class DummyKeeperClient:
    def __init__(self, instances):
        self._instances = instances

    def list_instances(self, page=1, page_size=100):
        return self._instances


def slow_picklable_command(args):
    time.sleep(0.3)
    print('slow command output')
    return 0


def test_interactive_command_delegates_to_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_command_interactive', lambda args: calls.append(args.command) or 0)

    code = cli.main(['interactive', '--config', 'config.yaml'])

    assert code == 0
    assert calls == ['interactive']


def test_interactive_home_shows_current_account_and_status(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    answers = iter(['0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert 'AutoDL Helper CLI' in captured.out
    assert '当前账号' in captured.out
    assert 'main' in captured.out
    assert '主任务' in captured.out
    assert 'token 来源' not in captured.out
    assert '最近登录时间' in captured.out
    assert '1. 抢机器' in captured.out
    assert '2. Keeper' in captured.out
    assert '3. 账号' in captured.out
    assert '4. 诊断' in captured.out
    assert '运行记录' not in captured.out
    assert '高级' not in captured.out
    assert '配置' not in captured.out
    assert '账号概览' not in captured.out


def test_interactive_home_uses_snapshot_loading_without_sync_keeper_probe(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    def blow_up(*args, **kwargs):
        raise AssertionError('keeper_probe_rows should not run synchronously on homepage render')

    monkeypatch.setattr(cli, 'keeper_probe_rows', blow_up)

    answers = iter(['0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert '数据状态' in captured.out
    assert '首次加载中' in captured.out


def test_interactive_enter_defaults_to_grab_menu(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    answers = iter(['', '0', '0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert '选择抢机器规则' in captured.out
    assert '\x1b[H\x1b[J' in captured.out


def test_interactive_keeper_workflow_confirms_rules_probes_and_executes(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(
        cli,
        'build_client',
        lambda settings, headed, account=None, store=None: DummyKeeperClient(
            [
                {'uuid': 'iid-ready', 'status': 'shutdown'},
                {'uuid': 'iid-wait', 'status': 'shutdown'},
            ]
        ),
    )
    monkeypatch.setattr(
        cli,
        'evaluate_keeper_instance',
        lambda **kwargs: KeeperResult(
            instance_id=kwargs['item']['uuid'],
            status='shutdown',
            release_at='',
            release_source='stopped_at',
            started_at='',
            stopped_at='2026-04-01T00:00:00+08:00',
            status_at='',
            release_deadline='2026-04-15T00:00:00+08:00',
            next_keeper_time='2026-04-14T18:00:00+08:00',
            seconds_until_release=0,
            seconds_until_keeper=0,
            started_duration_seconds=None,
            shutdown_duration_seconds=100,
            eligible=kwargs['item']['uuid'] == 'iid-ready',
            result='ready' if kwargs['item']['uuid'] == 'iid-ready' else 'skip_not_due',
            reason='keeper_window_reached' if kwargs['item']['uuid'] == 'iid-ready' else 'before_next_keeper_time',
            summary='',
        ),
    )
    monkeypatch.setattr(
        cli,
        'run_keeper_only',
        lambda **kwargs: [
            KeeperResult(
                instance_id='iid-ready',
                status='shutdown',
                release_at='',
                release_source='stopped_at',
                started_at='',
                stopped_at='2026-04-01T00:00:00+08:00',
                status_at='',
                release_deadline='2026-04-15T00:00:00+08:00',
                next_keeper_time='2026-04-14T18:00:00+08:00',
                seconds_until_release=0,
                seconds_until_keeper=0,
                started_duration_seconds=None,
                shutdown_duration_seconds=100,
                eligible=False,
                result='keeper_executed',
                reason='keeper_window_reached',
                summary='done',
            )
        ],
    )

    answers = iter(['2', '1', '1', '', '0', '0', '0', '0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert 'Keeper 规则确认' in captured.out
    assert '时间回退策略' not in captured.out
    assert '本次将执行' in captured.out
    assert 'iid-ready' in captured.out
    assert '暂不执行' not in captured.out
    assert 'iid-wait' not in captured.out
    assert '距离释放' in captured.out
    assert '距离接管' in captured.out
    assert 'Keeper 执行结果' in captured.out
    assert '已执行保活' in captured.out


def test_interactive_scheduled_workflow_selects_rule_runs_and_shows_status(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])
    seen = []
    def fake_run_scheduled_start_cycle(*, settings, headed, state_file, account_name=None, store=None, force_run_now=False):
        seen.append([job.name or job.instance_id for job in settings.tasks.scheduled_start.jobs])
        result = ScheduledStartResult(
            result='started',
            reason='started',
            instance_id='iid-1',
            status='running',
            gpu_idle_num=1,
            start_mode='gpu',
            target_time='14:00',
            deadline='2026-04-08T14:00:00+08:00',
            event_type='scheduled.started',
            severity='success',
            summary='job started',
        )
        store.add_scheduled_history(
            account_name or 'main',
            'job-1',
            'iid-1',
            '2026-04-08',
            result.result,
            result.reason,
            {
                'instance_id': result.instance_id,
                'target_time': result.target_time,
                'deadline': result.deadline,
                'candidate_count': 2,
                'candidate_details': [
                    {
                        'instance_id': 'iid-1',
                        'status': 'running',
                        'selected': True,
                        'reason': 'started',
                    },
                    {
                        'instance_id': 'iid-2',
                        'status': 'shutdown',
                        'selected': False,
                        'reason': 'waiting_for_gpu',
                    },
                ],
            },
            result.event_type,
            result.severity,
            result.summary,
        )
        return [result]
    monkeypatch.setattr(cli, 'run_scheduled_start_cycle', fake_run_scheduled_start_cycle)

    answers = iter(['1', '1', '1', 'y', '2', '0', '0', '0', '0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert seen == [['job-1']]
    assert '选择抢机器规则' in captured.out
    assert '抢机器规则' in captured.out
    assert '抢机器执行结果: job-1' not in captured.out
    assert '最近执行' in captured.out
    assert '手动立即执行' in captured.out
    assert '抢机进度: job-1' in captured.out
    assert '当前阶段' in captured.out
    assert '下一步动作' in captured.out
    assert '已命中' in captured.out
    assert '等待中' in captured.out
    assert '被淘汰' in captured.out
    assert '距离目标时间' in captured.out


def test_render_dashboard_uses_human_readable_labels_and_counts():
    view = {
        'current_account': 'main',
        'current_account_row': {
            'status': 'cached',
            'cached_at_iso': '2026-04-08T19:04:21+08:00',
        },
        'effective_scheduled_enabled': True,
        'effective_keeper_enabled': True,
        'scheduled_jobs': [
            {'job_name': 'job-running', 'target_time': '19:00', 'advance_hours': 1, 'enabled': True, 'latest_result': '', 'latest_created_at': ''},
            {'job_name': 'job-paused', 'target_time': '21:00', 'advance_hours': 2, 'enabled': False, 'latest_result': ''},
            {'job_name': 'job-failed', 'target_time': '22:00', 'advance_hours': 1, 'enabled': True, 'latest_result': 'deadline_failed', 'latest_created_at': '2026-04-08T18:00:00+08:00'},
        ],
        'keeper_summary': {
            'pending': 2,
            'expiring_soon': 4,
            'failed': 1,
        },
        'service_state_label': '状态异常',
        'service_state_tone': 'bad',
        'service_last_seen_at': '2026-04-10T16:38:25+08:00',
        'service_pid': None,
    }

    rendered = interactive_views.render_dashboard(view)

    assert 'token 来源' not in rendered
    assert '最近登录时间' in rendered
    assert '2026-04-08 19:04' in rendered
    assert '抢机器任务' in rendered
    assert '总数 3 / 已启用 2 / 已暂停 1 / 失败 1' in rendered
    assert '最近失败任务' in rendered
    assert 'job-failed' in rendered
    assert '后台服务状态' in rendered
    assert '状态异常' in rendered
    assert '建议去诊断页重启服务' in rendered
    assert 'Keeper 任务' in rendered
    assert '本次应接管 2 / 未到窗口 0 / 状态异常 0 / 一周内到期 4' in rendered
    assert 'job-running' in rendered
    assert 'job-failed' in rendered
    assert '任务状态' in rendered
    assert 'job-paused' not in rendered


def test_render_dashboard_uses_semantic_fallbacks_instead_of_dash():
    rendered = interactive_views.render_dashboard(
        {
            'current_account': 'main',
            'current_account_row': {},
            'scheduled_jobs': [],
            'keeper_summary': {},
        }
    )

    assert '最近登录时间' in rendered and '暂无记录' in rendered
    assert '后台服务状态' in rendered and '已停止' in rendered
    assert '服务详情' in rendered and '可去诊断页启动或重启服务' in rendered
    assert '状态未知' not in rendered
    assert '状态待确认' not in rendered


def test_dashboard_snapshot_view_preserves_cached_login_time_and_service_state(monkeypatch, tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    mark_daemon_heartbeat(store, pid=4321, mode='all')
    monkeypatch.setattr(interactive_app, 'read_launch_agent_status', lambda: {'installed': True, 'loaded': True})
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    snapshot_store.set_snapshot(
        interactive_app._snapshot_key('account_runtime', 'main'),
        {
            'auth_status': 'logged_in',
            'auth_source': 'runtime',
            'cached_at_iso': '2026-04-10T18:00:00+08:00',
        },
    )
    snapshot_store.set_snapshot(
        interactive_app._snapshot_key('keeper_probe', 'main'),
        [
            {'instance_id': 'iid-1', 'eligible': False, 'result': 'skip_not_due'},
            {'instance_id': 'iid-2', 'eligible': False, 'result': 'skip_missing_shutdown_time'},
        ],
    )

    view = interactive_app._dashboard_snapshot_view(
        settings=BASE_SETTINGS,
        store=store,
        current_account='main',
        scheduled_job_status_rows_fn=lambda *args, **kwargs: [],
        snapshot_store=snapshot_store,
    )

    assert view['current_account_row']['cached_at_iso'] == '2026-04-10T18:00:00+08:00'
    assert view['service_state_label'] == '运行中'
    assert view['keeper_summary']['not_due'] == 1
    assert view['keeper_summary']['abnormal'] == 1


def test_dashboard_placeholder_view_includes_service_state(monkeypatch, tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    mark_daemon_heartbeat(store, pid=1234, mode='all')
    monkeypatch.setattr(interactive_app, 'read_launch_agent_status', lambda: {'installed': True, 'loaded': True})

    view = interactive_app._dashboard_placeholder_view(
        settings=BASE_SETTINGS,
        store=store,
        current_account='main',
        scheduled_job_status_rows_fn=lambda *args, **kwargs: [],
    )

    assert view['service_state_label'] == '运行中'
    assert view['service_pid'] == 1234


def test_dashboard_placeholder_view_marks_service_abnormal_on_stale_heartbeat(monkeypatch, tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.set_runtime_value('daemon_running', '1')
    store.set_runtime_value('daemon_pid', '1234')
    store.set_runtime_value('daemon_last_seen_at', '2026-04-10T08:00:00+00:00')
    monkeypatch.setattr(interactive_app, 'read_launch_agent_status', lambda: {'installed': True, 'loaded': True})

    view = interactive_app._dashboard_placeholder_view(
        settings=BASE_SETTINGS,
        store=store,
        current_account='main',
        scheduled_job_status_rows_fn=lambda *args, **kwargs: [],
    )

    assert view['service_state_label'] == '状态异常'


def test_dashboard_placeholder_view_keeps_service_running_with_recent_30s_heartbeat(monkeypatch, tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    now = datetime.now(timezone.utc)
    store.set_runtime_value('daemon_state', 'running')
    store.set_runtime_value('daemon_pid', '1234')
    store.set_runtime_value('daemon_last_seen_at', (now - timedelta(seconds=30)).isoformat())
    monkeypatch.setattr(interactive_app, 'read_launch_agent_status', lambda: {'installed': True, 'loaded': True})

    view = interactive_app._dashboard_placeholder_view(
        settings=BASE_SETTINGS,
        store=store,
        current_account='main',
        scheduled_job_status_rows_fn=lambda *args, **kwargs: [],
    )

    assert view['service_state_label'] == '运行中'


def test_render_diagnostics_page_includes_service_and_reload_state():
    body = interactive_app._render_diagnostics_page(
        'main',
        {
            'instance_total': 1,
            'instance_running': 1,
            'instance_shutdown': 0,
            'keeper_total': 2,
            'keeper_eligible': 1,
            'healthcheck_status': '成功',
            'healthcheck_summary': 'Healthcheck OK.',
            'config_status': '成功',
            'config_summary': '配置有效',
            'fd_current': 12,
            'fd_soft_limit': 256,
            'fd_usage_percent': 4.7,
            'interactive_workers_max': 2,
            'interactive_running_count': 0,
            'interactive_queued_count': 0,
            'interactive_running_by_type': {},
            'daemon_launch_state': 'idle',
            'daemon_pid': None,
            'daemon_error_count': 0,
            'daemon_last_error': '',
            'daemon_fused_until': '',
            'interactive_circuit_open': False,
            'interactive_circuit_reason': '',
            'interactive_circuit_until': '',
            'service_installed': True,
            'service_loaded': True,
            'service_label': 'com.autodl.helper',
            'reload_status': 'success',
            'reload_error': '',
        },
    )

    assert '服务状态' in body
    assert '运行中' in body
    assert '服务标签' in body
    assert 'com.autodl.helper' in body
    assert '配置热重载' in body
    assert 'success' in body


def test_render_diagnostics_page_uses_two_column_section_layout():
    body = interactive_app._strip_ansi(
        interactive_app._render_diagnostics_page(
            'main',
            {
                'instance_total': 1,
                'keeper_total': 2,
                'healthcheck_status': '尚未执行',
                'config_status': '尚未执行',
                'service_installed': True,
                'service_loaded': False,
                'service_label': 'com.autodl.helper',
            },
        )
    )

    assert '[实例摘要]' in body
    assert '[后台服务]' in body
    assert any('[实例摘要]' in line and '[后台服务]' in line for line in body.splitlines())


def test_render_diagnostics_page_uses_non_misleading_interactive_task_label():
    body = interactive_app._strip_ansi(
        interactive_app._render_diagnostics_page(
            'main',
            {
                'daemon_launch_state': 'idle',
                'daemon_pid': None,
            },
        )
    )

    assert '交互轮询任务' in body
    assert '当前空闲' in body
    assert '轮询进程' not in body


def test_load_settings_parses_interactive_max_workers(tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('interactive:\n  max_workers: 10\n', encoding='utf-8')

    settings = cli.load_settings(str(config_path))

    assert settings.interactive.max_workers == 10


def test_format_human_datetime_includes_seconds():
    rendered = interactive_app._format_human_datetime('2026-04-10T13:41:58+08:00')

    assert rendered == '2026-04-10 13:41:58'


def test_humanize_datetime_text_rewrites_iso_timestamps():
    rendered = interactive_app._humanize_datetime_text(
        '释放时间=2026-04-11T13:00:00+08:00 下次保活=2026-04-11T15:30:45+08:00'
    )

    assert '2026-04-11 13:00:00' in rendered
    assert '2026-04-11 15:30:45' in rendered
    assert 'T13:00:00+08:00' not in rendered


def test_humanize_datetime_text_keeps_zero_values():
    assert interactive_app._humanize_datetime_text(0) == '0'


def test_coordinate_scheduled_background_prefers_service_start_when_installed(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = Settings(
        accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token')],
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                jobs=[ScheduledStartJob(name='job-1', instance_id='iid-1', target_time='14:00', advance_hours=1)],
            )
        ),
    )
    calls: list[str] = []

    code, detail = interactive_app._coordinate_scheduled_background(
        args=argparse.Namespace(config='config.yaml', headed=False),
        settings=settings,
        store=store,
        account_name='main',
        start_background_scheduled_fn=lambda args: (_ for _ in ()).throw(AssertionError('should not start fallback helper')),
        stop_background_polling_fn=lambda settings, store: (0, 'stopped'),
        service_status_fn=lambda: {'installed': True, 'loaded': False},
        service_start_fn=lambda: calls.append('start') or (0, 'started'),
    )

    assert code == 0
    assert calls == ['start']
    assert detail == '已启动后台服务'


def test_coordinate_scheduled_background_falls_back_when_service_missing(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = Settings(
        accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token')],
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                jobs=[ScheduledStartJob(name='job-1', instance_id='iid-1', target_time='14:00', advance_hours=1)],
            )
        ),
    )
    calls: list[str] = []

    code, detail = interactive_app._coordinate_scheduled_background(
        args=argparse.Namespace(config='config.yaml', headed=False),
        settings=settings,
        store=store,
        account_name='main',
        start_background_scheduled_fn=lambda args: calls.append('fallback') or (0, 'started'),
        stop_background_polling_fn=lambda settings, store: (0, 'stopped'),
        service_status_fn=lambda: {'installed': False, 'loaded': False},
        service_start_fn=lambda: (_ for _ in ()).throw(AssertionError('should not start managed service')),
    )

    assert code == 0
    assert calls == ['fallback']
    assert detail == '已自动启动后台（fallback 模式）'


def test_scheduled_status_uses_explicit_poll_and_target_countdown():
    rendered = interactive_app._render_scheduled_status(
        'selector-3080ti',
        [
            {
                'job_name': 'selector-3080ti',
                'enabled': True,
                'target_mode': 'selector',
                'target_summary': '地区=北京A区,北京B区; GPU=RTX 3080 Ti; 数量=1',
                'target_time': '23:59',
                'advance_hours': 1,
                'timezone': 'Asia/Shanghai',
                'latest_result': '',
                'latest_reason': '',
                'latest_summary': '',
                'latest_created_at': '',
                'latest_payload': {},
                'latest_instance_id': '',
            }
        ],
    )

    assert '距离开始轮询' in rendered
    assert '距离目标时间' in rendered
    assert '目标方式' in rendered
    assert '目标条件' in rendered
    assert '距离截止' not in rendered
    assert '规则开关' in rendered
    assert '执行状态' in rendered
    assert '暂无检查记录' in rendered


def test_render_scheduled_status_falls_back_when_live_next_action_is_blank():
    rendered = interactive_app._strip_ansi(
        interactive_app._render_scheduled_status(
            'selector-3080ti',
            [
                {
                    'job_name': 'selector-3080ti',
                    'enabled': True,
                    'target_mode': 'selector',
                    'target_summary': '未设置',
                    'target_time': '23:59',
                    'advance_hours': 1,
                    'timezone': 'Asia/Shanghai',
                    'latest_result': 'waiting_for_gpu',
                    'latest_reason': 'gpu_idle_zero',
                    'latest_summary': '',
                    'latest_created_at': '2026-04-10T16:38:25+08:00',
                    'latest_payload': {},
                    'latest_instance_id': '',
                    '_live_next_action': '',
                }
            ],
        )
    )

    assert '下一步动作' in rendered
    assert '继续轮询候选，等待可开机资源' in rendered


def test_scheduled_status_separates_enabled_and_real_execution_state():
    rendered = interactive_app._render_scheduled_status(
        'selector-3080ti',
        [
            {
                'job_name': 'selector-3080ti',
                'enabled': True,
                'target_time': '23:59',
                'advance_hours': 1,
                'timezone': 'Asia/Shanghai',
                'latest_result': '',
                'latest_reason': '',
                'latest_summary': '',
                'latest_created_at': '',
                'latest_payload': {},
                'latest_instance_id': '',
            }
        ],
    )

    assert '规则开关' in rendered
    assert '已启用' in rendered
    assert '执行状态' in rendered
    assert '暂无检查记录' in rendered


def test_scheduled_detail_shows_single_runtime_status():
    job = ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='19:30', advance_hours=1)
    rendered = interactive_app._render_scheduled_job_detail(
        job,
        {
            'job_name': 'job-1',
            'enabled': True,
            'daemon_running': False,
            'target_time': '19:30',
            'advance_hours': 1,
        },
        'main',
    )

    assert '任务状态' in rendered
    assert '等待执行' in rendered
    assert '规则开关' not in rendered
    assert '后台轮询' not in rendered
    assert '执行方式' not in rendered


def test_scheduled_detail_marks_single_once_job_complete():
    job = ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='19:30', advance_hours=1, schedule_mode='once')
    rendered = interactive_app._render_scheduled_job_detail(
        job,
        {
            'job_name': 'job-1',
            'enabled': False,
            'daemon_running': False,
            'target_time': '19:30',
            'advance_hours': 1,
            'schedule_mode': 'once',
            'latest_result': 'already_running',
        },
        'main',
    )

    assert '任务状态' in rendered
    assert '单次已完成' in rendered


def test_scheduled_execution_status_marks_single_once_job_complete():
    label, tone = interactive_app._scheduled_execution_status(
        {
            'schedule_mode': 'once',
            'latest_result': 'already_running',
        }
    )

    assert label == '单次已完成'
    assert tone == 'ok'


def test_scheduled_run_result_state_disables_single_once_job_immediately():
    base_row = {
        'job_name': 'job-1',
        'enabled': True,
        'schedule_mode': 'once',
        'daemon_running': True,
        'target_time': '20:20',
        'advance_hours': 1,
    }
    results = [
        SimpleNamespace(
            result='already_running',
            reason='already_running',
            summary='实例已在 GPU 模式运行',
            instance_id='iid-1',
            candidate_count=0,
            candidate_details=[],
            selected_instance_id='iid-1',
            selected_instance_label='iid-1',
            selector_summary='',
            status='running',
        )
    ]

    state = interactive_app._scheduled_run_result_state(base_row, results, trigger_label='后台执行')

    assert state['enabled'] is False
    assert state['task_status_label'] == '单次已完成'
    assert state['task_status_tone'] == 'ok'


def test_render_scheduled_status_marks_single_once_job_complete():
    rendered = interactive_app._render_scheduled_status(
        'job-1',
        [
            {
                'job_name': 'job-1',
                'enabled': False,
                'target_mode': 'instance',
                'target_summary': '固定实例=iid-1',
                'target_time': '20:20',
                'advance_hours': 1,
                'schedule_mode': 'once',
                'latest_result': 'already_running',
                'latest_reason': 'already_running',
                'latest_summary': '实例已在 GPU 模式运行',
                'latest_created_at': '2026-04-09T20:15:00+08:00',
                'latest_payload': {},
                'latest_instance_id': 'iid-1',
            }
        ],
    )

    assert '执行状态' in rendered
    assert '单次已完成' in rendered
    assert '已抢到机器' in rendered


def test_scheduled_status_marks_not_yet_polled_before_first_window(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2026, 4, 8, 19, 26, tzinfo=ZoneInfo('Asia/Shanghai'))
            return base.astimezone(tz) if tz is not None else base

    monkeypatch.setattr(interactive_app, 'datetime', FixedDateTime)

    rendered = interactive_app._render_scheduled_status(
        'selector-3080ti',
        [
            {
                'job_name': 'selector-3080ti',
                'enabled': True,
                'target_time': '23:59',
                'advance_hours': 1,
                'timezone': 'Asia/Shanghai',
                'latest_result': '',
                'latest_reason': '',
                'latest_summary': '',
                'latest_created_at': '',
                'latest_payload': {},
                'latest_instance_id': '',
                'daemon_running': False,
            }
        ],
    )

    assert '最近检查时间' in rendered
    assert '暂无检查记录' in rendered
    assert '未检查原因' in rendered
    assert '尚未到首次轮询' in rendered


def test_scheduled_status_marks_daemon_not_started_when_window_open(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2026, 4, 8, 19, 26, tzinfo=ZoneInfo('Asia/Shanghai'))
            return base.astimezone(tz) if tz is not None else base

    monkeypatch.setattr(interactive_app, 'datetime', FixedDateTime)

    rendered = interactive_app._render_scheduled_status(
        'selector-3080ti',
        [
            {
                'job_name': 'selector-3080ti',
                'enabled': True,
                'target_time': '19:30',
                'advance_hours': 1,
                'timezone': 'Asia/Shanghai',
                'latest_result': '',
                'latest_reason': '',
                'latest_summary': '',
                'latest_created_at': '',
                'latest_payload': {},
                'latest_instance_id': '',
                'daemon_running': False,
            }
        ],
    )

    assert '暂无检查记录' in rendered
    assert '未检查原因' in rendered
    assert '后台未启动' in rendered


def test_scheduled_status_marks_missing_persisted_poll_when_daemon_running(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2026, 4, 8, 19, 26, tzinfo=ZoneInfo('Asia/Shanghai'))
            return base.astimezone(tz) if tz is not None else base

    monkeypatch.setattr(interactive_app, 'datetime', FixedDateTime)

    rendered = interactive_app._render_scheduled_status(
        'selector-3080ti',
        [
            {
                'job_name': 'selector-3080ti',
                'enabled': True,
                'target_time': '19:30',
                'advance_hours': 1,
                'timezone': 'Asia/Shanghai',
                'latest_result': '',
                'latest_reason': '',
                'latest_summary': '',
                'latest_created_at': '',
                'latest_payload': {},
                'latest_instance_id': '',
                'daemon_running': True,
            }
        ],
    )

    assert '暂无检查记录' in rendered
    assert '未检查原因' in rendered
    assert '轮询未落库' in rendered


def test_scheduled_job_status_rows_reads_prefixed_history_job_name(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token')],
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=5,
                jobs=[ScheduledStartJob(name='selector-3080ti', target_time='00:30', advance_hours=1)],
            )
        ),
    )
    store.add_scheduled_history(
        'main',
        'main:selector-3080ti',
        '',
        '2026-04-09',
        'waiting_for_instance',
        'selector_no_match',
        {
            'candidate_count': 0,
            'candidate_details': [],
            'target_time': '00:30',
            'deadline': '2026-04-09T00:30:00+08:00',
            'selector_summary': 'gpu_model=RTX 3080 Ti',
        },
        'scheduled.wait.instance',
        'info',
        '等待候选实例出现',
    )

    rows = cli.scheduled_job_status_rows(settings, store, account_name='main', job_name='selector-3080ti')

    assert len(rows) == 1
    assert rows[0]['latest_result'] == 'waiting_for_instance'
    assert rows[0]['latest_reason'] == 'selector_no_match'
    assert rows[0]['latest_summary'] == '等待候选实例出现'


def test_scheduled_candidate_panel_data_reads_prefixed_history_job_name(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token')],
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=5,
                jobs=[ScheduledStartJob(name='selector-3080ti', target_time='00:30', advance_hours=1)],
            )
        ),
    )
    store.add_scheduled_history(
        'main',
        'main:selector-3080ti',
        '',
        '2026-04-09',
        'waiting_for_instance',
        'selector_no_match',
        {
            'job_name': 'main:selector-3080ti',
            'candidate_count': 2,
            'candidate_details': [
                {'instance_id': 'iid-1', 'status': 'shutdown', 'reason': 'selector_no_match', 'selected': False},
                {'instance_id': 'iid-2', 'status': 'shutdown', 'reason': 'selector_no_match', 'selected': False},
            ],
            'target_time': '00:30',
            'deadline': '2026-04-09T00:30:00+08:00',
            'selector_summary': 'gpu_model=RTX 3080 Ti',
        },
        'scheduled.wait.instance',
        'info',
        '等待候选实例出现',
    )

    panel = cli.scheduled_candidate_panel_data(settings, store, account_name='main', job_name='selector-3080ti')

    assert panel is not None
    assert panel['job_name'] == 'main:selector-3080ti'
    assert len(panel['candidate_details']) == 2


def test_scheduled_detail_explains_running_rule_edit_auto_executes_once():
    job = ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='19:30', advance_hours=1)
    rendered = interactive_app._render_scheduled_job_detail(
        job,
        {
            'job_name': 'job-1',
            'enabled': True,
            'target_time': '19:30',
            'advance_hours': 1,
        },
        'main',
    )

    assert '执行方式' not in rendered


def test_scheduled_detail_explains_paused_rule_edit_does_not_auto_execute():
    job = ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='19:30', advance_hours=1)
    rendered = interactive_app._render_scheduled_job_detail(
        job,
        {
            'job_name': 'job-1',
            'enabled': False,
            'target_time': '19:30',
            'advance_hours': 1,
        },
        'main',
    )

    assert '执行方式' not in rendered


def test_scheduled_detail_selector_rule_uses_clear_target_labels():
    job = ScheduledStartJob(
        name='selector-3080ti',
        target_time='00:30',
        advance_hours=1,
        selector=ScheduledStartSelector(
            regions=['北京A区', '北京B区'],
            gpu_model='RTX 3080 Ti',
            gpu_count=1,
            charge_types=['payg'],
        ),
    )
    rendered = interactive_app._render_scheduled_job_detail(
        job,
        {
            'job_name': 'selector-3080ti',
            'enabled': True,
            'daemon_running': True,
            'target_time': '00:30',
            'advance_hours': 1,
        },
        'main',
    )

    assert '规则类型' not in rendered
    assert '匹配条件' not in rendered
    assert '目标方式' in rendered
    assert '按条件筛选候选机器' in rendered
    assert '筛选条件' in rendered
    assert 'GPU=RTX 3080 Ti' in rendered
    assert '计费=' not in rendered


def test_scheduled_detail_shows_schedule_mode():
    job = ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='19:30', advance_hours=1, schedule_mode='once')
    rendered = interactive_app._render_scheduled_job_detail(
        job,
        {
            'job_name': 'job-1',
            'enabled': True,
            'daemon_running': False,
            'target_time': '19:30',
            'advance_hours': 1,
        },
        'main',
    )

    assert '执行计划' in rendered
    assert '单次' in rendered


def test_scheduled_status_splits_candidate_summary_into_three_groups():
    rendered = interactive_app._render_scheduled_status(
        'selector-3080ti',
        [
            {
                'job_name': 'selector-3080ti',
                'enabled': True,
                'target_time': '23:59',
                'advance_hours': 1,
                'timezone': 'Asia/Shanghai',
                'latest_result': 'started',
                'latest_reason': 'started',
                'latest_summary': '',
                'latest_created_at': '2026-04-08T18:00:00+08:00',
                'latest_payload': {
                    'candidate_details': [
                        {'instance_id': 'iid-hit', 'selected': True, 'reason': 'started', 'status': 'running'},
                        {'instance_id': 'iid-wait', 'selected': False, 'reason': 'eligible', 'status': 'shutdown'},
                        {'instance_id': 'iid-drop', 'selected': False, 'reason': 'gpu_idle_zero', 'status': 'shutdown'},
                    ]
                },
                'latest_instance_id': 'iid-hit',
            }
        ],
    )

    assert '已命中' in rendered
    assert '等待中' in rendered
    assert '被淘汰' in rendered
    assert 'iid-hit' in rendered
    assert 'iid-wait' in rendered
    assert 'iid-drop' in rendered


def test_interactive_diagnostics_menu_replaces_records_and_config(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    answers = iter(['4', '0', '0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert '诊断' in captured.out
    assert '查看实例' in captured.out
    assert '查看 Keeper 探测' in captured.out
    assert '健康自检' in captured.out
    assert '配置诊断' in captured.out
    assert '启动后台服务' in captured.out
    assert '停止后台服务' in captured.out
    assert '重启后台服务' in captured.out


def test_diagnostics_menu_can_start_service(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)
    selections = iter(['5', '0'])
    seen = []

    monkeypatch.setattr(interactive_app, '_submit_snapshot_task', lambda **kwargs: None)
    monkeypatch.setattr(interactive_app, '_choose_menu_with_refresh', lambda *args, **kwargs: next(selections))
    monkeypatch.setattr(
        interactive_app,
        '_print_execution_summary',
        lambda title, **kwargs: seen.append((title, kwargs.get('detail'), kwargs.get('code'))),
    )
    try:
        interactive_app._diagnostics_menu(
            args=SimpleNamespace(config='config.yaml'),
            current_account='main',
            command_list_instances_fn=lambda args: 0,
            command_healthcheck_fn=lambda args: 0,
            settings=BASE_SETTINGS,
            store=store,
            keeper_probe_rows_fn=lambda *args, **kwargs: [],
            load_settings_fn=lambda path: BASE_SETTINGS,
            validate_settings_fn=lambda settings, path: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
            service_status_fn=lambda: {'installed': True, 'loaded': False, 'label': 'com.autodl.helper'},
            service_start_fn=lambda: (0, 'started'),
        )
    finally:
        task_manager.shutdown(wait=False)

    assert seen == [('已启动后台服务', 'started', 0)]


def test_diagnostics_menu_restart_writes_service_log_and_event(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)
    selections = iter(['7', '0'])
    seen = []
    config_path = str(tmp_path / 'config.yaml')
    write_raw_settings(config_path, asdict(BASE_SETTINGS))

    monkeypatch.setattr(interactive_app, '_submit_snapshot_task', lambda **kwargs: None)
    monkeypatch.setattr(interactive_app, '_choose_menu_with_refresh', lambda *args, **kwargs: next(selections))
    monkeypatch.setattr(
        interactive_app,
        '_print_execution_summary',
        lambda title, **kwargs: seen.append((title, kwargs.get('detail'), kwargs.get('code'))),
    )
    try:
        interactive_app._diagnostics_menu(
            args=SimpleNamespace(config=config_path),
            current_account='main',
            command_list_instances_fn=lambda args: 0,
            command_healthcheck_fn=lambda args: 0,
            settings=BASE_SETTINGS,
            store=store,
            keeper_probe_rows_fn=lambda *args, **kwargs: [],
            load_settings_fn=lambda path: BASE_SETTINGS,
            validate_settings_fn=lambda settings, path: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
            service_status_fn=lambda: {'installed': True, 'loaded': True, 'label': 'com.autodl.helper'},
            service_stop_fn=lambda: (0, 'stopped'),
            service_start_fn=lambda: (0, 'started'),
        )
    finally:
        task_manager.shutdown(wait=False)

    assert seen == [('已重启后台服务', 'started', 0)]
    log_text = (tmp_path / 'logs' / 'service.stdout.log').read_text(encoding='utf-8')
    assert '[服务管理] 已重启后台服务 label=autodl-helper' in log_text
    history = store.read_history(task_type='service', limit=5)
    assert any(row.task_type == 'service' and row.result == '已重启后台服务' for row in history)


def test_interactive_run_once_resumes_paused_scheduled_job(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.upsert_scheduled_job_control('main', 'job-1', enabled=False, source='interactive')
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(
        cli,
        'run_scheduled_start_cycle',
        lambda **kwargs: [
            ScheduledStartResult(
                result='started',
                reason='started',
                instance_id='iid-1',
                status='running',
                gpu_idle_num=1,
                start_mode='gpu',
                target_time='14:00',
                deadline='2026-04-08T14:00:00+08:00',
                event_type='scheduled.started',
                severity='success',
                summary='job started',
            )
        ],
    )

    answers = iter(['1', '1', '1', '', '0', '0', '0', '0', '0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])

    assert code == 0
    assert store.get_scheduled_job_control('main', 'job-1')['enabled'] is True


def test_interactive_can_resume_scheduled_job(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.upsert_scheduled_job_control('main', 'job-1', enabled=False, source='interactive')
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    answers = iter(['1', '1', '5', '0', '0', '0', '0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])

    assert code == 0
    assert store.get_scheduled_job_control('main', 'job-1')['enabled'] is True


def test_interactive_running_scheduled_rule_uses_running_focused_actions(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    answers = iter(['1', '1', '0', '0', '0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert '立即执行一轮' in captured.out
    assert '暂停任务' in captured.out
    assert '已启用' in captured.out
    assert '暂停/恢复任务' not in captured.out
    assert '启动后台轮询' not in captured.out
    assert '停止后台轮询' not in captured.out


def test_prompt_scheduled_time_settings_uses_menu_editor(monkeypatch):
    choices = iter([
        '1',   # 修改目标时间
        '4',   # 5 分钟精细选择
        '16',  # 小时=15
        '7',   # 分钟=30
        '2',   # 修改提前启动
        '4',   # 6小时
        '0',   # 返回
    ])
    monkeypatch.setattr(interactive_app, '_choose_menu', lambda *args, **kwargs: next(choices))

    target_time, advance_hours, timezone = interactive_app._prompt_scheduled_time_settings(
        target_time='14:00',
        advance_hours=2,
        timezone='Asia/Shanghai',
    )

    assert target_time == '15:30'
    assert advance_hours == 6
    assert timezone == 'Asia/Shanghai'


def test_prompt_scheduled_time_settings_supports_quick_shortcuts(monkeypatch):
    choices = iter([
        '1',   # 修改目标时间
        '3',   # 15 分钟刻度
        '17',  # 16 点
        '4',   # 45 分
        '0',   # 返回
    ])
    monkeypatch.setattr(interactive_app, '_choose_menu', lambda *args, **kwargs: next(choices))

    target_time, advance_hours, timezone = interactive_app._prompt_scheduled_time_settings(
        target_time='14:00',
        advance_hours=2,
        timezone='Asia/Shanghai',
    )

    assert target_time == '16:45'
    assert advance_hours == 2
    assert timezone == 'Asia/Shanghai'


def test_prompt_scheduled_time_settings_whole_hour_is_single_step(monkeypatch):
    choices = iter([
        '1',   # 修改目标时间
        '1',   # 整点
        '16',  # 15:00
        '0',   # 返回
    ])
    monkeypatch.setattr(interactive_app, '_choose_menu', lambda *args, **kwargs: next(choices))

    target_time, advance_hours, timezone = interactive_app._prompt_scheduled_time_settings(
        target_time='14:00',
        advance_hours=2,
        timezone='Asia/Shanghai',
    )

    assert target_time == '15:00'
    assert advance_hours == 2
    assert timezone == 'Asia/Shanghai'


def test_prompt_scheduled_time_settings_half_hour_is_single_step(monkeypatch):
    choices = iter([
        '1',   # 修改目标时间
        '2',   # 半点
        '16',  # 15:30
        '0',   # 返回
    ])
    monkeypatch.setattr(interactive_app, '_choose_menu', lambda *args, **kwargs: next(choices))

    target_time, advance_hours, timezone = interactive_app._prompt_scheduled_time_settings(
        target_time='14:00',
        advance_hours=2,
        timezone='Asia/Shanghai',
    )

    assert target_time == '15:30'
    assert advance_hours == 2
    assert timezone == 'Asia/Shanghai'


def test_coordinate_scheduled_background_starts_when_needed(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    calls = []
    settings = Settings(
        accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token')],
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                jobs=[ScheduledStartJob(name='job-1', instance_id='iid-1')],
            )
        ),
    )

    code, message = interactive_app._coordinate_scheduled_background(
        args=SimpleNamespace(config='config.yaml', lock_file='.lock', state_file='.state', headed=False),
        settings=settings,
        store=store,
        account_name='main',
        start_background_scheduled_fn=lambda args: calls.append(args.account) or (0, 'pid=4321'),
        stop_background_polling_fn=lambda settings, store: (0, 'stopped'),
        service_status_fn=lambda: {'installed': False, 'loaded': False},
    )

    assert code == 0
    assert calls == ['main']
    assert message == '已自动启动后台（fallback 模式）'


def test_coordinate_scheduled_background_does_not_restart_covering_daemon(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    mark_daemon_heartbeat(store, mode='scheduled_start', pid=4321, account='main', origin='interactive-auto')
    calls = []
    settings = Settings(
        accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token')],
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                jobs=[ScheduledStartJob(name='job-1', instance_id='iid-1')],
            )
        ),
    )

    code, message = interactive_app._coordinate_scheduled_background(
        args=SimpleNamespace(config='config.yaml', lock_file='.lock', state_file='.state', headed=False),
        settings=settings,
        store=store,
        account_name='main',
        start_background_scheduled_fn=lambda args: calls.append(args.account) or (0, 'pid=9999'),
        stop_background_polling_fn=lambda settings, store: (0, 'stopped'),
    )

    assert code == 0
    assert calls == []
    assert message == '后台已在运行，新规则已生效'


def test_coordinate_scheduled_background_stops_owned_daemon_when_no_enabled_jobs(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    mark_daemon_heartbeat(store, mode='scheduled_start', pid=4321, account='main', origin='interactive-auto')
    stops = []
    settings = Settings(
        accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token')],
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(enabled=True, jobs=[]),
        ),
    )

    code, message = interactive_app._coordinate_scheduled_background(
        args=SimpleNamespace(config='config.yaml', lock_file='.lock', state_file='.state', headed=False),
        settings=settings,
        store=store,
        account_name='main',
        start_background_scheduled_fn=lambda args: (0, 'pid=9999'),
        stop_background_polling_fn=lambda settings, store: stops.append(True) or (0, 'stopped'),
    )

    assert code == 0
    assert stops == [True]
    assert message == '已自动停止后台（当前无启用任务）'


def test_interactive_home_keeps_focus_on_two_main_tasks(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    answers = iter(['0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert '抢机器任务' in captured.out
    assert 'Keeper 状态' in captured.out
    assert 'Keeper 任务' in captured.out
    assert '最近失败事件摘要' not in captured.out
    assert 'keeper.failed.power_on' not in captured.out


def test_interactive_home_uses_ansi_colors(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    answers = iter(['0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert '\x1b[' in captured.out


def test_key_value_aligns_by_visible_width():
    line1 = interactive_app._key_value('当前账号', 'main')
    line2 = interactive_app._key_value('Keeper 状态', '运行中')

    clean1 = interactive_app._strip_ansi(line1)
    clean2 = interactive_app._strip_ansi(line2)

    assert interactive_app._display_width(clean1.split(':', 1)[0]) == interactive_app._display_width(clean2.split(':', 1)[0])


def test_boxed_lines_aligns_cjk_and_ansi_width():
    rows = interactive_app._boxed_lines(
        '标题',
        [
            interactive_app._heading('账号详情: main'),
            '运行中实例: 12',
            '一周内到期: 3',
        ],
    )

    clean_rows = [interactive_app._strip_ansi(row) for row in rows]
    widths = [interactive_app._display_width(row) for row in clean_rows]

    assert len(set(widths)) == 1


def test_interactive_home_no_longer_shows_records_or_config_menu(tmp_path, monkeypatch, capsys):
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token')],
        tasks=TaskSettings(
            keeper=KeeperSettings(enabled=True),
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=300,
                jobs=[
                    ScheduledStartJob(
                        instance_id='',
                        name='job-1',
                        target_time='14:00',
                        advance_hours=2,
                        selector=ScheduledStartSelector(gpu_model='RTX 4090', gpu_count=1),
                        priority=[
                            ScheduledStartPriority(instance_id='iid-1'),
                            ScheduledStartPriority(region='华北', machine_alias='B'),
                        ],
                    )
                ],
            ),
        ),
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: settings)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    answers = iter(['0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert '候选解释' not in captured.out
    assert '运行记录' not in captured.out
    assert '配置' not in captured.out


def test_interactive_can_create_edit_and_delete_scheduled_job(tmp_path, monkeypatch):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        yaml.safe_dump(
            {
                'auth': {'authorization': 'Bearer token'},
                'accounts': [{'name': 'main', 'enabled': True, 'authorization': 'Bearer token'}],
                'tasks': {
                    'keeper': {'enabled': True},
                    'scheduled_start': {
                        'enabled': True,
                        'poll_interval_seconds': 300,
                        'jobs': [
                            {
                                'instance_id': 'iid-1',
                                'name': 'job-1',
                                'target_time': '14:00',
                                'advance_hours': 2,
                            }
                        ],
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(interactive_app, 'read_launch_agent_status', lambda: {'installed': False, 'loaded': False})
    starts = []
    seen = []
    monkeypatch.setattr(
        cli,
        'start_background_scheduled_polling',
        lambda args: starts.append(args.account) or (mark_daemon_heartbeat(store, mode='scheduled_start', pid=4321, account='main', origin='interactive-auto') or (0, 'pid=4321')),
    )
    monkeypatch.setattr(cli, 'stop_background_polling', lambda settings, store: (0, 'stopped'))
    monkeypatch.setattr(
        cli,
        'run_scheduled_start_cycle',
        lambda *, settings, headed, state_file, account_name=None, store=None, force_run_now=False: (
            seen.append(
                {
                    'name': settings.tasks.scheduled_start.jobs[0].name,
                    'target_time': settings.tasks.scheduled_start.jobs[0].target_time,
                    'advance_hours': settings.tasks.scheduled_start.jobs[0].advance_hours,
                    'force_run_now': force_run_now,
                }
            )
            or []
        ),
    )

    answers = iter([
        '1',      # 抢机器
        'n',      # 新建任务
        '1',      # 修改任务名称
        'job-2',  # name
        '3',      # 修改目标条件
        'iid-2',
        '4',      # 修改时间设置
        '1',      # 修改目标时间
        '4',      # 5分钟精细选择
        '16',     # 15点
        '7',      # 30分
        '2',      # 修改提前启动
        '4',      # 6小时
        '0',      # 返回时间设置
        'c',      # 保存
        '0',      # 关闭创建结果页
        '2',      # 选中 job-2
        '4',      # 修改规则
        '4',      # 修改时间设置
        '1',      # 修改目标时间
        '4',      # 5分钟精细选择
        '17',     # 16点
        '1',      # 00分
        '2',      # 修改提前启动
        '5',      # 12小时
        '0',      # 返回时间设置
        'c',      # 保存
        '6',      # 删除任务
        '',       # confirm delete, blank means yes
        '0',      # 返回规则列表
        '0',      # 返回首页
        '0',      # 退出
    ])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', str(config_path)])

    assert code == 0
    assert starts in ([], ['main'])
    assert seen == [
        {'name': 'job-2', 'target_time': '15:30', 'advance_hours': 6, 'force_run_now': True},
        {'name': 'job-2', 'target_time': '16:00', 'advance_hours': 12, 'force_run_now': True},
    ]
    settings = load_settings(str(config_path))
    job_names = [job.name or job.instance_id for job in settings.tasks.scheduled_start.jobs]
    assert job_names == ['job-1']
    assert all((job.timezone or 'Asia/Shanghai') == 'Asia/Shanghai' for job in settings.tasks.scheduled_start.jobs)


def test_interactive_can_delete_last_scheduled_job_and_disable_task(tmp_path, monkeypatch):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        yaml.safe_dump(
            {
                'auth': {'authorization': 'Bearer token'},
                'accounts': [{'name': 'main', 'enabled': True, 'authorization': 'Bearer token'}],
                'tasks': {
                    'keeper': {'enabled': True},
                    'scheduled_start': {
                        'enabled': True,
                        'poll_interval_seconds': 300,
                        'jobs': [
                            {'instance_id': 'iid-1', 'name': 'job-1', 'target_time': '14:00', 'advance_hours': 2}
                        ],
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    mark_daemon_heartbeat(store, mode='scheduled_start', pid=4321, account='main', origin='interactive-auto')
    stop_calls = []
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(
        cli,
        'start_background_scheduled_polling',
        lambda args: (mark_daemon_heartbeat(store, mode='scheduled_start', pid=4321, account='main', origin='interactive-auto') or (0, 'pid=4321')),
    )
    monkeypatch.setattr(
        cli,
        'stop_background_polling',
        lambda settings, store: (stop_calls.append(True) or clear_daemon_heartbeat(store) or (0, 'stopped')),
    )

    answers = iter([
        '1',  # 抢机器
        '1',  # 选中唯一任务
        '6',  # 删除任务
        '',   # 确认删除
        '0',  # 关闭结果页
        '0',  # 返回首页
        '0',  # 退出
    ])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', str(config_path)])

    assert code == 0
    assert stop_calls == [True]
    settings = load_settings(str(config_path))
    assert settings.tasks.scheduled_start.enabled is False
    assert settings.tasks.scheduled_start.jobs == []


def test_interactive_edit_scheduled_job_returns_to_detail_with_progress_near_top(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        yaml.safe_dump(
            {
                'auth': {'authorization': 'Bearer token'},
                'accounts': [{'name': 'main', 'enabled': True, 'authorization': 'Bearer token'}],
                'tasks': {
                    'keeper': {'enabled': True},
                    'scheduled_start': {
                        'enabled': True,
                        'poll_interval_seconds': 300,
                        'jobs': [
                            {'instance_id': 'iid-1', 'name': 'job-1', 'target_time': '14:00', 'advance_hours': 2}
                        ],
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    answers = iter([
        '1',  # 抢机器
        '1',  # job-1
        '3',  # 修改规则
        '4',  # 编辑时间设置
        '15:00',
        '3',
        '',
        'c',  # 保存
        '0',  # 返回规则列表
        '0',  # 返回首页
        '0',  # 退出
    ])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', str(config_path)])
    captured = capsys.readouterr()

    assert code == 0
    assert '已更新抢机器规则' not in captured.out
    assert '查看抢机进度' in captured.out
    assert captured.out.rfind('查看抢机进度') < captured.out.rfind('修改规则')


def test_interactive_edit_enabled_job_runs_once_with_latest_config(tmp_path, monkeypatch):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        yaml.safe_dump(
            {
                'auth': {'authorization': 'Bearer token'},
                'accounts': [{'name': 'main', 'enabled': True, 'authorization': 'Bearer token'}],
                'tasks': {
                    'keeper': {'enabled': True},
                    'scheduled_start': {
                        'enabled': True,
                        'poll_interval_seconds': 300,
                        'jobs': [
                            {'instance_id': 'iid-1', 'name': 'job-1', 'target_time': '14:00', 'advance_hours': 2}
                        ],
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    seen = []

    def fake_run_scheduled_start_cycle(*, settings, headed, state_file, account_name=None, store=None, force_run_now=False):
        job = settings.tasks.scheduled_start.jobs[0]
        seen.append({'name': job.name, 'target_time': job.target_time, 'advance_hours': job.advance_hours, 'force_run_now': force_run_now})
        return [
            ScheduledStartResult(
                result='waiting_for_instance',
                reason='selector_no_match',
                instance_id='',
                status='shutdown',
                gpu_idle_num=0,
                start_mode='',
                target_time=job.target_time,
                deadline='2026-04-08T15:00:00+08:00',
                event_type='scheduled.waiting',
                severity='info',
                summary='waiting',
            )
        ]

    monkeypatch.setattr(cli, 'run_scheduled_start_cycle', fake_run_scheduled_start_cycle)

    answers = iter([
        '1',  # 抢机器
        '1',  # job-1
        '4',  # 修改规则
        '4',  # 编辑时间设置
        '15:00',
        '3',
        '',
        'c',  # 保存并自动执行
        '0',  # 关闭执行结果页/返回规则详情
        '0',  # 返回规则列表
        '0',  # 返回首页
        '0',  # 退出
    ])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', str(config_path)])

    assert code == 0
    assert seen == [{'name': 'job-1', 'target_time': '15:00', 'advance_hours': 3, 'force_run_now': True}]


def test_interactive_modify_rule_returns_to_detail_with_recent_run_summary(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        yaml.safe_dump(
            {
                'auth': {'authorization': 'Bearer token'},
                'accounts': [{'name': 'main', 'enabled': True, 'authorization': 'Bearer token'}],
                'tasks': {
                    'keeper': {'enabled': True},
                    'scheduled_start': {
                        'enabled': True,
                        'poll_interval_seconds': 300,
                        'jobs': [
                            {'instance_id': 'iid-1', 'name': 'job-1', 'target_time': '14:00', 'advance_hours': 2}
                        ],
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(
        cli,
        'run_scheduled_start_cycle',
        lambda *, settings, headed, state_file, account_name=None, store=None, force_run_now=False: [
            ScheduledStartResult(
                result='waiting_for_instance',
                reason='selector_no_match',
                instance_id='',
                status='shutdown',
                gpu_idle_num=0,
                start_mode='',
                target_time=settings.tasks.scheduled_start.jobs[0].target_time,
                deadline='2026-04-08T15:00:00+08:00',
                event_type='scheduled.waiting',
                severity='info',
                summary='waiting',
            )
        ],
    )

    answers = iter([
        '1',  # 抢机器
        '1',  # job-1
        '4',  # 修改规则
        '4',  # 编辑时间设置
        '15:00',
        '3',
        '',
        'c',  # 保存并自动执行
        '0',  # 关闭执行结果页
        '0',  # 返回规则列表
        '0',  # 返回首页
        '0',  # 退出
    ])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', str(config_path)])
    captured = capsys.readouterr()

    assert code == 0
    assert '抢机器执行结果: job-1' not in captured.out
    assert '最近执行' in captured.out
    assert '修改规则后自动执行' in captured.out
    assert '本次结果' in captured.out
    assert '查看抢机进度' in captured.out


def test_interactive_modify_rule_captures_auto_run_logs(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        yaml.safe_dump(
            {
                'auth': {'authorization': 'Bearer token'},
                'accounts': [{'name': 'main', 'enabled': True, 'authorization': 'Bearer token'}],
                'tasks': {
                    'keeper': {'enabled': True},
                    'scheduled_start': {
                        'enabled': True,
                        'poll_interval_seconds': 300,
                        'jobs': [
                            {'instance_id': 'iid-1', 'name': 'job-1', 'target_time': '14:00', 'advance_hours': 2}
                        ],
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    def fake_run_scheduled_start_cycle(*, settings, headed, state_file, account_name=None, store=None, force_run_now=False):
        print('LEAKED_STDOUT_LOG')
        logging.getLogger().warning('LEAKED_ROOT_WARNING')
        logging.getLogger('autodl_helper.cli_handlers').warning('LEAKED_CHILD_WARNING')
        return [
            ScheduledStartResult(
                result='waiting_for_instance',
                reason='selector_no_match',
                instance_id='',
                status='shutdown',
                gpu_idle_num=0,
                start_mode='',
                target_time=settings.tasks.scheduled_start.jobs[0].target_time,
                deadline='2026-04-08T15:00:00+08:00',
                event_type='scheduled.waiting',
                severity='info',
                summary='waiting',
            )
        ]

    monkeypatch.setattr(cli, 'run_scheduled_start_cycle', fake_run_scheduled_start_cycle)

    answers = iter([
        '1',  # 抢机器
        '1',  # job-1
        '4',  # 修改规则
        '4',  # 编辑时间设置
        '15:00',
        '3',
        '',
        'c',  # 保存并自动执行
        '0',  # 返回规则列表
        '0',  # 返回首页
        '0',  # 退出
    ])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', str(config_path)])
    captured = capsys.readouterr()

    assert code == 0
    assert 'LEAKED_STDOUT_LOG' not in captured.out
    assert 'LEAKED_ROOT_WARNING' not in captured.out
    assert 'LEAKED_CHILD_WARNING' not in captured.out


def test_interactive_modify_rule_queues_background_auto_run(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        yaml.safe_dump(
            {
                'auth': {'authorization': 'Bearer token'},
                'accounts': [{'name': 'main', 'enabled': True, 'authorization': 'Bearer token'}],
                'tasks': {
                    'keeper': {'enabled': True},
                    'scheduled_start': {
                        'enabled': True,
                        'poll_interval_seconds': 300,
                        'jobs': [
                            {'instance_id': 'iid-1', 'name': 'job-1', 'target_time': '14:00', 'advance_hours': 2}
                        ],
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    def slow_run_scheduled_start_cycle(*, settings, headed, state_file, account_name=None, store=None, force_run_now=False):
        time.sleep(0.4)
        return [
            ScheduledStartResult(
                result='waiting_for_instance',
                reason='selector_no_match',
                instance_id='',
                status='shutdown',
                gpu_idle_num=0,
                start_mode='',
                target_time=settings.tasks.scheduled_start.jobs[0].target_time,
                deadline='2026-04-08T15:00:00+08:00',
                event_type='scheduled.waiting',
                severity='info',
                summary='waiting',
            )
        ]

    monkeypatch.setattr(cli, 'run_scheduled_start_cycle', slow_run_scheduled_start_cycle)

    answers = iter([
        '1',
        '1',
        '4',
        '4',
        '15:00',
        '3',
        '',
        'c',
        '0',
        '0',
        '0',
    ])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    started_at = time.time()
    code = cli.main(['interactive', '--config', str(config_path)])
    duration = time.time() - started_at
    captured = capsys.readouterr()

    assert code == 0
    assert duration < 0.35
    assert '修改规则后自动执行' in captured.out
    assert ('排队中' in captured.out) or ('正在执行' in captured.out)


def test_scheduled_menu_detail_refresh_keeps_ui_alive_when_status_store_temporarily_fails(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = BASE_SETTINGS
    args = SimpleNamespace(config=str(tmp_path / 'config.yaml'), headed=False, state_file='.autodl-helper-state.json')
    write_raw_settings(args.config, asdict(settings))

    call_count = {'value': 0}
    fake_now = {'value': 100.0}

    def flaky_status_rows(settings, store, *, account_name=None, job_name=None, limit=5):
        call_count['value'] += 1
        if call_count['value'] >= 2:
            raise sqlite3.OperationalError('unable to open database file')
        return [
            {
                'job_name': 'job-1',
                'enabled': True,
                'target_time': '14:00',
                'advance_hours': 2,
                'schedule_mode': 'daily',
                'timezone': 'Asia/Shanghai',
                'latest_result': '',
                'latest_reason': '',
                'latest_summary': '',
                'latest_created_at': '',
                'latest_payload': {},
                'latest_instance_id': '',
                'has_history': False,
                'latest_matches_current_rule': False,
                'task_status_label': '轮询中',
                'task_status_tone': 'ok',
                'daemon_running': True,
            }
        ]

    selections = iter(['1', '0', '0'])

    def fake_choose_menu(title, items, **kwargs):
        refresh_fn = kwargs.get('refresh_fn')
        if refresh_fn is not None:
            fake_now['value'] += 1.1
            refresh_fn(kwargs.get('default_key'))
        print(title)
        return next(selections)

    monkeypatch.setattr(interactive_app, '_choose_menu', fake_choose_menu)
    monkeypatch.setattr(interactive_app.time, 'monotonic', lambda: fake_now['value'])
    monkeypatch.setattr(
        interactive_app,
        '_nudge_background_tasks',
        lambda task_manager, settle_seconds=0.0: (
            task_manager.start_pending(),
            time.sleep(0.02),
            task_manager.drain_completed(),
        ),
    )
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)
    try:
        interactive_app._scheduled_menu(
            args,
            settings=settings,
            current_account='main',
            run_variant_fn=lambda *args, **kwargs: 0,
            start_background_scheduled_fn=lambda *args, **kwargs: (0, 'started'),
            stop_background_polling_fn=lambda *args, **kwargs: (0, 'stopped'),
            run_scheduled_start_cycle_fn=lambda **kwargs: [],
            set_job_enabled_fn=lambda *args, **kwargs: None,
            set_job_override_fn=lambda *args, **kwargs: None,
            request_reload_fn=lambda store: None,
            store=store,
            scheduled_job_status_rows_fn=flaky_status_rows,
            load_settings_fn=lambda path: settings,
            validate_settings_fn=lambda settings, path: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
    finally:
        task_manager.shutdown(wait=False)
    captured = capsys.readouterr()

    assert '抢机器规则' in captured.out
    assert '错误信息' in captured.out
    assert '数据库打开失败' in captured.out


def test_scheduled_menu_failed_status_refresh_uses_retry_backoff(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = BASE_SETTINGS
    args = SimpleNamespace(config=str(tmp_path / 'config.yaml'), headed=False, state_file='.autodl-helper-state.json')
    write_raw_settings(args.config, asdict(settings))

    call_count = {'value': 0}
    fake_now = {'value': 100.0}

    def flaky_status_rows(settings, store, *, account_name=None, job_name=None, limit=5):
        call_count['value'] += 1
        if call_count['value'] >= 2:
            raise sqlite3.OperationalError('unable to open database file')
        return [
            {
                'job_name': 'job-1',
                'enabled': True,
                'target_time': '14:00',
                'advance_hours': 2,
                'schedule_mode': 'daily',
                'timezone': 'Asia/Shanghai',
                'latest_result': '',
                'latest_reason': '',
                'latest_summary': '',
                'latest_created_at': '',
                'latest_payload': {},
                'latest_instance_id': '',
                'has_history': False,
                'latest_matches_current_rule': False,
                'task_status_label': '轮询中',
                'task_status_tone': 'ok',
                'daemon_running': True,
            }
        ]

    selections = iter(['1', '0', '0'])

    def fake_choose_menu(title, items, **kwargs):
        refresh_fn = kwargs.get('refresh_fn')
        if refresh_fn is not None:
            fake_now['value'] += 1.1
            refresh_fn(kwargs.get('default_key'))
            fake_now['value'] += 0.1
            refresh_fn(kwargs.get('default_key'))
            fake_now['value'] += 0.1
            refresh_fn(kwargs.get('default_key'))
        return next(selections)

    monkeypatch.setattr(interactive_app, '_choose_menu', fake_choose_menu)
    monkeypatch.setattr(interactive_app.time, 'monotonic', lambda: fake_now['value'])
    monkeypatch.setattr(
        interactive_app,
        '_nudge_background_tasks',
        lambda task_manager, settle_seconds=0.0: (
            task_manager.start_pending(),
            time.sleep(0.02),
            task_manager.drain_completed(),
        ),
    )

    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)
    try:
        interactive_app._scheduled_menu(
            args,
            settings=settings,
            current_account='main',
            run_variant_fn=lambda *args, **kwargs: 0,
            start_background_scheduled_fn=lambda *args, **kwargs: (0, 'started'),
            stop_background_polling_fn=lambda *args, **kwargs: (0, 'stopped'),
            run_scheduled_start_cycle_fn=lambda **kwargs: [],
            set_job_enabled_fn=lambda *args, **kwargs: None,
            set_job_override_fn=lambda *args, **kwargs: None,
            request_reload_fn=lambda store: None,
            store=store,
            scheduled_job_status_rows_fn=flaky_status_rows,
            load_settings_fn=lambda path: settings,
            validate_settings_fn=lambda settings, path: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
    finally:
        task_manager.shutdown(wait=False)

    assert call_count['value'] == 2


def test_scheduled_menu_list_refresh_uses_snapshot_cache_between_idle_ticks(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = BASE_SETTINGS
    args = SimpleNamespace(config=str(tmp_path / 'config.yaml'), headed=False, state_file='.autodl-helper-state.json')
    write_raw_settings(args.config, asdict(settings))

    call_count = {'value': 0}
    fake_now = {'value': 100.0}

    def status_rows(settings, store, *, account_name=None, job_name=None, limit=5):
        call_count['value'] += 1
        return [
            {
                'job_name': 'job-1',
                'enabled': True,
                'target_time': '14:00',
                'advance_hours': 2,
                'schedule_mode': 'daily',
                'timezone': 'Asia/Shanghai',
                'latest_result': '',
                'latest_reason': '',
                'latest_summary': '',
                'latest_created_at': '',
                'latest_payload': {},
                'latest_instance_id': '',
                'has_history': False,
                'latest_matches_current_rule': False,
                'task_status_label': '轮询中',
                'task_status_tone': 'ok',
                'daemon_running': True,
            }
        ]

    selections = iter(['0'])

    def fake_choose_menu(title, items, **kwargs):
        refresh_fn = kwargs.get('refresh_fn')
        if refresh_fn is not None:
            refresh_fn(kwargs.get('default_key'))
            refresh_fn(kwargs.get('default_key'))
            refresh_fn(kwargs.get('default_key'))
        return next(selections)

    monkeypatch.setattr(interactive_app, '_choose_menu', fake_choose_menu)
    monkeypatch.setattr(interactive_app.time, 'monotonic', lambda: fake_now['value'])

    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)
    try:
        interactive_app._scheduled_menu(
            args,
            settings=settings,
            current_account='main',
            run_variant_fn=lambda *args, **kwargs: 0,
            start_background_scheduled_fn=lambda *args, **kwargs: (0, 'started'),
            stop_background_polling_fn=lambda *args, **kwargs: (0, 'stopped'),
            run_scheduled_start_cycle_fn=lambda **kwargs: [],
            set_job_enabled_fn=lambda *args, **kwargs: None,
            set_job_override_fn=lambda *args, **kwargs: None,
            request_reload_fn=lambda store: None,
            store=store,
            scheduled_job_status_rows_fn=status_rows,
            load_settings_fn=lambda path: settings,
            validate_settings_fn=lambda settings, path: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
    finally:
        task_manager.shutdown(wait=False)

    assert call_count['value'] == 1


def test_scheduled_menu_detail_refresh_uses_snapshot_cache_between_idle_ticks(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = BASE_SETTINGS
    args = SimpleNamespace(config=str(tmp_path / 'config.yaml'), headed=False, state_file='.autodl-helper-state.json')
    write_raw_settings(args.config, asdict(settings))

    call_count = {'value': 0}
    fake_now = {'value': 100.0}

    def status_rows(settings, store, *, account_name=None, job_name=None, limit=5):
        call_count['value'] += 1
        return [
            {
                'job_name': 'job-1',
                'enabled': True,
                'target_time': '14:00',
                'advance_hours': 2,
                'schedule_mode': 'daily',
                'timezone': 'Asia/Shanghai',
                'latest_result': '',
                'latest_reason': '',
                'latest_summary': '',
                'latest_created_at': '',
                'latest_payload': {},
                'latest_instance_id': '',
                'has_history': False,
                'latest_matches_current_rule': False,
                'task_status_label': '轮询中',
                'task_status_tone': 'ok',
                'daemon_running': True,
            }
        ]

    selections = iter(['1', '0', '0'])
    invocation = {'value': 0}

    def fake_choose_menu(title, items, **kwargs):
        invocation['value'] += 1
        refresh_fn = kwargs.get('refresh_fn')
        if invocation['value'] == 2 and refresh_fn is not None:
            refresh_fn(kwargs.get('default_key'))
            refresh_fn(kwargs.get('default_key'))
            refresh_fn(kwargs.get('default_key'))
        return next(selections)

    monkeypatch.setattr(interactive_app, '_choose_menu', fake_choose_menu)
    monkeypatch.setattr(interactive_app.time, 'monotonic', lambda: fake_now['value'])

    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)
    try:
        interactive_app._scheduled_menu(
            args,
            settings=settings,
            current_account='main',
            run_variant_fn=lambda *args, **kwargs: 0,
            start_background_scheduled_fn=lambda *args, **kwargs: (0, 'started'),
            stop_background_polling_fn=lambda *args, **kwargs: (0, 'stopped'),
            run_scheduled_start_cycle_fn=lambda **kwargs: [],
            set_job_enabled_fn=lambda *args, **kwargs: None,
            set_job_override_fn=lambda *args, **kwargs: None,
            request_reload_fn=lambda store: None,
            store=store,
            scheduled_job_status_rows_fn=status_rows,
            load_settings_fn=lambda path: settings,
            validate_settings_fn=lambda settings, path: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
    finally:
        task_manager.shutdown(wait=False)

    assert call_count['value'] == 1


def test_scheduled_menu_shows_seeded_job_rows_before_snapshot_ready(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = BASE_SETTINGS
    args = SimpleNamespace(config=str(tmp_path / 'config.yaml'), headed=False, state_file='.autodl-helper-state.json')
    write_raw_settings(args.config, asdict(settings))

    rendered_items = {}

    def slow_status_rows(settings, store, *, account_name=None, job_name=None, limit=5):
        time.sleep(0.2)
        return []

    def fake_choose_menu(title, items, **kwargs):
        rendered_items['labels'] = [item.label for item in items]
        return '0'

    monkeypatch.setattr(interactive_app, '_choose_menu', fake_choose_menu)
    monkeypatch.setattr(
        interactive_app,
        '_nudge_background_tasks',
        lambda task_manager, settle_seconds=0.0: task_manager.start_pending(),
    )

    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)
    started_at = time.time()
    try:
        interactive_app._scheduled_menu(
            args,
            settings=settings,
            current_account='main',
            run_variant_fn=lambda *args, **kwargs: 0,
            start_background_scheduled_fn=lambda *args, **kwargs: (0, 'started'),
            stop_background_polling_fn=lambda *args, **kwargs: (0, 'stopped'),
            run_scheduled_start_cycle_fn=lambda **kwargs: [],
            set_job_enabled_fn=lambda *args, **kwargs: None,
            set_job_override_fn=lambda *args, **kwargs: None,
            request_reload_fn=lambda store: None,
            store=store,
            scheduled_job_status_rows_fn=slow_status_rows,
            load_settings_fn=lambda path: settings,
            validate_settings_fn=lambda settings, path: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
    finally:
        task_manager.shutdown(wait=False)
    duration = time.time() - started_at

    assert duration < 0.15
    assert any('job-1' in label for label in rendered_items['labels'])


def test_keeper_menu_continue_does_not_block_on_sync_probe_fetch(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = BASE_SETTINGS
    args = SimpleNamespace(config=str(tmp_path / 'config.yaml'), headed=False, state_file='.autodl-helper-state.json')
    write_raw_settings(args.config, asdict(settings))

    call_count = {'value': 0}

    def slow_probe_rows(settings, store, *, account_name=None):
        call_count['value'] += 1
        time.sleep(0.2)
        return []

    selections = iter(['1', '0', '0'])
    rendered = []

    def fake_choose_menu(title, items, **kwargs):
        rendered.append(title)
        return next(selections)

    monkeypatch.setattr(interactive_app, '_choose_menu', fake_choose_menu)
    monkeypatch.setattr(
        interactive_app,
        '_nudge_background_tasks',
        lambda task_manager, settle_seconds=0.0: task_manager.start_pending(),
    )

    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)
    started_at = time.time()
    try:
        interactive_app._keeper_menu(
            args,
            settings=settings,
            current_account='main',
            set_task_enabled_fn=lambda *args, **kwargs: None,
            request_reload_fn=lambda store: None,
            store=store,
            keeper_probe_rows_fn=slow_probe_rows,
            run_keeper_only_fn=lambda **kwargs: [],
            command_history_fn=lambda *args, **kwargs: 0,
            load_settings_fn=lambda path: settings,
            validate_settings_fn=lambda settings, path: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
    finally:
        task_manager.shutdown(wait=False)
    duration = time.time() - started_at

    assert duration < 0.15
    assert any('Keeper 规则确认' in title for title in rendered)
    assert any('Keeper 检测结果' in title for title in rendered)


def test_keeper_menu_start_execute_does_not_block_on_sync_keeper_run(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = BASE_SETTINGS
    args = SimpleNamespace(config=str(tmp_path / 'config.yaml'), headed=False, state_file='.autodl-helper-state.json')
    write_raw_settings(args.config, asdict(settings))

    rendered = []
    choose_calls = {'value': 0}
    plain_choose_calls = {'value': 0}
    started = threading.Event()
    release = threading.Event()

    def fake_choose_menu(title, items, **kwargs):
        rendered.append(title)
        plain_choose_calls['value'] += 1
        return '1' if plain_choose_calls['value'] == 1 else '0'

    def fake_choose_menu_with_refresh(title, items, **kwargs):
        choose_calls['value'] += 1
        rendered.append(title)
        if choose_calls['value'] == 1:
            return '4'
        return '0'

    monkeypatch.setattr(interactive_app, '_choose_menu', fake_choose_menu)
    monkeypatch.setattr(interactive_app, '_choose_menu_with_refresh', fake_choose_menu_with_refresh)
    monkeypatch.setattr(interactive_app, '_confirm_action', lambda *args, **kwargs: True)
    monkeypatch.setattr(
        interactive_app,
        '_nudge_background_tasks',
        lambda task_manager, settle_seconds=0.0: task_manager.start_pending(),
    )

    def probe_rows(settings, store, *, account_name=None):
        return []

    def slow_run_keeper_only(**kwargs):
        started.set()
        release.wait(timeout=1.0)
        return []

    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store, max_workers=2)
    started_at = time.time()
    try:
        interactive_app._keeper_menu(
            args,
            settings=settings,
            current_account='main',
            set_task_enabled_fn=lambda *args, **kwargs: None,
            request_reload_fn=lambda store: None,
            store=store,
            keeper_probe_rows_fn=probe_rows,
            run_keeper_only_fn=slow_run_keeper_only,
            command_history_fn=lambda *args, **kwargs: 0,
            load_settings_fn=lambda path: settings,
            validate_settings_fn=lambda settings, path: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
        duration = time.time() - started_at

        assert duration < 0.15
        assert started.wait(timeout=0.2)
        assert any('正在执行 Keeper' in interactive_app._strip_ansi(title) for title in rendered)
    finally:
        release.set()
        deadline = time.time() + 1.0
        while time.time() < deadline:
            task_manager.drain_completed()
            task = task_manager.get_task('keeper_execute_run', 'main')
            if task is None or task.status in {'succeeded', 'failed'}:
                break
            time.sleep(0.01)
        task_manager.shutdown(wait=True)


def test_interactive_modify_rule_reactivates_once_completed_job(tmp_path, monkeypatch):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        yaml.safe_dump(
            {
                'auth': {'authorization': 'Bearer token'},
                'accounts': [{'name': 'main', 'enabled': True, 'authorization': 'Bearer token'}],
                'tasks': {
                    'keeper': {'enabled': True},
                    'scheduled_start': {
                        'enabled': True,
                        'poll_interval_seconds': 300,
                        'jobs': [
                            {
                                'instance_id': 'iid-1',
                                'name': 'job-1',
                                'target_time': '14:00',
                                'advance_hours': 2,
                                'schedule_mode': 'once',
                            }
                        ],
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.upsert_scheduled_job_control(
        'main',
        'job-1',
        enabled=False,
        target_time_override='',
        advance_hours_override=None,
        source='scheduled_once_complete',
    )
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    seen = []

    def fake_run_scheduled_start_cycle(*, settings, headed, state_file, account_name=None, store=None, force_run_now=False):
        seen.append(force_run_now)
        return [
            ScheduledStartResult(
                result='waiting_for_instance',
                reason='selector_no_match',
                instance_id='',
                status='shutdown',
                gpu_idle_num=0,
                start_mode='',
                target_time=settings.tasks.scheduled_start.jobs[0].target_time,
                deadline='2026-04-08T15:00:00+08:00',
                event_type='scheduled.waiting',
                severity='info',
                summary='waiting',
            )
        ]

    monkeypatch.setattr(cli, 'run_scheduled_start_cycle', fake_run_scheduled_start_cycle)

    answers = iter([
        '1',  # 抢机器
        '1',  # job-1
        '4',  # 修改规则
        '4',  # 编辑时间设置
        '15:00',
        '3',
        '',
        'c',  # 保存并自动执行
        '0',  # 关闭执行结果页
        '0',  # 返回规则列表
        '0',  # 返回首页
        '0',  # 退出
    ])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', str(config_path)])

    assert code == 0
    assert seen == [True]
    control = store.get_scheduled_job_control('main', 'job-1')
    assert control is not None
    assert control['enabled'] is True


def test_persist_job_changes_reenables_scheduled_start_when_jobs_exist(tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        yaml.safe_dump(
            {
                'auth': {'authorization': 'Bearer token'},
                'accounts': [{'name': 'main', 'enabled': True, 'authorization': 'Bearer token'}],
                'tasks': {
                    'scheduled_start': {
                        'enabled': False,
                        'poll_interval_seconds': 300,
                        'jobs': [],
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    settings = cli.load_settings(str(config_path))

    interactive_app._persist_job_changes(
        config_path=str(config_path),
        settings=settings,
        load_settings_fn=cli.load_settings,
        validate_settings_fn=cli.validate_settings,
        mutator=lambda jobs: jobs.append(
            {
                'instance_id': 'iid-1',
                'name': 'job-1',
                'target_time': '15:00',
                'advance_hours': 2,
                'schedule_mode': 'daily',
                'timezone': 'Asia/Shanghai',
            }
        ),
    )

    reloaded = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    assert reloaded['tasks']['scheduled_start']['enabled'] is True
    assert len(reloaded['tasks']['scheduled_start']['jobs']) == 1


def test_scheduled_job_status_rows_respects_global_scheduled_pause(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token')],
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=False,
                poll_interval_seconds=5,
                jobs=[ScheduledStartJob(name='job-1', target_time='00:30', advance_hours=1, instance_id='iid-1')],
            )
        ),
    )

    rows = cli.scheduled_job_status_rows(settings, store, account_name='main', job_name='job-1')

    assert len(rows) == 1
    assert rows[0]['enabled'] is False
    assert rows[0]['task_status_label'] == '已暂停'


def test_render_menu_uses_repaint_instead_of_full_clear(capsys):
    interactive_app._render_menu('菜单标题', [interactive_app.MenuItem('1', '选项一')], 0)
    rendered = capsys.readouterr().out

    assert '\033[2J\033[H' not in rendered
    assert '\033[H\033[J' in rendered


def test_update_menu_selection_updates_only_changed_rows(capsys):
    interactive_app._update_menu_selection(
        [interactive_app.MenuItem('1', '选项一'), interactive_app.MenuItem('2', '选项二')],
        0,
        1,
    )
    rendered = capsys.readouterr().out

    assert '\033[2J\033[H' not in rendered
    assert '\033[s' in rendered
    assert '\033[u' in rendered
    assert '\033[2K' in rendered


def test_update_menu_title_updates_without_full_repaint(capsys):
    interactive_app._update_menu_title('标题A\n数据状态: 首次加载中', '标题A\n数据状态: 最近更新', 2)
    rendered = capsys.readouterr().out

    assert '\033[H\033[J' not in rendered
    assert '\033[s' in rendered
    assert '\033[u' in rendered
    assert '\033[2K' in rendered


def test_choose_menu_skips_rerender_when_refresh_content_unchanged(monkeypatch):
    render_calls = []
    title_update_calls = []
    keys = iter([None, 'ENTER'])
    items = [interactive_app.MenuItem('1', '选项一')]

    monkeypatch.setattr(interactive_app, '_supports_arrow_menu', lambda: True)
    monkeypatch.setattr(interactive_app, '_read_key_with_timeout', lambda timeout: next(keys))
    monkeypatch.setattr(interactive_app, '_render_menu', lambda title, items, selected_index: render_calls.append((title, list(items), selected_index)))
    monkeypatch.setattr(interactive_app, '_update_menu_title', lambda previous_title, new_title, item_count: title_update_calls.append((previous_title, new_title, item_count)))
    monotonic_values = iter([0.0, 0.11, 0.11, 0.12, 0.12, 0.12])
    monkeypatch.setattr(interactive_app.time, 'monotonic', lambda: next(monotonic_values))

    choice = interactive_app._choose_menu(
        '标题A\n数据状态: 首次加载中',
        items,
        refresh_fn=lambda current_key: ('标题A\n数据状态: 首次加载中', list(items), current_key),
        refresh_interval_seconds=0.01,
    )

    assert choice == '1'
    assert len(render_calls) == 1
    assert title_update_calls == []


def test_choose_menu_prefers_partial_title_update_when_items_unchanged(monkeypatch):
    render_calls = []
    title_update_calls = []
    keys = iter([None, 'ENTER'])
    items = [interactive_app.MenuItem('1', '选项一')]

    monkeypatch.setattr(interactive_app, '_supports_arrow_menu', lambda: True)
    monkeypatch.setattr(interactive_app, '_read_key_with_timeout', lambda timeout: next(keys))
    monkeypatch.setattr(interactive_app, '_render_menu', lambda title, items, selected_index: render_calls.append((title, list(items), selected_index)))
    monkeypatch.setattr(interactive_app, '_update_menu_title', lambda previous_title, new_title, item_count: title_update_calls.append((previous_title, new_title, item_count)) or True)
    monotonic_values = iter([0.0, 0.11, 0.11, 0.12, 0.12, 0.12])
    monkeypatch.setattr(interactive_app.time, 'monotonic', lambda: next(monotonic_values))

    choice = interactive_app._choose_menu(
        '标题A\n数据状态: 首次加载中',
        items,
        refresh_fn=lambda current_key: ('标题A\n数据状态: 最近更新', list(items), current_key),
        refresh_interval_seconds=0.01,
    )

    assert choice == '1'
    assert len(render_calls) == 1
    assert title_update_calls == [('标题A\n数据状态: 首次加载中', '标题A\n数据状态: 最近更新', 1)]


def test_choose_menu_skips_refresh_fn_when_revision_unchanged(monkeypatch):
    keys = iter([None, 'ENTER'])
    items = [interactive_app.MenuItem('1', '选项一')]
    refresh_calls = []

    monkeypatch.setattr(interactive_app, '_supports_arrow_menu', lambda: True)
    monkeypatch.setattr(interactive_app, '_read_key_with_timeout', lambda timeout: next(keys))
    monkeypatch.setattr(interactive_app, '_render_menu', lambda title, items, selected_index: None)

    choice = interactive_app._choose_menu(
        '标题A\n数据状态: 首次加载中',
        items,
        refresh_fn=lambda current_key: (refresh_calls.append(current_key) or ('标题A\n数据状态: 最近更新', list(items), current_key)),
        refresh_revision_fn=lambda: ('same-revision',),
        refresh_interval_seconds=1.0,
    )

    assert choice == '1'
    assert refresh_calls == []


def test_choose_menu_always_refreshes_even_when_revision_unchanged(monkeypatch):
    keys = iter([None, 'ENTER'])
    items = [interactive_app.MenuItem('1', '选项一')]
    refresh_calls = []
    pre_refresh_calls = []

    monkeypatch.setattr(interactive_app, '_supports_arrow_menu', lambda: True)
    monkeypatch.setattr(interactive_app, '_read_key_with_timeout', lambda timeout: next(keys))
    monkeypatch.setattr(interactive_app, '_render_menu', lambda title, items, selected_index: None)
    monotonic_values = iter([0.0, 0.11, 0.11, 0.12, 0.12, 0.12])
    monkeypatch.setattr(interactive_app.time, 'monotonic', lambda: next(monotonic_values))

    choice = interactive_app._choose_menu(
        '标题A\n数据状态: 首次加载中',
        items,
        refresh_fn=lambda current_key: (refresh_calls.append(current_key) or ('标题A\n数据状态: 最近更新', list(items), current_key)),
        refresh_revision_fn=lambda: ('same-revision',),
        refresh_interval_seconds=0.01,
        refresh_policy='always',
        pre_refresh_fn=lambda: pre_refresh_calls.append('tick'),
    )

    assert choice == '1'
    assert refresh_calls == ['1']
    assert pre_refresh_calls == ['tick']


def test_choose_menu_with_refresh_forwards_refresh_interval(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        interactive_app,
        '_choose_menu',
        lambda title, items, **kwargs: seen.setdefault('refresh_interval_seconds', kwargs.get('refresh_interval_seconds')) or '0',
    )

    interactive_app._choose_menu_with_refresh(
        '标题',
        [interactive_app.MenuItem('0', '返回')],
        refresh_fn=lambda preferred_key: ('标题', [interactive_app.MenuItem('0', '返回')], preferred_key),
        refresh_interval_seconds=1.0,
    )

    assert seen['refresh_interval_seconds'] == 1.0


def test_choose_menu_with_refresh_forwards_refresh_policy_and_pre_refresh(monkeypatch):
    seen = {}

    def fake_choose_menu(title, items, **kwargs):
        seen['refresh_policy'] = kwargs.get('refresh_policy')
        seen['pre_refresh_fn'] = kwargs.get('pre_refresh_fn')
        return '0'

    monkeypatch.setattr(interactive_app, '_choose_menu', fake_choose_menu)

    interactive_app._choose_menu_with_refresh(
        '标题',
        [interactive_app.MenuItem('0', '返回')],
        refresh_fn=lambda preferred_key: ('标题', [interactive_app.MenuItem('0', '返回')], preferred_key),
        refresh_interval_seconds=1.0,
        refresh_policy='always',
        pre_refresh_fn=lambda: None,
    )

    assert seen['refresh_policy'] == 'always'
    assert callable(seen['pre_refresh_fn'])


def test_choose_menu_with_refresh_does_not_swallow_type_error(monkeypatch):
    monkeypatch.setattr(
        interactive_app,
        '_choose_menu',
        lambda *args, **kwargs: (_ for _ in ()).throw(TypeError('boom')),
    )

    with pytest.raises(TypeError, match='boom'):
        interactive_app._choose_menu_with_refresh(
            '标题',
            [interactive_app.MenuItem('0', '返回')],
            refresh_fn=lambda preferred_key: ('标题', [interactive_app.MenuItem('0', '返回')], preferred_key),
        )


def test_interactive_scheduled_job_edit_can_cancel_from_field_prompt(tmp_path, monkeypatch):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        yaml.safe_dump(
            {
                'auth': {'authorization': 'Bearer token'},
                'accounts': [{'name': 'main', 'enabled': True, 'authorization': 'Bearer token'}],
                'tasks': {
                    'keeper': {'enabled': True},
                    'scheduled_start': {
                        'enabled': True,
                        'poll_interval_seconds': 300,
                        'jobs': [
                            {
                                'instance_id': 'iid-1',
                                'name': 'job-1',
                                'target_time': '14:00',
                                'advance_hours': 2,
                                'timezone': '',
                            }
                        ],
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    answers = iter([
        '1',   # 抢机器
        '1',   # job-1
        '3',   # 修改规则
        '4',   # 编辑时间设置
        ':q',  # 直接取消编辑
        '0',   # 返回规则详情
        '0',   # 返回规则列表
        '0',   # 返回首页
        '0',   # 退出
    ])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', str(config_path)])

    assert code == 0
    settings = load_settings(str(config_path))
    assert settings.tasks.scheduled_start.jobs[0].target_time == '14:00'
    assert (settings.tasks.scheduled_start.jobs[0].timezone or 'Asia/Shanghai') == 'Asia/Shanghai'


def test_interactive_can_edit_keeper_rules(tmp_path, monkeypatch):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        yaml.safe_dump(
            {
                'auth': {'authorization': 'Bearer token'},
                'accounts': [{'name': 'main', 'enabled': True, 'authorization': 'Bearer token'}],
                'tasks': {
                    'keeper': {
                        'enabled': True,
                        'shutdown_release_after_hours': 360,
                        'keeper_trigger_before_hours': 6,
                        'start_cooldown_minutes': 60,
                        'stop_cooldown_minutes': 360,
                        'interval_minutes': 60,
                        'power_on_wait_seconds': 60,
                        'power_off_wait_seconds': 5,
                        'fallback_to_status_at': True,
                    },
                    'scheduled_start': {
                        'enabled': True,
                        'poll_interval_seconds': 300,
                        'jobs': [{'instance_id': 'iid-1', 'name': 'job-1', 'target_time': '14:00', 'advance_hours': 2}],
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])
    captured = []
    monkeypatch.setattr(
        cli,
        'run_keeper_only',
        lambda **kwargs: captured.append(kwargs['settings'].tasks.keeper.interval_minutes) or [],
    )

    answers = iter([
        '2',  # Keeper
        '2',  # 编辑 Keeper 规则
        '2',  # 修改保留上限
        '400',
        '3',  # 修改接管时间
        '8',
        '4',  # 修改检查频率
        '90',
        'c',  # 保存
        '0',  # 关闭结果页
        '0',  # 返回首页
        '0',  # 退出
    ])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', str(config_path)])

    assert code == 0
    settings = load_settings(str(config_path))
    assert settings.tasks.keeper.shutdown_release_after_hours == 400
    assert settings.tasks.keeper.keeper_trigger_before_hours == 8
    assert settings.tasks.keeper.interval_minutes == 90
    assert settings.tasks.keeper.start_cooldown_minutes == 60
    assert settings.tasks.keeper.stop_cooldown_minutes == 360
    assert settings.tasks.keeper.fallback_to_status_at is True
    assert cli.read_config_reload_status(store)['requested_generation'] == 1
    assert captured == [90]


def test_interactive_keeper_pages_hide_cooldown_from_default_summary(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    answers = iter([
        '2',  # Keeper
        '2',  # 编辑 Keeper 规则
        '0',  # 取消
        '0',  # 返回首页
        '0',  # 退出
    ])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert '开机冷却' not in captured.out
    assert '关机冷却' not in captured.out
    assert '释放前开始接管' in captured.out
    assert '更多设置' not in captured.out
    assert '高级时间判断' not in captured.out


def test_interactive_diagnostics_menu_shows_current_scope(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    answers = iter(['4', '0', '0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert '诊断' in captured.out
    assert '查看账号' in captured.out
    assert 'main' in captured.out
    assert '切换查看账号' not in captured.out


def test_interactive_account_menu_is_single_account_only(tmp_path, monkeypatch, capsys):
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        accounts=[
            AccountSettings(name='main', enabled=True, authorization='Bearer token'),
            AccountSettings(name='backup', enabled=True, authorization='Bearer other', autodl_phone='1', autodl_password='2'),
        ],
        tasks=BASE_SETTINGS.tasks,
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: settings)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    login_calls = []
    monkeypatch.setattr(cli, '_command_login', lambda args: login_calls.append((args.account, args.all, getattr(args, 'headed', None))) or 0)

    answers = iter(['3', '3', '0', '0', '0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert login_calls == [('main', False, False)]
    assert '刷新全部账号登录' not in captured.out


def test_interactive_account_menu_can_switch_to_new_account_via_authorization(tmp_path, monkeypatch, capsys):
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        accounts=[
            AccountSettings(name='main', enabled=True, authorization='Bearer token'),
            AccountSettings(name='backup', enabled=True, authorization='Bearer other', autodl_phone='1', autodl_password='2'),
        ],
        tasks=BASE_SETTINGS.tasks,
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    config_path = tmp_path / 'config.yaml'
    write_raw_settings(config_path, asdict(settings))
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])
    login_calls = []
    monkeypatch.setattr(cli, '_command_login', lambda args: login_calls.append((args.account, args.all, getattr(args, 'headed', None))) or 0)

    answers = iter([
        '3',  # 账号
        '2',  # 切换到新账号
        '1',  # 粘贴 Authorization Token
        'Bearer switched-token',
        '0',  # 关闭结果页
        '0',  # 返回首页
        '0',  # 退出
    ])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', str(config_path)])
    captured = capsys.readouterr()
    updated = load_settings(str(config_path))

    assert code == 0
    assert login_calls == [('main', False, False)]
    assert updated.accounts[0].authorization == 'Bearer switched-token'
    assert updated.accounts[0].autodl_phone == ''
    assert updated.accounts[0].autodl_password == ''
    assert updated.auth.authorization == 'Bearer switched-token'
    assert '切换到新账号' in captured.out


def test_interactive_account_menu_can_switch_to_new_account_via_password(tmp_path, monkeypatch, capsys):
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token')],
        tasks=BASE_SETTINGS.tasks,
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    config_path = tmp_path / 'config.yaml'
    write_raw_settings(config_path, asdict(settings))
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])
    login_calls = []
    monkeypatch.setattr(cli, '_command_login', lambda args: login_calls.append((args.account, args.all, getattr(args, 'headed', None))) or 0)

    answers = iter([
        '3',  # 账号
        '2',  # 切换到新账号
        '2',  # 浏览器登录（手机号+密码）
        '13800138000',
        'secret',
        '0',
        '0',
        '0',
    ])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', str(config_path)])
    captured = capsys.readouterr()
    updated = load_settings(str(config_path))

    assert code == 0
    assert login_calls == [('main', False, True)]
    assert updated.accounts[0].authorization == ''
    assert updated.accounts[0].autodl_phone == '13800138000'
    assert updated.accounts[0].autodl_password == 'secret'
    assert updated.auth.authorization == ''
    assert updated.auth.autodl_phone == '13800138000'
    assert '切换到新账号' in captured.out


def test_interactive_account_detail_page_shows_full_fields(tmp_path, monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    due_soon = (now + timedelta(days=2)).isoformat()
    due_late = (now + timedelta(days=10)).isoformat()
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        accounts=[
            AccountSettings(
                name='main',
                enabled=True,
                authorization='Bearer token',
                autodl_phone='13800138000',
                autodl_password='secret',
                lightweight_mode='normal',
            ),
        ],
        tasks=BASE_SETTINGS.tasks,
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: settings)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(
        cli,
        'build_client',
        lambda settings, headed, account=None, store=None: DummyKeeperClient(
            [
                {'uuid': 'iid-run', 'status': 'running'},
                {'uuid': 'iid-due', 'status': 'shutdown'},
                {'uuid': 'iid-late', 'status': 'shutdown'},
            ]
        ),
    )
    monkeypatch.setattr(
        cli,
        'evaluate_keeper_instance',
            lambda **kwargs: KeeperResult(
                instance_id=kwargs['item']['uuid'],
                status=kwargs['item']['status'],
                release_at='',
                release_source='stopped_at',
                started_at='',
                stopped_at='2026-04-01T00:00:00+08:00',
                status_at='',
                release_deadline=due_soon if kwargs['item']['uuid'] == 'iid-due' else due_late,
                next_keeper_time='2026-04-09T18:00:00+08:00',
                seconds_until_release=0,
            seconds_until_keeper=0,
            started_duration_seconds=None,
            shutdown_duration_seconds=100,
            eligible=False,
            result='skip_not_due',
            reason='before_next_keeper_time',
            summary='',
        ),
    )

    answers = iter(['3', '1', '0', '0', '0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert '账号详情: main' in captured.out
    assert '运行中实例' in captured.out
    assert '一周内到期' in captured.out
    assert '抢机器任务' in captured.out
    assert '手机号' not in captured.out
    assert '轻量模式' not in captured.out


def test_interactive_account_detail_page_can_refresh_current_account(tmp_path, monkeypatch, capsys):
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        accounts=[
            AccountSettings(name='main', enabled=True, authorization='Bearer token'),
            AccountSettings(name='backup', enabled=True, autodl_phone='13800138000', autodl_password='secret'),
        ],
        tasks=BASE_SETTINGS.tasks,
    )
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: settings)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    login_calls = []
    monkeypatch.setattr(cli, '_command_login', lambda args: login_calls.append((args.account, args.all)) or 0)

    answers = iter([
        '3',  # 账号
        '1',  # 查看账号状态
        '1',  # 重新验证登录状态
        '0',  # 关闭刷新结果页
        '0',  # 返回账号菜单
        '0',  # 返回首页
        '0',  # 退出
    ])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert login_calls == [('main', False)]
    assert '账号详情: main' in captured.out
    assert '后台验证登录状态' in captured.out
    assert '数据状态' in captured.out


def test_interactive_views_line_uses_display_width_padding():
    line = interactive_views._line('最近登录时间', '2026-04-09 00:49')
    label_part = line.split(':', 1)[0]

    assert interactive_app._display_width(label_part) == 29


def test_scheduled_job_status_rows_ignores_history_from_old_job_signature(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token')],
        tasks=TaskSettings(
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=5,
                jobs=[ScheduledStartJob(name='selector-3080ti', target_time='00:30', advance_hours=1, instance_id='iid-new', schedule_mode='once')],
            )
        ),
    )
    store.add_scheduled_history(
        'main',
        'selector-3080ti',
        'iid-old',
        '2026-04-09',
        'already_running',
        'already_running',
        {
            'target_time': '23:30',
            'instance_id': 'iid-old',
            'job_signature': 'old-signature',
        },
    )

    rows = cli.scheduled_job_status_rows(
        settings,
        store,
        account_name='main',
        job_name='selector-3080ti',
    )

    assert len(rows) == 1
    assert rows[0]['latest_result'] == 'already_running'
    assert rows[0]['latest_created_at'] != ''
    assert rows[0]['latest_matches_current_rule'] is False


def test_render_scheduled_status_shows_waiting_for_first_check_after_rule_change():
    rendered = interactive_app._render_scheduled_status(
        'selector-3080ti',
        [
            {
                'job_name': 'selector-3080ti',
                'enabled': True,
                'daemon_running': True,
                'target_time': '00:30',
                'advance_hours': 1,
                'timezone': 'Asia/Shanghai',
                'schedule_mode': 'daily',
                'target_mode': 'instance',
                'target_summary': '固定实例=iid-new',
                'latest_result': '',
                'latest_reason': '',
                'latest_summary': '',
                'latest_created_at': '2026-04-10T15:04:40+08:00',
                'latest_matching_created_at': '',
                'latest_payload': {},
                'latest_instance_id': '',
                'has_history': True,
                'latest_matches_current_rule': False,
            }
        ],
    )

    assert '等待新规则首次检查' in rendered
    stripped = interactive_app._strip_ansi(rendered)
    assert '最近检查时间' in stripped
    assert '2026-04-10 15:04:40' in stripped
    assert '当前规则最近检查时间' not in stripped
    assert '规则匹配状态' in stripped
    assert '最近检查来自旧规则' in stripped
    assert '最近检查结果' in stripped
    assert '实例已在运行' not in stripped


def test_render_scheduled_status_uses_clear_default_copy_instead_of_dash():
    rendered = interactive_app._strip_ansi(
        interactive_app._render_scheduled_status(
            'selector-3080ti',
            [
                {
                    'job_name': 'selector-3080ti',
                    'enabled': True,
                    'target_mode': 'selector',
                    'target_summary': '',
                    'target_time': '',
                    'advance_hours': 1,
                    'timezone': 'Asia/Shanghai',
                    'latest_result': '',
                    'latest_reason': '',
                    'latest_summary': '',
                    'latest_created_at': '',
                    'latest_matching_created_at': '',
                    'latest_payload': {},
                    'latest_instance_id': '',
                    'has_history': False,
                    'latest_matches_current_rule': False,
                }
            ],
        )
    )

    assert '目标条件' in rendered and '未设置' in rendered
    assert '目标时间' in rendered and '未设置' in rendered
    assert '最近检查时间' in rendered and '待首次检查' in rendered
    assert '当前规则最近检查时间' not in rendered
    assert '最近检查结果' in rendered and '待首次检查' in rendered
    assert '已命中' in rendered and '暂无' in rendered
    assert '等待中' in rendered and '暂无' in rendered
    assert '被淘汰' in rendered and '暂无' in rendered


def test_render_scheduled_status_humanizes_outside_window_fields():
    rendered = interactive_app._strip_ansi(
        interactive_app._render_scheduled_status(
            'job-1',
            [
                {
                    'job_name': 'job-1',
                    'enabled': True,
                    'daemon_running': True,
                    'target_time': '13:00',
                    'advance_hours': 3,
                    'timezone': 'Asia/Shanghai',
                    'schedule_mode': 'daily',
                    'target_mode': 'instance',
                    'target_summary': '固定实例=iid-1',
                    'latest_result': 'outside_window',
                    'latest_reason': 'outside_window',
                    'latest_summary': '',
                    'latest_created_at': '2026-04-11T00:44:22+08:00',
                    'latest_matching_created_at': '2026-04-11T00:42:28+08:00',
                    'latest_payload': {},
                    'latest_instance_id': '',
                    'has_history': True,
                    'latest_matches_current_rule': True,
                }
            ],
        )
    )

    assert '当前阶段' in rendered and '等待抢机窗口' in rendered
    assert '最近检查结果' in rendered and '未到轮询窗口' in rendered
    assert '当前规则最近检查时间' not in rendered
    assert 'outside_window' not in rendered


def test_scheduled_run_result_state_overrides_waiting_for_first_check_after_modify():
    stale_row = {
        'job_name': 'selector-3080ti',
        'enabled': True,
        'daemon_running': True,
        'target_time': '00:30',
        'advance_hours': 1,
        'timezone': 'Asia/Shanghai',
        'schedule_mode': 'daily',
        'target_mode': 'instance',
        'target_summary': '固定实例=iid-new',
        'latest_result': '',
        'latest_reason': '',
        'latest_summary': '',
        'latest_created_at': '',
        'latest_payload': {},
        'latest_instance_id': '',
        'has_history': True,
        'latest_matches_current_rule': False,
    }
    results = [
        ScheduledStartResult(
            result='waiting_for_instance',
            reason='selector_no_match',
            instance_id='',
            status='shutdown',
            gpu_idle_num=0,
            start_mode='',
            target_time='00:30',
            deadline='2026-04-09T00:30:00+08:00',
            event_type='scheduled.waiting',
            severity='info',
            summary='等待候选实例出现',
        )
    ]

    merged = dict(stale_row)
    merged.update(
        interactive_app._scheduled_run_result_state(
            stale_row,
            results,
            trigger_label='修改规则后自动执行',
        )
    )
    rendered = interactive_app._render_scheduled_status('selector-3080ti', [merged])

    assert merged['latest_result'] == 'waiting_for_instance'
    assert merged['latest_matches_current_rule'] is True
    assert merged['task_status_label'] == '轮询中'
    assert '等待新规则首次检查' not in rendered
    assert '等待候选出现' in rendered


def test_render_instance_detail_shows_gpu_fields():
    rendered = interactive_app._render_instance_detail(
        {
            'instance_id': 'iid-1',
            'name': 'node-1',
            'region': '北京A区',
            'status': 'running',
            'machine_alias': '926机',
            'spec': 'RTX 4090 * 4',
            'charge_type': 'payg',
            'status_at': '2026-04-09T00:20:38+08:00',
            'gpu_all_num': 4,
            'gpu_idle_num': 2,
            'start_mode': 'gpu',
        },
        'main',
    )

    assert 'GPU 配置' in rendered
    assert '4 卡' in rendered
    assert '空闲 GPU' in rendered
    assert '2 / 4' in rendered
    assert '启动模式' in rendered
    assert 'GPU 模式' in rendered
    assert '规格' in rendered
    assert 'RTX 4090 * 4' in rendered
    assert '运行中' in rendered
    assert '按量计费' in rendered


def test_render_instance_detail_shows_zero_idle_gpu():
    rendered = interactive_app._render_instance_detail(
        {
            'instance_id': 'iid-1',
            'name': 'node-1',
            'region': '北京A区',
            'status': 'shutdown',
            'machine_alias': '926机',
            'spec': 'RTX 4090 * 8',
            'charge_type': 'payg',
            'status_at': '2026-04-09T00:20:38+08:00',
            'gpu_all_num': 8,
            'gpu_idle_num': 0,
            'start_mode': 'gpu',
        },
        'main',
    )

    stripped = interactive_app._strip_ansi(rendered)
    assert '空闲 GPU' in stripped
    assert '空闲 GPU           : 0 / 8' in stripped


def test_browse_instance_list_row_shows_gpu_summary(monkeypatch, capsys):
    settings = BASE_SETTINGS
    rows = [
        {
            'instance_id': 'iid-1',
            'name': '0.98',
            'region': '北京B区',
            'status': 'shutdown',
            'machine_alias': '926机',
            'spec': 'RTX 4090 * 4卡',
            'gpu_idle_num': 2,
            'status_at': '',
        }
    ]

    monkeypatch.setattr(interactive_app, '_load_instance_rows_via_command', lambda **kwargs: rows)

    choices = iter(['0'])
    def fake_choose_menu(title, items, *, default_key=None):
        print(title)
        for item in items:
            print(item.label)
        return next(choices)

    monkeypatch.setattr(interactive_app, '_choose_menu', fake_choose_menu)

    interactive_app._browse_instance_list(
        args=SimpleNamespace(config='config.yaml'),
        current_account='main',
        settings=settings,
        command_list_instances_fn=lambda args: 0,
    )
    rendered = capsys.readouterr().out

    assert '已关机' in rendered
    assert 'RTX 4090×4' in rendered
    assert '空闲2' in rendered


def test_instance_gpu_summary_uses_model_with_gpu_count_when_spec_has_no_count():
    assert interactive_app._instance_gpu_summary({'spec': 'RTX 3080 Ti', 'gpu_all_num': 8}) == 'RTX 3080 Ti×8'


def test_instance_idle_gpu_summary_keeps_zero():
    assert interactive_app._instance_idle_gpu_summary({'gpu_idle_num': 0}) == '空闲0'


def test_show_live_scheduled_status_refreshes_until_back(monkeypatch, capsys):
    calls = []
    rows = [{'job_name': 'job-1', 'enabled': True, 'latest_result': 'waiting_for_gpu', 'target_time': '01:00', 'advance_hours': 1}]
    actions = iter(['refresh', 'back'])
    monkeypatch.setattr(interactive_app, '_render_scheduled_status', lambda job_name, status_rows: (calls.append((job_name, list(status_rows))) or '实时进度'))
    monkeypatch.setattr(interactive_app, '_poll_live_action', lambda timeout: next(actions))

    interactive_app._show_live_scheduled_status(
        job_name='job-1',
        fetch_rows_fn=lambda: rows,
    )
    rendered = capsys.readouterr().out

    assert len(calls) == 2
    assert '3秒轻量自动刷新 / Enter 立即刷新 / q 返回' in rendered


def test_show_live_scheduled_status_static_rows_do_not_auto_refresh(monkeypatch, capsys):
    fetch_calls = []
    poll_timeouts = []
    rows = [{'job_name': 'job-1', 'enabled': False, 'latest_result': '', 'target_time': '01:00', 'advance_hours': 1}]
    monkeypatch.setattr(interactive_app, '_render_scheduled_status', lambda job_name, status_rows: '静态进度')

    interactive_app._show_live_scheduled_status(
        job_name='job-1',
        fetch_rows_fn=lambda: (fetch_calls.append(True) or rows),
        poll_action_fn=lambda timeout: (poll_timeouts.append(timeout) or 'back'),
    )
    rendered = capsys.readouterr().out

    assert len(fetch_calls) == 1
    assert poll_timeouts == [None]
    assert '当前无运行中任务 / Enter 手动刷新 / q 返回' in rendered


def test_render_account_detail_uses_snapshot_without_sync_auth_probe(monkeypatch):
    monkeypatch.setattr(interactive_app, 'inspect_auth_state', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('should not be called')))

    rendered = interactive_app._render_account_detail(
        BASE_SETTINGS,
        None,
        account_name='main',
        keeper_probe_rows_fn=lambda *args, **kwargs: [],
        scheduled_job_status_rows_fn=lambda *args, **kwargs: [],
        snapshot={
            'account_name': 'main',
            'account_enabled': True,
            'auth_status': '已配置 token',
            'auth_source': 'runtime',
            'running_instances': 2,
            'expiring_soon': 1,
            'scheduled_jobs': 3,
            'paused_jobs': 1,
            'keeper_enabled': True,
        },
    )

    assert '已配置 token' in rendered
    assert 'runtime' in rendered
    assert '运行中实例' in rendered


def test_browse_account_detail_login_verify_is_non_blocking(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)

    selections = iter(['1', '0'])
    monkeypatch.setattr(interactive_app, '_choose_menu', lambda *args, **kwargs: next(selections))
    monkeypatch.setattr(
        interactive_app,
        '_account_runtime_snapshot',
        lambda *args, **kwargs: {
            'account_name': 'main',
            'account_enabled': True,
            'auth_status': '已配置 token',
            'auth_source': 'runtime',
            'running_instances': 0,
            'expiring_soon': 0,
            'scheduled_jobs': 1,
            'paused_jobs': 0,
            'keeper_enabled': True,
        },
    )

    def slow_login(args):
        time.sleep(0.4)
        return 0

    started_at = time.time()
    try:
        interactive_app._browse_account_detail(
            args=SimpleNamespace(config='config.yaml'),
            settings=BASE_SETTINGS,
            store=store,
            current_account='main',
            command_login_fn=slow_login,
            keeper_probe_rows_fn=lambda *args, **kwargs: [],
            scheduled_job_status_rows_fn=lambda *args, **kwargs: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
    finally:
        task_manager.shutdown(wait=False)

    assert time.time() - started_at < 0.35


def test_trim_snapshot_payload_reduces_scheduled_progress_fields():
    payload = interactive_app._trim_snapshot_payload(
        interactive_app._snapshot_key('scheduled_progress', 'job:main:job-1'),
        [
            {
                'job_name': 'job-1',
                'enabled': True,
                'target_mode': 'instance',
                'target_summary': '固定实例=iid-1',
                'target_time': '01:00',
                'advance_hours': 1,
                'schedule_mode': 'daily',
                'timezone': 'Asia/Shanghai',
                'latest_created_at': '2026-04-09T10:00:00+08:00',
                'latest_result': 'waiting_for_instance',
                'latest_summary': 'x' * 1200,
                'latest_payload': {'hit_count': 1, 'waiting_count': 2, 'dropped_count': 3, 'huge': 'y' * 5000},
                '_live_stage_label': '正在轮询',
                '_live_stage_tone': 'ok',
                '_live_execution_label': '执行中',
                '_live_execution_tone': 'ok',
                '_live_next_action': '继续等待',
                '_live_poll_text': '10分钟',
                '_live_target_text': '40分钟',
                '_live_missing_reason_label': '',
                '_live_missing_reason_tone': 'muted',
                'unexpected': 'should-drop',
            }
        ],
    )

    assert isinstance(payload, list)
    assert len(payload) == 1
    row = payload[0]
    assert row['job_name'] == 'job-1'
    assert 'unexpected' not in row
    assert set(row['latest_payload']) == {'hit_count', 'waiting_count', 'dropped_count'}
    assert len(str(row['latest_summary'])) < 1200


def test_trim_snapshot_payload_reduces_dashboard_fields():
    payload = interactive_app._trim_snapshot_payload(
        interactive_app._snapshot_key('dashboard', 'main'),
        {
            'runtime_status': {'running': True, 'pid': 1234, 'heartbeat_age_seconds': 9, 'extra': 'drop'},
            'current_account': 'main',
            'current_account_row': {'status': '已缓存登录', 'auth_source': 'runtime', 'token': 'drop'},
            'enabled_accounts': 3,
            'effective_keeper_enabled': True,
            'effective_scheduled_enabled': True,
            'paused_job_count': 1,
            'scheduled_jobs': [{'job_name': 'job-1'}, {'job_name': 'job-2'}, {'job_name': 'job-3'}],
            'keeper_summary': {'pending': 1, 'expiring_soon': 2, 'failed': 3, 'drop': 'x'},
            'candidate_summary': {'job_name': 'job-1', 'selected_instance_id': 'iid-1', 'candidate_count': 9, 'top_reasons': ['a', 'b', 'c', 'd']},
            'recent_history': ['drop'],
            'recent_failures': ['drop'],
        },
    )

    assert payload['current_account'] == 'main'
    assert payload['runtime_status'] == {'running': True, 'pid': 1234, 'heartbeat_age_seconds': 9}
    assert payload['current_account_row'] == {'status': '已缓存登录', 'auth_source': 'runtime'}
    assert payload['scheduled_job_count'] == 3
    assert payload['keeper_summary'] == {'pending': 1, 'not_due': 0, 'abnormal': 0, 'expiring_soon': 2, 'failed': 3}
    assert payload['candidate_summary']['top_reasons'] == ['a', 'b', 'c']
    assert 'recent_history' not in payload


def test_show_live_scheduled_status_can_clear_scope_snapshot_on_exit():
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)
    rows = [
        {
            'job_name': 'job-1',
            'enabled': True,
            'target_mode': 'instance',
            'target_summary': '固定实例=iid-1',
            'target_time': '01:00',
            'advance_hours': 1,
            'schedule_mode': 'daily',
            'timezone': 'Asia/Shanghai',
            'latest_result': 'started',
            'latest_created_at': '',
            'latest_payload': {},
        }
    ]
    try:
        interactive_app._show_live_scheduled_status(
            job_name='job-1',
            fetch_rows_fn=lambda: rows,
            poll_action_fn=lambda timeout: 'back',
            task_manager=task_manager,
            snapshot_store=snapshot_store,
            current_account='main',
            clear_scope_snapshot_on_exit=True,
        )
    finally:
        task_manager.shutdown(wait=False)

    assert snapshot_store.get_snapshot(interactive_app._snapshot_key('scheduled_progress', 'job:main:job-1')) is None


def test_login_verify_snapshot_runs_without_spawn_timeout(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()

    class _NoSpawn:
        def get_context(self, *_args, **_kwargs):
            raise AssertionError('spawn path should not be used')

    monkeypatch.setattr(interactive_app, 'multiprocessing', _NoSpawn(), raising=False)

    snapshot = interactive_app._login_verify_snapshot(
        args=SimpleNamespace(config='config.yaml'),
        account_name='main',
        command_login_fn=slow_picklable_command,
        settings=BASE_SETTINGS,
        store=store,
        keeper_probe_rows_fn=lambda *args, **kwargs: [],
        scheduled_job_status_rows_fn=lambda *args, **kwargs: [],
        timeout_seconds=0.05,
    )

    assert snapshot['account_name'] == 'main'


def test_run_command_with_timeout_marks_long_running_without_spawn(monkeypatch):
    class _NoSpawn:
        def get_context(self, *_args, **_kwargs):
            raise AssertionError('spawn path should not be used')

    monkeypatch.setattr(interactive_app, 'multiprocessing', _NoSpawn(), raising=False)

    result = interactive_app._run_command_with_timeout(
        command_fn=slow_picklable_command,
        args=SimpleNamespace(config='config.yaml'),
        timeout_seconds=0.05,
        title='健康自检',
        timeout_summary='健康自检超时，已终止本次检查',
    )

    assert result['timed_out'] is False
    assert result['long_running'] is True
    assert result['code'] == 0
    assert result['elapsed_seconds'] >= 0.25


def test_run_command_with_timeout_updates_command_stats():
    before = interactive_app._subprocess_task_stats_snapshot()
    result = interactive_app._run_command_with_timeout(
        command_fn=slow_picklable_command,
        args=SimpleNamespace(config='config.yaml'),
        timeout_seconds=0.05,
        title='健康自检',
        timeout_summary='健康自检超时，已终止本次检查',
    )
    after = interactive_app._subprocess_task_stats_snapshot()

    assert result['timed_out'] is False
    assert after['started'] >= before['started'] + 1
    assert after['completed'] >= before['completed'] + 1
    assert after['long_running'] >= before['long_running'] + 1


def test_diagnostics_snapshot_payload_includes_resource_stats(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store, max_workers=1)
    gate = threading.Event()

    try:
        task_manager.submit(
            'healthcheck_run',
            scope='main',
            runner=lambda: gate.wait(timeout=1.0) or {'ok': True},
            status_message='正在执行健康自检',
        )
        task_manager.start_pending()
        time.sleep(0.05)

        payload = interactive_app._diagnostics_snapshot_payload(
            snapshot_store=snapshot_store,
            account_name='main',
            task_manager=task_manager,
            store=store,
        )

        assert payload['interactive_workers_max'] == 1
        assert payload['interactive_running_count'] == 1
        assert payload['interactive_running_by_type']['healthcheck_run'] == 1
        assert 'fd_current' in payload
        assert 'daemon_launch_state' in payload
        assert 'interactive_circuit_open' in payload
    finally:
        gate.set()
        task_manager.shutdown(wait=False)


def test_friendly_resource_error_message_rewrites_sqlite_open_failure():
    message = interactive_app._friendly_resource_error_message(
        'unable to open database file; path=/tmp/data.db'
    )

    assert '数据库打开失败' in message
    assert 'path=/tmp/data.db' in message


def test_diagnostics_menu_can_clear_scope_snapshots_on_exit(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)
    account_scope = 'main'
    snapshot_store.set_snapshot(interactive_app._snapshot_key('diagnostics', account_scope), {'instance_total': 1})
    snapshot_store.set_snapshot(interactive_app._snapshot_key('healthcheck', account_scope), {'status': '成功'})
    snapshot_store.set_snapshot(interactive_app._snapshot_key('instances', account_scope), [{'instance_id': 'iid-1'}])
    snapshot_store.set_snapshot(interactive_app._snapshot_key('keeper_probe', account_scope), [{'instance_id': 'iid-1'}])
    snapshot_store.set_snapshot(interactive_app._snapshot_key('config_diagnostics', account_scope), {'status': '成功'})
    monkeypatch.setattr(interactive_app, '_choose_menu', lambda *args, **kwargs: '0')
    monkeypatch.setattr(interactive_app, '_load_instance_rows_via_command', lambda **kwargs: [])
    try:
        interactive_app._diagnostics_menu(
            args=SimpleNamespace(config='config.yaml'),
            current_account='main',
            command_list_instances_fn=lambda args: 0,
            command_healthcheck_fn=lambda args: 0,
            settings=BASE_SETTINGS,
            store=store,
            keeper_probe_rows_fn=lambda *args, **kwargs: [],
            load_settings_fn=lambda path: BASE_SETTINGS,
            validate_settings_fn=lambda settings, path: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
            clear_scope_snapshots_on_exit=True,
        )
    finally:
        task_manager.shutdown(wait=False)

    assert snapshot_store.get_snapshot(interactive_app._snapshot_key('diagnostics', account_scope)) is None
    assert snapshot_store.get_snapshot(interactive_app._snapshot_key('healthcheck', account_scope)) is None
    assert snapshot_store.get_snapshot(interactive_app._snapshot_key('instances', account_scope)) is None
    assert snapshot_store.get_snapshot(interactive_app._snapshot_key('keeper_probe', account_scope)) is None
    assert snapshot_store.get_snapshot(interactive_app._snapshot_key('config_diagnostics', account_scope)) is None


def test_diagnostics_menu_uses_snapshot_status_lines(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)
    selections = iter(['0'])
    monkeypatch.setattr(interactive_app, '_choose_menu', lambda title, items, **kwargs: (print(title) or next(selections)))
    monkeypatch.setattr(interactive_app, '_load_instance_rows_via_command', lambda **kwargs: [])
    try:
        interactive_app._diagnostics_menu(
            args=SimpleNamespace(config='config.yaml'),
            current_account='main',
            command_list_instances_fn=lambda args: 0,
            command_healthcheck_fn=lambda args: 0,
            settings=BASE_SETTINGS,
            store=store,
            keeper_probe_rows_fn=lambda *args, **kwargs: [],
            load_settings_fn=lambda path: BASE_SETTINGS,
            validate_settings_fn=lambda settings, path: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
    finally:
        task_manager.shutdown(wait=False)

    rendered = capsys.readouterr().out
    assert '数据状态' in rendered
    assert '实例摘要' in rendered


def test_diagnostics_menu_prefers_related_snapshots_when_summary_missing(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)
    account_scope = 'main'
    snapshot_store.set_snapshot(
        interactive_app._snapshot_key('instances', account_scope),
        [
            {'instance_id': 'iid-1', 'status': 'running'},
            {'instance_id': 'iid-2', 'status': 'shutdown'},
        ],
    )
    snapshot_store.set_snapshot(
        interactive_app._snapshot_key('keeper_probe', account_scope),
        [
            {'instance_id': 'iid-1', 'eligible': True},
            {'instance_id': 'iid-2', 'eligible': False},
        ],
    )
    snapshot_store.set_snapshot(
        interactive_app._snapshot_key('healthcheck', account_scope),
        {'status': '成功', 'summary': 'Healthcheck OK.', 'body': 'Healthcheck OK.'},
    )
    snapshot_store.set_snapshot(
        interactive_app._snapshot_key('config_diagnostics', account_scope),
        {'status': '成功', 'summary': '配置已同步', 'body': '配置已同步'},
    )

    monkeypatch.setattr(interactive_app, '_submit_snapshot_task', lambda **kwargs: None)
    monkeypatch.setattr(
        interactive_app,
        '_choose_menu_with_refresh',
        lambda title, items, **kwargs: (print(title) or '0'),
    )

    try:
        interactive_app._diagnostics_menu(
            args=SimpleNamespace(config='config.yaml'),
            current_account='main',
            command_list_instances_fn=lambda args: 0,
            command_healthcheck_fn=lambda args: 0,
            settings=BASE_SETTINGS,
            store=store,
            keeper_probe_rows_fn=lambda *args, **kwargs: [],
            load_settings_fn=lambda path: BASE_SETTINGS,
            validate_settings_fn=lambda settings, path: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
    finally:
        task_manager.shutdown(wait=False)

    clean = interactive_app._strip_ansi(capsys.readouterr().out)
    assert '实例总数' in clean
    assert '2' in clean
    assert '运行中' in clean
    assert '1' in clean
    assert '健康自检' in clean
    assert '成功' in clean


def test_diagnostics_menu_uses_always_refresh_policy(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)
    seen = {}

    def fake_choose_menu_with_refresh(title, items, **kwargs):
        seen['refresh_policy'] = kwargs.get('refresh_policy')
        seen['pre_refresh_fn'] = kwargs.get('pre_refresh_fn')
        return '0'

    monkeypatch.setattr(interactive_app, '_choose_menu_with_refresh', fake_choose_menu_with_refresh)
    monkeypatch.setattr(interactive_app, '_load_instance_rows_via_command', lambda **kwargs: [])
    try:
        interactive_app._diagnostics_menu(
            args=SimpleNamespace(config='config.yaml'),
            current_account='main',
            command_list_instances_fn=lambda args: 0,
            command_healthcheck_fn=lambda args: 0,
            settings=BASE_SETTINGS,
            store=store,
            keeper_probe_rows_fn=lambda *args, **kwargs: [],
            load_settings_fn=lambda path: BASE_SETTINGS,
            validate_settings_fn=lambda settings, path: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
    finally:
        task_manager.shutdown(wait=False)

    assert seen['refresh_policy'] == 'always'
    assert callable(seen['pre_refresh_fn'])


def test_diagnostics_status_prefers_latest_instance_snapshot_label():
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    account_scope = 'main'
    instance_key = interactive_app._snapshot_key('instances', account_scope)
    keeper_key = interactive_app._snapshot_key('keeper_probe', account_scope)
    healthcheck_key = interactive_app._snapshot_key('healthcheck', account_scope)
    config_key = interactive_app._snapshot_key('config_diagnostics', account_scope)

    snapshot_store.set_snapshot(keeper_key, [{'instance_id': 'iid-1'}], status_message='最近更新')
    snapshot_store.set_snapshot(healthcheck_key, {'status': '成功'}, status_message='最近更新')
    snapshot_store.set_snapshot(config_key, {'status': '成功'}, status_message='最近更新')
    snapshot_store.set_snapshot(instance_key, [{'instance_id': 'iid-2'}], status_message='最近更新')

    status = interactive_app._diagnostics_page_status(
        snapshot_store=snapshot_store,
        account_scope=account_scope,
        instance_task=None,
        keeper_task=None,
        healthcheck_task=None,
    )

    assert status.state == 'ready'
    assert status.message == '最近实例更新'


def test_diagnostics_status_prefers_latest_healthcheck_snapshot_label():
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    account_scope = 'main'
    instance_key = interactive_app._snapshot_key('instances', account_scope)
    keeper_key = interactive_app._snapshot_key('keeper_probe', account_scope)
    healthcheck_key = interactive_app._snapshot_key('healthcheck', account_scope)

    snapshot_store.set_snapshot(instance_key, [{'instance_id': 'iid-1'}], status_message='最近更新')
    snapshot_store.set_snapshot(keeper_key, [{'instance_id': 'iid-2'}], status_message='最近更新')
    snapshot_store.set_snapshot(healthcheck_key, {'status': '成功'}, status_message='最近更新')

    status = interactive_app._diagnostics_page_status(
        snapshot_store=snapshot_store,
        account_scope=account_scope,
        instance_task=None,
        keeper_task=None,
        healthcheck_task=None,
    )

    assert status.state == 'ready'
    assert status.message == '最近健康自检更新'


def test_diagnostics_status_prefers_active_task_message_over_latest_snapshot():
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    account_scope = 'main'
    instance_key = interactive_app._snapshot_key('instances', account_scope)
    snapshot_store.set_snapshot(instance_key, [{'instance_id': 'iid-1'}], status_message='最近更新')
    task = InteractiveTaskResult(
        task_type='keeper_probe_refresh',
        scope=account_scope,
        task_key='keeper_probe_refresh:main',
        status='running',
        status_message='正在刷新 Keeper 探测',
    )

    status = interactive_app._diagnostics_page_status(
        snapshot_store=snapshot_store,
        account_scope=account_scope,
        instance_task=None,
        keeper_task=task,
        healthcheck_task=None,
    )

    assert status.state in {'refreshing', 'loading'}
    assert status.message == '正在刷新 Keeper 探测'


def test_account_detail_uses_one_second_idle_refresh(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)
    seen = {}

    def fake_choose_menu_with_refresh(title, items, **kwargs):
        seen['refresh_interval_seconds'] = kwargs.get('refresh_interval_seconds')
        seen['refresh_policy'] = kwargs.get('refresh_policy')
        seen['pre_refresh_fn'] = kwargs.get('pre_refresh_fn')
        return '0'

    monkeypatch.setattr(interactive_app, '_choose_menu_with_refresh', fake_choose_menu_with_refresh)
    try:
        interactive_app._browse_account_detail(
            args=SimpleNamespace(config='config.yaml'),
            settings=BASE_SETTINGS,
            store=store,
            current_account='main',
            command_login_fn=lambda args: 0,
            keeper_probe_rows_fn=lambda *args, **kwargs: [],
            scheduled_job_status_rows_fn=lambda *args, **kwargs: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
    finally:
        task_manager.shutdown(wait=False)

    assert seen['refresh_interval_seconds'] == 1.0
    assert seen['refresh_policy'] == 'always'
    assert callable(seen['pre_refresh_fn'])


def test_page_status_lines_hide_progress_for_login_verify_task():
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store, max_workers=1)
    gate = threading.Event()
    try:
        task_manager.submit(
            'login_verify_run',
            scope='main',
            runner=lambda: gate.wait(timeout=1.0) or {'ok': True},
            status_message='正在验证登录状态',
        )
        task_manager.start_pending()
        time.sleep(0.05)
        task = task_manager.get_task('login_verify_run', 'main')
        status = interactive_app._page_status_from_tasks(
            snapshot_store=snapshot_store,
            snapshot_key=interactive_app._snapshot_key('account_runtime', 'main'),
            secondary_tasks=[task],
        )
        lines = interactive_app._page_status_lines(status, active_task=task, progress_label='验证进度')
        rendered = '\n'.join(lines)
        clean = interactive_app._strip_ansi(rendered)

        assert '数据状态' in clean
        assert '正在验证登录状态' in clean
        assert '任务阶段' in clean
        assert '运行中' in clean
        assert '验证进度' not in clean
        assert '█' not in clean and '░' not in clean
        assert '\x1b[' in rendered
    finally:
        gate.set()
        task_manager.shutdown(wait=False)


def test_page_status_lines_show_long_running_hint_and_warn_label(monkeypatch):
    status = interactive_app.InteractivePageStatus(state='refreshing', message='正在验证登录状态')
    task = InteractiveTaskResult(
        task_type='login_verify_run',
        scope='main',
        task_key='login_verify_run:main',
        status='running',
        started_at='2026-04-09T14:00:00+08:00',
        status_message='正在验证登录状态',
    )

    monkeypatch.setattr(interactive_app, '_task_running_age_seconds', lambda current_task: 20)
    lines = interactive_app._page_status_lines(status, active_task=task, progress_label='验证进度')
    clean = interactive_app._strip_ansi('\n'.join(lines))

    assert '耗时较长' in clean
    assert '可按 q 返回，后台继续执行' in clean


def test_render_task_progress_bar_uses_animated_frame(monkeypatch):
    task = InteractiveTaskResult(
        task_type='healthcheck_run',
        scope='main',
        task_key='healthcheck_run:main',
        status='running',
        started_at='2026-04-09T14:00:00+08:00',
        status_message='正在执行健康自检',
    )
    monkeypatch.setattr(interactive_app, '_task_running_age_seconds', lambda current_task: 5)
    monkeypatch.setattr(interactive_app.time, 'monotonic', lambda: 10.0)
    frame_a = interactive_app._strip_ansi(interactive_app._render_task_progress_bar(task=task))
    monkeypatch.setattr(interactive_app.time, 'monotonic', lambda: 11.0)
    frame_b = interactive_app._strip_ansi(interactive_app._render_task_progress_bar(task=task))

    assert frame_a != frame_b


def test_browse_healthcheck_detail_shows_last_snapshot_without_auto_run(monkeypatch, capsys):
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store, max_workers=1)
    snapshot_store.set_snapshot(
        interactive_app._snapshot_key('healthcheck', 'main'),
        {'status': '成功', 'summary': 'Healthcheck OK.', 'body': 'Healthcheck OK.'},
    )

    monkeypatch.setattr(
        interactive_app,
        '_choose_menu_with_refresh',
        lambda title, items, **kwargs: (print(title) or '0'),
    )

    try:
        interactive_app._browse_healthcheck_detail(
            args=SimpleNamespace(config='config.yaml'),
            current_account='main',
            command_healthcheck_fn=lambda _args: (_ for _ in ()).throw(AssertionError('should not auto-run healthcheck on enter')),
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
    finally:
        task_manager.shutdown(wait=False)

    rendered = capsys.readouterr().out
    clean = interactive_app._strip_ansi(rendered)
    assert '最近状态' in clean
    assert '成功' in clean
    assert 'Healthcheck OK.' in clean
    assert '检查进度' not in clean


def test_browse_healthcheck_detail_uses_always_refresh_policy(monkeypatch):
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store, max_workers=1)
    seen = {}

    def fake_choose_menu_with_refresh(title, items, **kwargs):
        seen['refresh_policy'] = kwargs.get('refresh_policy')
        seen['pre_refresh_fn'] = kwargs.get('pre_refresh_fn')
        return '0'

    monkeypatch.setattr(interactive_app, '_choose_menu_with_refresh', fake_choose_menu_with_refresh)

    try:
        interactive_app._browse_healthcheck_detail(
            args=SimpleNamespace(config='config.yaml'),
            current_account='main',
            command_healthcheck_fn=lambda _args: 0,
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
    finally:
        task_manager.shutdown(wait=False)

    assert seen['refresh_policy'] == 'always'
    assert callable(seen['pre_refresh_fn'])


def test_keeper_probe_schedule_lines_show_next_and_last_execution(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.set_runtime_value('last_run:keeper', '2026-04-10T02:58:33+00:00')
    monkeypatch.setattr(interactive_app, 'read_daemon_status', lambda _store: {'running': True})

    lines = interactive_app._keeper_probe_schedule_lines(BASE_SETTINGS, store, account_name='main')
    rendered = '\n'.join(lines)

    assert '下次执行时间' in rendered
    assert '2026-04-10 11:58:33' in rendered
    assert '上次执行时间' in rendered
    assert '2026-04-10 10:58:33' in rendered


def test_keeper_last_execution_summary_prefers_latest_batch_id():
    rows = [
        HistoryRecord(
            created_at='2026-04-10T16:51:27.125458+00:00',
            account_name='main',
            task_type='keeper',
            result='skip_not_due',
            reason='before_next_keeper_time',
            instance_id='iid-1',
            payload={'batch_id': 'batch-new'},
        ),
        HistoryRecord(
            created_at='2026-04-10T16:51:27.123616+00:00',
            account_name='main',
            task_type='keeper',
            result='skip_not_due',
            reason='before_next_keeper_time',
            instance_id='iid-2',
            payload={'batch_id': 'batch-new'},
        ),
        HistoryRecord(
            created_at='2026-04-10T16:51:23.252875+00:00',
            account_name='main',
            task_type='keeper',
            result='skip_not_due',
            reason='before_next_keeper_time',
            instance_id='iid-old-1',
            payload={'batch_id': 'batch-old'},
        ),
        HistoryRecord(
            created_at='2026-04-10T16:51:23.251609+00:00',
            account_name='main',
            task_type='keeper',
            result='skip_not_due',
            reason='before_next_keeper_time',
            instance_id='iid-old-2',
            payload={'batch_id': 'batch-old'},
        ),
    ]
    store = SimpleNamespace(read_history=lambda **kwargs: rows)

    assert interactive_app._keeper_last_execution_summary(store, account_name='main') == '已处理 0 台 / 跳过 2 台 / 失败 0 台'


def test_keeper_last_execution_summary_falls_back_to_latest_second_bucket():
    latest_rows = [
        HistoryRecord(
            created_at=f'2026-04-10T16:51:27.{100000 + index:06d}+00:00',
            account_name='main',
            task_type='keeper',
            result='skip_not_due',
            reason='before_next_keeper_time',
            instance_id=f'iid-new-{index}',
            payload={},
        )
        for index in range(8)
    ]
    older_rows = [
        HistoryRecord(
            created_at=f'2026-04-10T16:51:23.{100000 + index:06d}+00:00',
            account_name='main',
            task_type='keeper',
            result='skip_not_due',
            reason='before_next_keeper_time',
            instance_id=f'iid-old-{index}',
            payload={},
        )
        for index in range(8)
    ]
    store = SimpleNamespace(read_history=lambda **kwargs: latest_rows + older_rows)

    assert interactive_app._keeper_last_execution_summary(store, account_name='main') == '已处理 0 台 / 跳过 8 台 / 失败 0 台'


def test_browse_keeper_probe_hides_detect_progress(monkeypatch, capsys):
    selections = iter(['0'])
    monkeypatch.setattr(
        interactive_app,
        '_choose_menu_with_refresh',
        lambda title, items, **kwargs: (print(title) or next(selections)),
    )

    def slow_probe(*args, **kwargs):
        time.sleep(0.2)
        return []

    interactive_app._browse_keeper_probe(
        settings=BASE_SETTINGS,
        store=None,
        current_account='main',
        keeper_probe_rows_fn=slow_probe,
    )
    rendered = capsys.readouterr().out
    clean = interactive_app._strip_ansi(rendered)
    assert '正在刷新 Keeper 探测' in clean
    assert '检测进度' not in clean


def test_render_keeper_rules_shows_summary_focused_copy(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.set_runtime_value('last_run:keeper', '2026-04-10T02:58:33+00:00')
    monkeypatch.setattr(interactive_app, 'read_daemon_status', lambda _store: {'running': True})

    rendered = interactive_app._render_keeper_rules(BASE_SETTINGS, 'main', store)

    assert '本次应接管' in rendered
    assert '下次执行时间' in rendered
    assert '上次执行时间' in rendered
    assert '上次执行结果' in rendered


def test_render_keeper_rules_with_refresh_uses_always_policy(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    seen = {}
    selections = iter(['0'])
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)

    def fake_choose_menu_with_refresh(title, items, **kwargs):
        seen['refresh_policy'] = kwargs.get('refresh_policy')
        seen['pre_refresh_fn'] = kwargs.get('pre_refresh_fn')
        return next(selections)

    monkeypatch.setattr(interactive_app, '_choose_menu_with_refresh', fake_choose_menu_with_refresh)
    try:
        interactive_app._keeper_menu(
            args=SimpleNamespace(config='config.yaml', headed=False, state_file='state.json'),
            settings=BASE_SETTINGS,
            current_account='main',
            set_task_enabled_fn=lambda *args, **kwargs: None,
            request_reload_fn=lambda *args, **kwargs: None,
            store=store,
            keeper_probe_rows_fn=lambda *args, **kwargs: [],
            run_keeper_only_fn=lambda **kwargs: [],
            command_history_fn=lambda args: 0,
            load_settings_fn=lambda path: BASE_SETTINGS,
            validate_settings_fn=lambda settings, path: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
    finally:
        task_manager.shutdown(wait=False)

    assert seen['refresh_policy'] == 'always'
    assert callable(seen['pre_refresh_fn'])


def test_render_keeper_probe_page_hides_skipped_group_and_keeps_abnormal():
    rendered = interactive_app._render_keeper_probe_page(
        [
            {
                'instance_id': 'iid-ready',
                'status': 'shutdown',
                'eligible': True,
                'result': 'ready',
                'reason': 'keeper_window_reached',
                'release_deadline': '2026-04-25T00:00:00+08:00',
                'next_keeper_time': '2026-04-24T04:00:00+08:00',
            },
            {
                'instance_id': 'iid-late',
                'status': 'shutdown',
                'eligible': False,
                'result': 'skip_not_due',
                'reason': 'before_next_keeper_time',
                'release_deadline': '2026-04-25T00:00:00+08:00',
                'next_keeper_time': '2026-04-24T04:00:00+08:00',
            },
            {
                'instance_id': 'iid-missing',
                'status': 'running',
                'eligible': False,
                'result': 'skip_missing_shutdown_time',
                'reason': 'missing_shutdown_time',
                'release_deadline': '',
                'next_keeper_time': '',
            },
        ]
    )

    assert '本次将执行' in rendered
    assert '暂不执行' not in rendered
    assert 'iid-late' not in rendered
    assert '状态异常' in rendered
    assert 'iid-missing' in rendered


def test_render_keeper_execution_page_shows_execution_summary_counts():
    results = [
        KeeperResult(
            instance_id='iid-1',
            status='shutdown',
            release_at='',
            release_source='stopped_at',
            started_at='',
            stopped_at='2026-04-01T00:00:00+08:00',
            status_at='',
            release_deadline='2026-04-15T00:00:00+08:00',
            next_keeper_time='2026-04-14T18:00:00+08:00',
            seconds_until_release=0,
            seconds_until_keeper=0,
            started_duration_seconds=None,
            shutdown_duration_seconds=100,
            eligible=False,
            result='keeper_executed',
            reason='keeper_window_reached',
            summary='执行成功',
        ),
        KeeperResult(
            instance_id='iid-2',
            status='shutdown',
            release_at='',
            release_source='stopped_at',
            started_at='',
            stopped_at='2026-04-01T00:00:00+08:00',
            status_at='',
            release_deadline='2026-04-15T00:00:00+08:00',
            next_keeper_time='2026-04-14T18:00:00+08:00',
            seconds_until_release=0,
            seconds_until_keeper=0,
            started_duration_seconds=None,
            shutdown_duration_seconds=100,
            eligible=False,
            result='skip_not_due',
            reason='before_next_keeper_time',
            summary='本次跳过，接管时间=2026-04-14T18:00:00+08:00',
        ),
    ]

    rendered = interactive_app._render_keeper_execution_page(results)

    assert '已处理' in rendered
    assert '跳过' in rendered
    assert '失败' in rendered
    assert '[已处理]' in rendered
    assert '[已跳过]' not in rendered
    assert 'iid-1' in rendered
    assert 'iid-2' not in rendered
    assert '实例状态' in rendered
    assert '当前阶段' in rendered
    assert '下一步动作' in rendered
    assert '处理结果' not in rendered
    assert '处理原因' not in rendered
    assert '2026-04-14 18:00:00' not in rendered
    assert 'T18:00:00+08:00' not in rendered


def test_show_live_scheduled_status_hides_refresh_progress(monkeypatch, capsys):
    def slow_rows():
        time.sleep(0.2)
        return [{'job_name': 'job-1', 'enabled': True, 'latest_result': 'waiting_for_gpu', 'target_time': '01:00', 'advance_hours': 1}]

    monkeypatch.setattr(
        interactive_app,
        '_render_scheduled_status',
        lambda job_name, status_rows, page_status_lines=None: '\n'.join(page_status_lines or []),
    )

    interactive_app._show_live_scheduled_status(
        job_name='job-1',
        fetch_rows_fn=slow_rows,
        poll_action_fn=lambda timeout: 'back',
    )
    rendered = capsys.readouterr().out
    clean = interactive_app._strip_ansi(rendered)
    assert '正在刷新抢机进度' in clean
    assert '刷新进度' not in clean


def test_show_live_scheduled_status_refreshes_latest_check_time_from_fetch_rows(monkeypatch, capsys):
    rows_by_call = iter([
        [{'job_name': 'job-1', 'enabled': True, 'latest_result': 'waiting_for_gpu', 'latest_created_at': '2026-04-10T16:37:50+08:00', 'target_time': '17:00', 'advance_hours': 2}],
        [{'job_name': 'job-1', 'enabled': True, 'latest_result': 'waiting_for_gpu', 'latest_created_at': '2026-04-10T16:38:25+08:00', 'target_time': '17:00', 'advance_hours': 2}],
    ])

    monkeypatch.setattr(interactive_app, '_poll_live_action', lambda timeout: 'refresh')
    actions = iter(['refresh', 'back'])
    interactive_app._show_live_scheduled_status(
        job_name='job-1',
        fetch_rows_fn=lambda: next(rows_by_call),
        poll_action_fn=lambda timeout: next(actions),
    )
    rendered = interactive_app._strip_ansi(capsys.readouterr().out)
    assert '2026-04-10 16:38:25' in rendered


def test_show_live_scheduled_status_auto_refreshes_latest_check_time_on_timeout(capsys):
    rows_by_call = iter([
        [{
            'job_name': 'job-1',
            'enabled': True,
            'daemon_running': True,
            'latest_result': 'outside_window',
            'latest_created_at': '2026-04-10T16:38:25+08:00',
            'target_time': '17:00',
            'advance_hours': 2,
            'timezone': 'Asia/Shanghai',
            'latest_payload': {},
            'has_history': True,
            'latest_matches_current_rule': True,
            'target_mode': 'instance',
            'target_summary': '固定实例=iid-1',
        }],
        [{
            'job_name': 'job-1',
            'enabled': True,
            'daemon_running': True,
            'latest_result': 'outside_window',
            'latest_created_at': '2026-04-10T16:38:30+08:00',
            'target_time': '17:00',
            'advance_hours': 2,
            'timezone': 'Asia/Shanghai',
            'latest_payload': {},
            'has_history': True,
            'latest_matches_current_rule': True,
            'target_mode': 'instance',
            'target_summary': '固定实例=iid-1',
        }],
    ])
    actions = iter(['stay', 'back'])

    interactive_app._show_live_scheduled_status(
        job_name='job-1',
        fetch_rows_fn=lambda: next(rows_by_call),
        poll_action_fn=lambda timeout: next(actions),
    )

    rendered = interactive_app._strip_ansi(capsys.readouterr().out)
    assert '3秒轻量自动刷新 / Enter 立即刷新 / q 返回' in rendered
    assert '2026-04-10 16:38:30' in rendered


def test_show_live_scheduled_status_renders_content_on_first_open_without_enter(capsys):
    interactive_app._show_live_scheduled_status(
        job_name='job-1',
        fetch_rows_fn=lambda: [{
            'job_name': 'job-1',
            'enabled': True,
            'daemon_running': True,
            'latest_result': 'outside_window',
            'latest_created_at': '2026-04-10T16:38:25+08:00',
            'latest_matching_created_at': '2026-04-10T16:38:25+08:00',
            'target_time': '17:00',
            'advance_hours': 2,
            'timezone': 'Asia/Shanghai',
            'latest_payload': {},
            'has_history': True,
            'latest_matches_current_rule': True,
            'target_mode': 'instance',
            'target_summary': '固定实例=iid-1',
        }],
        poll_action_fn=lambda timeout: 'back',
    )
    rendered = interactive_app._strip_ansi(capsys.readouterr().out)
    assert '任务 job-1' in rendered
    assert '最近检查时间' in rendered
    assert '2026-04-10 16:38:25' in rendered


def test_scheduled_live_footer_keeps_auto_refresh_when_daemon_is_running_outside_window():
    row = {
        'job_name': 'job-1',
        'enabled': True,
        'daemon_running': True,
        'schedule_mode': 'daily',
        'latest_result': 'outside_window',
        'target_time': '13:00',
        'advance_hours': 3,
        'timezone': 'Asia/Shanghai',
    }

    footer, timeout = interactive_app._scheduled_live_footer([row])

    assert '自动刷新' in footer
    assert timeout == 3.0


def test_render_keeper_probe_page_hides_empty_skipped_sections():
    rendered = interactive_app._strip_ansi(
        interactive_app._render_keeper_probe_page(
            [
                {
                    'instance_id': 'iid-ready',
                    'status': 'shutdown',
                    'eligible': True,
                    'result': 'ready',
                    'reason': 'keeper_window_reached',
                    'release_deadline': '2026-04-25T00:00:00+08:00',
                    'next_keeper_time': '2026-04-24T04:00:00+08:00',
                }
            ]
        )
    )

    assert '[本次将执行]' in rendered
    assert '[暂不执行]' not in rendered
    assert '[状态异常]' not in rendered


def test_diagnostics_menu_uses_one_second_idle_refresh(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    snapshot_store = interactive_app.InteractiveSnapshotStore()
    task_manager = interactive_app.InteractiveTaskManager(snapshot_store=snapshot_store)
    seen = {}

    def fake_choose_menu_with_refresh(title, items, **kwargs):
        seen['refresh_interval_seconds'] = kwargs.get('refresh_interval_seconds')
        return '0'

    monkeypatch.setattr(interactive_app, '_choose_menu_with_refresh', fake_choose_menu_with_refresh)
    try:
        interactive_app._diagnostics_menu(
            args=SimpleNamespace(config='config.yaml'),
            current_account='main',
            command_list_instances_fn=lambda args: 0,
            command_healthcheck_fn=lambda args: 0,
            settings=BASE_SETTINGS,
            store=store,
            keeper_probe_rows_fn=lambda *args, **kwargs: [],
            load_settings_fn=lambda path: BASE_SETTINGS,
            validate_settings_fn=lambda settings, path: [],
            task_manager=task_manager,
            snapshot_store=snapshot_store,
        )
    finally:
        task_manager.shutdown(wait=False)

    assert seen['refresh_interval_seconds'] == 1.0


def test_poll_live_action_exits_on_single_q_in_tty(monkeypatch):
    class DummyStdin:
        def isatty(self):
            return True

    class DummyStdout:
        def isatty(self):
            return True

    monkeypatch.setattr(interactive_app.sys, 'stdin', DummyStdin())
    monkeypatch.setattr(interactive_app.sys, 'stdout', DummyStdout())
    monkeypatch.setattr(interactive_app, '_read_key_with_timeout', lambda timeout: 'q')

    assert interactive_app._poll_live_action(5.0) == 'back'


def test_read_key_with_timeout_accepts_none_timeout(monkeypatch):
    class DummyStdin:
        def fileno(self):
            return 0

    calls = []
    monkeypatch.setattr(interactive_app.sys, 'stdin', DummyStdin())
    monkeypatch.setattr(interactive_app.termios, 'tcgetattr', lambda fd: 'prev')
    monkeypatch.setattr(interactive_app.termios, 'tcsetattr', lambda fd, when, prev: None)
    monkeypatch.setattr(interactive_app.tty, 'setcbreak', lambda fd: None)
    monkeypatch.setattr(interactive_app, '_read_fd_char', lambda fd: '\n')

    def fake_select(reads, writes, errors, timeout=None):
        calls.append(timeout)
        return ([reads[0]], [], [])

    monkeypatch.setattr(interactive_app.select, 'select', fake_select)

    assert interactive_app._read_key_with_timeout(None) == 'ENTER'
    assert calls[0] is None


def test_read_key_with_timeout_none_parses_arrow_sequence_with_blocking_reads(monkeypatch):
    class DummyStdin:
        def fileno(self):
            return 0

    calls = []
    chars = iter(['\x1b', '[', 'A'])
    monkeypatch.setattr(interactive_app.sys, 'stdin', DummyStdin())
    monkeypatch.setattr(interactive_app.termios, 'tcgetattr', lambda fd: 'prev')
    monkeypatch.setattr(interactive_app.termios, 'tcsetattr', lambda fd, when, prev: None)
    monkeypatch.setattr(interactive_app.tty, 'setcbreak', lambda fd: None)
    monkeypatch.setattr(interactive_app, '_read_fd_char', lambda fd: next(chars))

    def fake_select(reads, writes, errors, timeout=None):
        calls.append(timeout)
        return ([reads[0]], [], [])

    monkeypatch.setattr(interactive_app.select, 'select', fake_select)

    assert interactive_app._read_key_with_timeout(None) == 'UP'
    assert calls == [None]


def test_read_key_with_timeout_timeout_mode_parses_delayed_arrow_sequence(monkeypatch):
    class DummyStdin:
        def fileno(self):
            return 0

    call_time = {'value': 0.0}
    select_calls = {'value': 0}
    chars = iter(['\x1b', '[', 'B'])

    monkeypatch.setattr(interactive_app.sys, 'stdin', DummyStdin())
    monkeypatch.setattr(interactive_app.termios, 'tcgetattr', lambda fd: 'prev')
    monkeypatch.setattr(interactive_app.termios, 'tcsetattr', lambda fd, when, prev: None)
    monkeypatch.setattr(interactive_app.tty, 'setcbreak', lambda fd: None)
    monkeypatch.setattr(interactive_app.time, 'monotonic', lambda: call_time['value'])
    monkeypatch.setattr(interactive_app, '_read_fd_char', lambda fd: next(chars))

    def fake_select(reads, writes, errors, timeout=None):
        select_calls['value'] += 1
        if select_calls['value'] == 1:
            return ([reads[0]], [], [])
        if select_calls['value'] == 2:
            call_time['value'] += 0.02
            return ([reads[0]], [], [])
        if select_calls['value'] == 3:
            call_time['value'] += 0.02
            return ([reads[0]], [], [])
        return ([], [], [])

    monkeypatch.setattr(interactive_app.select, 'select', fake_select)

    assert interactive_app._read_key_with_timeout(1.0) == 'DOWN'


def test_read_key_with_timeout_none_parses_application_cursor_sequence(monkeypatch):
    class DummyStdin:
        def fileno(self):
            return 0

    chars = iter(['\x1b', 'O', 'B'])
    monkeypatch.setattr(interactive_app.sys, 'stdin', DummyStdin())
    monkeypatch.setattr(interactive_app.termios, 'tcgetattr', lambda fd: 'prev')
    monkeypatch.setattr(interactive_app.termios, 'tcsetattr', lambda fd, when, prev: None)
    monkeypatch.setattr(interactive_app.tty, 'setcbreak', lambda fd: None)
    monkeypatch.setattr(interactive_app.select, 'select', lambda reads, writes, errors, timeout=None: ([reads[0]], [], []))
    monkeypatch.setattr(interactive_app, '_read_fd_char', lambda fd: next(chars))

    assert interactive_app._read_key_with_timeout(None) == 'DOWN'


def test_read_key_with_timeout_timeout_mode_parses_application_cursor_sequence(monkeypatch):
    class DummyStdin:
        def fileno(self):
            return 0

    call_time = {'value': 0.0}
    select_calls = {'value': 0}
    chars = iter(['\x1b', 'O', 'A'])

    monkeypatch.setattr(interactive_app.sys, 'stdin', DummyStdin())
    monkeypatch.setattr(interactive_app.termios, 'tcgetattr', lambda fd: 'prev')
    monkeypatch.setattr(interactive_app.termios, 'tcsetattr', lambda fd, when, prev: None)
    monkeypatch.setattr(interactive_app.tty, 'setcbreak', lambda fd: None)
    monkeypatch.setattr(interactive_app.time, 'monotonic', lambda: call_time['value'])
    monkeypatch.setattr(interactive_app, '_read_fd_char', lambda fd: next(chars))

    def fake_select(reads, writes, errors, timeout=None):
        select_calls['value'] += 1
        if select_calls['value'] == 1:
            return ([reads[0]], [], [])
        if select_calls['value'] == 2:
            call_time['value'] += 0.02
            return ([reads[0]], [], [])
        if select_calls['value'] == 3:
            call_time['value'] += 0.02
            return ([reads[0]], [], [])
        return ([], [], [])

    monkeypatch.setattr(interactive_app.select, 'select', fake_select)

    assert interactive_app._read_key_with_timeout(1.0) == 'UP'


def test_read_key_with_timeout_timeout_mode_parses_extended_csi_sequence(monkeypatch):
    class DummyStdin:
        def fileno(self):
            return 0

    call_time = {'value': 0.0}
    select_calls = {'value': 0}
    chars = iter(['\x1b', '[', '1', ';', '2', 'B'])

    monkeypatch.setattr(interactive_app.sys, 'stdin', DummyStdin())
    monkeypatch.setattr(interactive_app.termios, 'tcgetattr', lambda fd: 'prev')
    monkeypatch.setattr(interactive_app.termios, 'tcsetattr', lambda fd, when, prev: None)
    monkeypatch.setattr(interactive_app.tty, 'setcbreak', lambda fd: None)
    monkeypatch.setattr(interactive_app.time, 'monotonic', lambda: call_time['value'])
    monkeypatch.setattr(interactive_app, '_read_fd_char', lambda fd: next(chars))

    def fake_select(reads, writes, errors, timeout=None):
        select_calls['value'] += 1
        if select_calls['value'] <= 6:
            call_time['value'] += 0.01
            return ([reads[0]], [], [])
        return ([], [], [])

    monkeypatch.setattr(interactive_app.select, 'select', fake_select)

    assert interactive_app._read_key_with_timeout(1.0) == 'DOWN'


def test_read_key_with_timeout_timeout_mode_uses_fd_reads_for_real_pty(monkeypatch):
    master_fd, slave_fd = pty.openpty()
    slave_stream = io.TextIOWrapper(os.fdopen(slave_fd, 'rb', buffering=0), encoding='utf-8', newline='')
    original_stdin = interactive_app.sys.stdin
    result: dict[str, str] = {}

    monkeypatch.setattr(interactive_app.sys, 'stdin', slave_stream)

    def reader():
        try:
            result['value'] = interactive_app._read_key_with_timeout(1.0)
        except Exception as exc:  # pragma: no cover - failure path only
            result['error'] = repr(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(0.05)
    os.write(master_fd, b'\x1b[B')
    thread.join(timeout=2.0)

    interactive_app.sys.stdin = original_stdin
    slave_stream.close()
    os.close(master_fd)

    assert result.get('error') is None
    assert result.get('value') == 'DOWN'


def test_render_scheduled_status_uses_frozen_live_fields_when_present():
    rendered = interactive_app._render_scheduled_status(
        'job-1',
        [
            {
                'job_name': 'job-1',
                'enabled': True,
                'target_mode': 'instance',
                'target_summary': '固定实例=iid-1',
                'target_time': '01:00',
                'advance_hours': 1,
                'schedule_mode': 'daily',
                'latest_result': '',
                'latest_created_at': '',
                'latest_payload': {},
                '_live_execution_label': '静态执行状态',
                '_live_execution_tone': 'info',
                '_live_stage_label': '静态阶段',
                '_live_stage_tone': 'info',
                '_live_next_action': '静态下一步',
                '_live_poll_text': '固定轮询时间',
                '_live_target_text': '固定目标时间',
                '_live_missing_reason_label': '',
                '_live_missing_reason_tone': 'muted',
            }
        ],
    )

    assert '静态执行状态' in rendered
    assert '静态阶段' in rendered
    assert '静态下一步' in rendered
    assert '固定轮询时间' in rendered
    assert '固定目标时间' in rendered


def test_render_scheduled_status_sanitizes_english_summary_codes():
    rendered = interactive_app._render_scheduled_status(
        'selector-3080ti',
        [
            {
                'job_name': 'selector-3080ti',
                'enabled': True,
                'target_mode': 'instance',
                'target_summary': '固定实例=fbda11ad52-d9008683',
                'target_time': '01:30',
                'advance_hours': 1,
                'timezone': 'Asia/Shanghai',
                'latest_result': 'waiting_for_gpu',
                'latest_reason': 'gpu_idle_zero',
                'latest_summary': '候选存在但暂不可抢；候选数=1；目标时间=01:30；候选=fbda11ad52-d9008683(running_with_gpu)；状态=shutdown；原因=gpu_idle_zero',
                'latest_created_at': '2026-04-09T01:29:00+08:00',
                'latest_payload': {},
                'latest_instance_id': '',
            }
        ],
    )

    assert 'running_with_gpu' not in rendered
    assert 'gpu_idle_zero' not in rendered
    assert 'shutdown' not in rendered
    assert '实例已在 GPU 模式运行' in rendered
    assert '空闲 GPU 数量为 0' in rendered
    assert '已关机' in rendered


def test_render_scheduled_job_detail_uses_task_status_label():
    job = ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='19:30', advance_hours=1)
    rendered = interactive_app._render_scheduled_job_detail(
        job,
        {
            'job_name': 'job-1',
            'enabled': True,
            'daemon_running': False,
            'target_time': '19:30',
            'advance_hours': 1,
            'task_status_label': '轮询中',
            'task_status_tone': 'ok',
        },
        'main',
    )

    assert '任务状态' in rendered
    assert '轮询中' in rendered
    assert '运行状态' not in rendered


def test_render_scheduled_job_detail_shows_last_run_summary():
    job = ScheduledStartJob(instance_id='iid-1', name='job-1', target_time='19:30', advance_hours=1)
    rendered = interactive_app._render_scheduled_job_detail(
        job,
        {
            'job_name': 'job-1',
            'enabled': True,
            'daemon_running': False,
            'target_time': '19:30',
            'advance_hours': 1,
            'task_status_label': '等待执行',
            'task_status_tone': 'info',
            'last_run_trigger': '修改规则后自动执行',
            'last_run_label': '有候选但暂时不可抢',
            'last_run_summary': '空闲 GPU 数量为 0',
        },
        'main',
    )

    assert '最近执行' in rendered
    assert '修改规则后自动执行' in rendered
    assert '本次结果' in rendered
    assert '有候选但暂时不可抢' in rendered
    assert '结果说明' in rendered
    assert '空闲 GPU 数量为 0' in rendered


def test_scheduled_picker_item_label_shows_last_run_summary():
    label = interactive_app._scheduled_picker_item_label(
        {
            'job_name': 'job-1',
            'target_time': '19:30',
            'advance_hours': 1,
            'enabled': True,
            'task_status_label': '轮询中',
            'last_run_label': '有候选但暂时不可抢',
            'last_run_summary': '空闲 GPU 数量为 0',
        }
    )

    assert 'job-1  19:30 提前1h  轮询中' in label
    assert '最近执行: 有候选但暂时不可抢' in label
    assert '空闲 GPU 数量为 0' in label


def test_browse_instance_list_preserves_selected_item(monkeypatch):
    settings = BASE_SETTINGS
    choices = iter(['2', '2', '0'])
    default_keys = []
    rows = [
        {'instance_id': 'iid-1', 'name': 'a', 'region': '北京A区', 'status': 'running', 'machine_alias': '1号', 'status_at': ''},
        {'instance_id': 'iid-2', 'name': 'b', 'region': '北京B区', 'status': 'shutdown', 'machine_alias': '2号', 'status_at': ''},
    ]

    monkeypatch.setattr(interactive_app, '_load_instance_rows_via_command', lambda **kwargs: rows)
    monkeypatch.setattr(interactive_app, '_show_result_screen', lambda *args, **kwargs: None)

    def fake_choose_menu(title, items, *, default_key=None):
        default_keys.append(default_key)
        return next(choices)

    monkeypatch.setattr(interactive_app, '_choose_menu', fake_choose_menu)

    interactive_app._browse_instance_list(
        args=SimpleNamespace(config='config.yaml'),
        current_account='main',
        settings=settings,
        command_list_instances_fn=lambda args: 0,
    )

    assert default_keys == ['1', '2', '2']


def test_browse_keeper_probe_preserves_selected_item(monkeypatch):
    settings = BASE_SETTINGS
    choices = iter(['2', '2', '0'])
    default_keys = []
    rows = [
        {'instance_id': 'iid-1', 'result': 'skip_not_due', 'reason': 'before_next_keeper_time', 'status': 'shutdown', 'eligible': False},
        {'instance_id': 'iid-2', 'result': 'ready', 'reason': 'keeper_window_reached', 'status': 'shutdown', 'eligible': True},
    ]

    monkeypatch.setattr(interactive_app, '_show_result_screen', lambda *args, **kwargs: None)

    def fake_choose_menu(title, items, *, default_key=None):
        default_keys.append(default_key)
        return next(choices)

    monkeypatch.setattr(interactive_app, '_choose_menu', fake_choose_menu)

    interactive_app._browse_keeper_probe(
        settings=settings,
        store=None,
        current_account='main',
        keeper_probe_rows_fn=lambda *args, **kwargs: rows,
    )

    assert default_keys == ['1', '2', '2']


def test_interactive_config_diagnostics_syncs_stable_fields_and_cleans_priority(tmp_path, monkeypatch):
    settings = Settings(
        auth=AuthSettings(authorization='Bearer token'),
        accounts=[AccountSettings(name='main', enabled=True, authorization='Bearer token')],
        tasks=TaskSettings(
            keeper=BASE_SETTINGS.tasks.keeper,
            scheduled_start=ScheduledStartSettings(
                enabled=True,
                poll_interval_seconds=300,
                jobs=[
                    ScheduledStartJob(
                        name='selector-3080ti',
                        target_time='20:00',
                        advance_hours=1,
                        schedule_mode='once',
                        timezone='Asia/Shanghai',
                        selector=ScheduledStartSelector(gpu_model='RTX 3080 Ti'),
                    )
                ],
            ),
        ),
    )
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        yaml.safe_dump(
            {
                'auth': {'authorization': 'Bearer token'},
                'storage': {'database_file': 'data/raw.db'},
                'accounts': [{'name': 'main', 'enabled': True, 'authorization': 'Bearer token'}],
                'tasks': {
                    'keeper': {'enabled': True},
                    'scheduled_start': {
                        'enabled': True,
                        'jobs': [
                            {
                                'name': 'selector-3080ti',
                                'target_time': '20:00',
                                'advance_hours': 1,
                                'selector': {'gpu_model': 'RTX 3080 Ti'},
                                'priority': [{'region': '北京A区'}],
                            }
                        ],
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    monkeypatch.setenv('AUTODL_DB_PATH', '/tmp/runtime.db')

    body = interactive_app._render_config_diagnostics(
        settings=settings,
        current_account='main',
        config_path=str(config_path),
        load_settings_fn=load_settings,
        validate_settings_fn=lambda updated, purpose='validate': [],
    )
    raw_payload = yaml.safe_load(config_path.read_text(encoding='utf-8'))

    assert '配置诊断' in body
    assert '已清理历史 priority 字段' in body
    assert 'storage.database_file ← 环境变量/.env (AUTODL_DB_PATH)' in body
    assert raw_payload['storage']['database_file'] == 'data/raw.db'
    assert 'priority' not in yaml.safe_dump(raw_payload, allow_unicode=True, sort_keys=False)
    saved_job = raw_payload['tasks']['scheduled_start']['jobs'][0]
    assert saved_job['schedule_mode'] == 'once'
    assert saved_job['timezone'] == 'Asia/Shanghai'


def test_interactive_diagnostics_menu_contains_config_diagnostics(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    answers = iter(['4', '0', '0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert '诊断' in captured.out
    assert '配置诊断' in captured.out
    assert '查看原始配置' not in captured.out
    assert '查看最终生效配置' not in captured.out


def test_interactive_no_separate_config_menu(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    monkeypatch.setattr(cli, 'load_settings', lambda path: BASE_SETTINGS)
    monkeypatch.setattr(cli, 'create_store', lambda settings: store)
    monkeypatch.setattr(cli, 'list_instances_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'history_panel_rows', lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, 'auth_panel_rows', lambda *args, **kwargs: [])

    answers = iter(['0'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))

    code = cli.main(['interactive', '--config', 'config.yaml'])
    captured = capsys.readouterr()

    assert code == 0
    assert '配置' not in captured.out
    assert '查看配置概览' not in captured.out
    assert '查看原始配置' not in captured.out
    assert '查看最终生效配置' not in captured.out
