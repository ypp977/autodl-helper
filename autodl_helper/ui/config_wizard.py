from __future__ import annotations

import copy
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from autodl_helper.cli.shared_settings import validate_settings
from autodl_helper.core.config import load_settings, read_raw_settings, write_raw_settings
from autodl_helper.runtime_control import request_config_reload
from autodl_helper.core.store import SQLiteStore

from .render import BLUE, BOLD, CYAN, GREEN, RED, YELLOW, clear_screen, color, print_numbered_menu, render_notice, render_rule, render_section

InputFn = Callable[[str], str]
PrintFn = Callable[[str], None]

_TIME_RE = re.compile(r'^(?:[01]\d|2[0-3]):[0-5]\d$')
_TIME_COMPACT_RE = re.compile(r'^\d{1,4}$')
_BOOL_TRUE = {'y', 'yes', 'true', '1', 'on', '启用', '是'}
_BOOL_FALSE = {'n', 'no', 'false', '0', 'off', '停用', '否'}
_WEEKDAY_LABELS = {
    1: '周一',
    2: '周二',
    3: '周三',
    4: '周四',
    5: '周五',
    6: '周六',
    7: '周日',
}
_SCHEDULE_LABELS = {
    'once': '单次',
    'daily': '每天',
    'weekly': '每周',
}
_KEEPER_CORE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ('keeper_trigger_before_hours', '到期前触发保活(小时)', 'int_zero'),
    ('shutdown_release_after_hours', '释放窗口(小时)', 'int_positive'),
)

_KEEPER_ADVANCED_FIELDS: tuple[tuple[str, str, str], ...] = (
    ('enabled', '启用 Keeper', 'bool'),
    ('interval_minutes', '检查间隔(分钟)', 'int_positive'),
    ('power_on_wait_seconds', '开机等待(秒)', 'int_zero'),
    ('power_off_wait_seconds', '关机等待(秒)', 'int_zero'),
    ('start_cooldown_minutes', '开机冷却(分钟)', 'int_zero'),
    ('stop_cooldown_minutes', '关机冷却(分钟)', 'int_zero'),
    ('fallback_to_status_at', '使用状态时间兜底', 'bool'),
)


def _as_mapping(value: Any, *, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f'{name} 必须是对象，当前是 {type(value).__name__}')
    return value


def _tasks(payload: dict[str, Any]) -> dict[str, Any]:
    tasks = _as_mapping(payload.get('tasks'), name='tasks')
    payload['tasks'] = tasks
    return tasks


def scheduled_payload(payload: dict[str, Any]) -> dict[str, Any]:
    tasks = _tasks(payload)
    scheduled = _as_mapping(tasks.get('scheduled_start'), name='tasks.scheduled_start')
    scheduled.setdefault('enabled', True)
    scheduled.setdefault('poll_interval_seconds', 5)
    scheduled.setdefault('jobs', [])
    if not isinstance(scheduled.get('jobs'), list):
        raise ValueError(f"tasks.scheduled_start.jobs 必须是列表，当前是 {type(scheduled.get('jobs')).__name__}")
    tasks['scheduled_start'] = scheduled
    return scheduled


def keeper_payload(payload: dict[str, Any]) -> dict[str, Any]:
    tasks = _tasks(payload)
    keeper = _as_mapping(tasks.get('keeper'), name='tasks.keeper')
    keeper.setdefault('enabled', True)
    keeper.setdefault('interval_minutes', 60)
    keeper.setdefault('keeper_trigger_before_hours', 6)
    keeper.setdefault('shutdown_release_after_hours', 360)
    keeper.setdefault('power_on_wait_seconds', 60)
    keeper.setdefault('power_off_wait_seconds', 5)
    keeper.setdefault('start_cooldown_minutes', 60)
    keeper.setdefault('stop_cooldown_minutes', 360)
    keeper.setdefault('fallback_to_status_at', True)
    tasks['keeper'] = keeper
    return keeper


def validate_keeper_payload(keeper: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    shutdown_release_after_hours = int(keeper.get('shutdown_release_after_hours', 0) or 0)
    keeper_trigger_before_hours = int(keeper.get('keeper_trigger_before_hours', 0) or 0)
    if shutdown_release_after_hours <= 0:
        errors.append('tasks.keeper.shutdown_release_after_hours must be a positive integer.')
    if keeper_trigger_before_hours < 0:
        errors.append('tasks.keeper.keeper_trigger_before_hours must be zero or a positive integer.')
    if keeper_trigger_before_hours >= shutdown_release_after_hours:
        errors.append('tasks.keeper.keeper_trigger_before_hours must be smaller than shutdown_release_after_hours.')
    return errors


def scheduled_jobs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = scheduled_payload(payload)['jobs']
    return jobs


def parse_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in _BOOL_TRUE:
        return True
    if value in _BOOL_FALSE:
        return False
    raise ValueError('请输入 y/n、true/false 或 1/0')


def parse_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(',') if item.strip()]


def _parse_int(raw: str, *, minimum: int, field: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f'{field} 必须是整数') from exc
    if value < minimum:
        raise ValueError(f'{field} 不能小于 {minimum}')
    return value


def validate_job_payload(job: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    label = str(job.get('name') or job.get('instance_id') or '未命名任务')
    has_instance = bool(str(job.get('instance_id') or '').strip())
    has_selector = bool(job.get('selector'))
    if has_instance == has_selector:
        errors.append(f'{label}: 必须且只能配置固定实例或条件筛选之一')
    target_time = str(job.get('target_time') or '')
    if not _TIME_RE.match(target_time):
        errors.append(f'{label}: target_time 必须是 HH:MM')
    schedule_mode = str(job.get('schedule_mode') or 'daily')
    if schedule_mode not in {'once', 'daily', 'weekly'}:
        errors.append(f'{label}: schedule_mode 必须是 once/daily/weekly')
    if schedule_mode == 'weekly':
        weekdays = job.get('weekdays') or []
        if not isinstance(weekdays, list) or not weekdays:
            errors.append(f'{label}: weekly 任务必须设置 weekdays')
        else:
            for day in weekdays:
                try:
                    day_int = int(day)
                except (TypeError, ValueError):
                    errors.append(f'{label}: weekdays 必须是 1-7 的数字')
                    break
                if day_int < 1 or day_int > 7:
                    errors.append(f'{label}: weekdays 必须是 1-7 的数字')
                    break
    try:
        advance = int(job.get('advance_hours', 0))
        if advance <= 0:
            errors.append(f'{label}: advance_hours 必须大于 0')
    except (TypeError, ValueError):
        errors.append(f'{label}: advance_hours 必须是整数')
    timezone = str(job.get('timezone') or 'Asia/Shanghai')
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        errors.append(f'{label}: timezone 无效: {timezone}')
    selector = job.get('selector')
    if selector:
        if not isinstance(selector, dict):
            errors.append(f'{label}: selector 必须是对象')
        else:
            if not str(selector.get('gpu_model') or '').strip():
                errors.append(f'{label}: selector.gpu_model 必填')
            try:
                gpu_count = int(selector.get('gpu_count', 0))
                if gpu_count <= 0:
                    errors.append(f'{label}: selector.gpu_count 必须大于 0')
            except (TypeError, ValueError):
                errors.append(f'{label}: selector.gpu_count 必须是整数')
    return errors


def list_job_summaries(payload: dict[str, Any]) -> list[str]:
    jobs = scheduled_jobs(payload)
    if not jobs:
        return ['暂无抢机任务']
    lines: list[str] = []
    for index, job in enumerate(jobs, start=1):
        enabled = color('启用', GREEN) if job.get('enabled', True) else color('停用', RED)
        name = job.get('name') or job.get('instance_id') or f'job-{index}'
        schedule = color(_format_schedule(job), BLUE if job.get('schedule_mode') == 'weekly' else GREEN)
        target = job.get('target_time', '-')
        advance = job.get('advance_hours', '-')
        if job.get('instance_id'):
            detail = f"固定实例 {color(job.get('instance_id'), YELLOW)}"
        else:
            selector = job.get('selector') or {}
            detail = f"筛选 {color(selector.get('gpu_model', '-'), YELLOW)} x{color(selector.get('gpu_count', '-'), YELLOW)}"
        lines.append(f'{index:>2}. [{enabled}] {name} | {schedule} | {target} | 提前 {advance}h | {detail}')
    return lines


def add_scheduled_job(payload: dict[str, Any], job: dict[str, Any]) -> None:
    job = copy.deepcopy(job)
    job.setdefault('enabled', True)
    errors = validate_job_payload(job)
    if errors:
        raise ValueError('; '.join(errors))
    jobs = scheduled_jobs(payload)
    name = str(job.get('name') or '').strip()
    if name and any(str(item.get('name') or '').strip() == name for item in jobs):
        raise ValueError(f'任务名已存在: {name}')
    jobs.append(job)


def update_scheduled_job(payload: dict[str, Any], index: int, patch: dict[str, Any]) -> None:
    jobs = scheduled_jobs(payload)
    if index < 0 or index >= len(jobs):
        raise IndexError('任务编号不存在')
    job = copy.deepcopy(jobs[index])
    job.update(copy.deepcopy(patch))
    if patch.get('instance_id'):
        job.pop('selector', None)
        job.pop('priority', None)
    if patch.get('selector'):
        job.pop('instance_id', None)
    errors = validate_job_payload(job)
    if errors:
        raise ValueError('; '.join(errors))
    name = str(job.get('name') or '').strip()
    if name and any(i != index and str(item.get('name') or '').strip() == name for i, item in enumerate(jobs)):
        raise ValueError(f'任务名已存在: {name}')
    jobs[index] = job


def delete_scheduled_job(payload: dict[str, Any], index: int) -> dict[str, Any]:
    jobs = scheduled_jobs(payload)
    if index < 0 or index >= len(jobs):
        raise IndexError('任务编号不存在')
    return jobs.pop(index)


def toggle_scheduled_job(payload: dict[str, Any], index: int) -> bool:
    jobs = scheduled_jobs(payload)
    if index < 0 or index >= len(jobs):
        raise IndexError('任务编号不存在')
    job = jobs[index]
    job['enabled'] = not bool(job.get('enabled', True))
    return bool(job['enabled'])


def update_keeper(payload: dict[str, Any], patch: dict[str, Any]) -> None:
    keeper = keeper_payload(payload)
    for key, _, kind in (*_KEEPER_CORE_FIELDS, *_KEEPER_ADVANCED_FIELDS):
        if key not in patch:
            continue
        value = patch[key]
        if kind == 'bool':
            if not isinstance(value, bool):
                raise ValueError(f'{key} 必须是布尔值')
        elif kind == 'int_positive':
            value = int(value)
            if value <= 0:
                raise ValueError(f'{key} 必须大于 0')
        else:
            value = int(value)
            if value < 0:
                raise ValueError(f'{key} 不能小于 0')
        keeper[key] = value


def validate_payload(config_path: str | Path, payload: dict[str, Any]) -> list[str]:
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.yaml', dir=path.parent, delete=False, encoding='utf-8') as tmp:
            tmp_path = Path(tmp.name)
        write_raw_settings(tmp_path, payload)
        settings = load_settings(tmp_path)
        errors = validate_settings(settings, purpose='validate')
        errors.extend(validate_keeper_payload(keeper_payload(payload)))
        return errors
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def save_payload(config_path: str | Path, payload: dict[str, Any], *, request_reload_fn=request_config_reload) -> list[str]:
    errors = validate_payload(config_path, payload)
    if errors:
        return errors
    path = Path(config_path)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + '.bak'))
    write_raw_settings(path, payload)
    try:
        settings = load_settings(path)
        store = SQLiteStore(settings.storage.database_file)
        store.init_schema()
        request_reload_fn(store)
    except Exception as exc:
        return [f'配置已保存，但重载请求失败: {exc}']
    return []


def _prompt(input_fn: InputFn, text: str, default: str | None = None) -> str:
    suffix = f' [{default}]' if default is not None else ''
    raw = input_fn(f'{text}{suffix}: ').strip()
    return raw if raw else (default or '')


def _prompt_bool(input_fn: InputFn, text: str, default: bool) -> bool:
    default_text = 'y' if default else 'n'
    while True:
        try:
            return parse_bool(_prompt(input_fn, text, default_text))
        except ValueError as exc:
            print(str(exc))


def _prompt_int(input_fn: InputFn, text: str, default: int, *, minimum: int) -> int:
    while True:
        raw = _prompt(input_fn, text, str(default))
        try:
            return _parse_int(raw, minimum=minimum, field=text)
        except ValueError as exc:
            print(str(exc))


def _prompt_time(input_fn: InputFn, text: str, default: str) -> str:
    while True:
        value = _prompt(input_fn, text, default)
        normalized = _normalize_time(value)
        if normalized:
            return normalized
        print('时间格式必须是 HH:MM，也可输入 7、730、0730、1100')


def _normalize_time(value: str) -> str | None:
    raw = value.strip()
    if _TIME_RE.match(raw):
        return raw
    if not _TIME_COMPACT_RE.match(raw):
        return None
    if len(raw) <= 2:
        hour = int(raw)
        minute = 0
    else:
        padded = raw.zfill(4)
        hour = int(padded[:-2])
        minute = int(padded[-2:])
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f'{hour:02d}:{minute:02d}'
    return None


def _format_schedule(job: dict[str, Any]) -> str:
    mode = str(job.get('schedule_mode') or 'daily')
    label = _SCHEDULE_LABELS.get(mode, mode)
    if mode == 'weekly':
        weekdays = [int(day) for day in (job.get('weekdays') or [])]
        days = ','.join(_WEEKDAY_LABELS.get(day, str(day)) for day in sorted(set(weekdays)))
        return f'{label} {days or "-"}'
    return label


def _prompt_schedule(input_fn: InputFn, job: dict[str, Any]) -> dict[str, Any]:
    current = str(job.get('schedule_mode') or 'daily')
    default = {'once': '1', 'daily': '2', 'weekly': '3'}.get(current, '2')
    while True:
        choice = _prompt(input_fn, '频率 1=单次 2=每天 3=每周', default).lower()
        if choice in {'1', 'once', '单次'}:
            return {'schedule_mode': 'once', 'weekdays': []}
        if choice in {'2', 'daily', '每天'}:
            return {'schedule_mode': 'daily', 'weekdays': []}
        if choice in {'3', 'weekly', '每周'}:
            weekdays = _prompt_weekdays(input_fn, job.get('weekdays') or [])
            return {'schedule_mode': 'weekly', 'weekdays': weekdays}
        print('频率只能输入 1/2/3')


def _prompt_weekdays(input_fn: InputFn, default: list[int]) -> list[int]:
    default_text = ','.join(str(day) for day in default) if default else '1'
    while True:
        raw = _prompt(input_fn, '每周几，1=周一 ... 7=周日，逗号分隔', default_text)
        days: list[int] = []
        ok = True
        for item in parse_csv(raw):
            try:
                day = int(item)
            except ValueError:
                ok = False
                break
            if day < 1 or day > 7:
                ok = False
                break
            days.append(day)
        if ok and days:
            return sorted(set(days))
        print('请输入 1-7 的数字，例如 1 或 1,3,5')


def _prompt_job(input_fn: InputFn) -> dict[str, Any]:
    while True:
        mode = _prompt(input_fn, '任务类型 1=固定实例 2=条件筛选', '1').lower()
        if mode in {'1', '2'}:
            break
        if mode in {'q', 'quit', 'cancel', '0'}:
            raise ValueError('已取消新增任务')
        print('任务类型只能输入 1 或 2')
    name = _prompt(input_fn, '任务名')
    target_time = _prompt_time(input_fn, '目标时间', '20:00')
    advance_hours = _prompt_int(input_fn, '提前小时', 1, minimum=1)
    job: dict[str, Any] = {
        'name': name,
        'enabled': True,
        'target_time': target_time,
        'advance_hours': advance_hours,
        'schedule_mode': 'daily',
        'weekdays': [],
        'timezone': 'Asia/Shanghai',
    }
    job.update(_prompt_schedule(input_fn, job))
    if mode == '2':
        selector = {
            'gpu_model': _prompt(input_fn, 'GPU 型号'),
            'gpu_count': _prompt_int(input_fn, 'GPU 数量', 1, minimum=1),
        }
        regions = parse_csv(_prompt(input_fn, '地区，逗号分隔，可空'))
        charge_types = parse_csv(_prompt(input_fn, '计费类型，逗号分隔，可空'))
        if regions:
            selector['regions'] = regions
        if charge_types:
            selector['charge_types'] = charge_types
        job['selector'] = selector
    else:
        job['instance_id'] = _prompt(input_fn, '实例 ID')
    return job


def _select_index(payload: dict[str, Any], input_fn: InputFn) -> int | None:
    jobs = scheduled_jobs(payload)
    if not jobs:
        print('暂无抢机任务')
        return None
    raw = _prompt(input_fn, '任务编号，0 返回').lower()
    if raw in {'0', 'q', 'quit', 'cancel'}:
        print('已取消')
        return None
    try:
        index = int(raw) - 1
    except ValueError:
        print('任务编号必须是数字')
        return None
    if index < 0 or index >= len(jobs):
        print('任务编号不存在')
        return None
    return index


def _job_detail_lines(job: dict[str, Any]) -> list[str]:
    lines = [
        color('核心:', BLUE),
        f"  任务名: {color(job.get('name') or '-', CYAN)}",
        f"  状态: {color('启用', GREEN) if job.get('enabled', True) else color('停用', RED)}",
        f"  频率: {color(_format_schedule(job), BLUE if job.get('schedule_mode') == 'weekly' else GREEN)}",
        f"  目标时间: {color(job.get('target_time') or '-', YELLOW)}",
        f"  提前小时: {color(str(job.get('advance_hours') or '-'), YELLOW)}",
    ]
    if job.get('selector'):
        selector = job.get('selector') or {}
        lines.extend(
            [
                color('来源: 条件筛选', BLUE),
                f"  GPU 型号: {color(selector.get('gpu_model') or '-', CYAN)}",
                f"  GPU 数量: {color(str(selector.get('gpu_count') or '-'), YELLOW)}",
                f"  地区: {', '.join(selector.get('regions') or []) or '-'}",
                f"  计费类型: {', '.join(selector.get('charge_types') or []) or '-'}",
            ]
        )
    else:
        lines.extend([color('来源: 固定实例', BLUE), f"  实例 ID: {color(job.get('instance_id') or '-', YELLOW)}"])
    return lines


def _edit_fixed_job_field(payload: dict[str, Any], index: int, choice: str, input_fn: InputFn) -> bool:
    job = scheduled_jobs(payload)[index]
    if choice == '1':
        update_scheduled_job(payload, index, {'name': _prompt(input_fn, '新的任务名', str(job.get('name') or ''))})
    elif choice == '2':
        update_scheduled_job(payload, index, _prompt_schedule(input_fn, job))
    elif choice == '3':
        update_scheduled_job(payload, index, {'target_time': _prompt_time(input_fn, '新的目标时间', str(job.get('target_time') or '20:00'))})
    elif choice == '4':
        update_scheduled_job(payload, index, {'advance_hours': _prompt_int(input_fn, '新的提前小时', int(job.get('advance_hours') or 1), minimum=1)})
    elif choice == '5':
        update_scheduled_job(payload, index, {'instance_id': _prompt(input_fn, '新的实例 ID', str(job.get('instance_id') or ''))})
    elif choice == '6':
        update_scheduled_job(payload, index, {'enabled': _prompt_bool(input_fn, '是否启用', bool(job.get('enabled', True)))})
    else:
        return False
    return True


def _edit_selector_job_field(payload: dict[str, Any], index: int, choice: str, input_fn: InputFn) -> bool:
    job = scheduled_jobs(payload)[index]
    selector = dict(job.get('selector') or {})
    if choice in {'1', '2', '3', '4'}:
        return _edit_fixed_job_field(payload, index, choice, input_fn)
    if choice == '5':
        selector['gpu_model'] = _prompt(input_fn, '新的 GPU 型号', str(selector.get('gpu_model') or ''))
        update_scheduled_job(payload, index, {'selector': selector})
    elif choice == '6':
        selector['gpu_count'] = _prompt_int(input_fn, '新的 GPU 数量', int(selector.get('gpu_count') or 1), minimum=1)
        update_scheduled_job(payload, index, {'selector': selector})
    elif choice == '7':
        selector['regions'] = parse_csv(_prompt(input_fn, '新的地区，逗号分隔，可空', ','.join(selector.get('regions') or [])))
        update_scheduled_job(payload, index, {'selector': selector})
    elif choice == '8':
        selector['charge_types'] = parse_csv(_prompt(input_fn, '新的计费类型，逗号分隔，可空', ','.join(selector.get('charge_types') or [])))
        update_scheduled_job(payload, index, {'selector': selector})
    elif choice == '9':
        update_scheduled_job(payload, index, {'enabled': _prompt_bool(input_fn, '是否启用', bool(job.get('enabled', True)))})
    else:
        return False
    return True


def _edit_job(payload: dict[str, Any], input_fn: InputFn, *, clear_screen_enabled: bool = False) -> bool:
    index = _select_index(payload, input_fn)
    if index is None:
        return False
    changed_any = False
    notice = ""
    while True:
        job = scheduled_jobs(payload)[index]
        clear_screen(enabled=clear_screen_enabled)
        print(f'\n{render_section("编辑抢机任务", color_enabled=True)}')
        if notice:
            print(render_notice(notice))
        notice = ""
        print()
        for line in _job_detail_lines(job):
            print(f'  {line}')
        print()
        if job.get('selector'):
            print(color('核心设置 / 来源设置', BLUE))
            print_numbered_menu([
                ('1', '修改任务名'),
                ('2', '修改频率'),
                ('3', '修改目标时间'),
                ('4', '修改提前小时'),
                ('5', '修改 GPU 型号'),
                ('6', '修改 GPU 数量'),
                ('7', '修改地区'),
                ('8', '修改计费类型'),
                ('9', '启用/停用任务'),
                ('0', '返回'),
            ])
            handler = _edit_selector_job_field
        else:
            print(color('核心设置 / 来源设置', BLUE))
            print_numbered_menu([
                ('1', '修改任务名'),
                ('2', '修改频率'),
                ('3', '修改目标时间'),
                ('4', '修改提前小时'),
                ('5', '修改实例 ID'),
                ('6', '启用/停用任务'),
                ('0', '返回'),
            ])
            handler = _edit_fixed_job_field
        choice = _prompt(input_fn, '选择编号').lower()
        if choice == '0':
            return changed_any
        try:
            before = copy.deepcopy(job)
            changed = handler(payload, index, choice, input_fn)
            changed_any = changed_any or (changed and scheduled_jobs(payload)[index] != before)
            notice = '已更新，按 0 返回' if changed else '无效选择'
        except ValueError as exc:
            notice = f'操作失败: {exc}'


def _print_scheduled_menu(payload: dict[str, Any], *, clear: bool = False, notice: str = "") -> None:
    scheduled = scheduled_payload(payload)
    status = '启用' if scheduled.get('enabled') else '停用'
    clear_screen(enabled=clear)
    status_text = color(status, GREEN if scheduled.get('enabled') else RED)
    poll_text = color(str(scheduled.get('poll_interval_seconds')) + 's', YELLOW)
    print(f'\n{render_section("抢机配置", color_enabled=True)}')
    if notice:
        print(render_notice(notice))
    print()
    print(f"状态: {status_text} | 轮询: {poll_text} | 任务数: {len(scheduled.get('jobs') or [])}")
    print(render_rule())
    for line in list_job_summaries(payload):
        print(line)
    print(render_rule())
    print()
    print_numbered_menu([
        ('1', '新增任务'),
        ('2', '编辑任务'),
        ('3', '删除任务'),
        ('4', '启停单个任务'),
        ('5', '启停整个抢机'),
        ('6', '修改轮询'),
        ('0', '返回'),
    ])


def _scheduled_menu(payload: dict[str, Any], input_fn: InputFn, *, clear_screen_enabled: bool = False) -> bool:
    original = copy.deepcopy(payload)
    changed_any = False
    notice = ""
    while True:
        scheduled = scheduled_payload(payload)
        _print_scheduled_menu(payload, clear=clear_screen_enabled, notice=notice)
        notice = ""
        choice = _prompt(input_fn, '选择').lower()
        try:
            if choice == '1':
                add_scheduled_job(payload, _prompt_job(input_fn))
                changed_any = True
                notice = '已新增任务'
            elif choice == '2':
                changed_any = _edit_job(payload, input_fn, clear_screen_enabled=clear_screen_enabled) or changed_any
            elif choice == '3':
                index = _select_index(payload, input_fn)
                if index is not None:
                    job = scheduled_jobs(payload)[index]
                    delete_label = job.get('name') or job.get('instance_id') or f'job-{index + 1}'
                    confirm = _prompt(input_fn, f'输入 {delete_label} 确认删除')
                    if confirm == delete_label:
                        delete_scheduled_job(payload, index)
                        changed_any = True
                        notice = '已删除任务'
                    else:
                        notice = '已取消删除'
            elif choice == '4':
                index = _select_index(payload, input_fn)
                if index is not None:
                    enabled = toggle_scheduled_job(payload, index)
                    changed_any = True
                    notice = f"任务已{'启用' if enabled else '停用'}"
            elif choice == '5':
                next_enabled = not bool(scheduled.get('enabled', True))
                action = '启用' if next_enabled else '停用'
                if parse_bool(_prompt(input_fn, f'确认{action}整个抢机功能? y/N', 'n')):
                    scheduled['enabled'] = next_enabled
                    changed_any = True
                    notice = f'整个抢机功能已{action}'
                else:
                    notice = '已取消'
            elif choice == '6':
                scheduled['poll_interval_seconds'] = _prompt_int(input_fn, '轮询间隔秒数', int(scheduled.get('poll_interval_seconds') or 5), minimum=5)
                changed_any = True
                notice = '轮询间隔已更新'
            elif choice == '0':
                if not changed_any:
                    payload.clear()
                    payload.update(original)
                return changed_any
            else:
                notice = '无效选择，请输入 1/2/3/4/5/6 或 0'
        except (IndexError, ValueError) as exc:
            notice = f'操作失败: {exc}'


def _edit_keeper_field(payload: dict[str, Any], choice: str, field_defs: tuple[tuple[str, str, str], ...], input_fn: InputFn) -> bool:
    keeper = keeper_payload(payload)
    index = int(choice) - 1
    if index < 0 or index >= len(field_defs):
        raise IndexError
    key, label, kind = field_defs[index]
    current = keeper.get(key)
    if kind == 'bool':
        value = _prompt_bool(input_fn, label, bool(current))
    else:
        minimum = 1 if kind == 'int_positive' else 0
        value = _prompt_int(input_fn, label, int(current), minimum=minimum)
    update_keeper(payload, {key: value})
    return True


def _print_keeper_fields(title: str, field_defs: tuple[tuple[str, str, str], ...], keeper: dict[str, Any]) -> None:
    print(f'\n{render_section(title, color_enabled=True)}')
    print()
    for index, (key, label, _) in enumerate(field_defs, start=1):
        print(f'  {color(str(index) + ".", BOLD + BLUE)} {label}: {keeper.get(key)}')
    print(f'  {color("0.", BOLD + BLUE)} 返回')


def _keeper_menu(payload: dict[str, Any], input_fn: InputFn, *, clear_screen_enabled: bool = False) -> bool:
    original = copy.deepcopy(payload)
    changed_any = False
    notice = ""
    while True:
        keeper = keeper_payload(payload)
        clear_screen(enabled=clear_screen_enabled)
        print(f'\n{render_section("Keeper 配置", color_enabled=True)}')
        if notice:
            print(render_notice(notice))
        notice = ""
        print()
        print_numbered_menu([
            ('1', '核心参数'),
            ('2', '高级参数'),
            ('0', '返回'),
        ])
        choice = _prompt(input_fn, '选择').lower()
        if choice == '0':
            if not changed_any:
                payload.clear()
                payload.update(original)
            return changed_any
        try:
            if choice == '1':
                while True:
                    keeper = keeper_payload(payload)
                    clear_screen(enabled=clear_screen_enabled)
                    _print_keeper_fields('Keeper 核心参数', _KEEPER_CORE_FIELDS, keeper)
                    print()
                    field_choice = _prompt(input_fn, '选择').lower()
                    if field_choice == '0':
                        break
                    _edit_keeper_field(payload, field_choice, _KEEPER_CORE_FIELDS, input_fn)
                    changed_any = True
                    notice = '草稿已更新，保存后生效'
                    break
            elif choice == '2':
                while True:
                    keeper = keeper_payload(payload)
                    clear_screen(enabled=clear_screen_enabled)
                    _print_keeper_fields('Keeper 高级参数', _KEEPER_ADVANCED_FIELDS, keeper)
                    print()
                    field_choice = _prompt(input_fn, '选择').lower()
                    if field_choice == '0':
                        break
                    _edit_keeper_field(payload, field_choice, _KEEPER_ADVANCED_FIELDS, input_fn)
                    changed_any = True
                    notice = '草稿已更新，保存后生效'
                    break
            else:
                notice = '无效选择，请输入 1/2/0'
        except (ValueError, IndexError) as exc:
            notice = f'操作失败: {exc}'


def run_config_wizard(config_path: str | Path, *, input_fn: InputFn = input, print_fn: PrintFn = print, clear_screen_enabled: bool = False) -> bool:
    path = Path(config_path)
    payload = read_raw_settings(path)
    if not isinstance(payload, dict):
        print_fn('配置文件顶层必须是 YAML 对象')
        return False
    working = copy.deepcopy(payload)
    dirty = False
    notice = ""
    while True:
        clear_screen(enabled=clear_screen_enabled)
        print_fn(color('\n== 配置管理 ==', BOLD + BLUE))
        if notice:
            print_fn(render_notice(notice))
        notice = ""
        print_fn('')
        print_fn(f'配置文件: {color(str(path), YELLOW)}')
        print_fn('')
        print_numbered_menu([
            ('1', '抢机任务'),
            ('2', 'Keeper 参数'),
            ('3', '校验配置'),
            ('0', '返回'),
        ])
        choice = input_fn('选择编号: ').strip().lower()
        try:
            if choice == '1':
                dirty = _scheduled_menu(working, input_fn, clear_screen_enabled=clear_screen_enabled) or dirty
            elif choice == '2':
                dirty = _keeper_menu(working, input_fn, clear_screen_enabled=clear_screen_enabled) or dirty
            elif choice == '3':
                errors = validate_payload(path, working)
                if errors:
                    notice = '配置校验失败: ' + '; '.join(errors)
                else:
                    notice = '配置校验通过'
            elif choice == '0':
                if dirty:
                    errors = save_payload(path, working)
                    if errors:
                        notice = '保存失败: ' + '; '.join(errors)
                        continue
                    print_fn(f'已保存: {path}')
                    backup_path = path.with_suffix(path.suffix + '.bak')
                    if backup_path.exists():
                        print_fn(f'备份: {backup_path}')
                    print_fn('草稿已更新，保存后生效')
                    return True
                return False
            else:
                notice = '无效选择，请输入 1/2/3/0'
        except (ValueError, IndexError) as exc:
            notice = f'操作失败: {exc}'
