from __future__ import annotations

from types import SimpleNamespace

from autodl_helper.core.config import load_settings, read_raw_settings, write_raw_settings
from autodl_helper.runtime.control import apply_runtime_controls_to_scheduled_jobs
from autodl_helper.storage import SQLiteStore
from autodl_helper.ui.app import run_ui
from autodl_helper.ui.config_wizard import (
    add_scheduled_job,
    delete_scheduled_job,
    list_job_summaries,
    save_payload,
    scheduled_jobs,
    toggle_scheduled_job,
    update_keeper,
    update_scheduled_job,
)


def _valid_payload(db_path: str) -> dict:
    return {
        'accounts': [{'name': 'main', 'enabled': True, 'authorization': 'Bearer token'}],
        'storage': {'database_file': db_path},
        'tasks': {
            'keeper': {'enabled': True},
            'scheduled_start': {'enabled': True, 'poll_interval_seconds': 5, 'jobs': []},
        },
    }


def test_config_wizard_scheduled_job_crud_helpers():
    payload = _valid_payload('data/test.db')
    add_scheduled_job(
        payload,
        {
            'name': 'daily-fixed',
            'instance_id': 'iid-1',
            'target_time': '13:00',
            'advance_hours': 2,
            'timezone': 'Asia/Shanghai',
        },
    )

    assert 'daily-fixed' in list_job_summaries(payload)[0]
    assert scheduled_jobs(payload)[0]['enabled'] is True

    assert toggle_scheduled_job(payload, 0) is False
    update_scheduled_job(payload, 0, {'target_time': '14:30', 'advance_hours': 1})

    job = scheduled_jobs(payload)[0]
    assert job['target_time'] == '14:30'
    assert job['advance_hours'] == 1
    assert job['enabled'] is False

    deleted = delete_scheduled_job(payload, 0)
    assert deleted['name'] == 'daily-fixed'
    assert scheduled_jobs(payload) == []


def test_config_wizard_updates_keeper_and_saves_with_backup(tmp_path):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    write_raw_settings(config_path, _valid_payload(str(db_path)))
    original_text = config_path.read_text(encoding='utf-8')

    payload = read_raw_settings(config_path)
    update_keeper(payload, {'enabled': False, 'interval_minutes': 120, 'keeper_trigger_before_hours': 12})
    add_scheduled_job(
        payload,
        {
            'name': 'selector-job',
            'target_time': '20:00',
            'advance_hours': 1,
            'timezone': 'Asia/Shanghai',
            'selector': {'gpu_model': 'RTX 4090', 'gpu_count': 1},
        },
    )

    errors = save_payload(config_path, payload)

    assert errors == []
    assert config_path.with_suffix('.yaml.bak').read_text(encoding='utf-8') == original_text
    settings = load_settings(config_path)
    assert settings.tasks.keeper.enabled is False
    assert settings.tasks.keeper.interval_minutes == 120
    assert settings.tasks.keeper.keeper_trigger_before_hours == 12
    assert settings.tasks.scheduled_start.jobs[0].name == 'selector-job'


def test_save_payload_requests_reload_after_write(tmp_path):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    payload = _valid_payload(str(db_path))
    payload['tasks']['scheduled_start']['jobs'] = [
        {
            'enabled': True,
            'name': 'selector-job',
            'target_time': '20:00',
            'advance_hours': 1,
            'timezone': 'Asia/Shanghai',
            'selector': {'gpu_model': 'RTX 4090', 'gpu_count': 1},
        }
    ]
    write_raw_settings(config_path, payload)
    payload = read_raw_settings(config_path)
    seen = []

    def request_reload(store):
        seen.append(store.path.name)
        return {}

    from autodl_helper.ui.config_wizard import save_payload

    errors = save_payload(config_path, payload, request_reload_fn=request_reload)

    assert errors == []
    assert seen == ['autodl-helper.db']


def test_disabled_scheduled_job_is_loaded_but_filtered_at_runtime(tmp_path):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    payload = _valid_payload(str(db_path))
    payload['tasks']['scheduled_start']['jobs'] = [
        {'enabled': False, 'name': 'disabled-job', 'instance_id': 'iid-1', 'target_time': '14:00', 'advance_hours': 1},
        {'enabled': True, 'name': 'enabled-job', 'instance_id': 'iid-2', 'target_time': '15:00', 'advance_hours': 1},
    ]
    write_raw_settings(config_path, payload)
    settings = load_settings(config_path)
    store = SQLiteStore(db_path)
    store.init_schema()

    effective = apply_runtime_controls_to_scheduled_jobs(store, 'main', settings.tasks.scheduled_start.jobs)

    assert [job.name for job in settings.tasks.scheduled_start.jobs] == ['disabled-job', 'enabled-job']
    assert [job.name for job in effective] == ['enabled-job']


def test_run_ui_can_open_config_management_menu_without_hanging(tmp_path, capsys):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    write_raw_settings(config_path, _valid_payload(str(db_path)))
    inputs = iter(['3', '2', '0', '0'])

    code = run_ui(SimpleNamespace(command='ui', config=str(config_path)), input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert code == 0
    assert 'autodl-helper dashboard' in captured.out
    assert '配置管理' in captured.out
    assert '抢机配置' in captured.out
    assert 'Keeper 配置' in captured.out


def test_config_wizard_labels_config_entries_explicitly(tmp_path, capsys):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    write_raw_settings(config_path, _valid_payload(str(db_path)))

    from autodl_helper.ui.config_wizard import run_config_wizard

    saved = run_config_wizard(config_path, input_fn=lambda prompt='': '0')
    captured = capsys.readouterr()

    assert saved is False
    assert '抢机配置' in captured.out
    assert 'Keeper 配置' in captured.out
    assert '抢机任务' not in captured.out
    assert 'Keeper 参数' not in captured.out


def test_config_wizard_edits_single_scheduled_job_field(tmp_path):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    payload = _valid_payload(str(db_path))
    payload['tasks']['scheduled_start']['jobs'] = [
        {
            'enabled': True,
            'name': 'fixed-job',
            'instance_id': 'iid-1',
            'target_time': '13:00',
            'advance_hours': 2,
            'timezone': 'Asia/Shanghai',
        }
    ]
    write_raw_settings(config_path, payload)

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter([
        '1',  # 抢机任务
        '2',  # 编辑任务
        '1',  # 任务编号
        '3',  # 只改目标时间
        '1430',
        '0',  # 返回编辑页
        '0',  # 返回配置管理
        '0',  # 退出并保存
    ])

    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    updated = read_raw_settings(config_path)
    job = updated['tasks']['scheduled_start']['jobs'][0]

    assert saved is True
    assert job['target_time'] == '14:30'
    assert job['name'] == 'fixed-job'
    assert job['instance_id'] == 'iid-1'
    assert job['advance_hours'] == 2
    assert job['timezone'] == 'Asia/Shanghai'
    assert job['enabled'] is True


def test_config_wizard_edits_scheduled_advance_with_duration_text(tmp_path):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    payload = _valid_payload(str(db_path))
    payload['tasks']['scheduled_start']['jobs'] = [
        {
            'enabled': True,
            'name': 'fixed-job',
            'instance_id': 'iid-1',
            'target_time': '13:00',
            'advance_hours': 2,
            'timezone': 'Asia/Shanghai',
        }
    ]
    write_raw_settings(config_path, payload)

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter([
        '1',
        '2',
        '1',
        '4',
        '90m',
        '0',
        '0',
        '0',
    ])

    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    job = read_raw_settings(config_path)['tasks']['scheduled_start']['jobs'][0]

    assert saved is True
    assert job['advance_hours'] == 1.5


def test_config_wizard_edits_scheduled_job_frequency_to_weekly(tmp_path):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    payload = _valid_payload(str(db_path))
    payload['tasks']['scheduled_start']['jobs'] = [
        {
            'enabled': True,
            'name': 'fixed-job',
            'instance_id': 'iid-1',
            'target_time': '13:00',
            'advance_hours': 2,
            'schedule_mode': 'daily',
            'timezone': 'Asia/Shanghai',
        }
    ]
    write_raw_settings(config_path, payload)

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter([
        '1',  # 抢机任务
        '2',  # 编辑任务
        '1',  # 任务编号
        '2',  # 修改频率
        '3',  # 每周
        '1,3,5',
        '0',
        '0',
        '0',
    ])

    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    job = read_raw_settings(config_path)['tasks']['scheduled_start']['jobs'][0]

    assert saved is True
    assert job['schedule_mode'] == 'weekly'
    assert job['weekdays'] == [1, 3, 5]


def test_config_wizard_accepts_friendly_weekday_input(tmp_path):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    payload = _valid_payload(str(db_path))
    payload['tasks']['scheduled_start']['jobs'] = [
        {
            'enabled': True,
            'name': 'fixed-job',
            'instance_id': 'iid-1',
            'target_time': '13:00',
            'advance_hours': 2,
            'schedule_mode': 'daily',
            'timezone': 'Asia/Shanghai',
        }
    ]
    write_raw_settings(config_path, payload)

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter([
        '1',
        '2',
        '1',
        '2',
        '3',
        '周一三五',
        '0',
        '0',
        '0',
    ])

    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    job = read_raw_settings(config_path)['tasks']['scheduled_start']['jobs'][0]

    assert saved is True
    assert job['schedule_mode'] == 'weekly'
    assert job['weekdays'] == [1, 3, 5]


def test_config_wizard_once_schedule_prompts_for_run_date(tmp_path):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    write_raw_settings(config_path, _valid_payload(str(db_path)))

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter([
        '1',
        '1',
        '1',
        'once-job',
        '930',
        '1.5h',
        '1',
        '2026-05-20',
        'iid-1',
        '0',
        '0',
    ])

    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    job = read_raw_settings(config_path)['tasks']['scheduled_start']['jobs'][0]

    assert saved is True
    assert job['schedule_mode'] == 'once'
    assert job['run_date'] == '2026-05-20'
    assert job['target_time'] == '09:30'
    assert job['advance_hours'] == 1.5


def test_config_wizard_keeper_menu_saves_on_exit_without_extra_confirm(tmp_path, capsys):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    payload = _valid_payload(str(db_path))
    payload['tasks']['scheduled_start']['enabled'] = False
    write_raw_settings(config_path, payload)

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter([
        '2',  # Keeper 参数
        '1',  # 核心参数
        '1',  # 释放前多久开始保活
        '18',
        '0',  # 返回 Keeper 菜单
        '0',  # 返回配置管理
        '0',  # 退出时自动保存
    ])

    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()
    updated = read_raw_settings(config_path)

    assert saved is True
    assert updated['tasks']['keeper']['keeper_trigger_before_hours'] == 18
    assert '保存并返回' not in captured.out
    assert '草稿已更新，保存后生效' in captured.out
    assert '配置已保存并已请求重载' in captured.out


def test_config_wizard_save_success_does_not_call_saved_config_a_draft(tmp_path, capsys):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    payload = _valid_payload(str(db_path))
    payload['tasks']['scheduled_start']['enabled'] = False
    write_raw_settings(config_path, payload)

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter([
        '2',  # Keeper 参数
        '1',  # 核心参数
        '1',  # 释放前多久开始保活
        '18',
        '0',
        '0',
        '0',
    ])

    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert saved is True
    assert '配置已保存并已请求重载' in captured.out
    assert '草稿已更新，保存后生效' not in captured.out.split('配置已保存并已请求重载')[-1]


def test_scheduled_job_edit_notice_says_draft_until_save(tmp_path, capsys):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    payload = _valid_payload(str(db_path))
    payload['tasks']['scheduled_start']['jobs'] = [
        {
            'enabled': True,
            'name': 'fixed-job',
            'instance_id': 'iid-1',
            'target_time': '13:00',
            'advance_hours': 2,
            'timezone': 'Asia/Shanghai',
        }
    ]
    write_raw_settings(config_path, payload)

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter([
        '1',  # 抢机任务
        '2',  # 编辑任务
        '1',  # 任务编号
        '3',  # 目标时间
        '1430',
        '0',
        '0',
        '0',
    ])

    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert saved is True
    assert '草稿已更新，保存后生效' in captured.out
    assert '已更新，按 0 返回' not in captured.out


def test_scheduled_top_level_notice_says_draft_until_save(tmp_path, capsys):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    payload = _valid_payload(str(db_path))
    payload['tasks']['scheduled_start']['enabled'] = False
    payload['tasks']['scheduled_start']['poll_interval_seconds'] = 5
    write_raw_settings(config_path, payload)

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter([
        '1',  # 抢机任务
        '6',  # 修改轮询
        '10',
        '0',
        '0',
    ])

    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert saved is True
    assert '草稿已更新轮询间隔，保存后生效' in captured.out
    assert '轮询间隔已更新' not in captured.out


def test_config_wizard_enter_submenu_without_changes_does_not_mark_dirty(tmp_path, capsys):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    write_raw_settings(config_path, _valid_payload(str(db_path)))
    before = config_path.read_text(encoding='utf-8')

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter(['1', '0', '0'])
    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert saved is False
    assert '有未保存修改' not in captured.out
    assert config_path.read_text(encoding='utf-8') == before
    assert not config_path.with_suffix('.yaml.bak').exists()


def test_config_wizard_deletes_unnamed_selector_job(tmp_path):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    payload = _valid_payload(str(db_path))
    payload['tasks']['scheduled_start']['jobs'] = [
        {
            'enabled': True,
            'target_time': '20:00',
            'advance_hours': 1,
            'timezone': 'Asia/Shanghai',
            'selector': {'gpu_model': 'RTX 4090', 'gpu_count': 1},
        }
    ]
    write_raw_settings(config_path, payload)

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter(['1', '3', '1', 'job-1', '5', 'y', '0', '0'])
    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    updated = read_raw_settings(config_path)

    assert saved is True
    assert updated['tasks']['scheduled_start']['jobs'] == []


def test_scheduled_menu_rejects_unknown_choice_without_change(tmp_path, capsys):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    payload = _valid_payload(str(db_path))
    payload['tasks']['scheduled_start']['jobs'] = [
        {'enabled': True, 'name': 'fixed-job', 'instance_id': 'iid-1', 'target_time': '13:00', 'advance_hours': 2}
    ]
    write_raw_settings(config_path, payload)
    before = config_path.read_text(encoding='utf-8')

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter(['1', 'x', '0', '0'])
    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert saved is False
    assert '无效选择，请输入 1/2/3/4/5/6 或 0' in captured.out or '无效选择，请输入 1/2/3/0' in captured.out
    assert '有未保存修改' not in captured.out
    assert config_path.read_text(encoding='utf-8') == before


def test_entering_missing_scheduled_menu_without_change_does_not_expand_config(tmp_path, capsys):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    payload = _valid_payload(str(db_path))
    del payload['tasks']['scheduled_start']
    write_raw_settings(config_path, payload)
    before = config_path.read_text(encoding='utf-8')

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter(['1', '0', '0'])
    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert saved is False
    assert '有未保存修改' not in captured.out
    assert config_path.read_text(encoding='utf-8') == before


def test_keeper_menu_negative_index_is_rejected_without_change(tmp_path, capsys):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    write_raw_settings(config_path, _valid_payload(str(db_path)))
    before = config_path.read_text(encoding='utf-8')

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter(['2', '1', '-1', '0', '0'])
    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert saved is False
    assert '操作失败' in captured.out or '无效选择，请输入 1/2/0' in captured.out
    assert '有未保存修改' not in captured.out
    assert config_path.read_text(encoding='utf-8') == before


def test_scheduled_job_helpers_reject_negative_index():
    payload = _valid_payload('data/test.db')
    add_scheduled_job(
        payload,
        {'name': 'fixed-job', 'instance_id': 'iid-1', 'target_time': '13:00', 'advance_hours': 2},
    )

    import pytest

    with pytest.raises(IndexError):
        update_scheduled_job(payload, -1, {'target_time': '14:00'})
    with pytest.raises(IndexError):
        delete_scheduled_job(payload, -1)
    with pytest.raises(IndexError):
        toggle_scheduled_job(payload, -1)


def test_selecting_task_can_be_cancelled_without_dirty(tmp_path, capsys):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    payload = _valid_payload(str(db_path))
    payload['tasks']['scheduled_start']['jobs'] = [
        {'enabled': True, 'name': 'fixed-job', 'instance_id': 'iid-1', 'target_time': '13:00', 'advance_hours': 2}
    ]
    write_raw_settings(config_path, payload)
    before = config_path.read_text(encoding='utf-8')

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter(['1', '2', '0', '0', '0'])
    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert saved is False
    assert '已取消' in captured.out
    assert '有未保存修改' not in captured.out
    assert config_path.read_text(encoding='utf-8') == before


def test_global_scheduled_toggle_requires_confirmation(tmp_path, capsys):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    write_raw_settings(config_path, _valid_payload(str(db_path)))
    before = config_path.read_text(encoding='utf-8')

    from autodl_helper.ui.config_wizard import run_config_wizard

    inputs = iter(['1', '5', 'n', '0', '0'])
    saved = run_config_wizard(config_path, input_fn=lambda prompt='': next(inputs))
    captured = capsys.readouterr()

    assert saved is False
    assert '已取消' in captured.out
    assert '有未保存修改' not in captured.out
    assert config_path.read_text(encoding='utf-8') == before


def test_save_payload_requests_reload_only_after_valid_save(tmp_path):
    config_path = tmp_path / 'config.yaml'
    db_path = tmp_path / 'data' / 'autodl-helper.db'
    write_raw_settings(config_path, _valid_payload(str(db_path)))
    payload = read_raw_settings(config_path)
    payload['tasks']['keeper']['shutdown_release_after_hours'] = 12
    payload['tasks']['keeper']['keeper_trigger_before_hours'] = 4
    payload['tasks']['scheduled_start']['enabled'] = False
    seen = []

    def request_reload(store):
        seen.append(store.path.name)
        return {}

    errors = save_payload(config_path, payload, request_reload_fn=request_reload)

    assert errors == []
    assert seen == ['autodl-helper.db']
