from __future__ import annotations

import inspect
import os
import re
import select
import sys
import termios
import time
import tty
from dataclasses import dataclass
from typing import Any, Callable

from autodl_helper.config import KeeperSettings, ScheduledStartJob, ScheduledStartSelector
from .presentation import (
    BLUE,
    CYAN,
    _boxed_lines,
    _format_hours_brief,
    _format_minutes_brief,
    _heading,
    _key_value,
    _section,
    _separator,
)

SCHEDULED_TIME_RE = re.compile(r'^(?:[01]\d|2[0-3]):[0-5]\d$')
_DELEGATE_CACHE: dict[str, Any] = {}


def _delegate(name: str):
    cached = _DELEGATE_CACHE.get(name)
    if cached is not None:
        return cached
    from .support import delegates as _delegates

    local = globals()[name]
    proxy = _delegates._delegate(name, local)
    _DELEGATE_CACHE[name] = proxy
    return proxy


class _InteractiveCancel(Exception):
    pass


def _prompt(text: str) -> str:
    _delegate('_show_cursor')()
    try:
        if sys.stdin.isatty():
            try:
                termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
            except Exception:
                pass
        return input(text).strip()
    finally:
        _delegate('_hide_cursor')()


def _clear_screen() -> None:
    print('\033[2J\033[H', end='')


def _repaint_screen() -> None:
    print('\033[H\033[J', end='')


def _hide_cursor() -> None:
    print('\033[?25l', end='', flush=True)


def _show_cursor() -> None:
    print('\033[?25h', end='', flush=True)


@dataclass(frozen=True)
class MenuItem:
    key: str
    label: str


def _supports_arrow_menu() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _read_fd_char(fd: int) -> str:
    data = os.read(fd, 1)
    if not data:
        return ''
    return data.decode(errors='ignore')


def _decode_arrow_escape_sequence(sequence: str) -> str:
    text = str(sequence or '')
    if not text:
        return 'ESC'
    if text[0] not in {'[', 'O'}:
        return 'ESC'
    final = text[-1]
    if final == 'A':
        return 'UP'
    if final == 'B':
        return 'DOWN'
    return 'ESC'


def _read_escape_sequence_blocking(read_char: Callable[[], str], *, max_chars: int = 8) -> str:
    sequence = ''
    while len(sequence) < max_chars:
        char = read_char()
        if not char:
            break
        sequence += char
        if len(sequence) > 1 and (char.isalpha() or char == '~'):
            break
    return _decode_arrow_escape_sequence(sequence)


def _read_escape_sequence_with_deadline(
    read_char: Callable[[], str],
    wait_for_char: Callable[[float], bool],
    *,
    deadline: float,
    max_chars: int = 8,
) -> str:
    sequence = ''
    while len(sequence) < max_chars:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not wait_for_char(min(0.03, max(0.0, remaining))):
            continue
        char = read_char()
        if not char:
            break
        sequence += char
        if len(sequence) > 1 and (char.isalpha() or char == '~'):
            break
    return _decode_arrow_escape_sequence(sequence)


def _read_menu_key() -> str:
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        raw = _delegate('_read_fd_char')(fd)
        if raw in {'\r', '\n'}:
            return 'ENTER'
        if raw == '\x1b':
            return _delegate('_read_escape_sequence_blocking')(lambda: _delegate('_read_fd_char')(fd))
        return raw
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _read_key_with_timeout(timeout_seconds: float | None) -> str | None:
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        if timeout_seconds is None:
            ready, _, _ = select.select([sys.stdin], [], [])
        else:
            ready, _, _ = select.select([sys.stdin], [], [], max(0.0, timeout_seconds))
        if not ready:
            return None
        raw = _delegate('_read_fd_char')(fd)
        if raw in {'\r', '\n'}:
            return 'ENTER'
        if raw == '\x1b':
            if timeout_seconds is None:
                return _delegate('_read_escape_sequence_blocking')(lambda: _delegate('_read_fd_char')(fd))
            deadline = time.monotonic() + 0.20
            return _delegate('_read_escape_sequence_with_deadline')(
                lambda: _delegate('_read_fd_char')(fd),
                lambda wait: bool(select.select([sys.stdin], [], [], wait)[0]),
                deadline=deadline,
            )
        return raw
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _render_menu(title: str, items: list[MenuItem], selected_index: int) -> None:
    _repaint_screen()
    print(title)
    print('')
    print(_section('↑/↓ 选择，Enter 确认'))
    print(_separator())
    print('')
    _render_menu_items(items, selected_index)


def _render_menu_line(item: MenuItem, selected: bool) -> str:
    prefix = _heading('❯', color=BLUE) if selected else ' '
    label = _heading(item.label, color=CYAN if selected else BLUE) if selected else item.label
    return f'{prefix} {label}'


def _render_menu_items(items: list[MenuItem], selected_index: int) -> None:
    for index, item in enumerate(items):
        print(_render_menu_line(item, index == selected_index))


def _update_menu_selection(items: list[MenuItem], previous_index: int, selected_index: int) -> None:
    if not items or previous_index == selected_index:
        return
    total = len(items)
    for index in {previous_index, selected_index}:
        if not (0 <= index < total):
            continue
        print('\033[s', end='')
        print(f'\033[{total - index}F', end='')
        print('\033[2K\r', end='')
        print(_render_menu_line(items[index], index == selected_index), end='')
        print('\033[u', end='', flush=True)


def _update_menu_title(previous_title: str, new_title: str, item_count: int) -> bool:
    previous_lines = str(previous_title).splitlines()
    new_lines = str(new_title).splitlines()
    if len(previous_lines) != len(new_lines):
        return False
    if previous_lines == new_lines:
        return True
    lines_from_cursor = len(previous_lines) + 4 + max(0, item_count)
    print('\033[s', end='')
    print(f'\033[{lines_from_cursor}F', end='')
    for index, line in enumerate(new_lines):
        if previous_lines[index] != line:
            print('\033[2K\r', end='')
            print(line, end='')
        if index != len(new_lines) - 1:
            print('\n', end='')
    print('\033[u', end='', flush=True)
    return True


def _choose_menu(
    title: str,
    items: list[MenuItem],
    *,
    default_key: str | None = None,
    refresh_fn: Callable[[str | None], tuple[str, list[MenuItem], str | None] | None] | None = None,
    refresh_revision_fn: Callable[[], Any] | None = None,
    refresh_interval_seconds: float = 1.0,
    on_rendered_fn: Callable[[], None] | None = None,
    refresh_policy: str = 'on_change',
    pre_refresh_fn: Callable[[], None] | None = None,
) -> str:
    if not items:
        raise ValueError('menu items must not be empty')
    if _delegate('_supports_arrow_menu')():
        current_title = title
        current_items = list(items)
        selected_index = 0
        if default_key is not None:
            for index, item in enumerate(current_items):
                if item.key == default_key:
                    selected_index = index
                    break
        _delegate('_render_menu')(current_title, current_items, selected_index)
        if on_rendered_fn is not None:
            on_rendered_fn()
        last_refresh_revision = refresh_revision_fn() if refresh_fn is not None and refresh_revision_fn is not None else None
        next_refresh_at = time.monotonic() + max(0.1, refresh_interval_seconds)
        while True:
            timeout_seconds: float | None
            if refresh_fn is None:
                timeout_seconds = None
            else:
                remaining = max(0.0, next_refresh_at - time.monotonic())
                timeout_seconds = min(0.05, remaining)
            key = _delegate('_read_key_with_timeout')(timeout_seconds)
            if key is None and refresh_fn is not None:
                if time.monotonic() < next_refresh_at:
                    continue
                next_refresh_at = time.monotonic() + max(0.1, refresh_interval_seconds)
                if pre_refresh_fn is not None:
                    pre_refresh_fn()
                should_refresh = True
                if refresh_policy != 'always' and refresh_revision_fn is not None:
                    current_revision = refresh_revision_fn()
                    if current_revision == last_refresh_revision:
                        should_refresh = False
                if not should_refresh:
                    continue
                current_selected_key = current_items[selected_index].key if current_items else None
                refreshed = refresh_fn(current_selected_key)
                if refreshed is not None:
                    if refresh_revision_fn is not None:
                        last_refresh_revision = refresh_revision_fn()
                    next_title, next_items, preferred_key = refreshed
                    next_items = list(next_items)
                    if not next_items:
                        raise ValueError('menu items must not be empty')
                    keep_key = preferred_key or current_selected_key
                    if keep_key is not None:
                        matched_index = next((index for index, item in enumerate(next_items) if item.key == keep_key), None)
                        next_selected_index = matched_index if matched_index is not None else 0
                    else:
                        next_selected_index = 0
                    if current_title == next_title and current_items == next_items and selected_index == next_selected_index:
                        continue
                    if current_items == next_items and selected_index == next_selected_index:
                        if _delegate('_update_menu_title')(current_title, next_title, len(current_items)):
                            current_title = next_title
                            current_items = next_items
                            continue
                    current_title = next_title
                    current_items = next_items
                    selected_index = next_selected_index
                    _delegate('_render_menu')(current_title, current_items, selected_index)
                continue
            if key == 'UP':
                previous_index = selected_index
                selected_index = (selected_index - 1) % len(current_items)
                _delegate('_update_menu_selection')(current_items, previous_index, selected_index)
                if refresh_fn is not None:
                    next_refresh_at = time.monotonic() + max(0.1, refresh_interval_seconds)
            elif key == 'DOWN':
                previous_index = selected_index
                selected_index = (selected_index + 1) % len(current_items)
                _delegate('_update_menu_selection')(current_items, previous_index, selected_index)
                if refresh_fn is not None:
                    next_refresh_at = time.monotonic() + max(0.1, refresh_interval_seconds)
            elif key == 'ENTER':
                return current_items[selected_index].key
            elif key in {'q', 'Q'} and any(item.key == '0' for item in current_items):
                return '0'
            elif any(item.key == key for item in current_items):
                return key
            elif key is None:
                continue
    if refresh_fn is not None:
        refreshed = refresh_fn(default_key)
        if refreshed is not None:
            title, items, refreshed_default = refreshed
            if refreshed_default is not None:
                default_key = refreshed_default
    _delegate('_repaint_screen')()
    print(title)
    print('')
    print(_separator())
    for item in items:
        print(f'{item.key}. {item.label}')
    if on_rendered_fn is not None:
        on_rendered_fn()
    return _delegate('_prompt')('选择: ') or (default_key or '')


def _menu_default_key(items: list[MenuItem], desired: str | None = None) -> str:
    if not items:
        return desired or '0'
    if desired is not None and any(item.key == desired for item in items):
        return desired
    return items[0].key


def _choose_menu_with_refresh(
    title: str,
    items: list[MenuItem],
    *,
    default_key: str | None = None,
    refresh_fn: Callable[[str | None], tuple[str, list[MenuItem], str | None] | None] | None = None,
    refresh_revision_fn: Callable[[], Any] | None = None,
    refresh_interval_seconds: float = 1.0,
    on_rendered_fn: Callable[[], None] | None = None,
    refresh_policy: str = 'on_change',
    pre_refresh_fn: Callable[[], None] | None = None,
) -> str:
    kwargs = {
        'default_key': default_key,
        'refresh_fn': refresh_fn,
        'refresh_revision_fn': refresh_revision_fn,
        'refresh_interval_seconds': refresh_interval_seconds,
        'on_rendered_fn': on_rendered_fn,
        'refresh_policy': refresh_policy,
        'pre_refresh_fn': pre_refresh_fn,
    }
    parameters = inspect.signature(_delegate('_choose_menu')).parameters
    accepts_var_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values())
    forwarded_kwargs = (
        kwargs
        if accepts_var_kwargs
        else {key: value for key, value in kwargs.items() if key in parameters}
    )
    return _delegate('_choose_menu')(title, items, **forwarded_kwargs)


def _prompt_with_default(prompt: str, default: str | None = None) -> str:
    suffix = f' [{default}]' if default not in {None, ''} else ''
    raw = _delegate('_prompt')(f'{prompt}{suffix}: ')
    if raw in {':q', '/q'}:
        raise _InteractiveCancel('已取消编辑。')
    if raw == '':
        return default or ''
    return raw


def _prompt_int_with_default(prompt: str, default: int | None = None) -> int:
    raw = _delegate('_prompt_with_default')(prompt, str(default) if default is not None else None)
    return int(raw)


def _scheduled_hour_key(value: str) -> str:
    try:
        hour = int(str(value).split(':', 1)[0])
    except (TypeError, ValueError):
        hour = 14
    hour = min(23, max(0, hour))
    return str(hour + 1)


def _scheduled_minute_key(value: str) -> str:
    try:
        minute = int(str(value).split(':', 1)[1])
    except (IndexError, TypeError, ValueError):
        minute = 0
    minute = min(55, max(0, minute - (minute % 5)))
    return str((minute // 5) + 1)


def _scheduled_direct_time_key(value: str) -> str:
    return _scheduled_hour_key(value)


def _scheduled_advance_key(value: int) -> str:
    mapping = {1: '1', 2: '2', 3: '3', 6: '4', 12: '5', 24: '6'}
    return mapping.get(int(value or 0), '7')


def _prompt_custom_positive_int(prompt: str, default: int) -> int:
    while True:
        try:
            value = _delegate('_prompt_int_with_default')(prompt, default)
        except ValueError:
            print('请输入正整数。')
            continue
        if value > 0:
            return value
        print('请输入大于 0 的整数。')


def _prompt_scheduled_time_settings(*, target_time: str, advance_hours: int, timezone: str) -> tuple[str, int, str]:
    current_target_time = target_time or '14:00'
    current_advance_hours = max(1, int(advance_hours or 1))
    current_timezone = timezone or 'Asia/Shanghai'
    while True:
        lines = [
            _heading('时间设置'),
            _separator(),
            _section('[当前设置]'),
            _key_value('目标时间', current_target_time),
            _key_value('提前启动', f'{current_advance_hours} 小时'),
            _key_value('时区', current_timezone),
        ]
        action = _delegate('_choose_menu')(
            '\n'.join(lines),
            [
                MenuItem('1', '修改目标时间'),
                MenuItem('2', '修改提前启动'),
                MenuItem('0', '返回'),
            ],
            default_key='0',
        )
        if action in {':q', '/q'}:
            raise _InteractiveCancel('已取消编辑。')
        if action == '1':
            mode_choice = _delegate('_choose_menu')(
                _heading('选择目标时间模式'),
                [
                    MenuItem('1', '整点'),
                    MenuItem('2', '半点'),
                    MenuItem('3', '15 分钟刻度'),
                    MenuItem('4', '5 分钟精细选择'),
                    MenuItem('0', '返回'),
                ],
                default_key='3',
            )
            if mode_choice == '0':
                continue
            if mode_choice in {'1', '2'}:
                direct_time_choice = _delegate('_choose_menu')(
                    _heading('选择目标时间'),
                    [
                        MenuItem(
                            str(index),
                            f'{index - 1:02d}:{"00" if mode_choice == "1" else "30"}',
                        )
                        for index in range(1, 25)
                    ] + [MenuItem('0', '返回')],
                    default_key=_scheduled_direct_time_key(current_target_time),
                )
                if direct_time_choice == '0':
                    continue
                minute_value = 0 if mode_choice == '1' else 30
                current_target_time = f'{int(direct_time_choice) - 1:02d}:{minute_value:02d}'
                continue
            hour_choice = _delegate('_choose_menu')(
                _heading('选择小时'),
                [MenuItem(str(index), f'{index - 1:02d} 点') for index in range(1, 25)] + [MenuItem('0', '返回')],
                default_key=_scheduled_hour_key(current_target_time),
            )
            if hour_choice == '0':
                continue
            minute_items: list[MenuItem]
            default_minute_key: str
            if mode_choice == '3':
                minute_items = [
                    MenuItem('1', '00 分'),
                    MenuItem('2', '15 分'),
                    MenuItem('3', '30 分'),
                    MenuItem('4', '45 分'),
                ]
                default_minute_key = {'1': '1', '4': '2', '7': '3', '10': '4'}.get(_scheduled_minute_key(current_target_time), '1')
            else:
                minute_items = [MenuItem(str(index), f'{(index - 1) * 5:02d} 分') for index in range(1, 13)]
                default_minute_key = _scheduled_minute_key(current_target_time)
            minute_choice = _delegate('_choose_menu')(
                _heading('选择分钟'),
                minute_items + [MenuItem('0', '返回')],
                default_key=default_minute_key,
            )
            if minute_choice == '0':
                continue
            minute_value = {
                '1': 0,
                '2': 15,
                '3': 30,
                '4': 45,
            }[minute_choice] if mode_choice == '3' else (30 if mode_choice == '2' else ((int(minute_choice) - 1) * 5 if mode_choice == '4' else 0))
            current_target_time = f'{int(hour_choice) - 1:02d}:{minute_value:02d}'
            continue
        if action == '2':
            advance_choice = _delegate('_choose_menu')(
                _heading('选择提前启动时间'),
                [
                    MenuItem('1', '1 小时'),
                    MenuItem('2', '2 小时'),
                    MenuItem('3', '3 小时'),
                    MenuItem('4', '6 小时'),
                    MenuItem('5', '12 小时'),
                    MenuItem('6', '24 小时'),
                    MenuItem('7', '自定义整数小时'),
                    MenuItem('0', '返回'),
                ],
                default_key=_scheduled_advance_key(current_advance_hours),
            )
            if advance_choice == '0':
                continue
            if advance_choice == '7':
                current_advance_hours = _delegate('_prompt_custom_positive_int')('提前启动 (小时)', current_advance_hours)
            else:
                current_advance_hours = {'1': 1, '2': 2, '3': 3, '4': 6, '5': 12, '6': 24}[advance_choice]
            continue
        if action == '0':
            return current_target_time, current_advance_hours, current_timezone
        if SCHEDULED_TIME_RE.match(str(action or '')):
            current_target_time = str(action)
            continue
        if str(action).isdigit() and int(str(action)) > 0:
            current_advance_hours = int(str(action))
            continue


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(',') if item.strip()]


def _prompt_scheduled_job(existing_job: ScheduledStartJob | None = None) -> ScheduledStartJob:
    existing_selector = existing_job.selector if existing_job else None
    draft = {
        'name': existing_job.name if existing_job else '',
        'source_kind': 'selector' if existing_selector is not None else 'instance',
        'instance_id': existing_job.instance_id if existing_job else '',
        'regions': list(existing_selector.regions) if existing_selector else [],
        'gpu_model': existing_selector.gpu_model if existing_selector else '',
        'gpu_count': existing_selector.gpu_count if existing_selector else 1,
        'charge_types': list(existing_selector.charge_types) if existing_selector else [],
        'priority': list(existing_job.priority) if existing_job else [],
        'target_time': existing_job.target_time if existing_job else '14:00',
        'advance_hours': existing_job.advance_hours if existing_job else 2,
        'schedule_mode': getattr(existing_job, 'schedule_mode', 'daily') if existing_job else 'daily',
        'timezone': (existing_job.timezone if existing_job else '') or 'Asia/Shanghai',
    }
    selected_key = '1'
    while True:
        preview_job = ScheduledStartJob(
            instance_id=draft['instance_id'] if draft['source_kind'] == 'instance' else '',
            name=draft['name'],
            target_time=draft['target_time'],
            advance_hours=draft['advance_hours'],
            schedule_mode=draft['schedule_mode'],
            timezone=draft['timezone'] or 'Asia/Shanghai',
            selector=ScheduledStartSelector(
                regions=draft['regions'],
                gpu_model=draft['gpu_model'],
                gpu_count=draft['gpu_count'],
                charge_types=draft['charge_types'],
            ) if draft['source_kind'] == 'selector' else None,
            priority=draft['priority'] if draft['source_kind'] == 'selector' else [],
        )
        if draft['source_kind'] == 'instance':
            target_mode = '固定实例'
            target_detail = draft['instance_id'] or '-'
            target_label = '目标实例 ID'
        else:
            target_mode = '按条件筛选候选机器'
            selector_lines = []
            selector_lines.append(f"地区={','.join(draft['regions'])}" if draft['regions'] else '地区=不限')
            selector_lines.append(f"GPU 型号={draft['gpu_model']}" if draft['gpu_model'] else 'GPU 型号=不限')
            selector_lines.append(f"GPU 数量={draft['gpu_count'] or 1}")
            selector_lines.append(f"计费方式={','.join(draft['charge_types'])}" if draft['charge_types'] else '计费方式=不限')
            target_detail = '；'.join(selector_lines)
            target_label = '筛选条件'
        summary_lines = [
            _heading('任务编辑向导'),
            _separator(),
            _section('[当前草稿]'),
            _key_value('任务名称', draft['name'] or '-'),
            _key_value('目标方式', target_mode),
            _key_value(target_label, target_detail),
            _key_value('执行计划', '单次' if draft['schedule_mode'] == 'once' else '每天'),
            _key_value('目标时间', draft['target_time']),
            _key_value('提前启动', f"{draft['advance_hours']} 小时"),
            _key_value('时区', draft['timezone']),
        ]
        action_items = [
            MenuItem('1', '修改任务名称'),
            MenuItem('2', '修改目标方式'),
            MenuItem('3', '修改目标条件'),
            MenuItem('4', '修改时间设置'),
            MenuItem('5', '修改执行计划'),
            MenuItem('c', '保存'),
            MenuItem('0', '取消'),
        ]
        action = _delegate('_choose_menu')(
            '\n'.join(summary_lines),
            action_items,
            default_key=_delegate('_menu_default_key')(action_items, selected_key),
        )
        selected_key = action
        if action == '1':
            draft['name'] = _delegate('_prompt_with_default')('任务名称', draft['name'])
        elif action == '2':
            source_kind = _delegate('_choose_menu')(
                _heading('选择目标方式'),
                [MenuItem('1', '固定实例'), MenuItem('2', '按条件筛选候选机器'), MenuItem('0', '返回')],
                default_key='1' if draft['source_kind'] == 'instance' else '2',
            )
            if source_kind == '1':
                draft['source_kind'] = 'instance'
            elif source_kind == '2':
                draft['source_kind'] = 'selector'
        elif action == '3':
            if draft['source_kind'] == 'instance':
                draft['instance_id'] = _delegate('_prompt_with_default')('目标实例 ID', draft['instance_id'])
            else:
                draft['regions'] = _split_csv(_delegate('_prompt_with_default')('地区 (多个用逗号分隔，留空表示不限)', ','.join(draft['regions'])))
                draft['gpu_model'] = _delegate('_prompt_with_default')('GPU 型号 (如 RTX 3080 Ti，留空表示不限)', draft['gpu_model'])
                draft['gpu_count'] = _delegate('_prompt_int_with_default')('GPU 数量', draft['gpu_count'] or 1)
                draft['charge_types'] = _split_csv(_delegate('_prompt_with_default')('计费方式 (按量/包日，多个用逗号分隔，留空表示不限)', ','.join(draft['charge_types'])))
        elif action == '4':
            draft['target_time'], draft['advance_hours'], draft['timezone'] = _delegate('_prompt_scheduled_time_settings')(
                target_time=draft['target_time'],
                advance_hours=draft['advance_hours'],
                timezone=draft['timezone'] or 'Asia/Shanghai',
            )
        elif action == '5':
            schedule_choice = _delegate('_choose_menu')(
                _heading('选择执行计划'),
                [MenuItem('1', '每天'), MenuItem('2', '单次'), MenuItem('0', '返回')],
                default_key='2' if draft['schedule_mode'] == 'once' else '1',
            )
            if schedule_choice == '1':
                draft['schedule_mode'] = 'daily'
            elif schedule_choice == '2':
                draft['schedule_mode'] = 'once'
        elif action == 'c':
            return ScheduledStartJob(
                instance_id=draft['instance_id'] if draft['source_kind'] == 'instance' else '',
                name=draft['name'],
                target_time=draft['target_time'],
                advance_hours=draft['advance_hours'],
                schedule_mode=draft['schedule_mode'],
                timezone=draft['timezone'] or 'Asia/Shanghai',
                selector=ScheduledStartSelector(
                    regions=draft['regions'],
                    gpu_model=draft['gpu_model'],
                    gpu_count=draft['gpu_count'],
                    charge_types=draft['charge_types'],
                ) if draft['source_kind'] == 'selector' else None,
                priority=[],
            )
        elif action == '0':
            raise _InteractiveCancel('已取消编辑。')


def _prompt_keeper_settings(existing: KeeperSettings) -> KeeperSettings:
    draft = {
        'enabled': existing.enabled,
        'shutdown_release_after_hours': existing.shutdown_release_after_hours,
        'keeper_trigger_before_hours': existing.keeper_trigger_before_hours,
        'start_cooldown_minutes': existing.start_cooldown_minutes,
        'stop_cooldown_minutes': existing.stop_cooldown_minutes,
        'fallback_to_status_at': existing.fallback_to_status_at,
        'interval_minutes': existing.interval_minutes,
        'power_on_wait_seconds': existing.power_on_wait_seconds,
        'power_off_wait_seconds': existing.power_off_wait_seconds,
        'min_day': existing.min_day,
    }
    selected_key = '1'
    while True:
        lines = [
            _heading('Keeper 规则编辑向导', color=CYAN),
            _separator(),
            _section('[当前草稿]'),
            _key_value('Keeper 状态', '运行中' if draft['enabled'] else '已暂停'),
            _key_value('最多保留', _format_hours_brief(draft['shutdown_release_after_hours'])),
            _key_value('释放前开始接管', _format_hours_brief(draft['keeper_trigger_before_hours'])),
            _key_value('检查频率', _format_minutes_brief(draft['interval_minutes'])),
        ]
        action_items = [
            MenuItem('1', '切换 Keeper 状态'),
            MenuItem('2', '修改保留上限'),
            MenuItem('3', '修改接管时间'),
            MenuItem('4', '修改检查频率'),
            MenuItem('c', '保存'),
            MenuItem('0', '取消'),
        ]
        action = _delegate('_choose_menu')(
            '\n'.join(lines),
            action_items,
            default_key=_delegate('_menu_default_key')(action_items, selected_key),
        )
        selected_key = action
        if action == '1':
            enabled_choice = _delegate('_choose_menu')(
                _heading('是否启用 Keeper'),
                [MenuItem('1', '启用'), MenuItem('2', '暂停'), MenuItem('0', '返回')],
                default_key='1' if draft['enabled'] else '2',
            )
            if enabled_choice == '1':
                draft['enabled'] = True
            elif enabled_choice == '2':
                draft['enabled'] = False
        elif action == '2':
            draft['shutdown_release_after_hours'] = _delegate('_prompt_int_with_default')('最多保留多久 (小时)', draft['shutdown_release_after_hours'])
        elif action == '3':
            draft['keeper_trigger_before_hours'] = _delegate('_prompt_int_with_default')('释放前多久开始接管 (小时)', draft['keeper_trigger_before_hours'])
        elif action == '4':
            draft['interval_minutes'] = _delegate('_prompt_int_with_default')('检查频率 (分钟)', draft['interval_minutes'])
        elif action == 'c':
            return KeeperSettings(
                enabled=draft['enabled'],
                min_day=draft['min_day'],
                shutdown_release_after_hours=draft['shutdown_release_after_hours'],
                keeper_trigger_before_hours=draft['keeper_trigger_before_hours'],
                interval_minutes=draft['interval_minutes'],
                power_on_wait_seconds=draft['power_on_wait_seconds'],
                power_off_wait_seconds=draft['power_off_wait_seconds'],
                start_cooldown_minutes=draft['start_cooldown_minutes'],
                stop_cooldown_minutes=draft['stop_cooldown_minutes'],
                fallback_to_status_at=draft['fallback_to_status_at'],
            )
        elif action == '0':
            raise _InteractiveCancel('已取消编辑。')


def _confirm_action(title: str, *lines: str) -> bool:
    _delegate('_repaint_screen')()
    card = _boxed_lines(f'即将执行: {title}', [line for line in lines if line], tone='warn')
    print('\n'.join(card))
    raw = _delegate('_prompt')('确认执行? [Y/n]: ').lower()
    if raw in {'', 'y', 'yes'}:
        return True
    if raw not in {'n', 'no'}:
        return True
    if raw in {'n', 'no'}:
        print('已取消。')
        return False
    return True
