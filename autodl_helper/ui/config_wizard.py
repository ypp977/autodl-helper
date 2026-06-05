from __future__ import annotations

import copy
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from autodl_helper.cli.shared_settings import validate_settings
from autodl_helper.core.config import load_settings, read_raw_settings, write_raw_settings
from autodl_helper.runtime_control import request_config_reload
from autodl_helper.core.store import SQLiteStore

from .scheduled_config import (
    add_scheduled_job,
    delete_scheduled_job,
    list_job_summaries,
    parse_bool,
    scheduled_jobs,
    scheduled_menu,
    scheduled_payload,
    toggle_scheduled_job,
    update_scheduled_job,
)
from .render import BLUE, BOLD, GREEN, YELLOW, clear_screen, color, print_numbered_menu, render_notice, render_section

InputFn = Callable[[str], str]
PrintFn = Callable[[str], None]

__all__ = [
    'add_scheduled_job',
    'delete_scheduled_job',
    'keeper_payload',
    'list_job_summaries',
    'run_config_wizard',
    'save_payload',
    'scheduled_jobs',
    'scheduled_payload',
    'toggle_scheduled_job',
    'update_keeper',
    'update_scheduled_job',
]

_KEEPER_CORE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ('keeper_trigger_before_hours', '释放前多久开始保活(小时)', 'int_zero'),
    ('shutdown_release_after_hours', '关机后最长保留时间(小时)', 'int_positive'),
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


def _payload_int(keeper: dict[str, Any], key: str, *, label: str, errors: list[str]) -> int | None:
    try:
        return int(keeper.get(key, 0) or 0)
    except (TypeError, ValueError):
        errors.append(f'tasks.keeper.{key} must be an integer. ({label})')
        return None


def validate_keeper_payload(keeper: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    shutdown_release_after_hours = _payload_int(
        keeper,
        'shutdown_release_after_hours',
        label='关机后最长保留时间(小时)',
        errors=errors,
    )
    keeper_trigger_before_hours = _payload_int(
        keeper,
        'keeper_trigger_before_hours',
        label='释放前多久开始保活(小时)',
        errors=errors,
    )
    interval_minutes = _payload_int(keeper, 'interval_minutes', label='检查间隔(分钟)', errors=errors)
    power_on_wait_seconds = _payload_int(keeper, 'power_on_wait_seconds', label='开机等待(秒)', errors=errors)
    power_off_wait_seconds = _payload_int(keeper, 'power_off_wait_seconds', label='关机等待(秒)', errors=errors)
    start_cooldown_minutes = _payload_int(keeper, 'start_cooldown_minutes', label='开机冷却(分钟)', errors=errors)
    stop_cooldown_minutes = _payload_int(keeper, 'stop_cooldown_minutes', label='关机冷却(分钟)', errors=errors)
    if shutdown_release_after_hours is not None and shutdown_release_after_hours <= 0:
        errors.append('tasks.keeper.shutdown_release_after_hours must be a positive integer.')
    if keeper_trigger_before_hours is not None and keeper_trigger_before_hours < 0:
        errors.append('tasks.keeper.keeper_trigger_before_hours must be zero or a positive integer.')
    if (
        keeper_trigger_before_hours is not None
        and shutdown_release_after_hours is not None
        and keeper_trigger_before_hours >= shutdown_release_after_hours
    ):
        errors.append('tasks.keeper.keeper_trigger_before_hours must be smaller than shutdown_release_after_hours.')
    if interval_minutes is not None and interval_minutes <= 0:
        errors.append('tasks.keeper.interval_minutes must be a positive integer.')
    for key, value in (
        ('power_on_wait_seconds', power_on_wait_seconds),
        ('power_off_wait_seconds', power_off_wait_seconds),
        ('start_cooldown_minutes', start_cooldown_minutes),
        ('stop_cooldown_minutes', stop_cooldown_minutes),
    ):
        if value is not None and value < 0:
            errors.append(f'tasks.keeper.{key} must be zero or a positive integer.')
    return errors


def _parse_int(raw: str, *, minimum: int, field: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f'{field} 必须是整数') from exc
    if value < minimum:
        raise ValueError(f'{field} 不能小于 {minimum}')
    return value


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
            ('1', '抢机配置'),
            ('2', 'Keeper 配置'),
            ('3', '校验配置'),
            ('0', '返回'),
        ])
        choice = input_fn('选择编号: ').strip().lower()
        try:
            if choice == '1':
                dirty = scheduled_menu(working, input_fn, clear_screen_enabled=clear_screen_enabled) or dirty
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
                    print_fn('配置已保存并已请求重载')
                    return True
                return False
            else:
                notice = '无效选择，请输入 1/2/3/0'
        except (ValueError, IndexError) as exc:
            notice = f'操作失败: {exc}'
