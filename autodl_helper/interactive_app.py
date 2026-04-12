from __future__ import annotations

import argparse
import contextlib
import copy
import io
import inspect
import json
import logging
import os
import re
import select
import sys
import termios
import threading
import time
import tty
import unicodedata
from datetime import datetime, timedelta
from dataclasses import asdict, dataclass, is_dataclass
from types import SimpleNamespace
from typing import Any, Callable
from zoneinfo import ZoneInfo

from autodl_helper.auth import clear_runtime_authorization, inspect_auth_state
from autodl_helper.auth_cache import write_auth_cache
from autodl_helper.models import HistoryRecord
from autodl_helper.interactive_runtime import (
    InteractivePageStatus,
    InteractiveSnapshotStore,
    InteractiveTaskManager,
    InteractiveTaskResult,
    capture_callable_output,
    reset_thread_capture_state,
)
from autodl_helper.config import (
    AccountSettings,
    KeeperSettings,
    ScheduledStartJob,
    ScheduledStartPriority,
    ScheduledStartSelector,
    Settings,
    read_raw_settings,
    write_raw_settings,
)
from autodl_helper.runtime_control import (
    get_task_enabled,
    read_config_reload_status,
    read_daemon_launch_status,
    read_daemon_status,
    scheduled_job_identity,
)
from autodl_helper.service_launchd import (
    DEFAULT_SERVICE_LABEL,
    append_service_lifecycle_log,
    read_launch_agent_status,
    start_launch_agent,
    stop_launch_agent,
)

RESET = '\033[0m'
DIM = '\033[38;5;245m'
BLUE = '\033[38;5;75m'
CYAN = '\033[38;5;80m'
GREEN = '\033[38;5;114m'
YELLOW = '\033[38;5;179m'
RED = '\033[38;5;174m'
ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
ISO_DATETIME_RE = re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?')
SCHEDULED_TIME_RE = re.compile(r'^(?:[01]\d|2[0-3]):[0-5]\d$')
GPU_SPEC_RE = re.compile(r'(?P<model>.+?)\s*[*×x]\s*(?P<count>\d+)\s*(?:卡)?\s*$')
SNAPSHOT_TEXT_LIMIT = 512
SNAPSHOT_BODY_LIMIT = 2048
SERVICE_HEARTBEAT_OK_SECONDS = 75


def _interactive_max_workers(settings: Settings | None) -> int:
    try:
        return max(1, int(getattr(getattr(settings, 'interactive', None), 'max_workers', 6) or 6))
    except Exception:
        return 6
LOGIN_VERIFY_TIMEOUT_SECONDS = 12.0
HEALTHCHECK_TIMEOUT_SECONDS = 8.0
KEEPER_EXECUTE_LONG_RUNNING_SECONDS = 12.0
_SUBPROCESS_TASK_STATS_LOCK = threading.Lock()
_SUBPROCESS_TASK_STATS: dict[str, int] = {
    'started': 0,
    'completed': 0,
    'long_running': 0,
    'failed': 0,
}


class _InteractiveCancel(Exception):
    pass


def _prompt(text: str) -> str:
    _show_cursor()
    try:
        if sys.stdin.isatty():
            try:
                termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
            except Exception:
                pass
        return input(text).strip()
    finally:
        _hide_cursor()


def _clear_screen() -> None:
    print('\033[2J\033[H', end='')


def _repaint_screen() -> None:
    print('\033[H\033[J', end='')


def _hide_cursor() -> None:
    print('\033[?25l', end='', flush=True)


def _show_cursor() -> None:
    print('\033[?25h', end='', flush=True)


def _style_text(text: str, color: str, *, bold: bool = False) -> str:
    prefix = '\033[1m' if bold else ''
    return f'{prefix}{color}{text}{RESET}'


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub('', text)


def _display_width(text: str) -> int:
    width = 0
    for ch in _strip_ansi(text):
        if unicodedata.combining(ch):
            continue
        if unicodedata.east_asian_width(ch) in {'W', 'F'}:
            width += 2
        else:
            width += 1
    return width


def _pad_display(text: str, width: int) -> str:
    padding = max(0, width - _display_width(text))
    return text + (' ' * padding)


def _heading(text: str, *, color: str = BLUE) -> str:
    return _style_text(text, color, bold=True)


def _section(text: str) -> str:
    return _style_text(text, DIM)


def _separator() -> str:
    return _style_text('────────────────────────────────────────────────────────', DIM)


def _key_value(label: str, value: object) -> str:
    styled_label = _style_text(label, DIM)
    return f'{_pad_display(styled_label, 18)} : {_humanize_datetime_text(value)}'


def _render_two_columns(
    left_lines: list[str],
    right_lines: list[str],
    *,
    gap: int = 4,
    left_width: int = 44,
) -> list[str]:
    left = list(left_lines)
    right = list(right_lines)
    total = max(len(left), len(right))
    left.extend([''] * (total - len(left)))
    right.extend([''] * (total - len(right)))
    rendered: list[str] = []
    for left_line, right_line in zip(left, right):
        left_text = _pad_display(left_line, left_width)
        if right_line:
            rendered.append(f'{left_text}{" " * gap}{right_line}')
        else:
            rendered.append(left_text)
    return rendered


def _tone_chip(label: str, tone: str) -> str:
    color = {'ok': GREEN, 'warn': YELLOW, 'bad': RED, 'info': CYAN, 'muted': DIM}.get(tone, DIM)
    return _style_text(label, color, bold=True)


def _format_hours_brief(hours: int | None) -> str:
    value = max(0, int(hours or 0))
    days, remain = divmod(value, 24)
    parts: list[str] = []
    if days:
        parts.append(f'{days}天')
    if remain:
        parts.append(f'{remain}小时')
    return ' '.join(parts) or '0小时'


def _format_minutes_brief(minutes: int | None) -> str:
    value = max(0, int(minutes or 0))
    hours, remain = divmod(value, 60)
    parts: list[str] = []
    if hours:
        parts.append(f'{hours}小时')
    if remain:
        parts.append(f'{remain}分钟')
    return ' '.join(parts) or '0分钟'


def _boxed_lines(title: str, lines: list[str], *, tone: str = 'info') -> list[str]:
    border_color = {'ok': GREEN, 'warn': YELLOW, 'bad': RED, 'info': CYAN, 'muted': DIM}.get(tone, CYAN)
    width = max([_display_width(title)] + [_display_width(line) for line in lines] + [10])
    top = _style_text(f'┌{"─" * (width + 2)}┐', border_color)
    bottom = _style_text(f'└{"─" * (width + 2)}┘', border_color)
    header = _style_text(f'│ {_pad_display(title, width)} │', border_color, bold=True)
    body = [_style_text(f'│ {_pad_display(line, width)} │', border_color) for line in lines]
    return [top, header] + body + [bottom]


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


def _is_scheduled_once_complete_result(result: Any) -> bool:
    return str(result or '') in {'started', 'already_running', 'power_on_submitted'}


def _is_scheduled_once_terminal_result(result: Any) -> bool:
    return str(result or '') in {'started', 'already_running', 'power_on_submitted', 'deadline_failed', 'instance_missing'}


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
        raw = _read_fd_char(fd)
        if raw in {'\r', '\n'}:
            return 'ENTER'
        if raw == '\x1b':
            return _read_escape_sequence_blocking(lambda: _read_fd_char(fd))
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
        raw = _read_fd_char(fd)
        if raw in {'\r', '\n'}:
            return 'ENTER'
        if raw == '\x1b':
            if timeout_seconds is None:
                return _read_escape_sequence_blocking(lambda: _read_fd_char(fd))
            deadline = time.monotonic() + 0.20
            return _read_escape_sequence_with_deadline(
                lambda: _read_fd_char(fd),
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
    if _supports_arrow_menu():
        current_title = title
        current_items = list(items)
        selected_index = 0
        if default_key is not None:
            for index, item in enumerate(current_items):
                if item.key == default_key:
                    selected_index = index
                    break
        _render_menu(current_title, current_items, selected_index)
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
            key = _read_key_with_timeout(timeout_seconds)
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
                        if _update_menu_title(current_title, next_title, len(current_items)):
                            current_title = next_title
                            current_items = next_items
                            continue
                    current_title = next_title
                    current_items = next_items
                    selected_index = next_selected_index
                    _render_menu(current_title, current_items, selected_index)
                continue
            if key == 'UP':
                previous_index = selected_index
                selected_index = (selected_index - 1) % len(current_items)
                _update_menu_selection(current_items, previous_index, selected_index)
                if refresh_fn is not None:
                    next_refresh_at = time.monotonic() + max(0.1, refresh_interval_seconds)
            elif key == 'DOWN':
                previous_index = selected_index
                selected_index = (selected_index + 1) % len(current_items)
                _update_menu_selection(current_items, previous_index, selected_index)
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
    _repaint_screen()
    print(title)
    print('')
    print(_separator())
    for item in items:
        print(f'{item.key}. {item.label}')
    if on_rendered_fn is not None:
        on_rendered_fn()
    return _prompt('选择: ') or (default_key or '')


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
    parameters = inspect.signature(_choose_menu).parameters
    accepts_var_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values())
    forwarded_kwargs = (
        kwargs
        if accepts_var_kwargs
        else {key: value for key, value in kwargs.items() if key in parameters}
    )
    return _choose_menu(title, items, **forwarded_kwargs)


def _nudge_background_tasks(task_manager: InteractiveTaskManager, *, settle_seconds: float = 0.01) -> None:
    task_manager.start_pending()
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    task_manager.drain_completed()


def _prompt_with_default(prompt: str, default: str | None = None) -> str:
    suffix = f' [{default}]' if default not in {None, ''} else ''
    raw = _prompt(f'{prompt}{suffix}: ')
    if raw in {':q', '/q'}:
        raise _InteractiveCancel('已取消编辑。')
    if raw == '':
        return default or ''
    return raw


def _prompt_int_with_default(prompt: str, default: int | None = None) -> int:
    raw = _prompt_with_default(prompt, str(default) if default is not None else None)
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
            value = _prompt_int_with_default(prompt, default)
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
        action = _choose_menu(
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
            mode_choice = _choose_menu(
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
                direct_time_choice = _choose_menu(
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
            hour_choice = _choose_menu(
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
            minute_choice = _choose_menu(
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
            advance_choice = _choose_menu(
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
                current_advance_hours = _prompt_custom_positive_int('提前启动 (小时)', current_advance_hours)
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


def _serialize_priority(priority: list[ScheduledStartPriority]) -> str:
    parts: list[str] = []
    for item in priority:
        fields: list[str] = []
        if item.instance_id:
            fields.append(f'iid={item.instance_id}')
        if item.region:
            fields.append(f'region={item.region}')
        if item.machine_alias:
            fields.append(f'alias={item.machine_alias}')
        if fields:
            parts.append(';'.join(fields))
    return ' | '.join(parts)


def _parse_priority(raw: str) -> list[ScheduledStartPriority]:
    if not raw.strip():
        return []
    items: list[ScheduledStartPriority] = []
    for chunk in raw.split('|'):
        payload: dict[str, str] = {}
        for field in chunk.split(';'):
            if '=' not in field:
                continue
            key, value = field.split('=', 1)
            payload[key.strip()] = value.strip()
        items.append(
            ScheduledStartPriority(
                instance_id=payload.get('iid', ''),
                region=payload.get('region', ''),
                machine_alias=payload.get('alias', ''),
            )
        )
    return [item for item in items if item.instance_id or item.region or item.machine_alias]


def _job_to_payload(job: ScheduledStartJob) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'instance_id': job.instance_id,
        'name': job.name,
        'target_time': job.target_time,
        'advance_hours': job.advance_hours,
        'schedule_mode': getattr(job, 'schedule_mode', 'daily') or 'daily',
        'timezone': job.timezone,
    }
    if job.selector is not None:
        payload['selector'] = asdict(job.selector)
    return payload


def _job_target_summary(job: ScheduledStartJob) -> str:
    if job.instance_id:
        return f'固定实例={job.instance_id}'
    if job.selector is None:
        return '-'
    parts = []
    if job.selector.regions:
        parts.append(f"地区={','.join(job.selector.regions)}")
    if job.selector.gpu_model:
        parts.append(f'GPU={job.selector.gpu_model}')
    if job.selector.gpu_count:
        parts.append(f'数量={job.selector.gpu_count}')
    return '；'.join(parts) or '-'


def _scheduled_result_label(value: str) -> str:
    return {
        'started': '已发起开机',
        'already_running': '实例已在运行',
        'outside_window': '未到轮询窗口',
        'waiting_for_gpu': '有候选但暂时不可抢',
        'waiting_for_instance': '还没等到目标实例',
        'no_eligible_candidate': '有候选但当前都不可开机',
        'selector_no_match': '当前没有命中筛选条件的候选',
        'instance_missing': '实例不存在',
        'started_without_gpu': '已开机但未进 GPU 模式',
        'power_on_submitted': '开机请求已提交',
        'deadline_failed': '超过截止时间仍失败',
    }.get(value, value or '-')


def _scheduled_reason_label(value: str) -> str:
    return {
        'started': '已提交开机动作',
        'already_running': '实例已在 GPU 模式运行',
        'outside_window': '当前还没到轮询窗口',
        'no_eligible_candidate': '有匹配候选，但都暂时不满足开机条件',
        'selector_no_match': '当前没有任何候选命中筛选条件',
        'waiting_for_gpu': '当前候选暂时不可开机',
        'waiting_for_instance': '当前还没有等到目标实例',
        'running_with_gpu': '实例已在 GPU 模式运行',
        'gpu_idle_zero': '空闲 GPU 数量为 0',
        'eligible': '候选满足条件，等待执行',
        'instance_missing': '目标实例不存在',
        'power_on_submitted': '平台已接受开机请求',
        'started_without_gpu': '实例已开机但未进入 GPU 模式',
        'deadline_failed': '超过截止时间仍未成功',
        'deadline_missed': '超过目标截止时间',
    }.get(value, value or '-')


def _scheduled_summary_replacements() -> dict[str, str]:
    return {
        'started': _scheduled_result_label('started'),
        'already_running': _scheduled_result_label('already_running'),
        'outside_window': _scheduled_result_label('outside_window'),
        'waiting_for_gpu': _scheduled_result_label('waiting_for_gpu'),
        'waiting_for_instance': _scheduled_result_label('waiting_for_instance'),
        'no_eligible_candidate': _scheduled_result_label('no_eligible_candidate'),
        'selector_no_match': _scheduled_result_label('selector_no_match'),
        'instance_missing': _scheduled_result_label('instance_missing'),
        'started_without_gpu': _scheduled_result_label('started_without_gpu'),
        'power_on_submitted': _scheduled_result_label('power_on_submitted'),
        'deadline_failed': _scheduled_result_label('deadline_failed'),
        'running_with_gpu': _scheduled_reason_label('running_with_gpu'),
        'gpu_idle_zero': _scheduled_reason_label('gpu_idle_zero'),
        'eligible': _scheduled_reason_label('eligible'),
        'deadline_missed': _scheduled_reason_label('deadline_missed'),
        'shutdown': _normalize_instance_status('shutdown'),
        'stopped': _normalize_instance_status('stopped'),
        'running': _normalize_instance_status('running'),
        'booting': _normalize_instance_status('booting'),
        'starting': _normalize_instance_status('starting'),
        'pending': _normalize_instance_status('pending'),
        'stopping': _normalize_instance_status('stopping'),
        'gpu': _normalize_start_mode('gpu'),
        'non_gpu': _normalize_start_mode('non_gpu'),
    }


def _sanitize_scheduled_summary(text: Any) -> str:
    raw = str(text or '').strip()
    if not raw:
        return '-'
    sanitized = raw
    for source, target in sorted(_scheduled_summary_replacements().items(), key=lambda item: len(item[0]), reverse=True):
        sanitized = re.sub(rf'(?<![A-Za-z0-9_]){re.escape(source)}(?![A-Za-z0-9_])', target, sanitized)
    return _humanize_datetime_text(sanitized)


def _keeper_result_label(value: str) -> str:
    return {
        'ready': '可执行保活',
        'keeper_executed': '已执行保活',
        'keeper_failed_power_on': '开机失败',
        'keeper_failed_power_off': '关机失败',
        'skip_not_due': '未到保活窗口',
        'skip_recently_stopped': '最近关机，处于冷却期',
        'skip_recently_started': '最近开机，处于冷却期',
        'skip_missing_shutdown_time': '缺少关机时间',
        'skip_already_executed_in_cycle': '本周期已执行过',
        'skip_missing_instance_id': '缺少实例 ID',
    }.get(value, value or '-')


def _keeper_reason_label(value: str) -> str:
    return {
        'before_next_keeper_time': '还没到下次保活时间',
        'stopped_within_cooldown': '最近关机时间未超过冷却窗口',
        'started_within_cooldown': '最近启动时间未超过冷却窗口',
        'fallback_status_at_recently_stopped': '只能用 status_at 兜底，且仍在关机冷却窗口',
        'fallback_status_at_recently_started': '只能用 status_at 兜底，且仍在开机冷却窗口',
        'fallback_status_at_ready': '只能用 status_at 兜底，但已到保活窗口',
        'keeper_window_reached': '已到保活执行窗口',
        'missing_shutdown_time': '没有可用的关机时间',
        'already_executed_in_release_cycle': '本轮释放周期里已经执行过',
        'power_on_failed': '开机接口执行失败',
        'power_off_failed': '关机接口执行失败',
        'missing_instance_id': '实例缺少 uuid',
    }.get(value, value or '-')


def _ensure_jobs_payload(raw_payload: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    tasks_payload = raw_payload.setdefault('tasks', {})
    scheduled_payload = tasks_payload.setdefault('scheduled_start', {})
    jobs_payload = scheduled_payload.setdefault('jobs', [])
    if not jobs_payload and settings.tasks.scheduled_start.jobs:
        jobs_payload.extend(_job_to_payload(job) for job in settings.tasks.scheduled_start.jobs)
    return jobs_payload


def _persist_job_changes(
    *,
    config_path: str,
    settings: Settings,
    load_settings_fn: Callable[[str], Settings],
    validate_settings_fn: Callable[[Settings, str], list[str]],
    mutator: Callable[[list[dict[str, Any]]], None],
) -> None:
    raw_payload = read_raw_settings(config_path)
    original_payload = copy.deepcopy(raw_payload)
    tasks_payload = raw_payload.setdefault('tasks', {})
    scheduled_payload = tasks_payload.setdefault('scheduled_start', {})
    jobs_payload = _ensure_jobs_payload(raw_payload, settings)
    mutator(jobs_payload)
    if jobs_payload:
        scheduled_payload['enabled'] = True
    else:
        scheduled_payload['enabled'] = False
    write_raw_settings(config_path, raw_payload)
    updated_settings = load_settings_fn(config_path)
    errors = validate_settings_fn(updated_settings, purpose='validate')
    if errors:
        write_raw_settings(config_path, original_payload)
        raise ValueError('配置写回失败: ' + '; '.join(errors))


def _persist_keeper_changes(
    *,
    config_path: str,
    settings: Settings,
    load_settings_fn: Callable[[str], Settings],
    validate_settings_fn: Callable[[Settings, str], list[str]],
    keeper_settings: KeeperSettings,
) -> None:
    raw_payload = read_raw_settings(config_path)
    original_payload = copy.deepcopy(raw_payload)
    tasks_payload = raw_payload.setdefault('tasks', {})
    tasks_payload['keeper'] = {
        'enabled': keeper_settings.enabled,
        'min_day': keeper_settings.min_day,
        'shutdown_release_after_hours': keeper_settings.shutdown_release_after_hours,
        'keeper_trigger_before_hours': keeper_settings.keeper_trigger_before_hours,
        'interval_minutes': keeper_settings.interval_minutes,
        'power_on_wait_seconds': keeper_settings.power_on_wait_seconds,
        'power_off_wait_seconds': keeper_settings.power_off_wait_seconds,
        'start_cooldown_minutes': keeper_settings.start_cooldown_minutes,
        'stop_cooldown_minutes': keeper_settings.stop_cooldown_minutes,
        'fallback_to_status_at': keeper_settings.fallback_to_status_at,
    }
    write_raw_settings(config_path, raw_payload)
    updated_settings = load_settings_fn(config_path)
    errors = validate_settings_fn(updated_settings, purpose='validate')
    if errors:
        write_raw_settings(config_path, original_payload)
        raise ValueError('配置写回失败: ' + '; '.join(errors))


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
        action = _choose_menu(
            '\n'.join(summary_lines),
            action_items,
            default_key=_menu_default_key(action_items, selected_key),
        )
        selected_key = action
        if action == '1':
            draft['name'] = _prompt_with_default('任务名称', draft['name'])
        elif action == '2':
            source_kind = _choose_menu(
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
                draft['instance_id'] = _prompt_with_default('目标实例 ID', draft['instance_id'])
            else:
                draft['regions'] = _split_csv(_prompt_with_default('地区 (多个用逗号分隔，留空表示不限)', ','.join(draft['regions'])))
                draft['gpu_model'] = _prompt_with_default('GPU 型号 (如 RTX 3080 Ti，留空表示不限)', draft['gpu_model'])
                draft['gpu_count'] = _prompt_int_with_default('GPU 数量', draft['gpu_count'] or 1)
                draft['charge_types'] = _split_csv(_prompt_with_default('计费方式 (按量/包日，多个用逗号分隔，留空表示不限)', ','.join(draft['charge_types'])))
        elif action == '4':
            draft['target_time'], draft['advance_hours'], draft['timezone'] = _prompt_scheduled_time_settings(
                target_time=draft['target_time'],
                advance_hours=draft['advance_hours'],
                timezone=draft['timezone'] or 'Asia/Shanghai',
            )
        elif action == '5':
            schedule_choice = _choose_menu(
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
        action = _choose_menu(
            '\n'.join(lines),
            action_items,
            default_key=_menu_default_key(action_items, selected_key),
        )
        selected_key = action
        if action == '1':
            enabled_choice = _choose_menu(
                _heading('是否启用 Keeper'),
                [MenuItem('1', '启用'), MenuItem('2', '暂停'), MenuItem('0', '返回')],
                default_key='1' if draft['enabled'] else '2',
            )
            if enabled_choice == '1':
                draft['enabled'] = True
            elif enabled_choice == '2':
                draft['enabled'] = False
        elif action == '2':
            draft['shutdown_release_after_hours'] = _prompt_int_with_default('最多保留多久 (小时)', draft['shutdown_release_after_hours'])
        elif action == '3':
            draft['keeper_trigger_before_hours'] = _prompt_int_with_default('释放前多久开始接管 (小时)', draft['keeper_trigger_before_hours'])
        elif action == '4':
            draft['interval_minutes'] = _prompt_int_with_default('检查频率 (分钟)', draft['interval_minutes'])
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


def _copy_args(args: argparse.Namespace, **updates: Any) -> SimpleNamespace:
    payload = dict(vars(args))
    payload.update(updates)
    return SimpleNamespace(**payload)


def _mask_phone(phone: str | None) -> str:
    raw = str(phone or '').strip()
    if len(raw) < 7:
        return '-'
    return f'{raw[:3]}****{raw[-4:]}'


def _account_display_name(settings: Settings, account_name: str | None) -> str:
    if not account_name:
        return '-'
    account = next((item for item in settings.accounts if item.name == account_name), None)
    if account is None:
        return account_name
    phone = _mask_phone(account.autodl_phone) if account.autodl_phone else ''
    if phone and phone != '-':
        return f'{account.name} ({phone})'
    return account.name


def _auth_rank(status: str) -> int:
    order = {
        'logged_in': 5,
        'cached': 4,
        'token_configured': 3,
        'login_ready': 2,
        'not_configured': 1,
    }
    return order.get(status, 0)


def _pick_default_account(settings: Settings, preferred_account: str | None, store) -> str | None:
    enabled_accounts = [account for account in settings.accounts if account.enabled] if settings.accounts else []
    if preferred_account and any(account.name == preferred_account for account in enabled_accounts):
        return preferred_account
    if not enabled_accounts:
        return preferred_account or 'default'
    ranked = []
    for index, account in enumerate(enabled_accounts):
        state = inspect_auth_state(account.to_auth_settings(), store=store, account_name=account.name)
        ranked.append((_auth_rank(str(state.get('status') or '')), index, account.name))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked[0][2] if ranked else enabled_accounts[0].name


def _capture_action_output(action: Callable[[], Any]) -> tuple[Any, str]:
    result, output = capture_callable_output(action)
    output = output or '无输出。'
    return result, output


def _run_captured_action(title: str, action: Callable[[], int | None]) -> tuple[int | None, str]:
    del title
    result, output = _capture_action_output(action)
    code = result if isinstance(result, int) or result is None else 0
    return code, output


def _background_command_entry(command_fn, args_payload: dict[str, Any], result_queue) -> None:
    try:
        code, output = _run_captured_action(
            '后台命令',
            lambda: command_fn(SimpleNamespace(**args_payload)),
        )
        result_queue.put(
            {
                'ok': True,
                'code': code,
                'output': output,
                'summary': '',
                'timed_out': False,
            }
        )
    except BaseException as exc:
        result_queue.put(
            {
                'ok': False,
                'code': 1,
                'output': '',
                'summary': str(exc),
                'timed_out': False,
            }
        )


def _run_command_with_timeout(
    *,
    command_fn,
    args: SimpleNamespace,
    timeout_seconds: float,
    title: str,
    timeout_summary: str,
) -> dict[str, Any]:
    started_at = time.time()
    try:
        _bump_subprocess_task_stat('started')
        code, output = _run_captured_action(title, lambda: command_fn(args))
        elapsed_seconds = round(max(0.0, time.time() - started_at), 3)
        long_running = elapsed_seconds > max(0.0, timeout_seconds)
        if long_running:
            _bump_subprocess_task_stat('long_running')
        _bump_subprocess_task_stat('completed')
        return {
            'ok': True,
            'code': code,
            'output': output,
            'summary': timeout_summary if long_running else '',
            'timed_out': False,
            'long_running': long_running,
            'elapsed_seconds': elapsed_seconds,
        }
    except Exception:
        _bump_subprocess_task_stat('failed')
        raise


def _snapshot_key(namespace: str, scope: str | None) -> str:
    return f'{namespace}:{scope or "default"}'


def _bump_subprocess_task_stat(name: str, amount: int = 1) -> None:
    with _SUBPROCESS_TASK_STATS_LOCK:
        _SUBPROCESS_TASK_STATS[name] = int(_SUBPROCESS_TASK_STATS.get(name, 0)) + amount


def _subprocess_task_stats_snapshot() -> dict[str, int]:
    with _SUBPROCESS_TASK_STATS_LOCK:
        return {key: int(value) for key, value in _SUBPROCESS_TASK_STATS.items()}


def _friendly_resource_error_message(error_message: str) -> str:
    message = str(error_message or '').strip()
    lowered = message.lower()
    if 'unable to open database file' in lowered:
        path_match = re.search(r'(path=[^;]+)$', message)
        suffix = f' / {path_match.group(1)}' if path_match else ''
        return f'数据库打开失败（可能为文件描述符耗尽或资源熔断）{suffix}'
    if 'too many open files' in lowered:
        return f'资源不足：文件描述符耗尽 ({message})'
    if 'resource temporarily unavailable' in lowered:
        return f'资源不足：系统暂时不可用 ({message})'
    return message


def _truncate_text(value: Any, *, limit: int = SNAPSHOT_TEXT_LIMIT) -> str:
    text = str(value or '')
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + '...'


def _trim_snapshot_payload(snapshot_key: str, payload: Any) -> Any:
    namespace = str(snapshot_key).split(':', 1)[0]
    if namespace == 'account_runtime' and isinstance(payload, dict):
        return {
            'account_name': payload.get('account_name'),
            'account_enabled': bool(payload.get('account_enabled', True)),
            'auth_status': _truncate_text(payload.get('auth_status')),
            'auth_source': _truncate_text(payload.get('auth_source')),
            'cached_at_iso': payload.get('cached_at_iso'),
            'running_instances': int(payload.get('running_instances') or 0),
            'expiring_soon': int(payload.get('expiring_soon') or 0),
            'scheduled_jobs': int(payload.get('scheduled_jobs') or 0),
            'paused_jobs': int(payload.get('paused_jobs') or 0),
            'keeper_enabled': bool(payload.get('keeper_enabled', False)),
        }
    if namespace == 'diagnostics' and isinstance(payload, dict):
        return {
            'instance_total': int(payload.get('instance_total') or 0),
            'instance_running': int(payload.get('instance_running') or 0),
            'instance_shutdown': int(payload.get('instance_shutdown') or 0),
            'keeper_total': int(payload.get('keeper_total') or 0),
            'keeper_eligible': int(payload.get('keeper_eligible') or 0),
            'healthcheck_status': _truncate_text(payload.get('healthcheck_status')),
            'healthcheck_summary': _truncate_text(payload.get('healthcheck_summary')),
            'config_status': _truncate_text(payload.get('config_status')),
            'config_summary': _truncate_text(payload.get('config_summary')),
            'fd_current': payload.get('fd_current'),
            'fd_soft_limit': payload.get('fd_soft_limit'),
            'fd_usage_percent': payload.get('fd_usage_percent'),
            'interactive_workers_max': int(payload.get('interactive_workers_max') or 0),
            'interactive_running_count': int(payload.get('interactive_running_count') or 0),
            'interactive_queued_count': int(payload.get('interactive_queued_count') or 0),
            'interactive_running_by_type': dict(payload.get('interactive_running_by_type') or {}),
            'daemon_launch_state': _truncate_text(payload.get('daemon_launch_state')),
            'daemon_pid': payload.get('daemon_pid'),
            'daemon_error_count': int(payload.get('daemon_error_count') or 0),
            'daemon_last_error': _truncate_text(payload.get('daemon_last_error')),
            'daemon_fused_until': payload.get('daemon_fused_until'),
            'interactive_circuit_open': bool(payload.get('interactive_circuit_open', False)),
            'interactive_circuit_reason': _truncate_text(payload.get('interactive_circuit_reason')),
            'interactive_circuit_until': payload.get('interactive_circuit_until'),
        }
    if namespace == 'healthcheck' and isinstance(payload, dict):
        return {
            'status': _truncate_text(payload.get('status')),
            'summary': _truncate_text(payload.get('summary')),
            'code': payload.get('code'),
            'body': _truncate_text(payload.get('body') or '无输出。', limit=SNAPSHOT_BODY_LIMIT),
        }
    if namespace == 'config_diagnostics' and isinstance(payload, dict):
        return {
            'status': _truncate_text(payload.get('status')),
            'summary': _truncate_text(payload.get('summary')),
            'body': _truncate_text(payload.get('body') or '', limit=SNAPSHOT_BODY_LIMIT),
        }
    if namespace == 'dashboard' and isinstance(payload, dict):
        scheduled_jobs = []
        for job in list(payload.get('scheduled_jobs') or [])[:6]:
            if not isinstance(job, dict):
                continue
            scheduled_jobs.append(
                {
                    'job_name': job.get('job_name'),
                    'enabled': bool(job.get('enabled', False)),
                    'target_time': job.get('target_time'),
                    'advance_hours': job.get('advance_hours'),
                    'latest_result': job.get('latest_result'),
                    'latest_created_at': job.get('latest_created_at'),
                    'task_status_label': _truncate_text(job.get('task_status_label')),
                    'task_status_tone': job.get('task_status_tone'),
                }
            )
        candidate_summary = payload.get('candidate_summary') if isinstance(payload.get('candidate_summary'), dict) else {}
        runtime_status = payload.get('runtime_status') if isinstance(payload.get('runtime_status'), dict) else {}
        current_account_row = payload.get('current_account_row') if isinstance(payload.get('current_account_row'), dict) else {}
        keeper_summary = payload.get('keeper_summary') if isinstance(payload.get('keeper_summary'), dict) else {}
        return {
            'runtime_status': {
                'running': bool(runtime_status.get('running', False)),
                'pid': runtime_status.get('pid'),
                'heartbeat_age_seconds': runtime_status.get('heartbeat_age_seconds'),
            },
            'current_account': payload.get('current_account'),
            'current_account_row': {
                key: value
                for key, value in {
                    'status': _truncate_text(current_account_row.get('status')),
                    'auth_source': _truncate_text(current_account_row.get('auth_source')),
                    'cached_at_iso': current_account_row.get('cached_at_iso'),
                }.items()
                if value not in {None, ''}
            },
            'enabled_accounts': int(payload.get('enabled_accounts') or 0),
            'effective_keeper_enabled': bool(payload.get('effective_keeper_enabled', False)),
            'effective_scheduled_enabled': bool(payload.get('effective_scheduled_enabled', False)),
            'paused_job_count': int(payload.get('paused_job_count') or 0),
            'scheduled_job_count': len(list(payload.get('scheduled_jobs') or [])),
            'scheduled_jobs': scheduled_jobs,
            'keeper_summary': {
                'pending': int(keeper_summary.get('pending') or 0),
                'not_due': int(keeper_summary.get('not_due') or 0),
                'abnormal': int(keeper_summary.get('abnormal') or 0),
                'expiring_soon': int(keeper_summary.get('expiring_soon') or 0),
                'failed': int(keeper_summary.get('failed') or 0),
            },
            'candidate_summary': {
                'job_name': candidate_summary.get('job_name'),
                'selected_instance_id': candidate_summary.get('selected_instance_id'),
                'candidate_count': int(candidate_summary.get('candidate_count') or 0),
                'top_reasons': list(candidate_summary.get('top_reasons') or [])[:3],
            },
            'service_state_label': _truncate_text(payload.get('service_state_label')),
            'service_state_tone': payload.get('service_state_tone'),
            'service_last_seen_at': payload.get('service_last_seen_at'),
            'service_pid': payload.get('service_pid'),
        }
    if namespace == 'scheduled_progress' and isinstance(payload, list):
        kept_rows: list[dict[str, Any]] = []
        allowed_row_keys = {
            'job_name', 'enabled', 'target_mode', 'target_summary', 'target_time', 'advance_hours',
            'schedule_mode', 'timezone', 'latest_created_at', 'latest_result', 'latest_summary',
            'latest_matching_created_at', 'latest_matches_current_rule', 'has_history',
            'daemon_running',
            'latest_payload', '_live_stage_label', '_live_stage_tone', '_live_execution_label',
            '_live_execution_tone', '_live_next_action', '_live_poll_text', '_live_target_text',
            '_live_missing_reason_label', '_live_missing_reason_tone',
        }
        for item in payload:
            if not isinstance(item, dict):
                continue
            row = {key: item.get(key) for key in allowed_row_keys if key in item}
            latest_payload = item.get('latest_payload')
            if isinstance(latest_payload, dict):
                row['latest_payload'] = {
                    'hit_count': latest_payload.get('hit_count'),
                    'waiting_count': latest_payload.get('waiting_count'),
                    'dropped_count': latest_payload.get('dropped_count'),
                }
            else:
                row['latest_payload'] = {}
            row['latest_summary'] = _truncate_text(item.get('latest_summary'))
            row['target_summary'] = _truncate_text(item.get('target_summary'))
            row['_live_next_action'] = _truncate_text(item.get('_live_next_action'))
            kept_rows.append(row)
        return kept_rows
    if namespace == 'scheduled_status' and isinstance(payload, list):
        kept_rows: list[dict[str, Any]] = []
        allowed_row_keys = {
            'job_name', 'enabled', 'target_time', 'advance_hours', 'schedule_mode', 'timezone',
            'latest_result', 'latest_reason', 'latest_summary', 'latest_created_at', 'latest_instance_id',
            'has_history', 'latest_matches_current_rule', 'task_status_label', 'task_status_tone',
            'daemon_running', 'last_run_trigger', 'last_run_label', 'last_run_summary',
        }
        for item in payload:
            if not isinstance(item, dict):
                continue
            row = {key: item.get(key) for key in allowed_row_keys if key in item}
            latest_payload = item.get('latest_payload')
            if isinstance(latest_payload, dict):
                row['latest_payload'] = {
                    'candidate_count': latest_payload.get('candidate_count'),
                    'selected_instance_id': latest_payload.get('selected_instance_id'),
                    'selected_instance_label': _truncate_text(latest_payload.get('selected_instance_label')),
                    'selector_summary': _truncate_text(latest_payload.get('selector_summary')),
                    'status': latest_payload.get('status'),
                }
            else:
                row['latest_payload'] = {}
            row['latest_summary'] = _truncate_text(item.get('latest_summary'))
            row['last_run_summary'] = _truncate_text(item.get('last_run_summary'))
            kept_rows.append(row)
        return kept_rows
    return payload


def _store_snapshot(
    snapshot_store: InteractiveSnapshotStore,
    snapshot_key: str,
    payload: Any,
    *,
    status_message: str = '最近更新',
) -> None:
    snapshot_store.set_snapshot(snapshot_key, _trim_snapshot_payload(snapshot_key, payload), status_message=status_message)


def _clear_scheduled_progress_scope_snapshots(
    snapshot_store: InteractiveSnapshotStore,
    *,
    current_account: str | None,
    job_name: str | None,
) -> None:
    account_scope = current_account or 'default'
    scope = f'job:{account_scope}:{job_name}' if job_name else f'all:{account_scope}'
    snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', scope))


def _clear_diagnostics_scope_snapshots(
    snapshot_store: InteractiveSnapshotStore,
    *,
    current_account: str | None,
) -> None:
    account_scope = current_account or 'default'
    for namespace in ('diagnostics', 'healthcheck', 'instances', 'keeper_probe', 'config_diagnostics'):
        snapshot_store.clear_prefix(_snapshot_key(namespace, account_scope))


def _page_status_tone(status: InteractivePageStatus, active_task: InteractiveTaskResult | None = None) -> str:
    if active_task is not None and active_task.status in {'queued', 'running'}:
        age_seconds = _task_running_age_seconds(active_task)
        threshold = _task_long_running_threshold_seconds(active_task)
        if threshold > 0 and age_seconds >= threshold:
            return 'warn'
        return 'info'
    return {
        'ready': 'ok',
        'failed': 'bad',
        'refreshing': 'info',
        'loading': 'info',
        'idle': 'muted',
    }.get(status.state, 'muted')


def _task_activity_label(task: InteractiveTaskResult) -> tuple[str, str]:
    if task.status == 'queued':
        return '排队中', 'info'
    age_seconds = _task_running_age_seconds(task)
    threshold = _task_long_running_threshold_seconds(task)
    if threshold > 0 and age_seconds >= threshold:
        return '耗时较长', 'warn'
    return '运行中', 'info'


def _render_task_progress_bar(
    *,
    task: InteractiveTaskResult,
    width: int = 10,
) -> str:
    frame = int(max(0.0, time.monotonic()) * 4)
    pulse_width = min(2, width)
    if task.status == 'queued':
        fill = 0
    else:
        threshold = max(1.0, float(_task_long_running_threshold_seconds(task) or 10.0))
        fill_ratio = min(1.0, _task_running_age_seconds(task) / threshold)
        fill = max(1, int(round(fill_ratio * width)))
    fill = max(0, min(width, fill))
    label, tone = _task_activity_label(task)
    tone_color = {'info': CYAN, 'warn': YELLOW}.get(tone, CYAN)
    cells = ['░'] * width
    for index in range(fill):
        cells[index] = '█'
    if width > 0:
        if task.status == 'queued':
            pulse_start = frame % width
            pulse_indexes = [(pulse_start + offset) % width for offset in range(pulse_width)]
        else:
            animation_width = max(fill, pulse_width)
            pulse_start = frame % animation_width
            pulse_indexes = [(pulse_start + offset) % animation_width for offset in range(pulse_width)]
        pulse_indexes = [index for index in pulse_indexes if 0 <= index < width]
    else:
        pulse_indexes = []
    rendered_cells: list[str] = []
    for index, cell in enumerate(cells):
        if index in pulse_indexes:
            rendered_cells.append(_style_text('▓', tone_color, bold=True))
        elif cell == '█':
            rendered_cells.append(_style_text(cell, tone_color, bold=True))
        else:
            rendered_cells.append(_style_text(cell, DIM))
    suffix = '排队中' if task.status == 'queued' else f'{_task_running_age_seconds(task)}s'
    return f"[{''.join(rendered_cells)}] {suffix}"


def _page_status_lines(
    status: InteractivePageStatus,
    *,
    prefix: str = '数据状态',
    active_task: InteractiveTaskResult | None = None,
    progress_label: str = '任务进度',
    show_task_stage: bool = True,
    show_progress: bool = False,
    show_hint: bool = True,
) -> list[str]:
    message = str(status.message or '').strip() or '首次加载中'
    if status.updated_at:
        message = f'{message} / 最近更新于 {_format_human_datetime(status.updated_at)}'
    lines = [_key_value(prefix, _tone_chip(message, _page_status_tone(status, active_task)))]
    if active_task is not None and active_task.status in {'queued', 'running'}:
        if show_task_stage:
            activity_label, activity_tone = _task_activity_label(active_task)
            lines.append(_key_value('任务阶段', _tone_chip(activity_label, activity_tone)))
        if show_progress:
            lines.append(_key_value(progress_label, _render_task_progress_bar(task=active_task)))
        age_seconds = _task_running_age_seconds(active_task)
        threshold = _task_long_running_threshold_seconds(active_task)
        if show_hint and active_task.status == 'running' and threshold > 0 and age_seconds >= threshold:
            lines.append(_key_value('提示', _style_text('可按 q 返回，后台继续执行', DIM)))
    if status.error_message:
        lines.append(_key_value('错误信息', _tone_chip(status.error_message, 'bad')))
    return lines


def _page_status_from_snapshot_keys(
    *,
    snapshot_store: InteractiveSnapshotStore,
    snapshot_keys: list[str],
    primary_task: InteractiveTaskResult | None = None,
    secondary_tasks: list[InteractiveTaskResult | None] | None = None,
) -> InteractivePageStatus:
    active_task = primary_task
    for task in secondary_tasks or []:
        if task is not None and task.status in {'queued', 'running'}:
            active_task = task
            break

    latest_ready_entry = None
    latest_failed_entry = None
    for key in snapshot_keys:
        entry = snapshot_store.get_entry(key)
        if entry is None:
            continue
        if entry.updated_at:
            if latest_ready_entry is None or str(entry.updated_at) >= str(latest_ready_entry.updated_at):
                latest_ready_entry = entry
        if entry.error_message:
            if latest_failed_entry is None:
                latest_failed_entry = entry
            elif entry.updated_at and (not latest_failed_entry.updated_at or str(entry.updated_at) >= str(latest_failed_entry.updated_at)):
                latest_failed_entry = entry

    if active_task is not None and active_task.status in {'queued', 'running'}:
        if latest_ready_entry is not None and latest_ready_entry.updated_at:
            status = InteractivePageStatus(
                state='refreshing',
                message='正在刷新',
                updated_at=latest_ready_entry.updated_at,
                error_message='',
            )
        else:
            status = InteractivePageStatus(
                state='loading',
                message='首次加载中',
                updated_at='',
                error_message='',
            )
        age_seconds = _task_running_age_seconds(active_task)
        threshold = _task_long_running_threshold_seconds(active_task)
        if active_task.status == 'running' and threshold > 0 and age_seconds >= threshold:
            return InteractivePageStatus(
                state=status.state,
                message=f'{active_task.status_message or status.message}（已持续 {age_seconds}s，超时风险）',
                updated_at=status.updated_at,
                error_message=status.error_message,
            )
        if active_task.status_message:
            return InteractivePageStatus(
                state=status.state,
                message=active_task.status_message,
                updated_at=status.updated_at,
                error_message=status.error_message,
            )
        return status

    if latest_failed_entry is not None and not latest_ready_entry:
        return InteractivePageStatus(
            state='failed',
            message='刷新失败',
            updated_at='',
            error_message=latest_failed_entry.error_message,
        )
    if latest_failed_entry is not None and latest_failed_entry.updated_at and (
        latest_ready_entry is None or str(latest_failed_entry.updated_at) >= str(latest_ready_entry.updated_at)
    ):
        return InteractivePageStatus(
            state='failed',
            message='刷新失败（保留上次结果）',
            updated_at=latest_failed_entry.updated_at,
            error_message=latest_failed_entry.error_message,
        )
    if latest_ready_entry is not None and latest_ready_entry.updated_at:
        return InteractivePageStatus(
            state='ready',
            message=latest_ready_entry.status_message or '最近更新',
            updated_at=latest_ready_entry.updated_at,
            error_message='',
        )
    return InteractivePageStatus(
        state='idle',
        message='首次加载中',
        updated_at='',
        error_message='',
    )


def _diagnostics_page_status(
    *,
    snapshot_store: InteractiveSnapshotStore,
    account_scope: str,
    instance_task: InteractiveTaskResult | None,
    keeper_task: InteractiveTaskResult | None,
    healthcheck_task: InteractiveTaskResult | None,
) -> InteractivePageStatus:
    sources = [
        (
            _snapshot_key('instances', account_scope),
            '最近实例更新',
            '实例刷新失败（保留上次结果）',
            '实例刷新失败',
        ),
        (
            _snapshot_key('keeper_probe', account_scope),
            '最近 Keeper 更新',
            'Keeper 刷新失败（保留上次结果）',
            'Keeper 刷新失败',
        ),
        (
            _snapshot_key('healthcheck', account_scope),
            '最近健康自检更新',
            '健康自检刷新失败（保留上次结果）',
            '健康自检刷新失败',
        ),
        (
            _snapshot_key('config_diagnostics', account_scope),
            '最近配置诊断更新',
            '配置诊断刷新失败（保留上次结果）',
            '配置诊断刷新失败',
        ),
    ]
    status = _page_status_from_snapshot_keys(
        snapshot_store=snapshot_store,
        snapshot_keys=[key for key, *_ in sources],
        primary_task=instance_task,
        secondary_tasks=[keeper_task, healthcheck_task],
    )
    if any(task is not None and task.status in {'queued', 'running'} for task in (instance_task, keeper_task, healthcheck_task)):
        return status

    latest_ready: tuple[str, Any] | None = None
    latest_failed: tuple[str, Any] | None = None
    for key, ready_message, failed_keep_message, failed_message in sources:
        entry = snapshot_store.get_entry(key)
        if entry is None:
            continue
        if entry.updated_at:
            if latest_ready is None or str(entry.updated_at) >= str(latest_ready[1].updated_at):
                latest_ready = (ready_message, entry)
        if entry.error_message:
            failed_label = failed_keep_message if entry.updated_at else failed_message
            if latest_failed is None:
                latest_failed = (failed_label, entry)
            elif entry.updated_at and (not latest_failed[1].updated_at or str(entry.updated_at) >= str(latest_failed[1].updated_at)):
                latest_failed = (failed_label, entry)

    if latest_failed is not None and (latest_ready is None or str(latest_failed[1].updated_at or '') >= str(latest_ready[1].updated_at or '')):
        return InteractivePageStatus(
            state='failed',
            message=latest_failed[0],
            updated_at=str(latest_failed[1].updated_at or ''),
            error_message=str(latest_failed[1].error_message or ''),
        )
    if latest_ready is not None:
        return InteractivePageStatus(
            state='ready',
            message=latest_ready[0],
            updated_at=str(latest_ready[1].updated_at or ''),
            error_message='',
        )
    return status


def _task_long_running_threshold_seconds(task: InteractiveTaskResult | None) -> float:
    if task is None:
        return 0.0
    if task.task_type == 'login_verify_run':
        return LOGIN_VERIFY_TIMEOUT_SECONDS
    if task.task_type == 'healthcheck_run':
        return HEALTHCHECK_TIMEOUT_SECONDS
    if task.task_type == 'keeper_execute_run':
        return KEEPER_EXECUTE_LONG_RUNNING_SECONDS
    return 0.0


def _task_running_age_seconds(task: InteractiveTaskResult | None) -> int:
    if task is None or not task.started_at:
        return 0
    try:
        started_at = datetime.fromisoformat(task.started_at)
    except ValueError:
        return 0
    return int(max(0.0, (datetime.now().astimezone() - started_at).total_seconds()))


def _page_status_from_tasks(
    *,
    snapshot_store: InteractiveSnapshotStore,
    snapshot_key: str,
    primary_task: InteractiveTaskResult | None = None,
    secondary_tasks: list[InteractiveTaskResult | None] | None = None,
) -> InteractivePageStatus:
    active_task = primary_task
    for task in secondary_tasks or []:
        if task is not None and task.status in {'queued', 'running'}:
            active_task = task
            break
    status = snapshot_store.page_status(snapshot_key, active_task)
    if active_task is not None and active_task.status in {'queued', 'running'}:
        age_seconds = _task_running_age_seconds(active_task)
        threshold = _task_long_running_threshold_seconds(active_task)
        if active_task.status == 'running' and threshold > 0 and age_seconds >= threshold:
            return InteractivePageStatus(
                state=status.state,
                message=f'{active_task.status_message or status.message}（已持续 {age_seconds}s，超时风险）',
                updated_at=status.updated_at,
                error_message=status.error_message,
            )
        if active_task.status_message:
            return InteractivePageStatus(
                state=status.state,
                message=active_task.status_message,
                updated_at=status.updated_at,
                error_message=status.error_message,
            )
    return status


def _page_status_from_task_result(
    task: InteractiveTaskResult | None,
    *,
    success_message: str,
    idle_message: str,
) -> InteractivePageStatus:
    if task is None:
        return InteractivePageStatus(state='idle', message=idle_message)
    if task.status in {'queued', 'running'}:
        return InteractivePageStatus(state='refreshing', message=task.status_message or idle_message)
    if task.status == 'succeeded':
        return InteractivePageStatus(
            state='ready',
            message=success_message,
            updated_at=task.finished_at or task.started_at,
        )
    if task.status == 'failed':
        return InteractivePageStatus(
            state='failed',
            message='执行失败',
            updated_at=task.finished_at or task.started_at,
            error_message=_friendly_resource_error_message(task.error_message),
        )
    return InteractivePageStatus(state='idle', message=idle_message)


def _menu_refresh_revision(
    *,
    snapshot_store: InteractiveSnapshotStore | None = None,
    snapshot_keys: list[str] | None = None,
    task_manager: InteractiveTaskManager | None = None,
    task_keys: list[str] | None = None,
) -> tuple[Any, ...]:
    snapshot_token = tuple(
        snapshot_store.entry_revision(key) for key in (snapshot_keys or [])
    ) if snapshot_store is not None else ()
    task_token = tuple(
        task_manager.task_revision(key) for key in (task_keys or [])
    ) if task_manager is not None else ()
    return (snapshot_token, task_token)


def _dashboard_placeholder_view(
    *,
    settings: Settings,
    store,
    current_account: str | None,
    scheduled_job_status_rows_fn,
) -> dict[str, Any]:
    account_name = current_account or _pick_default_account(settings, None, store) or 'default'
    account = next((item for item in settings.accounts if item.name == account_name), None)
    current_account_row = None
    if isinstance(account, AccountSettings):
        current_account_row = inspect_auth_state(account.to_auth_settings(), store=store, account_name=account.name)
    scheduled_rows = scheduled_job_status_rows_fn(settings, store, account_name=account_name)
    service_state = _service_state_snapshot(store)
    return {
        'runtime_status': read_daemon_status(store),
        'current_account': account_name,
        'current_account_row': current_account_row or {},
        'account_rows': [current_account_row] if current_account_row else [],
        'enabled_accounts': len([item for item in settings.accounts if item.enabled]) if settings.accounts else 1,
        'keeper_enabled': settings.tasks.keeper.enabled,
        'scheduled_enabled': settings.tasks.scheduled_start.enabled,
        'effective_keeper_enabled': get_task_enabled(store, account_name, 'keeper', default_enabled=settings.tasks.keeper.enabled),
        'effective_scheduled_enabled': get_task_enabled(store, account_name, 'scheduled_start', default_enabled=settings.tasks.scheduled_start.enabled),
        'paused_task_count': 0,
        'paused_job_count': sum(1 for row in scheduled_rows if not row.get('enabled')),
        'scheduled_jobs': scheduled_rows,
        'instance_rows': [],
        'recent_history': [],
        'recent_failures': [],
        'failure_account_summary': [],
        'recent_auth_rows': [],
        'candidate_summary': {'job_name': '', 'selected_instance_id': '', 'candidate_count': 0, 'top_reasons': []},
        'keeper_summary': {'pending': 0, 'expiring_soon': 0, 'failed': 0},
        'service_state_label': service_state['label'],
        'service_state_tone': service_state['tone'],
        'service_last_seen_at': service_state['last_seen_at'],
        'service_pid': service_state['pid'],
    }


def _dashboard_snapshot_view(
    *,
    settings: Settings,
    store,
    current_account: str | None,
    scheduled_job_status_rows_fn,
    snapshot_store: InteractiveSnapshotStore,
) -> dict[str, Any]:
    view = _dashboard_placeholder_view(
        settings=settings,
        store=store,
        current_account=current_account,
        scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
    )
    account_name = current_account or 'default'
    account_snapshot = snapshot_store.get_snapshot(_snapshot_key('account_runtime', account_name))
    keeper_rows = snapshot_store.get_snapshot(_snapshot_key('keeper_probe', account_name))
    if isinstance(account_snapshot, dict):
        view['current_account_row'] = {
            'status': account_snapshot.get('auth_status') or view.get('current_account_row', {}).get('status'),
            'auth_source': account_snapshot.get('auth_source') or view.get('current_account_row', {}).get('auth_source'),
            'cached_at_iso': account_snapshot.get('cached_at_iso') or view.get('current_account_row', {}).get('cached_at_iso'),
        }
    if isinstance(keeper_rows, list):
        expiring_cutoff = datetime.now().astimezone() + timedelta(days=7)
        expiring_soon = 0
        failed = 0
        abnormal = 0
        for row in keeper_rows:
            deadline = _parse_iso_datetime(str(row.get('release_deadline') or ''))
            if deadline is not None:
                if deadline.tzinfo is None:
                    deadline = deadline.astimezone()
                if datetime.now().astimezone() <= deadline <= expiring_cutoff:
                    expiring_soon += 1
            if str(row.get('result') or '') in {'skip_missing_shutdown_time', 'skip_missing_instance_id'}:
                abnormal += 1
            if str(row.get('result') or '') in {'keeper_failed_power_on', 'keeper_failed_power_off'}:
                failed += 1
        view['keeper_summary'] = {
            'pending': sum(1 for row in keeper_rows if bool(row.get('eligible'))),
            'not_due': sum(1 for row in keeper_rows if str(row.get('result') or '') == 'skip_not_due'),
            'abnormal': abnormal,
            'expiring_soon': expiring_soon,
            'failed': failed,
        }
    return view


def _service_state_snapshot(store) -> dict[str, Any]:
    runtime_status = read_daemon_status(store) if store is not None else {}
    launch_agent = read_launch_agent_status()
    service_installed = bool(launch_agent.get('installed'))
    service_loaded = bool(launch_agent.get('loaded'))
    daemon_running = bool(runtime_status.get('running'))
    launch_status = read_daemon_launch_status(store) if store is not None else {}
    launch_state = str(launch_status.get('state') or '')
    last_error = str(runtime_status.get('last_error') or '')
    last_seen_raw = str(runtime_status.get('last_seen_at') or '')
    last_seen = _parse_iso_datetime(last_seen_raw)
    heartbeat_age_seconds: float | None = None
    if last_seen is not None:
        heartbeat_age_seconds = max(0.0, (datetime.now().astimezone() - last_seen.astimezone()).total_seconds())
    if not service_installed:
        label, tone = '未安装', 'warn'
    elif launch_state == 'starting':
        label, tone = '启动中', 'info'
    elif service_loaded and daemon_running and heartbeat_age_seconds is not None and heartbeat_age_seconds <= SERVICE_HEARTBEAT_OK_SECONDS:
        label, tone = '运行中', 'ok'
    elif service_loaded and (launch_state == 'fused' or last_error or (heartbeat_age_seconds is not None and heartbeat_age_seconds > SERVICE_HEARTBEAT_OK_SECONDS)):
        label, tone = '状态异常', 'bad'
    elif service_loaded and not daemon_running:
        label, tone = '状态异常', 'bad'
    else:
        label, tone = '已停止', 'warn'
    return {
        'label': label,
        'tone': tone,
        'last_seen_at': runtime_status.get('last_seen_at', ''),
        'pid': runtime_status.get('pid'),
    }


def _append_interactive_service_log(config_path: str, message: str) -> None:
    try:
        append_service_lifecycle_log(config_path, message)
    except Exception:
        logging.getLogger(__name__).exception('写入交互式服务管理日志失败')


def _record_interactive_service_event(
    store,
    *,
    action: str,
    message: str,
    level: str = 'info',
    detail: str = '',
) -> None:
    try:
        store.add_event(
            '',
            'service',
            level,
            message,
            payload={
                'label': DEFAULT_SERVICE_LABEL,
                'action': action,
                'detail': detail,
                'plist_path': '',
            },
        )
    except Exception:
        logging.getLogger(__name__).exception('写入交互式服务事件历史失败')


def _submit_snapshot_task(
    *,
    task_manager: InteractiveTaskManager,
    snapshot_store: InteractiveSnapshotStore,
    task_type: str,
    scope: str,
    snapshot_key: str,
    runner: Callable[[], Any],
    status_message: str,
    replace_queued: bool = True,
) -> None:
    task_manager.submit(
        task_type,
        scope=scope,
        runner=runner,
        status_message=status_message,
        on_success=lambda task_result: _store_snapshot(snapshot_store, snapshot_key, task_result.payload, status_message='最近更新'),
        on_error=lambda task_result: (
            task_manager.record_resource_error(task_result.error_message),
            snapshot_store.record_failure(snapshot_key, _friendly_resource_error_message(task_result.error_message)),
        ),
        replace_queued=replace_queued,
    )


def _show_result_screen(title: str, body: str, *, code: int | None = None) -> None:
    tone = 'info'
    status_label = ''
    if code is not None:
        tone = 'ok' if code == 0 else 'bad'
        status_label = '成功' if code == 0 else '失败'
    body_lines = [line for line in body.splitlines()] or ['无输出。']
    title_block = _boxed_lines(title, [status_label] if status_label else ['结果详情'], tone=tone)
    content_block = _boxed_lines('详情', body_lines[:40], tone='muted')
    _choose_menu('\n'.join(title_block + [''] + content_block), [MenuItem('0', '返回')], default_key='0')


def _render_login_refresh_progress(account_name: str, *, code: int | None, output: str) -> str:
    success = code == 0
    normalized = output.strip()
    result_line = '登录状态已更新'
    if normalized:
        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        if lines:
            result_line = lines[-1]
    progress_lines = [
        _key_value('账号', account_name),
        _key_value('步骤 1', '已读取当前账号配置'),
        _key_value('步骤 2', '已发起登录校验与凭据刷新'),
        _key_value('步骤 3', '已检查刷新后的登录状态' if success else '登录校验失败'),
        _key_value('结果', result_line),
    ]
    return '\n'.join(progress_lines)


def _show_login_refresh_progress(
    *,
    args: argparse.Namespace,
    account_name: str,
    command_login_fn,
    title: str,
    headed_override: bool | None = None,
) -> None:
    code, output = _run_captured_action(
        title,
        lambda: command_login_fn(
            _copy_args(
                args,
                account=account_name,
                all=False,
                **({'headed': headed_override} if headed_override is not None else {}),
            )
        ),
    )
    _show_result_screen(title, _render_login_refresh_progress(account_name, code=code, output=output), code=code)


def _poll_live_action(timeout_seconds: float | None) -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raw = _prompt('回车刷新，输入 q/0 返回: ').strip()
        if raw in {'q', 'Q', '0', 'ESC'}:
            return 'back'
        return 'refresh'
    key = _read_key_with_timeout(timeout_seconds if timeout_seconds is not None else 86400.0)
    if key is None:
        return 'refresh'
    if key in {'q', 'Q', 'ESC'}:
        return 'back'
    if key == 'ENTER':
        return 'refresh'
    return 'stay'


def _scheduled_row_needs_live_refresh(row: dict[str, Any]) -> bool:
    if not row.get('enabled', True):
        return False
    result = str(row.get('latest_result') or '')
    if row.get('schedule_mode') == 'once' and _is_scheduled_once_complete_result(result):
        return False
    if bool(row.get('daemon_running')):
        return True
    if result in {'deadline_failed', 'instance_missing', 'already_running'}:
        return False
    if result in {'waiting_for_gpu', 'waiting_for_instance', 'no_eligible_candidate', 'selector_no_match', 'started', 'power_on_submitted', 'started_without_gpu'}:
        return True
    phase_label, _, _ = _scheduled_window_phase(row)
    return phase_label == '正在轮询候选'


def _freeze_scheduled_live_row(row: dict[str, Any]) -> dict[str, Any]:
    frozen = dict(row)
    stage_label, stage_tone = _scheduled_stage_label(frozen)
    execution_label, execution_tone = _scheduled_execution_status(frozen)
    missing_reason_label, missing_reason_tone = _scheduled_missing_check_reason(frozen)
    poll_text, target_text = _scheduled_window_countdowns(frozen)
    frozen['_live_stage_label'] = stage_label
    frozen['_live_stage_tone'] = stage_tone
    frozen['_live_execution_label'] = execution_label
    frozen['_live_execution_tone'] = execution_tone
    frozen['_live_missing_reason_label'] = missing_reason_label
    frozen['_live_missing_reason_tone'] = missing_reason_tone
    frozen['_live_next_action'] = _scheduled_next_action(frozen)
    frozen['_live_poll_text'] = poll_text
    frozen['_live_target_text'] = target_text
    return frozen


def _prepare_live_scheduled_rows(
    rows: list[dict[str, Any]],
    *,
    previous_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for row in rows:
        prepared.append(row if _scheduled_row_needs_live_refresh(row) else _freeze_scheduled_live_row(row))
    return prepared


def _scheduled_live_footer(rows: list[dict[str, Any]], *, refresh_interval_seconds: float = 3.0) -> tuple[str, float | None]:
    if any(_scheduled_row_needs_live_refresh(row) for row in rows):
        return f'{int(refresh_interval_seconds)}秒轻量自动刷新 / Enter 立即刷新 / q 返回', refresh_interval_seconds
    return '当前无运行中任务 / Enter 手动刷新 / q 返回', None


def _show_live_scheduled_status(
    *,
    job_name: str | None,
    fetch_rows_fn: Callable[[], list[dict[str, Any]]],
    poll_action_fn: Callable[[float | None], str] | None = None,
    refresh_interval_seconds: float = 3.0,
    task_manager: InteractiveTaskManager | None = None,
    snapshot_store: InteractiveSnapshotStore | None = None,
    current_account: str | None = None,
    clear_scope_snapshot_on_exit: bool = False,
    settings: Settings | None = None,
) -> None:
    owns_runtime = False
    if task_manager is None or snapshot_store is None:
        snapshot_store = InteractiveSnapshotStore()
        task_manager = InteractiveTaskManager(snapshot_store=snapshot_store, max_workers=_interactive_max_workers(settings))
        owns_runtime = True
    poll_action = poll_action_fn or _poll_live_action
    account_scope = current_account or 'default'
    scope = f'job:{account_scope}:{job_name}' if job_name else f'all:{account_scope}'
    snapshot_key = _snapshot_key('scheduled_progress', scope)

    def _queue_progress_refresh() -> None:
        _submit_snapshot_task(
            task_manager=task_manager,
            snapshot_store=snapshot_store,
            task_type='scheduled_progress_refresh',
            scope=scope,
            snapshot_key=snapshot_key,
            runner=lambda: _prepare_live_scheduled_rows(fetch_rows_fn()),
            status_message='正在刷新抢机进度',
            replace_queued=True,
        )
        _nudge_background_tasks(task_manager, settle_seconds=0.01)

    def _refresh_progress_now(previous_rows: list[dict[str, Any]] | None = None) -> None:
        refreshed_rows = _prepare_live_scheduled_rows(fetch_rows_fn(), previous_rows=previous_rows)
        _store_snapshot(snapshot_store, snapshot_key, refreshed_rows, status_message='最近更新')

    _refresh_progress_now()
    first_render = True
    try:
        while True:
            task_manager.drain_completed()
            task = task_manager.get_task('scheduled_progress_refresh', scope)
            rows_snapshot = snapshot_store.get_snapshot(snapshot_key)
            rows = list(rows_snapshot) if isinstance(rows_snapshot, list) else []
            status = _page_status_from_tasks(
                snapshot_store=snapshot_store,
                snapshot_key=snapshot_key,
                primary_task=task,
            )
            footer_text, wait_timeout = _scheduled_live_footer(rows, refresh_interval_seconds=refresh_interval_seconds)
            if first_render and wait_timeout is not None and task is None:
                status = InteractivePageStatus(
                    state='refreshing',
                    message='正在刷新抢机进度',
                    updated_at=status.updated_at,
                    error_message=status.error_message,
                )
            first_render = False
            _repaint_screen()
            try:
                body = _render_scheduled_status(
                    job_name,
                    rows,
                    page_status_lines=_page_status_lines(status, active_task=task, progress_label='刷新进度'),
                )
            except TypeError:
                body = _render_scheduled_status(job_name, rows)
            print(body)
            print('')
            print(_section(footer_text))
            action = poll_action(wait_timeout)
            if action == 'back':
                return
            if action == 'refresh':
                _refresh_progress_now(rows)
            elif action == 'stay' and wait_timeout is not None:
                _refresh_progress_now(rows)
    finally:
        if clear_scope_snapshot_on_exit:
            _clear_scheduled_progress_scope_snapshots(
                snapshot_store,
                current_account=current_account,
                job_name=job_name,
            )
        if owns_runtime:
            _nudge_background_tasks(task_manager, settle_seconds=0.01)
            task_manager.shutdown(wait=False)


def _enabled_account_names(settings: Settings) -> list[str]:
    if settings.accounts:
        return [account.name for account in settings.accounts if account.enabled]
    return ['default']


def _ensure_account_payloads(raw_payload: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    accounts_payload = raw_payload.get('accounts')
    if isinstance(accounts_payload, list) and accounts_payload:
        return accounts_payload
    payload_accounts: list[dict[str, Any]] = []
    for account in settings.accounts:
        payload_accounts.append(
            {
                'name': account.name,
                'enabled': account.enabled,
                'authorization': account.authorization,
                'autodl_phone': account.autodl_phone,
                'autodl_password': account.autodl_password,
                'cache_file': account.cache_file,
                'cache_max_age_seconds': account.cache_max_age_seconds,
                'lightweight_mode': account.lightweight_mode,
                'runtime_auth_revalidate_seconds': account.runtime_auth_revalidate_seconds,
                'force_refresh_min_interval_seconds': account.force_refresh_min_interval_seconds,
                'auth_failure_backoff_seconds': account.auth_failure_backoff_seconds,
            }
        )
    if not payload_accounts:
        payload_accounts.append({'name': 'default', 'enabled': True})
    raw_payload['accounts'] = payload_accounts
    return payload_accounts


def _resolve_current_account_slot(settings: Settings, current_account: str | None) -> str:
    if current_account:
        return current_account
    if settings.accounts:
        enabled = [account.name for account in settings.accounts if account.enabled]
        if enabled:
            return enabled[0]
        return settings.accounts[0].name
    return 'default'


def _clear_persisted_auth_state(*, store, account_name: str, cache_file: str) -> None:
    clear_runtime_authorization(account_name)
    if store is not None:
        store.set_auth_cache(account_name, '', 0)
    write_auth_cache(cache_file, '', cached_at=0)


def _persist_account_credentials(
    *,
    config_path: str,
    settings: Settings,
    current_account: str | None,
    mode: str,
    authorization: str = '',
    phone: str = '',
    password: str = '',
    load_settings_fn: Callable[[str], Settings],
    validate_settings_fn: Callable[[Settings, str], list[str]],
    store,
) -> tuple[Settings, str]:
    account_name = _resolve_current_account_slot(settings, current_account)
    raw_payload = read_raw_settings(config_path)
    original_payload = copy.deepcopy(raw_payload)
    accounts_payload = _ensure_account_payloads(raw_payload, settings)
    target_payload = next((item for item in accounts_payload if str(item.get('name') or '') == account_name), None)
    if target_payload is None:
        target_payload = {'name': account_name, 'enabled': True}
        accounts_payload.insert(0, target_payload)
    auth_payload = raw_payload.setdefault('auth', {})
    if mode == 'authorization':
        token = authorization.strip()
        if not token:
            raise ValueError('Authorization 不能为空。')
        target_payload['authorization'] = token
        target_payload['autodl_phone'] = ''
        target_payload['autodl_password'] = ''
        auth_payload['authorization'] = token
        auth_payload['autodl_phone'] = ''
        auth_payload['autodl_password'] = ''
    elif mode == 'password':
        phone = phone.strip()
        password = password.strip()
        if not phone or not password:
            raise ValueError('手机号和密码都不能为空。')
        target_payload['authorization'] = ''
        target_payload['autodl_phone'] = phone
        target_payload['autodl_password'] = password
        auth_payload['authorization'] = ''
        auth_payload['autodl_phone'] = phone
        auth_payload['autodl_password'] = password
    else:
        raise ValueError(f'未知账号切换方式: {mode}')
    target_payload['enabled'] = True
    write_raw_settings(config_path, raw_payload)
    try:
        updated_settings = load_settings_fn(config_path)
        errors = validate_settings_fn(updated_settings, purpose='validate')
        if errors:
            raise ValueError('; '.join(errors))
    except Exception:
        write_raw_settings(config_path, original_payload)
        raise
    account = next((item for item in updated_settings.accounts if item.name == account_name), None)
    auth_settings = account.to_auth_settings() if isinstance(account, AccountSettings) else updated_settings.auth
    _clear_persisted_auth_state(
        store=store,
        account_name=account_name,
        cache_file=auth_settings.cache_file,
    )
    return updated_settings, account_name


def _switch_to_new_account(
    *,
    args: argparse.Namespace,
    settings: Settings,
    store,
    current_account: str | None,
    command_login_fn,
    load_settings_fn: Callable[[str], Settings],
    validate_settings_fn: Callable[[Settings, str], list[str]],
) -> tuple[Settings, str | None]:
    choice = _choose_menu(
        '切换到新账号',
        [
            MenuItem('1', '粘贴 Authorization Token'),
            MenuItem('2', '浏览器登录（手机号+密码）'),
            MenuItem('0', '取消'),
        ],
        default_key='1',
    )
    if choice == '0':
        return settings, current_account
    account_name = _resolve_current_account_slot(settings, current_account)
    try:
        if choice == '1':
            token = _prompt_with_default('Authorization')
            settings, account_name = _persist_account_credentials(
                config_path=args.config,
                settings=settings,
                current_account=account_name,
                mode='authorization',
                authorization=token,
                load_settings_fn=load_settings_fn,
                validate_settings_fn=validate_settings_fn,
                store=store,
            )
        elif choice == '2':
            phone = _prompt_with_default('AutoDL 手机号')
            password = _prompt_with_default('AutoDL 密码')
            settings, account_name = _persist_account_credentials(
                config_path=args.config,
                settings=settings,
                current_account=account_name,
                mode='password',
                phone=phone,
                password=password,
                load_settings_fn=load_settings_fn,
                validate_settings_fn=validate_settings_fn,
                store=store,
            )
        else:
            print('无效选择。')
            return settings, current_account
    except _InteractiveCancel:
        return settings, current_account
    except ValueError as exc:
        _print_execution_summary('切换新账号失败', detail=str(exc))
        return settings, current_account
    _show_login_refresh_progress(
        args=args,
        account_name=account_name,
        command_login_fn=command_login_fn,
        title='切换到新账号',
        headed_override=True if choice == '2' else None,
    )
    return settings, account_name


def _confirm_action(title: str, *lines: str) -> bool:
    _repaint_screen()
    card = _boxed_lines(f'即将执行: {title}', [line for line in lines if line], tone='warn')
    print('\n'.join(card))
    raw = _prompt('确认执行? [Y/n]: ').lower()
    if raw in {'', 'y', 'yes'}:
        return True
    if raw not in {'n', 'no'}:
        return True
    if raw in {'n', 'no'}:
        print('已取消。')
        return False
    return True


def _parse_iso_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _format_human_datetime(raw: str | None) -> str:
    dt = _parse_iso_datetime(raw)
    if dt is None:
        return '-'
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone().strftime('%Y-%m-%d %H:%M:%S')


def _humanize_datetime_text(text: Any) -> str:
    if text is None:
        return ''
    raw = str(text)
    if raw == '':
        return ''

    def _replace(match: re.Match[str]) -> str:
        value = match.group(0)
        formatted = _format_human_datetime(value)
        return formatted if formatted != '-' else value

    return ISO_DATETIME_RE.sub(_replace, raw)


def _account_runtime_snapshot(
    settings: Settings,
    store,
    *,
    account_name: str,
    keeper_probe_rows_fn: Callable[..., list[dict[str, Any]]],
    scheduled_job_status_rows_fn: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    account = next((item for item in settings.accounts if item.name == account_name), None)
    auth_status = '未配置'
    auth_source = '-'
    account_enabled = True
    if account is not None:
        state = inspect_auth_state(account.to_auth_settings(), store=store, account_name=account.name)
        auth_status = str(state.get('status') or '未配置')
        auth_source = str(state.get('auth_source') or '未配置')
        cached_at_iso = str(state.get('cached_at_iso') or '')
        account_enabled = bool(account.enabled)
    else:
        cached_at_iso = ''
    probe_rows = keeper_probe_rows_fn(settings, store, account_name=account_name)
    now = datetime.now().astimezone()
    deadline_cutoff = now + timedelta(days=7)
    running_instances = sum(1 for row in probe_rows if str(row.get('status') or '').lower() == 'running')
    expiring_soon = 0
    for row in probe_rows:
        deadline = _parse_iso_datetime(str(row.get('release_deadline') or ''))
        if deadline is None:
            continue
        if deadline.tzinfo is None:
            deadline = deadline.astimezone()
        if now <= deadline <= deadline_cutoff:
            expiring_soon += 1
    scheduled_rows = scheduled_job_status_rows_fn(settings, store, account_name=account_name)
    paused_jobs = sum(1 for row in scheduled_rows if not bool(row.get('enabled', True)))
    return {
        'account_name': account_name,
        'account_enabled': account_enabled,
        'auth_status': auth_status,
        'auth_source': auth_source,
        'cached_at_iso': cached_at_iso,
        'running_instances': running_instances,
        'expiring_soon': expiring_soon,
        'scheduled_jobs': len(scheduled_rows),
        'paused_jobs': paused_jobs,
        'keeper_enabled': get_task_enabled(store, account_name, 'keeper', default_enabled=settings.tasks.keeper.enabled),
    }


def _login_verify_snapshot(
    *,
    args: argparse.Namespace,
    account_name: str,
    command_login_fn,
    settings: Settings,
    store,
    keeper_probe_rows_fn: Callable[..., list[dict[str, Any]]],
    scheduled_job_status_rows_fn: Callable[..., list[dict[str, Any]]],
    timeout_seconds: float = LOGIN_VERIFY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    result = _run_command_with_timeout(
        command_fn=command_login_fn,
        args=_copy_args(args, account=account_name, headed=False, all=False),
        timeout_seconds=timeout_seconds,
        title='登录状态验证',
        timeout_summary='登录状态验证超时，已终止本次后台验证',
    )
    if not bool(result.get('ok')):
        raise ValueError(str(result.get('summary') or '登录状态验证失败'))
    code = result.get('code')
    if code not in {0, None}:
        raise ValueError(f'登录状态验证失败（code={code}）')
    return _account_runtime_snapshot(
        settings,
        store,
        account_name=account_name,
        keeper_probe_rows_fn=keeper_probe_rows_fn,
        scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
    )


def _print_execution_summary(title: str, *, code: int | None = None, detail: str | None = None) -> None:
    body = _humanize_datetime_text(detail or '无额外信息。')
    _show_result_screen(f'执行完成: {title}', body, code=code)


def _find_scheduled_job(settings: Settings, job_name: str):
    for job in settings.tasks.scheduled_start.jobs:
        if job.name == job_name or job.instance_id == job_name or scheduled_job_identity(job) == job_name:
            return job
    raise ValueError(f'job 不存在: {job_name}')


def _render_keeper_rules(settings: Settings, account_name: str, store) -> str:
    enabled = get_task_enabled(store, account_name, 'keeper', default_enabled=settings.tasks.keeper.enabled)
    keeper = settings.tasks.keeper
    overview = _keeper_probe_overview([])
    schedule_lines = _keeper_probe_schedule_lines(settings, store, account_name=account_name)
    lines = [
        _heading('Keeper 规则确认', color=CYAN),
        _separator(),
        _section('[当前账号]'),
        _key_value('账号', account_name),
        _key_value('Keeper 状态', _tone_chip('运行中', 'ok') if enabled else _tone_chip('已暂停', 'warn')),
        *schedule_lines,
        _key_value('本次应接管', f"{overview['due']} 台"),
        _key_value('未到接管窗口', f"{overview['not_due']} 台"),
        _key_value('状态异常', f"{overview['abnormal']} 台"),
        _key_value('一周内接近释放', f"{overview['expiring']} 台"),
        '',
        _section('[规则详情]'),
        _key_value('最多保留', _format_hours_brief(keeper.shutdown_release_after_hours)),
        _key_value('释放前开始接管', _format_hours_brief(keeper.keeper_trigger_before_hours)),
        _key_value('检查频率', _format_minutes_brief(keeper.interval_minutes)),
    ]
    return '\n'.join(lines)


def _keeper_probe_schedule_lines(settings: Settings, store, *, account_name: str | None) -> list[str]:
    if store is None:
        return [
            _key_value('下次执行时间', '后台未运行'),
            _key_value('上次执行时间', '待首次执行'),
            _key_value('上次执行结果', '暂无结果'),
        ]
    scope = account_name or 'default'
    last_run_raw = str(store.get_runtime_value('last_run:keeper', '') or '').strip()
    last_run = _parse_iso_datetime(last_run_raw)
    last_run_text = _format_human_datetime(last_run_raw) if last_run_raw else '待首次执行'
    daemon_running = bool(read_daemon_status(store).get('running'))
    if not get_task_enabled(store, scope, 'keeper', default_enabled=settings.tasks.keeper.enabled):
        next_text = '未启用'
    elif not daemon_running:
        next_text = '后台未运行'
    elif last_run is None:
        next_text = '待首次执行'
    else:
        next_dt = last_run + timedelta(minutes=max(1, int(settings.tasks.keeper.interval_minutes or 1)))
        next_text = _format_human_datetime(next_dt.isoformat())
    last_result_text = _keeper_last_execution_summary(store, account_name=scope)
    return [
        _key_value('下次执行时间', next_text),
        _key_value('上次执行时间', last_run_text),
        _key_value('上次执行结果', last_result_text),
    ]


def _keeper_last_execution_summary(store, *, account_name: str) -> str:
    if store is None:
        return '暂无结果'
    history_rows = store.read_history(account_name=account_name, task_type='keeper', limit=100)
    if not history_rows:
        return '暂无结果'
    latest_dt = _parse_iso_datetime(str(history_rows[0].created_at or ''))
    if latest_dt is None:
        return '暂无结果'
    latest_payload = history_rows[0].payload if isinstance(history_rows[0].payload, dict) else {}
    latest_batch_id = str(latest_payload.get('batch_id') or '').strip()
    if latest_batch_id:
        batch = [
            row for row in history_rows
            if str((row.payload or {}).get('batch_id') or '').strip() == latest_batch_id
        ]
    else:
        latest_bucket = latest_dt.replace(microsecond=0)
        batch = []
        for row in history_rows:
            row_dt = _parse_iso_datetime(str(row.created_at or ''))
            if row_dt is None:
                continue
            if row_dt.replace(microsecond=0) == latest_bucket:
                batch.append(row)
    if not batch:
        return '暂无结果'
    success = sum(1 for row in batch if str(row.result or '') == 'keeper_executed')
    failed = sum(1 for row in batch if str(row.result or '') in {'keeper_failed_power_on', 'keeper_failed_power_off'})
    skipped = max(0, len(batch) - success - failed)
    return f'已处理 {success} 台 / 跳过 {skipped} 台 / 失败 {failed} 台'


def _keeper_probe_schedule_texts(settings: Settings, store, *, account_name: str | None) -> tuple[str, str]:
    lines = _keeper_probe_schedule_lines(settings, store, account_name=account_name)
    values: list[str] = []
    for line in lines:
        values.append(_strip_ansi(line).split(':', 1)[1].strip() if ':' in _strip_ansi(line) else '暂无结果')
    while len(values) < 2:
        values.append('暂无结果')
    return values[0], values[1]


def _keeper_probe_overview(rows: list[dict[str, Any]]) -> dict[str, int]:
    due = sum(1 for row in rows if bool(row.get('eligible')))
    not_due = sum(1 for row in rows if str(row.get('result') or '') == 'skip_not_due')
    abnormal = sum(1 for row in rows if str(row.get('result') or '') in {'skip_missing_shutdown_time', 'skip_missing_instance_id'})
    expiring = sum(1 for row in rows if _keeper_release_within_days(row, days=7))
    return {
        'due': due,
        'not_due': not_due,
        'abnormal': abnormal,
        'expiring': expiring,
    }


def _keeper_release_within_days(row: dict[str, Any], *, days: int) -> bool:
    deadline = _parse_iso_datetime(str(row.get('release_deadline') or ''))
    if deadline is None:
        return False
    now = datetime.now().astimezone()
    deadline = deadline.astimezone()
    return now <= deadline <= now + timedelta(days=days)


def _render_keeper_probe_page(rows: list[dict[str, Any]], *, page_status_lines: list[str] | None = None) -> str:
    will_run = [row for row in rows if row.get('eligible')]
    abnormal = [row for row in rows if str(row.get('result') or '') in {'skip_missing_shutdown_time', 'skip_missing_instance_id'}]
    lines = [
        _heading('Keeper 检测结果', color=CYAN),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        '',
        _section('[本次将执行]'),
    ])
    if will_run:
        for row in will_run:
            release_text = _format_relative_deadline(str(row.get('release_deadline') or '')) if row.get('release_deadline') else '待确认'
            keeper_text = _format_relative_deadline(str(row.get('next_keeper_time') or '')) if row.get('next_keeper_time') else '现在'
            card_lines = [
                _key_value('实例状态', row.get('status') or '待确认'),
                _key_value('当前阶段', _tone_chip('进入 Keeper 窗口', 'ok')),
                _key_value('下一步动作', '按顺序执行本轮 Keeper'),
                _key_value('距离释放', release_text),
                _key_value('距离接管', keeper_text),
            ]
            lines.extend(_boxed_lines(f"实例 {row['instance_id']}", card_lines, tone='ok'))
            lines.append('')
    else:
        lines.append('暂无需要执行的实例')
    if abnormal:
        lines.extend(['', _section('[状态异常]')])
        for row in abnormal[:10]:
            card_lines = [
                _key_value('实例状态', row.get('status') or '未知'),
                _key_value('当前阶段', _tone_chip(_keeper_result_label(str(row.get('result') or 'skip_missing_shutdown_time')), 'bad')),
                _key_value('下一步动作', _keeper_reason_label(str(row.get('reason') or 'missing_shutdown_time'))),
                _key_value('距离释放', _format_relative_deadline(str(row.get('release_deadline') or '')) if row.get('release_deadline') else '暂无结果'),
                _key_value('距离接管', _format_relative_deadline(str(row.get('next_keeper_time') or '')) if row.get('next_keeper_time') else '暂无结果'),
            ]
            lines.extend(_boxed_lines(f"实例 {row['instance_id']}", card_lines, tone='bad'))
            lines.append('')
    return '\n'.join(lines)


def _normalize_charge_type(value: Any) -> str:
    if isinstance(value, list):
        parts = [_normalize_charge_type(item) for item in value if str(item).strip()]
        return ', '.join(parts) if parts else '-'
    text = str(value or '').strip()
    mapping = {
        'payg': '按量计费',
        'pay_as_you_go': '按量计费',
        'day': '包日计费',
        'package_day': '包日计费',
        'monthly': '包月计费',
        'month': '包月计费',
    }
    return mapping.get(text.lower(), text or '-')


def _normalize_instance_status(value: Any) -> str:
    text = str(value or '').strip().lower()
    mapping = {
        'running': '运行中',
        'on': '运行中',
        'shutdown': '已关机',
        'stopped': '已关机',
        'off': '已关机',
        'booting': '启动中',
        'starting': '启动中',
        'pending': '启动中',
        'stopping': '关机中',
    }
    return mapping.get(text, str(value or '-').strip() or '-')


def _normalize_start_mode(value: Any) -> str:
    text = str(value or '').strip().lower()
    mapping = {
        'gpu': 'GPU 模式',
        'non_gpu': '非 GPU 模式',
        'cpu': '非 GPU 模式',
    }
    return mapping.get(text, str(value or '-').strip() or '-')


def _load_instance_rows_via_command(
    *,
    args: argparse.Namespace,
    current_account: str | None,
    command_list_instances_fn,
) -> list[dict[str, Any]]:
    code, output = _run_captured_action(
        '实例列表(JSON)',
        lambda: command_list_instances_fn(_copy_args(args, account=current_account, headed=False, json=True)),
    )
    if code != 0:
        raise ValueError(output)
    payload = json.loads(output or '[]')
    if not isinstance(payload, list):
        raise ValueError('实例列表返回格式无效。')
    return [item for item in payload if isinstance(item, dict)]


def _render_instance_list_page(account_label: str, rows: list[dict[str, Any]], *, page_status_lines: list[str] | None = None) -> str:
    running = sum(1 for row in rows if str(row.get('status') or '').lower() == 'running')
    shutdown = sum(1 for row in rows if str(row.get('status') or '').lower() == 'shutdown')
    lines = [
        _heading('实例列表', color=CYAN),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        _key_value('查看账号', account_label),
        _key_value('实例总数', len(rows)),
        _key_value('运行中', running),
        _key_value('已关机', shutdown),
        '',
        _section('[选择实例查看详情]'),
    ])
    return '\n'.join(lines)


def _instance_gpu_summary(row: dict[str, Any]) -> str:
    spec = str(row.get('spec') or '').strip()
    match = GPU_SPEC_RE.match(spec)
    if match:
        model = str(match.group('model') or '').strip()
        count = str(match.group('count') or '').strip()
        if model and count:
            return f'{model}×{count}'
    gpu_all_num = str(row.get('gpu_all_num') or '').strip()
    if spec and gpu_all_num.isdigit():
        return f'{spec}×{gpu_all_num}'
    if spec:
        return spec
    if gpu_all_num.isdigit():
        return f'GPU×{gpu_all_num}'
    return 'GPU=-'


def _instance_idle_gpu_summary(row: dict[str, Any]) -> str:
    idle_value = row.get('gpu_idle_num')
    idle = '' if idle_value is None else str(idle_value).strip()
    return f'空闲{idle}' if idle not in {'', '-'} else '空闲-'


def _render_instance_detail(row: dict[str, Any], account_label: str) -> str:
    gpu_total_raw = row.get('gpu_all_num')
    gpu_total = '' if gpu_total_raw is None else str(gpu_total_raw).strip()
    gpu_idle_raw = row.get('gpu_idle_num')
    gpu_idle = '' if gpu_idle_raw is None else str(gpu_idle_raw).strip()
    if gpu_idle not in {'', '-'} and gpu_total not in {'', '-'}:
        idle_summary = f'{gpu_idle} / {gpu_total}'
    elif gpu_idle not in {'', '-'}:
        idle_summary = gpu_idle
    else:
        idle_summary = '-'
    lines = [
        _heading('实例详情', color=CYAN),
        _separator(),
        _key_value('当前账号', account_label),
        _key_value('实例 ID', row.get('instance_id') or '-'),
        _key_value('名称', row.get('name') or '-'),
        _key_value('地区', row.get('region') or '-'),
        _key_value('状态', _normalize_instance_status(row.get('status'))),
        _key_value('机器/规格', row.get('machine_alias') or '-'),
        _key_value('规格', row.get('spec') or row.get('machine_alias') or '-'),
        _key_value('GPU 配置', f'{gpu_total} 卡' if gpu_total not in {'', '-'} else '-'),
        _key_value('空闲 GPU', idle_summary),
        _key_value('启动模式', _normalize_start_mode(row.get('start_mode'))),
        _key_value('计费方式', _normalize_charge_type(row.get('charge_type'))),
        _key_value('最近状态时间', _format_human_datetime(str(row.get('status_at') or ''))),
    ]
    raw_release_at = str(row.get('release_at') or '').strip()
    if raw_release_at:
        lines.append(_key_value('预计释放时间', _format_human_datetime(raw_release_at)))
    return '\n'.join(lines)


def _browse_instance_list(
    *,
    args: argparse.Namespace,
    current_account: str | None,
    settings: Settings,
    command_list_instances_fn,
    task_manager: InteractiveTaskManager | None = None,
    snapshot_store: InteractiveSnapshotStore | None = None,
) -> None:
    owns_runtime = False
    if task_manager is None or snapshot_store is None:
        snapshot_store = InteractiveSnapshotStore()
        task_manager = InteractiveTaskManager(snapshot_store=snapshot_store, max_workers=_interactive_max_workers(settings))
        owns_runtime = True
    account_label = _account_display_name(settings, current_account)
    scope = current_account or 'default'
    snapshot_key = _snapshot_key('instances', scope)
    _submit_snapshot_task(
        task_manager=task_manager,
        snapshot_store=snapshot_store,
        task_type='instances_refresh',
        scope=scope,
        snapshot_key=snapshot_key,
        runner=lambda: _load_instance_rows_via_command(
            args=args,
            current_account=current_account,
            command_list_instances_fn=command_list_instances_fn,
        ),
        status_message='正在刷新实例列表',
    )
    _nudge_background_tasks(task_manager)
    selected_key = '1'

    def _current_rows() -> list[dict[str, Any]]:
        rows = snapshot_store.get_snapshot(snapshot_key)
        return list(rows) if isinstance(rows, list) else []

    def _menu_snapshot(preferred_key: str | None) -> tuple[str, list[MenuItem], str | None]:
        task_manager.drain_completed()
        rows = _current_rows()
        status = snapshot_store.page_status(snapshot_key, task_manager.get_task('instances_refresh', scope))
        items = [
            MenuItem(
                str(index),
                f"{row.get('name') or '-'} / {row.get('region') or '-'} / {_normalize_instance_status(row.get('status'))} / {_instance_gpu_summary(row)} / {_instance_idle_gpu_summary(row)} / {row.get('machine_alias') or '-'}",
            )
            for index, row in enumerate(rows, start=1)
        ]
        if not items:
            items.append(MenuItem('r', '实例列表加载中…'))
        items.append(MenuItem('0', '返回诊断'))
        title = _render_instance_list_page(account_label, rows, page_status_lines=_page_status_lines(status))
        keep_key = preferred_key if preferred_key and any(item.key == preferred_key for item in items) else _menu_default_key(items, '1')
        return title, items, keep_key

    try:
        while True:
            title, items, default_key = _menu_snapshot(selected_key)
            choice = _choose_menu_with_refresh(
                title,
                items,
                default_key=default_key,
                refresh_fn=lambda preferred_key: _menu_snapshot(preferred_key),
                refresh_revision_fn=lambda: _menu_refresh_revision(
                    snapshot_store=snapshot_store,
                    snapshot_keys=[snapshot_key],
                    task_manager=task_manager,
                    task_keys=[task_manager.task_key('instances_refresh', scope)],
                ),
                refresh_interval_seconds=1.0,
                on_rendered_fn=task_manager.start_pending,
                refresh_policy='always',
                pre_refresh_fn=task_manager.drain_completed,
            )
            if choice == '0':
                return
            if choice == 'r':
                continue
            if not choice.isdigit():
                continue
            selected_key = choice
            rows = _current_rows()
            if not rows or int(choice) - 1 >= len(rows):
                continue
            row = rows[int(choice) - 1]
            _show_result_screen('实例详情', _render_instance_detail(row, account_label))
    finally:
        if owns_runtime:
            _nudge_background_tasks(task_manager, settle_seconds=0.01)
            task_manager.shutdown(wait=False)


def _render_keeper_probe_list_page(account_label: str, rows: list[dict[str, Any]], *, page_status_lines: list[str] | None = None) -> str:
    eligible = sum(1 for row in rows if bool(row.get('eligible')))
    lines = [
        _heading('Keeper 检测', color=CYAN),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        _key_value('查看账号', account_label),
        _key_value('实例总数', len(rows)),
        _key_value('本次可执行', eligible),
        '',
        _section('[选择实例查看详情]'),
    ])
    return '\n'.join(lines)


def _render_keeper_probe_detail(row: dict[str, Any], account_label: str) -> str:
    release_text = _format_relative_deadline(str(row.get('release_deadline') or '')) if row.get('release_deadline') else '暂无结果'
    keeper_text = _format_relative_deadline(str(row.get('next_keeper_time') or '')) if row.get('next_keeper_time') else '暂无结果'
    lines = [
        _heading('Keeper 检测详情', color=CYAN),
        _separator(),
        _key_value('当前账号', account_label),
        _key_value('下次执行时间', row.get('_keeper_next_run_text') or '暂无结果'),
        _key_value('上次执行时间', row.get('_keeper_last_run_text') or '暂无结果'),
        _key_value('实例 ID', row.get('instance_id') or '未设置'),
        _key_value('当前状态', row.get('status') or '未知'),
        _key_value('当前结论', _keeper_result_label(str(row.get('result') or '暂无结果'))),
        _key_value('下一步动作', _keeper_reason_label(str(row.get('reason') or '暂无结果'))),
        _key_value('距离释放', release_text),
        _key_value('距离下次 Keeper', keeper_text),
        _key_value('最近关机时间', _format_human_datetime(str(row.get('stopped_at') or '')) if row.get('stopped_at') else '暂无结果'),
    ]
    if row.get('executed_in_cycle'):
        lines.append(_key_value('本周期状态', '已执行过'))
    return '\n'.join(lines)


def _diagnostics_snapshot_payload(
    *,
    snapshot_store: InteractiveSnapshotStore,
    account_name: str,
    task_manager: InteractiveTaskManager | None = None,
    store=None,
) -> dict[str, Any]:
    instance_rows = snapshot_store.get_snapshot(_snapshot_key('instances', account_name))
    keeper_rows = snapshot_store.get_snapshot(_snapshot_key('keeper_probe', account_name))
    healthcheck_snapshot = snapshot_store.get_snapshot(_snapshot_key('healthcheck', account_name))
    config_snapshot = snapshot_store.get_snapshot(_snapshot_key('config_diagnostics', account_name))
    instances = list(instance_rows) if isinstance(instance_rows, list) else []
    keeper = list(keeper_rows) if isinstance(keeper_rows, list) else []
    health = healthcheck_snapshot if isinstance(healthcheck_snapshot, dict) else {}
    config_diag = config_snapshot if isinstance(config_snapshot, dict) else {}
    runtime_stats = task_manager.runtime_stats() if task_manager is not None else {}
    circuit_state = task_manager.circuit_state() if task_manager is not None else {}
    daemon_launch = read_daemon_launch_status(store) if store is not None else {}
    launch_agent = read_launch_agent_status() if store is not None else {}
    reload_status = read_config_reload_status(store) if store is not None else {}
    daemon_status = read_daemon_status(store) if store is not None else {}
    return {
        'instance_total': len(instances),
        'instance_running': sum(1 for row in instances if str(row.get('status') or '').lower() == 'running'),
        'instance_shutdown': sum(1 for row in instances if str(row.get('status') or '').lower() == 'shutdown'),
        'keeper_total': len(keeper),
        'keeper_eligible': sum(1 for row in keeper if bool(row.get('eligible'))),
        'healthcheck_status': str(health.get('status') or '尚未执行'),
        'healthcheck_summary': str(health.get('summary') or '暂无结果'),
        'config_status': str(config_diag.get('status') or '尚未执行'),
        'config_summary': str(config_diag.get('summary') or '暂无结果'),
        'fd_current': runtime_stats.get('fd_current'),
        'fd_soft_limit': runtime_stats.get('fd_soft_limit'),
        'fd_usage_percent': runtime_stats.get('fd_usage_percent'),
        'interactive_workers_max': runtime_stats.get('max_workers') or 0,
        'interactive_running_count': runtime_stats.get('running_count') or 0,
        'interactive_queued_count': runtime_stats.get('queued_count') or 0,
        'interactive_running_by_type': dict(runtime_stats.get('running_by_type') or {}),
        'daemon_launch_state': str(daemon_launch.get('launch_state') or 'idle'),
        'daemon_pid': daemon_launch.get('launch_pid'),
        'daemon_error_count': int(daemon_launch.get('launch_error_count') or 0),
        'daemon_last_error': str(daemon_launch.get('launch_last_error') or ''),
        'daemon_fused_until': str(daemon_launch.get('launch_fused_until') or ''),
        'daemon_running': bool(daemon_status.get('running', False)),
        'daemon_last_seen_at': str(daemon_status.get('last_seen_at') or ''),
        'interactive_circuit_open': bool(circuit_state.get('circuit_open', False)),
        'interactive_circuit_reason': str(circuit_state.get('circuit_reason') or ''),
        'interactive_circuit_until': str(circuit_state.get('circuit_until') or ''),
        'service_installed': bool(launch_agent.get('installed', False)),
        'service_loaded': bool(launch_agent.get('loaded', False)),
        'service_label': str(launch_agent.get('label') or '未安装'),
        'reload_status': str(reload_status.get('last_reload_status') or '尚未执行'),
        'reload_error': str(reload_status.get('last_reload_error') or ''),
    }


def _render_diagnostics_page(
    account_label: str,
    snapshot: dict[str, Any] | None,
    *,
    page_status_lines: list[str] | None = None,
) -> str:
    default_data = {
        'instance_total': 0,
        'instance_running': 0,
        'instance_shutdown': 0,
        'keeper_total': 0,
        'keeper_eligible': 0,
        'healthcheck_status': '尚未执行',
        'healthcheck_summary': '暂无结果',
        'config_status': '尚未执行',
        'config_summary': '暂无结果',
        'fd_current': '未知',
        'fd_soft_limit': '未知',
        'fd_usage_percent': 0.0,
        'interactive_workers_max': 0,
        'interactive_running_count': 0,
        'interactive_queued_count': 0,
        'interactive_running_by_type': {},
        'daemon_launch_state': 'idle',
        'daemon_pid': None,
        'daemon_error_count': 0,
        'daemon_last_error': '',
        'daemon_fused_until': '',
        'daemon_running': False,
        'daemon_last_seen_at': '',
        'interactive_circuit_open': False,
        'interactive_circuit_reason': '',
        'interactive_circuit_until': '',
        'service_installed': False,
        'service_loaded': False,
        'service_label': '未安装',
        'reload_status': '尚未执行',
        'reload_error': '',
    }
    data = {**default_data, **(snapshot or {})}
    heartbeat_age_seconds: float | None = None
    if data.get('daemon_last_seen_at'):
        heartbeat_dt = _parse_iso_datetime(str(data.get('daemon_last_seen_at') or ''))
        if heartbeat_dt is not None:
            heartbeat_age_seconds = max(0.0, (datetime.now().astimezone() - heartbeat_dt.astimezone()).total_seconds())
    if not data['service_installed']:
        service_state = '未安装'
    elif data['daemon_launch_state'] == 'starting':
        service_state = '启动中'
    elif data['service_loaded'] and data['daemon_running'] and heartbeat_age_seconds is not None and heartbeat_age_seconds <= SERVICE_HEARTBEAT_OK_SECONDS:
        service_state = '运行中'
    elif data['service_loaded'] and (data['daemon_last_error'] or data['daemon_launch_state'] == 'fused' or (heartbeat_age_seconds is not None and heartbeat_age_seconds > SERVICE_HEARTBEAT_OK_SECONDS)):
        service_state = '状态异常'
    elif data['service_loaded'] and not data['daemon_running']:
        service_state = '状态异常'
    else:
        service_state = '已停止'
    lines = [
        _heading('诊断', color=CYAN),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        _key_value('查看账号', account_label),
        '',
    ])
    left_column = [
        _section('[实例摘要]'),
        _key_value('实例总数', data['instance_total']),
        _key_value('运行中', data['instance_running']),
        _key_value('已关机', data['instance_shutdown']),
        '',
        _section('[Keeper 摘要]'),
        _key_value('实例总数', data['keeper_total']),
        _key_value('本次可执行', data['keeper_eligible']),
        '',
        _section('[最近检查]'),
        _key_value('健康自检', data['healthcheck_status']),
        _key_value('配置诊断', data['config_status']),
    ]
    right_column = [
        _section('[后台服务]'),
        _key_value('服务状态', service_state),
        _key_value('服务标签', data['service_label'] or '未安装'),
        _key_value('最近心跳', _format_human_datetime(data['daemon_last_seen_at']) if data.get('daemon_last_seen_at') else '暂无结果'),
        _key_value('服务说明', '后台运行正常' if service_state == '运行中' else ('后台正在启动，请稍后刷新' if service_state == '启动中' else ('最近心跳延迟或超时，建议重启' if service_state == '状态异常' else '可去诊断页启动或重启服务'))),
        '',
        _section('[任务状态]'),
        _key_value('交互任务池', f"运行中 {data['interactive_running_count']} / 排队 {data['interactive_queued_count']} / 并发上限 {data['interactive_workers_max']}"),
        _key_value(
            '交互轮询任务',
            '当前空闲'
            if str(data['daemon_launch_state'] or '') == 'idle' and not data.get('daemon_pid')
            else f"{data['daemon_launch_state']} / pid={data['daemon_pid'] or '未运行'}",
        ),
        _key_value('最近错误', data['daemon_last_error'] or '暂无错误'),
        '',
        _section('[热重载状态]'),
        _key_value('配置热重载', data['reload_status'] or '尚未执行'),
    ]
    lines.extend(_render_two_columns(left_column, right_column))
    if data.get('reload_error'):
        lines.append(_key_value('重载错误', data['reload_error']))
    return '\n'.join(lines)


def _healthcheck_snapshot_payload(*, code: int | None, output: str) -> dict[str, Any]:
    lines = [line.strip() for line in str(output or '').splitlines() if line.strip()]
    summary = lines[-1] if lines else ('健康自检成功' if code in {0, None} else '健康自检失败')
    return {
        'status': '成功' if code in {0, None} else '失败',
        'summary': summary,
        'code': 0 if code is None else int(code),
        'body': output or '无输出。',
    }


def _healthcheck_snapshot_payload_from_result(result: dict[str, Any]) -> dict[str, Any]:
    if result.get('timed_out'):
        return {
            'status': '超时',
            'summary': _truncate_text(result.get('summary') or '健康自检超时'),
            'code': result.get('code', 124),
            'body': _truncate_text(result.get('summary') or '健康自检超时，已终止本次检查', limit=SNAPSHOT_BODY_LIMIT),
        }
    if result.get('long_running'):
        return {
            'status': '耗时较长',
            'summary': _truncate_text(result.get('summary') or '健康自检耗时较长，但已完成'),
            'code': result.get('code', 0),
            'body': _truncate_text(result.get('output') or '无输出。', limit=SNAPSHOT_BODY_LIMIT),
        }
    return _healthcheck_snapshot_payload(
        code=result.get('code'),
        output=str(result.get('output') or ''),
    )


def _render_healthcheck_detail(snapshot: dict[str, Any] | None, *, page_status_lines: list[str] | None = None) -> str:
    data = snapshot or {'status': '未执行', 'summary': '首次加载中', 'body': '尚未执行健康检查。'}
    lines = [
        _heading('健康自检', color=CYAN),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        _key_value('最近状态', data.get('status') or '未执行'),
        _key_value('结果摘要', data.get('summary') or '-'),
        '',
        _section('[详情]'),
        str(data.get('body') or '无输出。'),
    ])
    return '\n'.join(lines)


def _browse_keeper_probe(
    *,
    settings: Settings,
    store,
    current_account: str | None,
    keeper_probe_rows_fn,
    task_manager: InteractiveTaskManager | None = None,
    snapshot_store: InteractiveSnapshotStore | None = None,
) -> None:
    owns_runtime = False
    if task_manager is None or snapshot_store is None:
        snapshot_store = InteractiveSnapshotStore()
        task_manager = InteractiveTaskManager(snapshot_store=snapshot_store, max_workers=_interactive_max_workers(settings))
        owns_runtime = True
    account_label = _account_display_name(settings, current_account)
    scope = current_account or 'default'
    snapshot_key = _snapshot_key('keeper_probe', scope)
    _submit_snapshot_task(
        task_manager=task_manager,
        snapshot_store=snapshot_store,
        task_type='keeper_probe_refresh',
        scope=scope,
        snapshot_key=snapshot_key,
        runner=lambda: keeper_probe_rows_fn(settings, store, account_name=current_account),
        status_message='正在刷新 Keeper 探测',
    )
    _nudge_background_tasks(task_manager)
    selected_key = '1'

    def _current_rows() -> list[dict[str, Any]]:
        rows = snapshot_store.get_snapshot(snapshot_key)
        return list(rows) if isinstance(rows, list) else []

    def _menu_snapshot(preferred_key: str | None) -> tuple[str, list[MenuItem], str | None]:
        task_manager.drain_completed()
        rows = _current_rows()
        probe_task = task_manager.get_task('keeper_probe_refresh', scope)
        status = _page_status_from_tasks(
            snapshot_store=snapshot_store,
            snapshot_key=snapshot_key,
            primary_task=probe_task,
        )
        items = [
            MenuItem(
                str(index),
                f"{row.get('instance_id') or '-'} / {_keeper_result_label(str(row.get('result') or '-'))}",
            )
            for index, row in enumerate(rows, start=1)
        ]
        if not items:
            items.append(MenuItem('r', 'Keeper 探测加载中…'))
        items.append(MenuItem('0', '返回诊断'))
        status_lines = _page_status_lines(
            status,
            active_task=probe_task,
            progress_label='检测进度',
            show_progress=False,
        )
        if rows:
            status_lines = [*status_lines, *_keeper_probe_schedule_lines(settings, store, account_name=current_account)]
        title = _render_keeper_probe_list_page(
            account_label,
            rows,
            page_status_lines=status_lines,
        )
        keep_key = preferred_key if preferred_key and any(item.key == preferred_key for item in items) else _menu_default_key(items, '1')
        return title, items, keep_key

    try:
        while True:
            title, items, default_key = _menu_snapshot(selected_key)
            choice = _choose_menu_with_refresh(
                title,
                items,
                default_key=default_key,
                refresh_fn=lambda preferred_key: _menu_snapshot(preferred_key),
                refresh_revision_fn=lambda: _menu_refresh_revision(
                    snapshot_store=snapshot_store,
                    snapshot_keys=[snapshot_key],
                    task_manager=task_manager,
                    task_keys=[task_manager.task_key('keeper_probe_refresh', scope)],
                ),
                refresh_interval_seconds=1.0,
                on_rendered_fn=task_manager.start_pending,
                refresh_policy='always',
                pre_refresh_fn=task_manager.drain_completed,
            )
            if choice == '0':
                return
            if choice == 'r':
                continue
            if not choice.isdigit():
                continue
            selected_key = choice
            rows = _current_rows()
            if not rows or int(choice) - 1 >= len(rows):
                continue
            row = rows[int(choice) - 1]
            _show_result_screen('Keeper 检测详情', _render_keeper_probe_detail(row, account_label))
    finally:
        if owns_runtime:
            _nudge_background_tasks(task_manager, settle_seconds=0.01)
            task_manager.shutdown(wait=False)


def _stable_keeper_payload(settings: Settings, raw_payload: dict[str, Any]) -> dict[str, Any]:
    current = copy.deepcopy(((raw_payload.get('tasks') or {}).get('keeper') or {}))
    current.update(
        {
            'enabled': settings.tasks.keeper.enabled,
            'shutdown_release_after_hours': settings.tasks.keeper.shutdown_release_after_hours,
            'keeper_trigger_before_hours': settings.tasks.keeper.keeper_trigger_before_hours,
            'interval_minutes': settings.tasks.keeper.interval_minutes,
            'power_on_wait_seconds': settings.tasks.keeper.power_on_wait_seconds,
            'power_off_wait_seconds': settings.tasks.keeper.power_off_wait_seconds,
            'start_cooldown_minutes': settings.tasks.keeper.start_cooldown_minutes,
            'stop_cooldown_minutes': settings.tasks.keeper.stop_cooldown_minutes,
            'fallback_to_status_at': settings.tasks.keeper.fallback_to_status_at,
        }
    )
    return current


def _stable_scheduled_payload(settings: Settings, raw_payload: dict[str, Any]) -> dict[str, Any]:
    current = copy.deepcopy(((raw_payload.get('tasks') or {}).get('scheduled_start') or {}))
    current.update(
        {
            'enabled': settings.tasks.scheduled_start.enabled,
            'poll_interval_seconds': settings.tasks.scheduled_start.poll_interval_seconds,
            'jobs': [_job_to_payload(job) for job in settings.tasks.scheduled_start.jobs],
        }
    )
    return current


def _normalized_stable_tasks_payload_from_raw(raw_payload: dict[str, Any]) -> dict[str, Any]:
    tasks_payload = raw_payload.get('tasks') or {}
    keeper_payload = copy.deepcopy(tasks_payload.get('keeper') or {})
    scheduled_payload = copy.deepcopy(tasks_payload.get('scheduled_start') or {})
    jobs = []
    for item in scheduled_payload.get('jobs') or []:
        if not isinstance(item, dict):
            continue
        normalized_job = {
            'instance_id': item.get('instance_id', ''),
            'name': item.get('name', ''),
            'target_time': item.get('target_time', '14:00'),
            'advance_hours': item.get('advance_hours', 2),
            'schedule_mode': str(item.get('schedule_mode', 'daily') or 'daily'),
            'timezone': item.get('timezone', 'Asia/Shanghai') or 'Asia/Shanghai',
        }
        selector_payload = item.get('selector')
        if isinstance(selector_payload, dict):
            normalized_job['selector'] = {
                'regions': list(selector_payload.get('regions') or []),
                'gpu_model': selector_payload.get('gpu_model', ''),
                'gpu_count': selector_payload.get('gpu_count', 1),
                'charge_types': list(selector_payload.get('charge_types') or []),
            }
        jobs.append(normalized_job)
    return {
        'keeper': {
            'enabled': keeper_payload.get('enabled', True),
            'shutdown_release_after_hours': keeper_payload.get('shutdown_release_after_hours', 360),
            'keeper_trigger_before_hours': keeper_payload.get('keeper_trigger_before_hours', 6),
            'interval_minutes': keeper_payload.get('interval_minutes', 60),
            'power_on_wait_seconds': keeper_payload.get('power_on_wait_seconds', 60),
            'power_off_wait_seconds': keeper_payload.get('power_off_wait_seconds', 5),
            'start_cooldown_minutes': keeper_payload.get('start_cooldown_minutes', 60),
            'stop_cooldown_minutes': keeper_payload.get('stop_cooldown_minutes', 360),
            'fallback_to_status_at': keeper_payload.get('fallback_to_status_at', True),
        },
        'scheduled_start': {
            'enabled': scheduled_payload.get('enabled', False),
            'poll_interval_seconds': scheduled_payload.get('poll_interval_seconds', 300),
            'jobs': jobs,
        },
    }


def _normalized_stable_tasks_payload_from_settings(settings: Settings, raw_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        'keeper': _stable_keeper_payload(settings, raw_payload),
        'scheduled_start': _stable_scheduled_payload(settings, raw_payload),
    }


def _diff_structures(before: Any, after: Any, *, prefix: str = '') -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        keys = sorted(set(before) | set(after))
        diffs: list[str] = []
        for key in keys:
            next_prefix = f'{prefix}.{key}' if prefix else str(key)
            diffs.extend(_diff_structures(before.get(key), after.get(key), prefix=next_prefix))
        return diffs
    if isinstance(before, list) and isinstance(after, list):
        diffs: list[str] = []
        length = max(len(before), len(after))
        for index in range(length):
            next_prefix = f'{prefix}[{index}]'
            before_item = before[index] if index < len(before) else None
            after_item = after[index] if index < len(after) else None
            diffs.extend(_diff_structures(before_item, after_item, prefix=next_prefix))
        return diffs
    if before != after:
        return [prefix]
    return []


def _collect_legacy_config_fields(raw_payload: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    jobs = (((raw_payload.get('tasks') or {}).get('scheduled_start') or {}).get('jobs') or [])
    for index, item in enumerate(jobs, start=1):
        if isinstance(item, dict) and item.get('priority'):
            label = item.get('name') or item.get('instance_id') or f'job-{index}'
            fields.append(f'{label}.priority')
    return fields


def _sync_stable_config_payload(
    *,
    config_path: str,
    raw_payload: dict[str, Any],
    settings: Settings,
    load_settings_fn: Callable[[str], Settings],
    validate_settings_fn: Callable[[Settings, str], list[str]],
) -> tuple[list[str], list[str]]:
    before = _normalized_stable_tasks_payload_from_raw(raw_payload)
    after = _normalized_stable_tasks_payload_from_settings(settings, raw_payload)
    diff_paths = _diff_structures(before, after, prefix='tasks')
    legacy_fields = _collect_legacy_config_fields(raw_payload)
    if not diff_paths and not legacy_fields:
        return [], legacy_fields
    original_payload = copy.deepcopy(raw_payload)
    tasks_payload = raw_payload.setdefault('tasks', {})
    tasks_payload['keeper'] = _stable_keeper_payload(settings, raw_payload)
    tasks_payload['scheduled_start'] = _stable_scheduled_payload(settings, raw_payload)
    write_raw_settings(config_path, raw_payload)
    updated_settings = load_settings_fn(config_path)
    errors = validate_settings_fn(updated_settings, purpose='validate')
    if errors:
        write_raw_settings(config_path, original_payload)
        raise ValueError('配置同步失败: ' + '; '.join(errors))
    return diff_paths, legacy_fields


def _runtime_override_rows() -> list[str]:
    mapping = [
        ('Authorization', 'auth.authorization'),
        ('AUTODL_PHONE', 'auth.autodl_phone'),
        ('AUTODL_PASSWORD', 'auth.autodl_password'),
        ('AUTODL_AUTH_CACHE_FILE', 'auth.cache_file'),
        ('AUTODL_LOGIN_RETRIES', 'auth.login_retries'),
        ('AUTODL_LOGIN_TIMEOUT_MS', 'auth.login_timeout_ms'),
        ('AUTODL_POST_LOGIN_WAIT_SECONDS', 'auth.post_login_wait_seconds'),
        ('AUTODL_AUTH_CACHE_MAX_AGE_SECONDS', 'auth.cache_max_age_seconds'),
        ('AUTODL_DB_PATH', 'storage.database_file'),
        ('MIN_DAY', 'tasks.keeper.min_day'),
        ('PUSHPLUS_TOKEN', 'notifications.pushplus.token'),
        ('SERVERCHAN_SENDKEY', 'notifications.serverchan.token'),
        ('SMTP_HOST', 'notifications.email.smtp_host'),
        ('SMTP_PORT', 'notifications.email.smtp_port'),
        ('SMTP_USERNAME', 'notifications.email.username'),
        ('SMTP_PASSWORD', 'notifications.email.password'),
    ]
    rows = []
    for env_name, config_key in mapping:
        if os.getenv(env_name, '').strip():
            rows.append(f'{config_key} ← 环境变量/.env ({env_name})')
    return rows


def _render_config_diagnostics(
    *,
    settings: Settings,
    current_account: str | None,
    config_path: str,
    load_settings_fn: Callable[[str], Settings],
    validate_settings_fn: Callable[[Settings, str], list[str]],
) -> str:
    raw_payload = read_raw_settings(config_path)
    diff_paths, legacy_fields = _sync_stable_config_payload(
        config_path=config_path,
        raw_payload=raw_payload,
        settings=settings,
        load_settings_fn=load_settings_fn,
        validate_settings_fn=validate_settings_fn,
    )
    override_rows = _runtime_override_rows()
    lines = [
        _heading('配置诊断', color=CYAN),
        _separator(),
        _key_value('当前账号', _account_display_name(settings, current_account)),
        _key_value('稳定配置同步', '已同步' if diff_paths or legacy_fields else '无需同步'),
        '',
        _section('[同步结果]'),
    ]
    if diff_paths:
        lines.append(_key_value('已同步字段数', len(diff_paths)))
        for path in diff_paths[:10]:
            lines.append(f'- {path}')
    else:
        lines.append('- 没有需要同步的稳定字段')
    if legacy_fields:
        lines.append('')
        lines.append(_section('[历史字段]'))
        for field in legacy_fields:
            lines.append(f'- {field}（已清理历史 priority 字段）')
    else:
        lines.append('')
        lines.append(_section('[历史字段]'))
        lines.append('- 没有历史字段')
    lines.append('')
    lines.append(_section('[运行时覆盖]'))
    if override_rows:
        for row in override_rows:
            lines.append(f'- {row}')
    else:
        lines.append('- 当前没有环境变量/.env 覆盖')
    lines.extend([
        '',
        _section('[说明]'),
        '- 只会自动同步 YAML 可持久化的稳定字段。',
        '- 环境变量/.env、敏感字段和 CLI 临时覆盖不会写回配置文件。',
    ])
    return '\n'.join(lines)


def _run_healthcheck_diagnostics(
    *,
    args: argparse.Namespace,
    current_account: str | None,
    settings: Settings,
    command_healthcheck_fn,
) -> None:
    code, output = _run_captured_action(
        '健康自检',
        lambda: command_healthcheck_fn(_copy_args(args, account=current_account, smoke=True)),
    )
    lines = [
        _heading('健康自检', color=CYAN),
        _separator(),
        _key_value('查看账号', _account_display_name(settings, current_account)),
        _key_value('检查范围', '配置解析 / 认证状态 / 本地存储 / AutoDL 连通性'),
        _key_value('检查结果', _tone_chip('通过' if code == 0 else '失败', 'ok' if code == 0 else 'bad')),
        '',
        _section('[详情]'),
    ]
    if code == 0:
        lines.extend([
            '- 配置可读且可解析',
            '- 当前账号认证状态可判定',
            '- 本地存储与 SQLite 可访问',
            '- AutoDL 登录与实例查询可执行',
        ])
    else:
        error_lines = [line for line in output.splitlines() if line.strip()]
        lines.extend(error_lines[:20] or ['- 自检失败，但没有返回详细信息'])
    _show_result_screen('健康自检', '\n'.join(lines), code=code)


def _browse_healthcheck_detail(
    *,
    args: argparse.Namespace,
    current_account: str | None,
    command_healthcheck_fn,
    task_manager: InteractiveTaskManager,
    snapshot_store: InteractiveSnapshotStore,
    timeout_seconds: float = HEALTHCHECK_TIMEOUT_SECONDS,
) -> None:
    account_scope = current_account or 'default'
    snapshot_key = _snapshot_key('healthcheck', account_scope)

    def _queue_healthcheck() -> None:
        task_manager.submit(
            'healthcheck_run',
            scope=account_scope,
            runner=lambda: _run_command_with_timeout(
                command_fn=command_healthcheck_fn,
                args=_copy_args(args, account=current_account, smoke=True),
                timeout_seconds=timeout_seconds,
                title='健康自检',
                timeout_summary='健康自检超时，已终止本次检查',
            ),
            status_message='正在执行健康自检',
            on_success=lambda task_result: _store_snapshot(
                snapshot_store,
                snapshot_key,
                _healthcheck_snapshot_payload_from_result(task_result.payload if isinstance(task_result.payload, dict) else {}),
                status_message='最近更新',
            ),
            on_error=lambda task_result: (
                task_manager.record_resource_error(task_result.error_message),
                snapshot_store.record_failure(snapshot_key, _friendly_resource_error_message(task_result.error_message)),
            ),
            replace_queued=True,
        )
        task_manager.start_pending()

    selected_key = '1'
    while True:
        task_manager.drain_completed()
        healthcheck_task = task_manager.get_task('healthcheck_run', account_scope)
        status = _page_status_from_tasks(
            snapshot_store=snapshot_store,
            snapshot_key=snapshot_key,
            primary_task=healthcheck_task,
        )
        payload = snapshot_store.get_snapshot(snapshot_key)
        items = [MenuItem('1', '重新运行检查'), MenuItem('0', '返回诊断')]
        action = _choose_menu_with_refresh(
            _render_healthcheck_detail(
                payload if isinstance(payload, dict) else None,
                page_status_lines=_page_status_lines(
                    status,
                    active_task=healthcheck_task,
                    progress_label='检查进度',
                    show_progress=False,
                ),
            ),
            items,
            default_key=_menu_default_key(items, selected_key),
            refresh_fn=lambda preferred_key: (
                _render_healthcheck_detail(
                    snapshot_store.get_snapshot(snapshot_key) if isinstance(snapshot_store.get_snapshot(snapshot_key), dict) else None,
                    page_status_lines=_page_status_lines(
                        _page_status_from_tasks(
                            snapshot_store=snapshot_store,
                            snapshot_key=snapshot_key,
                            primary_task=task_manager.get_task('healthcheck_run', account_scope),
                        ),
                        active_task=task_manager.get_task('healthcheck_run', account_scope),
                        progress_label='检查进度',
                        show_progress=False,
                    ),
                ),
                items,
                preferred_key or selected_key,
            ),
            refresh_revision_fn=lambda: _menu_refresh_revision(
                snapshot_store=snapshot_store,
                snapshot_keys=[snapshot_key],
                task_manager=task_manager,
                task_keys=[task_manager.task_key('healthcheck_run', account_scope)],
            ),
            refresh_interval_seconds=1.0,
            on_rendered_fn=task_manager.start_pending,
            refresh_policy='always',
            pre_refresh_fn=task_manager.drain_completed,
        )
        selected_key = action
        if action == '1':
            _queue_healthcheck()
        elif action == '0':
            return


def _render_keeper_execution_page(
    results: list[Any],
    *,
    page_status_lines: list[str] | None = None,
) -> str:
    executed = [item for item in results if getattr(item, 'result', '') == 'keeper_executed']
    failed = [item for item in results if getattr(item, 'result', '') in {'keeper_failed_power_on', 'keeper_failed_power_off'}]
    skipped = [item for item in results if getattr(item, 'result', '') not in {'keeper_executed', 'keeper_failed_power_on', 'keeper_failed_power_off'}]
    lines = [
        _heading('Keeper 执行结果', color=CYAN),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        _key_value('已处理', len(executed)),
        _key_value('失败', len(failed)),
        _key_value('跳过', len(skipped)),
        '',
    ])

    def _render_section(title: str, items: list[Any], tone: str) -> None:
        if not items:
            return
        lines.extend([_section(title)])
        for item in items:
            result = str(getattr(item, 'result', '') or '')
            reason = str(getattr(item, 'reason', '') or '')
            summary = _humanize_datetime_text(str(getattr(item, 'summary', '') or '').strip())
            release_deadline = str(getattr(item, 'release_deadline', '') or '').strip()
            next_keeper_time = str(getattr(item, 'next_keeper_time', '') or '').strip()
            release_text = _format_relative_deadline(release_deadline) if release_deadline else '暂无结果'
            keeper_text = _format_relative_deadline(next_keeper_time) if next_keeper_time else '暂无结果'
            card_lines = [
                _key_value('实例状态', getattr(item, 'status', '') or '待确认'),
                _key_value('当前阶段', _tone_chip(_keeper_result_label(result or '暂无结果'), tone)),
                _key_value('下一步动作', _keeper_reason_label(reason or '暂无结果')),
                _key_value('距离释放', release_text),
                _key_value('距离接管', keeper_text),
            ]
            if summary:
                card_lines.append(_key_value('结果说明', summary))
            lines.extend(
                _boxed_lines(
                    f"实例 {getattr(item, 'instance_id', '未知实例')}",
                    card_lines,
                    tone=tone,
                )
            )
            lines.append('')

    _render_section('[已处理]', executed, 'ok')
    _render_section('[执行失败]', failed, 'bad')
    if not results:
        lines.append('暂无执行结果')
    elif not executed and not failed:
        lines.append('暂无需要执行的实例')
    return '\n'.join(lines)


def _render_scheduled_job_picker(
    settings: Settings,
    account_name: str,
    status_rows: list[dict[str, Any]],
    *,
    page_status_lines: list[str] | None = None,
) -> str:
    lines = [
        _heading('选择抢机器规则'),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        _key_value('当前账号', account_name),
        '',
        _section('[任务列表]'),
    ])
    return '\n'.join(lines)


def _scheduled_picker_item_label(row: dict[str, Any]) -> str:
    status_label = str(row.get('task_status_label') or ('已启用' if row.get('enabled') else '已暂停'))
    base = f"{row['job_name']}  {row['target_time']} 提前{row['advance_hours']}h  {status_label}"
    last_run_label = str(row.get('last_run_label') or '').strip()
    if not last_run_label:
        return base
    last_run_summary = str(row.get('last_run_summary') or '').strip()
    if last_run_summary:
        if len(last_run_summary) > 18:
            last_run_summary = last_run_summary[:15] + '...'
        return f'{base}  / 最近执行: {last_run_label} ({last_run_summary})'
    return f'{base}  / 最近执行: {last_run_label}'


def _scheduled_runtime_status_label(job, status_row: dict[str, Any]) -> tuple[str, str]:
    if status_row.get('task_status_label'):
        return str(status_row.get('task_status_label') or ''), str(status_row.get('task_status_tone') or 'info')
    latest_result = str(status_row.get('latest_result') or '')
    schedule_mode = getattr(job, 'schedule_mode', 'daily') or 'daily'
    if schedule_mode == 'once' and latest_result in {'started', 'already_running', 'power_on_submitted'}:
        return '单次已完成', 'ok'
    if not status_row.get('enabled', True):
        return '已暂停', 'warn'
    if bool(status_row.get('daemon_running')):
        return '轮询中', 'ok'
    return '等待执行', 'info'


def _render_scheduled_job_detail(
    job,
    status_row: dict[str, Any],
    account_name: str,
    *,
    page_status_lines: list[str] | None = None,
) -> str:
    target_summary = _job_target_summary(job)
    schedule_mode = getattr(job, 'schedule_mode', 'daily') or 'daily'
    runtime_label, runtime_tone = _scheduled_runtime_status_label(job, status_row)
    runtime_status = _tone_chip(runtime_label, runtime_tone)
    lines = [
        _heading('抢机器规则'),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        _section('[基本信息]'),
        _key_value('当前账号', account_name),
        _key_value('任务名称', scheduled_job_identity(job)),
        _key_value('目标时间', status_row["target_time"]),
        _key_value('提前启动', f'{status_row["advance_hours"]} 小时'),
        _key_value('执行计划', '单次' if schedule_mode == 'once' else '每天'),
        _key_value('任务状态', runtime_status),
        '',
    ])
    if status_row.get('last_run_label'):
        lines.extend([
            _section('[最近执行]'),
            _key_value('执行方式', status_row.get('last_run_trigger') or '-'),
            _key_value('本次结果', status_row.get('last_run_label') or '-'),
            _key_value('结果说明', status_row.get('last_run_summary') or '-'),
            '',
        ])
    lines.append(_section('[目标条件]'))
    if job.instance_id:
        lines.append(_key_value('目标方式', '固定实例'))
        lines.append(_key_value('目标实例', job.instance_id or '-'))
    else:
        lines.append(_key_value('目标方式', '按条件筛选候选机器'))
        lines.append(_key_value('筛选条件', target_summary))
    return '\n'.join(lines)


def _scheduled_seed_status_rows(
    settings: Settings,
    store,
    *,
    account_name: str,
) -> list[dict[str, Any]]:
    task_enabled = get_task_enabled(store, account_name, 'scheduled_start', default_enabled=settings.tasks.scheduled_start.enabled)
    daemon_running = bool(read_daemon_status(store).get('running'))
    rows: list[dict[str, Any]] = []
    for job in settings.tasks.scheduled_start.jobs:
        identity = scheduled_job_identity(job)
        control = store.get_scheduled_job_control(account_name, identity) or {}
        enabled = bool(task_enabled) and bool(control.get('enabled', True))
        row = {
            'job_name': identity,
            'enabled': enabled,
            'target_time': str(control.get('target_time_override') or job.target_time),
            'advance_hours': control.get('advance_hours_override')
            if control.get('advance_hours_override') is not None
            else job.advance_hours,
            'schedule_mode': str(getattr(job, 'schedule_mode', 'daily') or 'daily'),
            'timezone': getattr(job, 'timezone', 'Asia/Shanghai') or 'Asia/Shanghai',
            'latest_result': '',
            'latest_reason': '',
            'latest_summary': '',
            'latest_created_at': '',
            'latest_payload': {},
            'latest_instance_id': '',
            'has_history': False,
            'latest_matches_current_rule': False,
            'target_mode': 'instance' if job.instance_id else 'selector',
            'target_summary': _job_target_summary(job),
            'daemon_running': daemon_running,
            'last_run_trigger': '',
            'last_run_label': '',
            'last_run_summary': '',
        }
        row.update(_scheduled_runtime_status_fields(row))
        rows.append(row)
    return rows


def _account_has_enabled_scheduled_jobs(settings: Settings, store, *, account_name: str) -> bool:
    if not get_task_enabled(store, account_name, 'scheduled_start', default_enabled=settings.tasks.scheduled_start.enabled):
        return False
    for job in settings.tasks.scheduled_start.jobs:
        control = store.get_scheduled_job_control(account_name, scheduled_job_identity(job)) or {}
        if bool(control.get('enabled', True)):
            return True
    return False


def _coordinate_scheduled_background(
    *,
    args: argparse.Namespace,
    settings: Settings,
    store,
    account_name: str,
    start_background_scheduled_fn,
    stop_background_polling_fn,
    service_status_fn: Callable[[], dict[str, Any]] = read_launch_agent_status,
    service_start_fn: Callable[[], Any] = start_launch_agent,
) -> tuple[int, str]:
    enabled_jobs_exist = _account_has_enabled_scheduled_jobs(settings, store, account_name=account_name)
    daemon_status = read_daemon_status(store)
    daemon_mode = str(daemon_status.get('mode') or '')
    daemon_account = str(daemon_status.get('account') or '')
    daemon_running = bool(daemon_status.get('running'))

    if enabled_jobs_exist:
        if daemon_running and daemon_mode == 'all':
            return 0, '后台已在运行，新规则已生效'
        if daemon_running and daemon_mode == 'scheduled_start' and (daemon_account == account_name or not daemon_account):
            return 0, '后台已在运行，新规则已生效'
        if daemon_running:
            return 0, '检测到其他后台在运行，未自动接管'
        service_status = service_status_fn() if callable(service_status_fn) else {}
        if bool(service_status.get('installed')):
            result = service_start_fn()
            if isinstance(result, tuple):
                code, _detail = result
            else:
                code, _detail = 0, ''
            return code, '已启动后台服务' if code == 0 else '启动后台服务失败'
        code, _detail = start_background_scheduled_fn(_copy_args(args, account=account_name, run_once=False, daemon_origin='interactive-auto'))
        return code, '已自动启动后台（fallback 模式）' if code == 0 else '自动启动后台失败（fallback 模式）'

    if daemon_running and daemon_mode == 'scheduled_start' and daemon_account == account_name:
        code, _detail = stop_background_polling_fn(settings, store)
        return code, '已自动停止后台（当前无启用任务）' if code == 0 else '自动停止后台失败'

    return 0, '已保存，任务已暂停，不启动后台'


def _build_scheduled_detail_menu_items(enabled: bool, daemon_running: bool) -> list[MenuItem]:
    return [
        MenuItem('1', '立即执行一轮' if enabled else '恢复并执行一轮'),
        MenuItem('2', '查看抢机进度'),
        MenuItem('4', '修改规则'),
        MenuItem('5', '暂停任务' if enabled else '恢复任务'),
        MenuItem('6', '删除任务'),
        MenuItem('0', '返回规则列表'),
    ]


def _normalize_service_action_result(result: Any) -> tuple[int, str]:
    if isinstance(result, tuple):
        try:
            code = int(result[0] or 0)
        except Exception:
            code = 1
        detail = str(result[1] or '') if len(result) > 1 else ''
        return code, detail
    if hasattr(result, 'returncode'):
        code = int(getattr(result, 'returncode', 0) or 0)
        detail = str(getattr(result, 'stderr', '') or getattr(result, 'stdout', '') or '')
        return code, detail.strip()
    if result is None:
        return 0, ''
    return 0, str(result)


def _render_scheduled_run_results(job_name: str, results: list[Any]) -> str:
    lines = [_heading(f'抢机器执行结果: {job_name}'), _separator(), '']
    if not results:
        lines.append('- 本次没有产生新的执行结果（可能已被当前窗口成功记录跳过）')
    else:
        for item in results:
            lines.append(
                f"- result={_scheduled_result_label(getattr(item, 'result', '-'))} reason={_scheduled_reason_label(getattr(item, 'reason', '-'))} "
                f"instance={getattr(item, 'instance_id', '-') or '-'} summary={getattr(item, 'summary', '') or '-'}"
            )
    return '\n'.join(lines)


def _scheduled_run_result_summary(results: list[Any], *, trigger_label: str) -> dict[str, str]:
    if not results:
        return {
            'last_run_trigger': trigger_label,
            'last_run_label': '本次没有产生新的执行结果',
            'last_run_summary': '可能已被当前窗口成功记录跳过',
        }
    item = results[-1]
    summary = _sanitize_scheduled_summary(getattr(item, 'summary', '') or '')
    if summary == '-':
        summary = _scheduled_reason_label(str(getattr(item, 'reason', '') or ''))
    return {
        'last_run_trigger': trigger_label,
        'last_run_label': _scheduled_result_label(str(getattr(item, 'result', '') or '')),
        'last_run_summary': summary,
    }


def _scheduled_runtime_status_fields(row: dict[str, Any]) -> dict[str, str]:
    latest_result = str(row.get('latest_result') or '')
    schedule_mode = str(row.get('schedule_mode') or 'daily')
    if schedule_mode == 'once' and _is_scheduled_once_complete_result(latest_result):
        return {'task_status_label': '单次已完成', 'task_status_tone': 'ok'}
    if not row.get('enabled', True):
        return {'task_status_label': '已暂停', 'task_status_tone': 'warn'}
    if bool(row.get('daemon_running')):
        return {'task_status_label': '轮询中', 'task_status_tone': 'ok'}
    return {'task_status_label': '等待执行', 'task_status_tone': 'info'}


def _scheduled_result_payload(item: Any) -> dict[str, Any]:
    candidate_details: list[dict[str, Any]] = []
    for detail in list(getattr(item, 'candidate_details', []) or []):
        if isinstance(detail, dict):
            candidate_details.append(dict(detail))
        elif is_dataclass(detail):
            candidate_details.append(asdict(detail))
    return {
        'candidate_count': int(getattr(item, 'candidate_count', 0) or 0),
        'candidate_details': candidate_details,
        'selected_instance_id': str(getattr(item, 'selected_instance_id', '') or ''),
        'selected_instance_label': str(getattr(item, 'selected_instance_label', '') or ''),
        'selector_summary': str(getattr(item, 'selector_summary', '') or ''),
        'status': str(getattr(item, 'status', '') or ''),
    }


def _scheduled_run_result_state(base_row: dict[str, Any], results: list[Any], *, trigger_label: str) -> dict[str, Any]:
    state: dict[str, Any] = _scheduled_run_result_summary(results, trigger_label=trigger_label)
    if not results:
        return state
    item = results[-1]
    latest_result = str(getattr(item, 'result', '') or '')
    state.update(
        {
            'latest_result': latest_result,
            'latest_reason': str(getattr(item, 'reason', '') or ''),
            'latest_summary': str(getattr(item, 'summary', '') or ''),
            'latest_created_at': datetime.now().astimezone().isoformat(),
            'latest_payload': _scheduled_result_payload(item),
            'latest_instance_id': str(getattr(item, 'instance_id', '') or ''),
            'has_history': True,
            'latest_matches_current_rule': True,
        }
    )
    if str(base_row.get('schedule_mode') or 'daily') == 'once' and _is_scheduled_once_terminal_result(latest_result):
        state['enabled'] = False
    merged_row = dict(base_row)
    merged_row.update(state)
    state.update(_scheduled_runtime_status_fields(merged_row))
    return state


def _scheduled_run_pending_state(
    base_row: dict[str, Any],
    *,
    trigger_label: str,
    task_type: str,
    task_scope: str,
) -> dict[str, Any]:
    return {
        '_task_type': task_type,
        '_task_scope': task_scope,
        '_base_row': dict(base_row),
        '_trigger_label': trigger_label,
        'last_run_trigger': trigger_label,
        'last_run_label': '排队中',
        'last_run_summary': '后台任务排队中',
    }


def _refresh_scheduled_transient_state(
    transient_state: dict[str, dict[str, Any]],
    task_manager: InteractiveTaskManager,
) -> None:
    for job_name, overlay in list(transient_state.items()):
        task_type = str(overlay.get('_task_type') or '').strip()
        task_scope = str(overlay.get('_task_scope') or '').strip()
        if not task_type or not task_scope:
            continue
        task = task_manager.get_task(task_type, task_scope)
        if task is None:
            continue
        if task.status == 'queued':
            overlay['last_run_label'] = '排队中'
            overlay['last_run_summary'] = '后台任务排队中'
            continue
        if task.status == 'running':
            overlay['last_run_label'] = '正在执行'
            overlay['last_run_summary'] = '后台任务正在执行'
            continue
        base_row = dict(overlay.get('_base_row') or {'job_name': job_name})
        trigger_label = str(overlay.get('_trigger_label') or overlay.get('last_run_trigger') or '后台执行')
        if task.status == 'failed':
            error_message = str(task.error_message or '后台任务执行失败')
            transient_state[job_name] = {
                'last_run_trigger': trigger_label,
                'last_run_label': '执行失败',
                'last_run_summary': error_message,
                'task_status_label': base_row.get('task_status_label') or '等待执行',
                'task_status_tone': base_row.get('task_status_tone') or 'info',
            }
            continue
        results = task.payload if isinstance(task.payload, list) else []
        transient_state[job_name] = _scheduled_run_result_state(base_row, results, trigger_label=trigger_label)


def _merge_scheduled_transient_state(
    rows: list[dict[str, Any]],
    transient_state: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    merged_rows: list[dict[str, Any]] = []
    for row in rows:
        overlay = transient_state.get(str(row.get('job_name') or ''))
        if not overlay:
            merged_rows.append(row)
            continue
        merged = dict(row)
        merged.update(overlay)
        merged_rows.append(merged)
    return merged_rows


def _show_scheduled_run_results_screen(
    *,
    job_name: str,
    results: list[Any],
    fetch_rows_fn: Callable[[], list[dict[str, Any]]],
    back_label: str,
) -> None:
    post_selected_key = '1'
    while True:
        post_items = [MenuItem('1', '查看抢机进度'), MenuItem('0', back_label)]
        action = _choose_menu(
            _render_scheduled_run_results(job_name, results),
            post_items,
            default_key=_menu_default_key(post_items, post_selected_key),
        )
        post_selected_key = action
        if action == '1':
            _show_live_scheduled_status(job_name=job_name, fetch_rows_fn=fetch_rows_fn, settings=None)
        elif action == '0':
            break
        else:
            print('无效选择。')


def _format_relative_deadline(deadline: str) -> str:
    target = _parse_iso_datetime(deadline)
    if target is None:
        return '-'
    now = datetime.now().astimezone()
    if target.tzinfo is None:
        target = target.astimezone()
    delta = target - now
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return '已过期'
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f'{days}天')
    if hours:
        parts.append(f'{hours}小时')
    if minutes and not days:
        parts.append(f'{minutes}分钟')
    return ''.join(parts) or '不足1分钟'


def _scheduled_stage_label(row: dict[str, Any]) -> tuple[str, str]:
    result = str(row.get('latest_result') or '')
    if row.get('schedule_mode') == 'once' and _is_scheduled_once_complete_result(result):
        mapping = {
            'started': ('已发起开机', 'ok'),
            'power_on_submitted': ('已提交开机请求', 'ok'),
            'already_running': ('已抢到机器', 'ok'),
        }
        return mapping[result]
    if not row.get('enabled', True):
        return '已暂停', 'warn'
    if result == 'outside_window':
        phase_label, phase_tone, _ = _scheduled_window_phase(row)
        return phase_label, phase_tone
    mapping = {
        'started': ('已发起开机', 'ok'),
        'power_on_submitted': ('已提交开机请求', 'ok'),
        'already_running': ('已抢到机器', 'ok'),
        'waiting_for_gpu': ('等待可开机候选', 'info'),
        'waiting_for_instance': ('等待候选出现', 'info'),
        'selector_no_match': ('等待候选出现', 'info'),
        'deadline_failed': ('已超时', 'bad'),
        'instance_missing': ('规则失效', 'bad'),
        'started_without_gpu': ('已开机但未进入 GPU', 'warn'),
    }
    if result in mapping:
        return mapping[result]
    if result:
        return _scheduled_result_label(result), 'info'
    phase = _scheduled_window_phase(row)
    return phase[0], phase[1]


def _scheduled_execution_status(row: dict[str, Any]) -> tuple[str, str]:
    result = str(row.get('latest_result') or '')
    if not result:
        return '暂无检查记录', 'muted'
    if str(row.get('schedule_mode') or 'daily') == 'once' and _is_scheduled_once_complete_result(result):
        return '单次已完成', 'ok'
    if result in {'started', 'power_on_submitted', 'already_running'}:
        return '最近检查成功', 'ok'
    if result in {'deadline_failed', 'instance_missing'}:
        return '最近检查失败', 'bad'
    return '最近检查已执行', 'info'


def _scheduled_missing_check_reason(row: dict[str, Any]) -> tuple[str, str]:
    result = str(row.get('latest_result') or '')
    if result:
        return '', 'muted'
    if bool(row.get('has_history')) and not bool(row.get('latest_matches_current_rule')):
        return '等待新规则首次检查', 'info'
    phase_label, _, _ = _scheduled_window_phase(row)
    if phase_label == '等待抢机窗口':
        return '尚未到首次轮询', 'info'
    if bool(row.get('daemon_running')):
        return '轮询未落库', 'warn'
    return '后台未启动', 'warn'


def _scheduled_rule_match_note(row: dict[str, Any]) -> tuple[str, str]:
    if bool(row.get('has_history')) and not bool(row.get('latest_matches_current_rule')):
        return '最近检查来自旧规则', 'warn'
    return '', 'muted'


def _scheduled_next_action(row: dict[str, Any]) -> str:
    result = str(row.get('latest_result') or '')
    if row.get('schedule_mode') == 'once' and _is_scheduled_once_complete_result(result):
        if result == 'already_running':
            return '机器已可用，可以直接使用'
        return '等待实例启动完成，随后直接使用'
    if not row.get('enabled', True):
        return '先恢复任务，再继续轮询'
    if result == 'outside_window':
        return '等待进入下一次抢机窗口'
    if result in {'started', 'power_on_submitted'}:
        return '等待实例启动完成，随后继续刷新'
    if result == 'already_running':
        return '机器已可用，可以直接使用'
    if result in {'waiting_for_gpu', 'no_eligible_candidate'}:
        return '继续轮询候选，等待可开机资源'
    if result == 'selector_no_match':
        return '继续等待命中筛选条件的候选'
    if result == 'waiting_for_instance':
        return '继续等待匹配机器出现'
    if result == 'deadline_failed':
        return '调整目标时间或筛选条件后重试'
    if result == 'instance_missing':
        return '检查实例 ID，或改用筛选条件'
    if result == 'started_without_gpu':
        return '继续观察机器状态，必要时手动检查'
    phase = _scheduled_window_phase(row)
    return phase[2]


def _scheduled_candidate_summary(payload: dict[str, Any], row: dict[str, Any]) -> str:
    details = payload.get('candidate_details')
    if isinstance(details, list) and details:
        selected = next((item for item in details if isinstance(item, dict) and item.get('selected')), None)
        if isinstance(selected, dict):
            selected_id = selected.get('instance_id') or payload.get('selected_instance_id') or row.get('latest_instance_id') or '-'
            selected_status = _normalize_instance_status(selected.get('status') or payload.get('status') or '-')
            return f'已选中 {selected_id} / {selected_status}'
        fragments: list[str] = []
        for item in details[:3]:
            if not isinstance(item, dict):
                continue
            instance_id = item.get('instance_id') or '-'
            status = _normalize_instance_status(item.get('status') or '-')
            reason = _scheduled_reason_label(str(item.get('reason') or '-'))
            fragments.append(f'{instance_id}({status}/{reason})')
        if fragments:
            suffix = f' ...+{len(details) - 3}' if len(details) > 3 else ''
            return f'{len(details)} 个候选；' + '；'.join(fragments) + suffix
    candidate_count = payload.get('candidate_count')
    if isinstance(candidate_count, int) and candidate_count > 0:
        return f'{candidate_count} 个候选，等待更明确结果'
    selector_summary = str(payload.get('selector_summary') or '')
    if selector_summary:
        return f'当前没有候选；规则={selector_summary}'
    return '当前没有候选机器'


def _scheduled_candidate_groups(payload: dict[str, Any]) -> tuple[str, str, str]:
    details = payload.get('candidate_details')
    if not isinstance(details, list) or not details:
        return '-', '-', '-'
    hit: list[str] = []
    waiting: list[str] = []
    dropped: list[str] = []
    waiting_reasons = {'eligible', 'running_with_gpu', 'started', 'power_on_submitted', 'already_running'}
    for item in details:
        if not isinstance(item, dict):
            continue
        instance_id = str(item.get('instance_id') or '-')
        reason = str(item.get('reason') or '')
        if item.get('selected'):
            hit.append(instance_id)
        elif reason in waiting_reasons:
            waiting.append(instance_id)
        else:
            dropped.append(instance_id)
    def fmt(items: list[str]) -> str:
        if not items:
            return '-'
        return ' / '.join(items[:3]) + (f' ...+{len(items) - 3}' if len(items) > 3 else '')
    return fmt(hit), fmt(waiting), fmt(dropped)


def _scheduled_window_phase(row: dict[str, Any]) -> tuple[str, str, str]:
    target_time = str(row.get('target_time') or '').strip()
    if not target_time or ':' not in target_time:
        return '等待首次检查', 'info', '等待调度器开始轮询'
    try:
        hour, minute = [int(part) for part in target_time.split(':', 1)]
    except ValueError:
        return '等待首次检查', 'info', '等待调度器开始轮询'
    timezone_name = str(row.get('timezone') or 'Asia/Shanghai')
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        zone = ZoneInfo('Asia/Shanghai')
    now = datetime.now(zone)
    target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_dt <= now:
        target_dt += timedelta(days=1)
    start_dt = target_dt - timedelta(hours=int(row.get('advance_hours') or 0))
    if now < start_dt:
        return '等待抢机窗口', 'info', f'约 {_format_relative_deadline(start_dt.isoformat())} 后开始轮询'
    if now < target_dt:
        return '正在轮询候选', 'ok', f'继续轮询，距离目标时间还有 {_format_relative_deadline(target_dt.isoformat())}'
    return '等待下一轮窗口', 'info', '当前窗口已结束，等待下一次抢机时间'


def _scheduled_window_countdowns(row: dict[str, Any]) -> tuple[str, str]:
    target_time = str(row.get('target_time') or '').strip()
    if not target_time or ':' not in target_time:
        return '待计算', '待计算'
    try:
        hour, minute = [int(part) for part in target_time.split(':', 1)]
    except ValueError:
        return '待计算', '待计算'
    timezone_name = str(row.get('timezone') or 'Asia/Shanghai')
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        zone = ZoneInfo('Asia/Shanghai')
    now = datetime.now(zone)
    target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_dt <= now:
        target_dt += timedelta(days=1)
    start_dt = target_dt - timedelta(hours=int(row.get('advance_hours') or 0))
    start_text = '已经开始轮询' if now >= start_dt else _format_relative_deadline(start_dt.isoformat())
    target_text = _format_relative_deadline(target_dt.isoformat())
    return start_text, target_text


def _scheduled_latest_check_text(row: dict[str, Any]) -> str:
    created_at = str(row.get('latest_created_at') or '')
    if created_at:
        return _format_human_datetime(created_at)
    if bool(row.get('has_history')) and not bool(row.get('latest_matches_current_rule')):
        return '待同步最近检查记录'
    return '待首次检查'


def _scheduled_latest_matching_check_text(row: dict[str, Any]) -> str:
    created_at = str(row.get('latest_matching_created_at') or '')
    if created_at:
        return _format_human_datetime(created_at)
    if bool(row.get('has_history')):
        return '待当前规则首次检查'
    return '待首次检查'


def _scheduled_latest_result_text(row: dict[str, Any]) -> str:
    result = str(row.get('latest_result') or '')
    if result:
        return _scheduled_result_label(result)
    if bool(row.get('has_history')) and not bool(row.get('latest_matches_current_rule')):
        return '最近检查来自旧规则'
    return '待首次检查'


def _scheduled_field_fallback(value: str | None, *, empty_text: str) -> str:
    rendered = str(value or '').strip()
    return rendered or empty_text


def _scheduled_candidate_group_text(value: str) -> str:
    rendered = str(value or '').strip()
    if not rendered or rendered == '-':
        return '暂无'
    return rendered


def _render_scheduled_status(
    job_name: str | None,
    status_rows: list[dict[str, Any]],
    *,
    page_status_lines: list[str] | None = None,
) -> str:
    title = f'抢机进度: {job_name}' if job_name else '抢机进度'
    lines = [_heading(title), _separator()]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.append('')
    for row in status_rows:
        payload = row.get('latest_payload') or {}
        stage_label = str(row.get('_live_stage_label')) if '_live_stage_label' in row else None
        stage_tone = str(row.get('_live_stage_tone')) if '_live_stage_tone' in row else None
        if stage_label is None or stage_tone is None:
            stage_label, stage_tone = _scheduled_stage_label(row)
        execution_label = str(row.get('_live_execution_label')) if '_live_execution_label' in row else None
        execution_tone = str(row.get('_live_execution_tone')) if '_live_execution_tone' in row else None
        if execution_label is None or execution_tone is None:
            execution_label, execution_tone = _scheduled_execution_status(row)
        missing_reason_label = str(row.get('_live_missing_reason_label')) if '_live_missing_reason_label' in row else None
        missing_reason_tone = str(row.get('_live_missing_reason_tone')) if '_live_missing_reason_tone' in row else None
        if missing_reason_label is None or missing_reason_tone is None:
            missing_reason_label, missing_reason_tone = _scheduled_missing_check_reason(row)
        rule_match_label, rule_match_tone = _scheduled_rule_match_note(row)
        poll_text = str(row.get('_live_poll_text')) if '_live_poll_text' in row else None
        target_text = str(row.get('_live_target_text')) if '_live_target_text' in row else None
        if poll_text is None or target_text is None:
            poll_text, target_text = _scheduled_window_countdowns(row)
        next_action = str(row.get('_live_next_action') or '').strip() if '_live_next_action' in row else ''
        if not next_action:
            next_action = _scheduled_next_action(row)
        hit_text, waiting_text, dropped_text = _scheduled_candidate_groups(payload)
        card_lines = [
            _key_value('规则开关', _tone_chip('已启用', 'ok') if row['enabled'] else _tone_chip('已停用', 'warn')),
            _key_value('执行状态', _tone_chip(execution_label, execution_tone)),
            _key_value('当前阶段', _tone_chip(stage_label, stage_tone)),
            _key_value('下一步动作', next_action),
            _key_value('执行计划', '单次' if row.get('schedule_mode') == 'once' else '每天'),
            _key_value('目标方式', '固定实例' if row.get('target_mode') == 'instance' else '按条件筛选候选机器'),
            _key_value('目标条件', _scheduled_field_fallback(row.get('target_summary'), empty_text='未设置')),
            _key_value('目标时间', _scheduled_field_fallback(row.get('target_time'), empty_text='未设置')),
            _key_value('距离开始轮询', poll_text),
            _key_value('距离目标时间', target_text),
            _key_value('最近检查时间', _scheduled_latest_check_text(row)),
            _key_value('最近检查结果', _scheduled_latest_result_text(row)),
            _key_value('已命中', _scheduled_candidate_group_text(hit_text)),
            _key_value('等待中', _scheduled_candidate_group_text(waiting_text)),
            _key_value('被淘汰', _scheduled_candidate_group_text(dropped_text)),
        ]
        if missing_reason_label:
            card_lines.insert(8, _key_value('未检查原因', _tone_chip(missing_reason_label, missing_reason_tone)))
        if rule_match_label:
            card_lines.insert(12 if missing_reason_label else 11, _key_value('规则匹配状态', _tone_chip(rule_match_label, rule_match_tone)))
        if row.get('latest_summary'):
            card_lines.append(_key_value('结果说明', _sanitize_scheduled_summary(row['latest_summary'])))
        lines.extend(_boxed_lines(f"任务 {row['job_name']}", card_lines, tone=stage_tone))
        lines.append('')
    return '\n'.join(lines)


def _render_accounts_summary(settings: Settings, store, *, current_account: str | None) -> str:
    lines = [_heading('账号状态', color=CYAN), _separator(), '']
    if not settings.accounts:
        return '账号状态\n\n未配置多账号。'
    for account in settings.accounts:
        state = inspect_auth_state(account.to_auth_settings(), store=store, account_name=account.name)
        marker = '当前' if account.name == current_account else '   '
        lines.append(
            f"[{marker}] {account.name} / {_mask_phone(account.autodl_phone)} "
            f"启用={'是' if account.enabled else '否'} "
            f"状态={state['status']} 来源={state['auth_source']}"
        )
    return '\n'.join(lines)


def _render_account_detail(
    settings: Settings,
    store,
    *,
    account_name: str,
    keeper_probe_rows_fn: Callable[..., list[dict[str, Any]]],
    scheduled_job_status_rows_fn: Callable[..., list[dict[str, Any]]],
    snapshot: dict[str, Any] | None = None,
    page_status_lines: list[str] | None = None,
) -> str:
    account = next((item for item in settings.accounts if item.name == account_name), None)
    if account is None:
        return f'账号详情\n\n账号不存在: {account_name}'
    runtime_snapshot = snapshot or {
        'account_name': account.name,
        'account_enabled': bool(account.enabled),
        'auth_status': '首次加载中',
        'auth_source': '-',
        'running_instances': 0,
        'expiring_soon': 0,
        'scheduled_jobs': 0,
        'paused_jobs': 0,
        'keeper_enabled': get_task_enabled(store, account.name, 'keeper', default_enabled=settings.tasks.keeper.enabled),
    }
    lines = [
        _heading(f'账号详情: {account.name}', color=CYAN),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        _section('[账号状态]'),
        _key_value('账号名', account.name),
        _key_value('登录状态', runtime_snapshot.get('auth_status') or '首次加载中'),
        _key_value('认证来源', runtime_snapshot.get('auth_source') or '-'),
        _key_value('是否启用', _tone_chip('启用', 'ok') if runtime_snapshot.get('account_enabled', True) else _tone_chip('停用', 'warn')),
        '',
        _section('[Helper 关注的数据]'),
        _key_value('运行中实例', runtime_snapshot['running_instances']),
        _key_value('一周内到期', runtime_snapshot['expiring_soon']),
        _key_value('Keeper', _tone_chip('启用', 'ok') if runtime_snapshot['keeper_enabled'] else _tone_chip('暂停', 'warn')),
        _key_value('抢机器任务', runtime_snapshot['scheduled_jobs']),
        _key_value('已暂停任务', runtime_snapshot['paused_jobs']),
    ])
    return '\n'.join(lines)


def _history_record_subject(row: HistoryRecord) -> str:
    payload = row.payload or {}
    if row.task_type == 'keeper':
        return str(payload.get('instance_id') or row.instance_id or '-')
    if row.task_type == 'service':
        return str(payload.get('label') or 'LaunchAgent')
    return str(payload.get('selected_instance_id') or payload.get('instance_id') or row.instance_id or '-')


def _history_record_summary(row: HistoryRecord) -> str:
    if row.summary:
        return _humanize_datetime_text(row.summary)
    payload = row.payload or {}
    if row.task_type == 'keeper':
        return _humanize_datetime_text(
            f"释放时间={payload.get('release_deadline') or '-'} 下次保活={payload.get('next_keeper_time') or '-'}"
        )
    if row.task_type == 'service':
        return _humanize_datetime_text(f"动作={payload.get('action') or '-'} 详情={payload.get('detail') or '-'}")
    return _humanize_datetime_text(
        f"目标时间={payload.get('target_time') or '-'} deadline={payload.get('deadline') or '-'}"
    )


def _render_history_record_detail(row: HistoryRecord) -> str:
    lines = [
        _heading('记录详情', color=CYAN),
        _separator(),
        _key_value('时间', _format_human_datetime(row.created_at)),
        _key_value('账号', row.account_name),
        _key_value('任务', row.task_type),
        _key_value('事件', row.event_type or '-'),
        _key_value('级别', row.severity or '-'),
        _key_value('对象', _history_record_subject(row)),
        _key_value(
            '结果',
            row.result
            if row.task_type == 'service'
            else (_keeper_result_label(row.result) if row.task_type == 'keeper' else _scheduled_result_label(row.result)),
        ),
        _key_value(
            '原因',
            (row.reason or '-')
            if row.task_type == 'service'
            else (_keeper_reason_label(row.reason) if row.task_type == 'keeper' else _scheduled_reason_label(row.reason)),
        ),
        '',
        _section('[摘要]'),
        _history_record_summary(row),
    ]
    return '\n'.join(lines)


def _history_task_label(task_type: str) -> str:
    return {
        'scheduled_start': '抢机器',
        'keeper': 'Keeper',
        'service': '后台服务',
    }.get(task_type, task_type or '-')


def _history_brief_line(row: HistoryRecord) -> str:
    subject = _history_record_subject(row)
    summary = _history_record_summary(row)
    if len(summary) > 42:
        summary = summary[:39] + '...'
    return f"{_history_task_label(row.task_type)} / {subject} / {summary}"


def _is_success_record(row: HistoryRecord) -> bool:
    if row.task_type == 'service':
        return row.severity not in {'error', 'fatal'}
    if row.severity == 'success':
        return True
    return row.result in {'started', 'already_running', 'keeper_executed', 'power_on_submitted'}


def _is_failure_record(row: HistoryRecord) -> bool:
    if row.task_type == 'service':
        return row.severity in {'error', 'fatal'}
    if row.severity in {'error', 'fatal'}:
        return True
    return row.result in {'deadline_failed', 'instance_missing', 'keeper_failed_power_on', 'keeper_failed_power_off'}


def _render_records_overview(settings: Settings, store, *, current_account: str | None) -> str:
    account_label = _account_display_name(settings, current_account)
    rows = store.read_history(account_name=current_account, limit=30)
    recent_success = next((row for row in rows if _is_success_record(row)), None)
    recent_failure = next((row for row in rows if _is_failure_record(row)), None)
    auth_summaries = store.summarize_auth_failures(account_name=current_account, limit=3)

    success_lines = [
        _key_value('账号范围', account_label),
        _key_value('最近一条', _history_brief_line(recent_success) if recent_success else '暂无成功记录'),
    ]
    if recent_success is not None:
        success_lines.append(_key_value('结果', _scheduled_result_label(recent_success.result) if recent_success.task_type == 'scheduled_start' else _keeper_result_label(recent_success.result)))

    failure_lines = [
        _key_value('账号范围', account_label),
        _key_value('最近一条', _history_brief_line(recent_failure) if recent_failure else '暂无失败记录'),
    ]
    if recent_failure is not None:
        failure_lines.append(_key_value('原因', _scheduled_reason_label(recent_failure.reason) if recent_failure.task_type == 'scheduled_start' else _keeper_reason_label(recent_failure.reason)))

    anomaly_lines = [_key_value('账号范围', account_label)]
    if auth_summaries:
        top = auth_summaries[0]
        message = top.msg or top.code or '未知异常'
        if len(message) > 40:
            message = message[:37] + '...'
        anomaly_lines.extend([
            _key_value('最近一条', message),
            _key_value('出现次数', top.count),
        ])
    else:
        anomaly_lines.append(_key_value('最近一条', '暂无认证异常'))

    blocks = [
        _heading('运行记录', color=CYAN),
        _separator(),
        '',
        *_boxed_lines('最近成功', success_lines, tone='ok'),
        '',
        *_boxed_lines('最近失败', failure_lines, tone='bad'),
        '',
        *_boxed_lines('最近异常', anomaly_lines, tone='warn'),
    ]
    return '\n'.join(blocks)


def _render_instance_reference(row: HistoryRecord) -> str:
    payload = row.payload or {}
    instance_id = payload.get('selected_instance_id') or payload.get('instance_id') or row.instance_id or payload.get('label') or '-'
    lines = [
        _heading('关联实例', color=CYAN),
        _separator(),
        _key_value('实例 ID', instance_id),
        _key_value('任务类型', row.task_type),
        _key_value('结果', row.result if row.task_type == 'service' else (_keeper_result_label(row.result) if row.task_type == 'keeper' else _scheduled_result_label(row.result))),
        _key_value('原因', (row.reason or '-') if row.task_type == 'service' else (_keeper_reason_label(row.reason) if row.task_type == 'keeper' else _scheduled_reason_label(row.reason))),
    ]
    if row.task_type == 'keeper':
        lines.extend([
            _key_value('释放时间', _format_human_datetime(str(payload.get('release_deadline') or '')) if payload.get('release_deadline') else '-'),
            _key_value('下次保活', _format_human_datetime(str(payload.get('next_keeper_time') or '')) if payload.get('next_keeper_time') else '-'),
        ])
    elif row.task_type == 'service':
        lines.extend([
            _key_value('服务标签', payload.get('label') or '-'),
            _key_value('动作', payload.get('action') or '-'),
        ])
    else:
        lines.extend([
            _key_value('目标时间', payload.get('target_time') or '-'),
            _key_value('截止时间', payload.get('deadline') or '-'),
        ])
    return '\n'.join(lines)


def _render_config_summary(settings: Settings, *, current_account: str | None) -> str:
    current_label = _account_display_name(settings, current_account)
    keeper_days, keeper_hours = divmod(int(settings.tasks.keeper.shutdown_release_after_hours), 24)
    keeper_limit = f'{keeper_days}天' if keeper_days and not keeper_hours else (f'{keeper_days}天 {keeper_hours}小时' if keeper_days else f'{keeper_hours}小时')
    lines = [
        _heading('配置概览'),
        _separator(),
        '',
        _key_value('当前账号', current_label),
        '',
        _section('[Keeper]'),
        _key_value('状态', _tone_chip('运行中', 'ok') if settings.tasks.keeper.enabled else _tone_chip('已暂停', 'warn')),
        _key_value('最长保留时间', keeper_limit),
        '',
        _section('[抢机器任务]'),
        _key_value('状态', _tone_chip('运行中', 'ok') if settings.tasks.scheduled_start.enabled else _tone_chip('已暂停', 'warn')),
        _key_value('任务数量', len(settings.tasks.scheduled_start.jobs)),
    ]
    for job in settings.tasks.scheduled_start.jobs:
        target = job.instance_id or (
            f"GPU={job.selector.gpu_model} x{job.selector.gpu_count}" if job.selector else '-'
        )
        lines.append(f"  • {_tone_chip('运行中' if settings.tasks.scheduled_start.enabled else '已暂停', 'ok' if settings.tasks.scheduled_start.enabled else 'warn')} {scheduled_job_identity(job)} / {job.target_time} / 提前{job.advance_hours}h / {target}")
    return '\n'.join(lines)


def _choose_account_scope(settings: Settings, current_account: str | None, *, title: str, allow_all: bool = True) -> str | None:
    items: list[MenuItem] = []
    default_key = '1'
    if allow_all:
        items.append(MenuItem('a', '全部账号'))
        default_key = 'a'
    for index, name in enumerate(_enabled_account_names(settings), start=1):
        items.append(MenuItem(str(index), _account_display_name(settings, name)))
        if name == current_account:
            default_key = str(index)
    items.append(MenuItem('0', '返回'))
    choice = _choose_menu(title, items, default_key=default_key)
    if choice == '0':
        return None
    if choice == 'a':
        return None
    if choice.isdigit():
        names = _enabled_account_names(settings)
        if 1 <= int(choice) <= len(names):
            return names[int(choice) - 1]
    raise ValueError('无效账号选择。')


def _history_filter_wizard(settings: Settings, current_account: str | None) -> SimpleNamespace | None:
    draft = {
        'account': current_account,
        'task': None,
        'limit': 20,
    }
    while True:
        lines = [
            _heading('最近记录筛选向导', color=CYAN),
            _separator(),
            _key_value('账号范围', _account_display_name(settings, draft['account']) if draft['account'] else '全部账号'),
            _key_value('任务类型', draft['task'] or '全部'),
            _key_value('数量限制', draft['limit']),
        ]
        choice = _choose_menu(
            '\n'.join(lines),
            [
                MenuItem('1', '选择账号范围'),
                MenuItem('2', '选择任务类型'),
                MenuItem('3', '修改数量限制'),
                MenuItem('c', '查看记录'),
                MenuItem('0', '取消'),
            ],
            default_key='1',
        )
        if choice == '1':
            draft['account'] = _choose_account_scope(settings, draft['account'], title=_heading('选择账号范围', color=CYAN), allow_all=True)
        elif choice == '2':
            task_choice = _choose_menu(
                _heading('选择任务类型', color=CYAN),
                [MenuItem('a', '全部'), MenuItem('1', 'keeper'), MenuItem('2', 'scheduled_start'), MenuItem('3', 'service'), MenuItem('0', '返回')],
                default_key='a',
            )
            if task_choice == 'a':
                draft['task'] = None
            elif task_choice == '1':
                draft['task'] = 'keeper'
            elif task_choice == '2':
                draft['task'] = 'scheduled_start'
            elif task_choice == '3':
                draft['task'] = 'service'
        elif choice == '3':
            draft['limit'] = _prompt_int_with_default('limit', draft['limit'])
        elif choice == 'c':
            draft['event_type'] = None
            return SimpleNamespace(**draft)
        elif choice == '0':
            return None


def _auth_report_filter_wizard(settings: Settings, current_account: str | None) -> SimpleNamespace | None:
    draft = {
        'account': current_account,
        'limit': 20,
        'only_unmapped': False,
        'only_likely_auth': False,
    }
    while True:
        lines = [
            _heading('认证异常筛选向导', color=CYAN),
            _separator(),
            _key_value('账号范围', _account_display_name(settings, draft['account']) if draft['account'] else '全部账号'),
            _key_value('数量限制', draft['limit']),
            _key_value('仅未覆盖', '是' if draft['only_unmapped'] else '否'),
            _key_value('仅疑似认证错误', '是' if draft['only_likely_auth'] else '否'),
        ]
        choice = _choose_menu(
            '\n'.join(lines),
            [
                MenuItem('1', '选择账号范围'),
                MenuItem('2', '修改数量限制'),
                MenuItem('3', '切换仅未覆盖'),
                MenuItem('4', '切换仅疑似认证错误'),
                MenuItem('c', '查看异常'),
                MenuItem('0', '取消'),
            ],
            default_key='1',
        )
        if choice == '1':
            draft['account'] = _choose_account_scope(settings, draft['account'], title=_heading('选择账号范围', color=CYAN), allow_all=True)
        elif choice == '2':
            draft['limit'] = _prompt_int_with_default('limit', draft['limit'])
        elif choice == '3':
            draft['only_unmapped'] = not draft['only_unmapped']
        elif choice == '4':
            draft['only_likely_auth'] = not draft['only_likely_auth']
        elif choice == 'c':
            return SimpleNamespace(**draft)
        elif choice == '0':
            return None


def _instances_filter_wizard(settings: Settings, current_account: str | None) -> SimpleNamespace | None:
    draft = {'account': current_account}
    while True:
        lines = [
            _heading('实例列表筛选向导', color=CYAN),
            _separator(),
            _key_value('账号范围', _account_display_name(settings, draft['account']) if draft['account'] else '全部账号'),
        ]
        choice = _choose_menu(
            '\n'.join(lines),
            [MenuItem('1', '选择账号范围'), MenuItem('c', '查看实例'), MenuItem('0', '取消')],
            default_key='1',
        )
        if choice == '1':
            draft['account'] = _choose_account_scope(settings, draft['account'], title=_heading('选择账号范围', color=CYAN), allow_all=True)
        elif choice == 'c':
            return SimpleNamespace(**draft)
        elif choice == '0':
            return None


def _keeper_probe_filter_wizard(settings: Settings, current_account: str | None) -> SimpleNamespace | None:
    draft = {'account': current_account, 'only_eligible': False}
    while True:
        lines = [
            _heading('Keeper 探测筛选向导', color=CYAN),
            _separator(),
            _key_value('账号范围', _account_display_name(settings, draft['account']) if draft['account'] else '全部账号'),
            _key_value('只看可执行实例', '是' if draft['only_eligible'] else '否'),
        ]
        choice = _choose_menu(
            '\n'.join(lines),
            [MenuItem('1', '选择账号范围'), MenuItem('2', '切换只看可执行实例'), MenuItem('c', '查看探测'), MenuItem('0', '取消')],
            default_key='1',
        )
        if choice == '1':
            draft['account'] = _choose_account_scope(settings, draft['account'], title=_heading('选择账号范围', color=CYAN), allow_all=True)
        elif choice == '2':
            draft['only_eligible'] = not draft['only_eligible']
        elif choice == 'c':
            return SimpleNamespace(**draft)
        elif choice == '0':
            return None


def _healthcheck_filter_wizard() -> SimpleNamespace | None:
    draft = {'smoke': False}
    while True:
        lines = [
            _heading('健康检查向导', color=CYAN),
            _separator(),
            _key_value('附带登录/实例烟雾测试', '是' if draft['smoke'] else '否'),
        ]
        choice = _choose_menu(
            '\n'.join(lines),
            [MenuItem('1', '切换烟雾测试'), MenuItem('c', '开始检查'), MenuItem('0', '取消')],
            default_key='1',
        )
        if choice == '1':
            draft['smoke'] = not draft['smoke']
        elif choice == 'c':
            return SimpleNamespace(**draft)
        elif choice == '0':
            return None


def _browse_account_detail(
    *,
    args: argparse.Namespace,
    settings: Settings,
    store,
    current_account: str | None,
    command_login_fn,
    keeper_probe_rows_fn,
    scheduled_job_status_rows_fn,
    task_manager: InteractiveTaskManager,
    snapshot_store: InteractiveSnapshotStore,
    trigger_verify_on_open: bool = False,
) -> str | None:
    selected_account = current_account or (_enabled_account_names(settings)[0] if _enabled_account_names(settings) else None)
    if selected_account is None:
        _show_result_screen('账号详情', '当前没有可用账号。')
        return current_account
    snapshot_key = _snapshot_key('account_runtime', selected_account)

    def _queue_account_refresh() -> None:
        _submit_snapshot_task(
            task_manager=task_manager,
            snapshot_store=snapshot_store,
            task_type='account_refresh',
            scope=selected_account,
            snapshot_key=snapshot_key,
            runner=lambda: _account_runtime_snapshot(
                settings,
                store,
                account_name=selected_account,
                keeper_probe_rows_fn=keeper_probe_rows_fn,
                scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
            ),
            status_message='正在刷新账号状态',
            replace_queued=True,
        )

    def _queue_login_verify() -> None:
        task_manager.submit(
            'login_verify_run',
            scope=selected_account,
            runner=lambda: _login_verify_snapshot(
                args=args,
                account_name=selected_account,
                command_login_fn=command_login_fn,
                settings=settings,
                store=store,
                keeper_probe_rows_fn=keeper_probe_rows_fn,
                scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
            ),
            status_message='正在验证登录状态',
            on_success=lambda task_result: _store_snapshot(snapshot_store, snapshot_key, task_result.payload, status_message='最近更新'),
            on_error=lambda task_result: (
                task_manager.record_resource_error(task_result.error_message),
                snapshot_store.record_failure(snapshot_key, _friendly_resource_error_message(task_result.error_message)),
            ),
            replace_queued=True,
        )
        task_manager.start_pending()

    _queue_account_refresh()
    task_manager.start_pending()
    if trigger_verify_on_open:
        _queue_login_verify()
        _nudge_background_tasks(task_manager, settle_seconds=0.01)
    selected_key = '1'

    def _account_detail_body() -> str:
        task_manager.drain_completed()
        account_refresh_task = task_manager.get_task('account_refresh', selected_account)
        login_verify_task = task_manager.get_task('login_verify_run', selected_account)
        active_task = None
        progress_label = '任务进度'
        if login_verify_task is not None and login_verify_task.status in {'queued', 'running'}:
            active_task = login_verify_task
            progress_label = '验证进度'
        elif account_refresh_task is not None and account_refresh_task.status in {'queued', 'running'}:
            active_task = account_refresh_task
            progress_label = '刷新进度'
        runtime_snapshot = snapshot_store.get_snapshot(snapshot_key)
        status = _page_status_from_tasks(
            snapshot_store=snapshot_store,
            snapshot_key=snapshot_key,
            primary_task=account_refresh_task,
            secondary_tasks=[login_verify_task],
        )
        return _render_account_detail(
            settings,
            store,
            account_name=selected_account,
            keeper_probe_rows_fn=keeper_probe_rows_fn,
            scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
            snapshot=runtime_snapshot if isinstance(runtime_snapshot, dict) else None,
            page_status_lines=_page_status_lines(status, active_task=active_task, progress_label=progress_label),
        )

    while True:
        items = [
            MenuItem('1', '后台验证登录状态'),
            MenuItem('0', '返回'),
        ]
        action = _choose_menu_with_refresh(
            _account_detail_body(),
            items,
            default_key=_menu_default_key(items, selected_key),
            refresh_fn=lambda preferred_key: (_account_detail_body(), items, preferred_key or selected_key),
            refresh_revision_fn=lambda: _menu_refresh_revision(
                snapshot_store=snapshot_store,
                snapshot_keys=[snapshot_key],
                task_manager=task_manager,
                task_keys=[
                    task_manager.task_key('account_refresh', selected_account),
                    task_manager.task_key('login_verify_run', selected_account),
                ],
            ),
            refresh_interval_seconds=1.0,
            on_rendered_fn=task_manager.start_pending,
            refresh_policy='always',
            pre_refresh_fn=task_manager.drain_completed,
        )
        selected_key = action
        if action == '1':
            _queue_login_verify()
            _nudge_background_tasks(task_manager, settle_seconds=0.01)
        elif action == '0':
            return current_account


def _browse_history_records(
    *,
    settings: Settings,
    store,
    current_account: str | None,
    rows: list[HistoryRecord],
    keeper_probe_rows_fn,
    scheduled_job_status_rows_fn,
) -> str | None:
    if not rows:
        _show_result_screen('最近记录', '没有符合条件的记录。')
        return current_account
    selected_key = '1'
    while True:
        items = [
            MenuItem(str(index), f"{row.created_at} | {row.account_name} | {row.task_type} | {_history_record_subject(row)}")
            for index, row in enumerate(rows, start=1)
        ] + [MenuItem('0', '返回')]
        choice = _choose_menu(_heading('最近记录列表', color=CYAN), items, default_key=_menu_default_key(items, selected_key))
        if choice == '0':
            return current_account
        if not choice.isdigit():
            continue
        selected_key = choice
        row = rows[int(choice) - 1]
        detail_selected_key = '1'
        while True:
            detail_items = [
                MenuItem('1', '查看关联账号'),
                MenuItem('2', '查看关联任务'),
                MenuItem('3', '查看关联实例'),
                MenuItem('0', '返回记录列表'),
            ]
            action = _choose_menu(
                _render_history_record_detail(row),
                detail_items,
                default_key=_menu_default_key(detail_items, detail_selected_key),
            )
            detail_selected_key = action
            if action == '1':
                _show_result_screen(
                    '关联账号',
                    _render_account_detail(
                        settings,
                        store,
                        account_name=row.account_name,
                        keeper_probe_rows_fn=keeper_probe_rows_fn,
                        scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
                    ),
                )
            elif action == '2':
                if row.task_type == 'keeper':
                    _show_result_screen('关联任务', _render_keeper_rules(settings, row.account_name, store))
                else:
                    try:
                        job = _find_scheduled_job(settings, row.payload.get('job_name') or row.instance_id or _history_record_subject(row))
                        status_rows = [{
                            'job_name': scheduled_job_identity(job),
                            'target_time': row.payload.get('target_time') or job.target_time,
                            'advance_hours': row.payload.get('advance_hours') or job.advance_hours,
                            'enabled': True,
                        }]
                        _show_result_screen('关联任务', _render_scheduled_job_detail(job, status_rows[0], row.account_name))
                    except Exception:
                        _show_result_screen('关联任务', '当前配置里找不到这条任务规则，可能已经被删除或改名。')
            elif action == '3':
                _show_result_screen('关联实例', _render_instance_reference(row))
            elif action == '0':
                break


def _scheduled_menu(
    args: argparse.Namespace,
    *,
    settings: Settings,
    current_account: str | None,
    run_variant_fn,
    start_background_scheduled_fn,
    stop_background_polling_fn,
    run_scheduled_start_cycle_fn,
    set_job_enabled_fn,
    set_job_override_fn,
    request_reload_fn,
    store,
    scheduled_job_status_rows_fn,
    load_settings_fn,
    validate_settings_fn,
    task_manager: InteractiveTaskManager,
    snapshot_store: InteractiveSnapshotStore,
) -> None:
    account_label = current_account or 'default'
    selected_key = '1'
    transient_run_state: dict[str, dict[str, Any]] = {}
    snapshot_key = _snapshot_key('scheduled_status', account_label)
    status_refresh_scope = account_label
    scheduled_status_retry_after = 0.0
    scheduled_status_last_submit_at = 0.0

    def _scheduled_status_task() -> InteractiveTaskResult | None:
        return task_manager.get_task('scheduled_status_refresh', status_refresh_scope)

    def _scheduled_status_page_lines() -> list[str]:
        return _page_status_lines(snapshot_store.page_status(snapshot_key, _scheduled_status_task()))

    def _base_status_rows() -> list[dict[str, Any]]:
        rows = snapshot_store.get_snapshot(snapshot_key)
        if isinstance(rows, list) and rows:
            return list(rows)
        return _scheduled_seed_status_rows(settings, store, account_name=account_label)

    def _scheduled_refresh_task_keys() -> list[str]:
        task_keys = [
            task_manager.task_key('scheduled_status_refresh', status_refresh_scope),
            task_manager.task_key('scheduled_background_sync', account_label),
        ]
        for overlay in transient_run_state.values():
            task_type = str(overlay.get('_task_type') or '').strip()
            task_scope = str(overlay.get('_task_scope') or '').strip()
            if task_type and task_scope:
                task_keys.append(task_manager.task_key(task_type, task_scope))
        return task_keys

    def _refresh_status_backoff_state() -> None:
        nonlocal scheduled_status_retry_after
        task_manager.drain_completed()
        _refresh_scheduled_transient_state(transient_run_state, task_manager)
        entry = snapshot_store.get_entry(snapshot_key)
        if entry is not None and entry.error_message:
            if scheduled_status_retry_after <= 0.0:
                scheduled_status_retry_after = time.monotonic() + 3.0
        else:
            scheduled_status_retry_after = 0.0

    def _queue_status_refresh(*, force: bool = False, settle_seconds: float = 0.0) -> bool:
        nonlocal scheduled_status_retry_after, scheduled_status_last_submit_at
        task = _scheduled_status_task()
        if task is not None and task.status in {'queued', 'running'}:
            if settle_seconds > 0:
                _nudge_background_tasks(task_manager, settle_seconds=settle_seconds)
            return False
        now = time.monotonic()
        if not force:
            if scheduled_status_retry_after and now < scheduled_status_retry_after:
                return False
            if now - scheduled_status_last_submit_at < 1.0:
                return False
        scheduled_status_last_submit_at = now

        def _on_success(task_result: InteractiveTaskResult) -> None:
            nonlocal scheduled_status_retry_after
            task_manager.clear_resource_error()
            scheduled_status_retry_after = 0.0
            _store_snapshot(snapshot_store, snapshot_key, task_result.payload, status_message='最近更新')

        def _on_error(task_result: InteractiveTaskResult) -> None:
            nonlocal scheduled_status_retry_after
            task_manager.record_resource_error(task_result.error_message)
            scheduled_status_retry_after = time.monotonic() + 3.0
            snapshot_store.record_failure(snapshot_key, _friendly_resource_error_message(task_result.error_message))

        task_manager.submit(
            'scheduled_status_refresh',
            scope=status_refresh_scope,
            runner=lambda: scheduled_job_status_rows_fn(settings, store, account_name=account_label),
            status_message='正在刷新抢机器规则',
            on_success=_on_success,
            on_error=_on_error,
            replace_queued=True,
        )
        if settle_seconds > 0:
            _nudge_background_tasks(task_manager, settle_seconds=settle_seconds)
        else:
            task_manager.start_pending()
        return True

    def _refresh_status_snapshot_if_due(*, force: bool = False, settle_seconds: float = 0.0) -> None:
        _refresh_status_backoff_state()
        if not force:
            entry = snapshot_store.get_entry(snapshot_key)
            if (
                task_manager.circuit_state().get('circuit_open')
                and entry is not None
                and entry.error_message
            ):
                return
            if scheduled_status_retry_after and time.monotonic() < scheduled_status_retry_after:
                return
        _queue_status_refresh(force=force, settle_seconds=settle_seconds)

    def _fetch_status_rows(*, job_name: str | None = None, force_refresh: bool = False, settle_seconds: float = 0.0) -> list[dict[str, Any]]:
        if force_refresh:
            _refresh_status_snapshot_if_due(force=True, settle_seconds=settle_seconds)
        _refresh_status_backoff_state()
        rows = _merge_scheduled_transient_state(copy.deepcopy(_base_status_rows()), transient_run_state)
        if job_name is not None:
            rows = [row for row in rows if str(row.get('job_name') or '') == job_name]
        return rows

    def _fetch_live_status_rows(*, job_name: str | None = None) -> list[dict[str, Any]]:
        rows = scheduled_job_status_rows_fn(settings, store, account_name=account_label, job_name=job_name)
        _store_snapshot(snapshot_store, snapshot_key, rows, status_message='最近更新')
        _refresh_status_backoff_state()
        rows = _merge_scheduled_transient_state(copy.deepcopy(rows), transient_run_state)
        if job_name is not None:
            rows = [row for row in rows if str(row.get('job_name') or '') == job_name]
        return rows

    def _fetch_single_status_row(job_name: str, fallback: dict[str, Any]) -> dict[str, Any]:
        rows = _fetch_status_rows(job_name=job_name)
        if rows:
            return rows[0]
        merged = dict(fallback)
        overlay = transient_run_state.get(job_name)
        if overlay:
            merged.update(overlay)
        return merged

    def _queue_scheduled_background_sync(scoped_args: argparse.Namespace) -> None:
        task_manager.submit(
            'scheduled_background_sync',
            scope=account_label,
            runner=lambda scoped_args=scoped_args, settings=settings, store=store: _coordinate_scheduled_background(
                args=scoped_args,
                settings=settings,
                store=store,
                account_name=account_label,
                start_background_scheduled_fn=start_background_scheduled_fn,
                stop_background_polling_fn=stop_background_polling_fn,
            ),
            status_message='正在协调后台轮询',
        )
        task_manager.start_pending()

    _refresh_status_snapshot_if_due(force=True, settle_seconds=0.01)

    def _scheduled_picker_snapshot(preferred_key: str | None) -> tuple[str, list[MenuItem], str | None]:
        _refresh_status_snapshot_if_due(settle_seconds=0.01)
        refreshed_rows = _fetch_status_rows()
        items = [MenuItem(str(index), _scheduled_picker_item_label(row)) for index, row in enumerate(refreshed_rows, start=1)]
        items += [
            MenuItem('n', '新建任务'),
            MenuItem('s', '查看全部抢机进度'),
            MenuItem('0', '返回首页'),
        ]
        return (
            _render_scheduled_job_picker(
                settings,
                account_label,
                refreshed_rows,
                page_status_lines=_scheduled_status_page_lines(),
            ),
            items,
            preferred_key or selected_key,
        )

    while True:
        status_rows = _fetch_status_rows()
        items = [MenuItem(str(index), _scheduled_picker_item_label(row)) for index, row in enumerate(status_rows, start=1)]
        items += [
            MenuItem('n', '新建任务'),
            MenuItem('s', '查看全部抢机进度'),
            MenuItem('0', '返回首页'),
        ]
        choice = _choose_menu(
            _render_scheduled_job_picker(
                settings,
                account_label,
                status_rows,
                page_status_lines=_scheduled_status_page_lines(),
            ),
            items,
            default_key=_menu_default_key(items, selected_key),
            refresh_fn=lambda preferred_key: _scheduled_picker_snapshot(preferred_key),
            refresh_revision_fn=lambda: _menu_refresh_revision(
                snapshot_store=snapshot_store,
                snapshot_keys=[snapshot_key],
                task_manager=task_manager,
                task_keys=_scheduled_refresh_task_keys(),
            ),
            refresh_interval_seconds=1.0,
            on_rendered_fn=task_manager.start_pending,
        )
        selected_key = choice
        scoped_args = _copy_args(args, account=account_label)
        if choice == '0':
            return
        if choice.lower() == 'n':
            try:
                new_job = _prompt_scheduled_job()
                if not new_job.name and not new_job.instance_id:
                    raise ValueError('任务至少要有 name 或 instance_id。')
                _persist_job_changes(
                    config_path=args.config,
                    settings=settings,
                    load_settings_fn=load_settings_fn,
                    validate_settings_fn=validate_settings_fn,
                    mutator=lambda jobs: jobs.append(_job_to_payload(new_job)),
                )
                settings = load_settings_fn(args.config)
                if get_task_enabled(store, account_label, 'scheduled_start', default_enabled=settings.tasks.scheduled_start.enabled):
                    scoped_settings = copy.deepcopy(settings)
                    scoped_settings.tasks.scheduled_start.jobs = [copy.deepcopy(new_job)]
                    task_scope = f'{account_label}:{scheduled_job_identity(new_job)}'
                    task_manager.submit(
                        'scheduled_auto_run',
                        scope=task_scope,
                        runner=lambda scoped_settings=scoped_settings, account_label=account_label, store=store: run_scheduled_start_cycle_fn(
                            settings=scoped_settings,
                            headed=args.headed,
                            state_file=args.state_file,
                            account_name=account_label,
                            force_run_now=True,
                            store=store,
                        ),
                        status_message='正在执行抢机器检查',
                    )
                    _nudge_background_tasks(task_manager)
                    transient_run_state[scheduled_job_identity(new_job)] = _scheduled_run_pending_state(
                        {
                            'job_name': scheduled_job_identity(new_job),
                            'enabled': True,
                            'target_time': new_job.target_time,
                            'advance_hours': new_job.advance_hours,
                            'schedule_mode': getattr(new_job, 'schedule_mode', 'daily') or 'daily',
                            'timezone': getattr(new_job, 'timezone', 'Asia/Shanghai') or 'Asia/Shanghai',
                            'daemon_running': bool(read_daemon_status(store).get('running')),
                        },
                        trigger_label='新建规则后自动执行',
                        task_type='scheduled_auto_run',
                        task_scope=task_scope,
                    )
                request_reload_fn(store)
                snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', f'all:{account_label}'))
                _queue_scheduled_background_sync(scoped_args)
                _refresh_status_snapshot_if_due(force=True, settle_seconds=0.01)
                _print_execution_summary('已创建抢机器任务', detail=f'job={scheduled_job_identity(new_job)}\n后台任务已排队执行')
                refreshed_rows = _fetch_status_rows()
                for index, row in enumerate(refreshed_rows, start=1):
                    if row['job_name'] == scheduled_job_identity(new_job):
                        selected_key = str(index)
                        break
            except _InteractiveCancel:
                continue
            except ValueError as exc:
                _print_execution_summary('创建失败', detail=str(exc))
            continue
        if choice.lower() == 's':
            _show_live_scheduled_status(
                job_name=None,
                fetch_rows_fn=lambda: _fetch_live_status_rows(),
                task_manager=task_manager,
                snapshot_store=snapshot_store,
                current_account=account_label,
                clear_scope_snapshot_on_exit=True,
                settings=settings,
            )
            continue
        current_status_rows = _fetch_status_rows()
        if not choice.isdigit() or not (1 <= int(choice) <= len(current_status_rows)):
            print('无效选择。')
            continue
        selected_row = current_status_rows[int(choice) - 1]
        selected_job = _find_scheduled_job(settings, selected_row['job_name'])
        detail_selected_key = '1'

        def _scheduled_detail_snapshot(preferred_key: str | None) -> tuple[str, list[MenuItem], str | None]:
            _refresh_status_snapshot_if_due(settle_seconds=0.01)
            refreshed_row = _fetch_single_status_row(selected_row['job_name'], selected_row)
            detail_items = _build_scheduled_detail_menu_items(bool(refreshed_row['enabled']), bool(refreshed_row.get('daemon_running')))
            return (
                _render_scheduled_job_detail(
                    selected_job,
                    refreshed_row,
                    account_label,
                    page_status_lines=_scheduled_status_page_lines(),
                ),
                detail_items,
                preferred_key or detail_selected_key,
            )

        while True:
            detail_items = _build_scheduled_detail_menu_items(bool(selected_row['enabled']), bool(selected_row.get('daemon_running')))
            detail_status_row = dict(selected_row)
            inner = _choose_menu(
                _render_scheduled_job_detail(
                    selected_job,
                    detail_status_row,
                    account_label,
                    page_status_lines=_scheduled_status_page_lines(),
                ),
                detail_items,
                default_key=_menu_default_key(detail_items, detail_selected_key),
                refresh_fn=lambda preferred_key: _scheduled_detail_snapshot(preferred_key),
                refresh_revision_fn=lambda: _menu_refresh_revision(
                    snapshot_store=snapshot_store,
                    snapshot_keys=[snapshot_key],
                    task_manager=task_manager,
                    task_keys=_scheduled_refresh_task_keys(),
                ),
                refresh_interval_seconds=1.0,
                on_rendered_fn=task_manager.start_pending,
            )
            detail_selected_key = inner
            if inner == '1':
                if not _confirm_action(
                    '立即执行一轮' if bool(selected_row['enabled']) else '恢复并执行一轮',
                    f'当前账号: {account_label}',
                    f'job: {selected_row["job_name"]}',
                    f'时间窗口: {selected_row["target_time"]} / 提前{selected_row["advance_hours"]}h',
                ):
                    continue
                if not bool(selected_row['enabled']):
                    set_job_enabled_fn(store, account_label, selected_row['job_name'], True)
                    _refresh_status_snapshot_if_due(force=True, settle_seconds=0.01)
                    selected_row = _fetch_single_status_row(selected_row['job_name'], selected_row)
                scoped_settings = copy.deepcopy(settings)
                scoped_settings.tasks.scheduled_start.jobs = [copy.deepcopy(selected_job)]
                task_scope = f'{account_label}:{selected_row["job_name"]}'
                task_manager.submit(
                    'scheduled_manual_run',
                    scope=task_scope,
                    runner=lambda scoped_settings=scoped_settings, account_label=account_label, store=store: run_scheduled_start_cycle_fn(
                        settings=scoped_settings,
                        headed=args.headed,
                        state_file=args.state_file,
                        account_name=account_label,
                        store=store,
                    ),
                    status_message='正在执行抢机器检查',
                )
                _nudge_background_tasks(task_manager)
                transient_run_state[selected_row['job_name']] = _scheduled_run_pending_state(
                    selected_row,
                    trigger_label='手动立即执行',
                    task_type='scheduled_manual_run',
                    task_scope=task_scope,
                )
                selected_row = _fetch_single_status_row(selected_row['job_name'], selected_row)
            elif inner == '2':
                _show_live_scheduled_status(
                    job_name=selected_row['job_name'],
                    fetch_rows_fn=lambda: _fetch_live_status_rows(job_name=selected_row['job_name']),
                    task_manager=task_manager,
                    snapshot_store=snapshot_store,
                    current_account=account_label,
                    clear_scope_snapshot_on_exit=True,
                )
            elif inner in {'3', '4'}:
                try:
                    legacy_edit_flow = inner == '3'
                    updated_job = _prompt_scheduled_job(selected_job)
                    _persist_job_changes(
                        config_path=args.config,
                        settings=settings,
                        load_settings_fn=load_settings_fn,
                        validate_settings_fn=validate_settings_fn,
                        mutator=lambda jobs: jobs.__setitem__(
                            next(index for index, item in enumerate(jobs) if item.get('name') == selected_row['job_name'] or item.get('instance_id') == selected_row['job_name']),
                            _job_to_payload(updated_job),
                        ),
                    )
                    settings = load_settings_fn(args.config)
                    selected_job = _find_scheduled_job(settings, updated_job.name or updated_job.instance_id)
                    selected_job_name = scheduled_job_identity(selected_job)
                    if selected_job_name != selected_row['job_name']:
                        snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', f'job:{account_label}:{selected_row["job_name"]}'))
                    snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', f'job:{account_label}:{selected_job_name}'))
                    current_control = store.get_scheduled_job_control(account_label, selected_job_name) or {}
                    if (
                        not legacy_edit_flow
                        and not bool(current_control.get('enabled', True))
                        and str(current_control.get('source') or '') == 'scheduled_once_complete'
                    ):
                        set_job_enabled_fn(store, account_label, selected_job_name, True)
                    _refresh_status_snapshot_if_due(force=True, settle_seconds=0.01)
                    selected_row = _fetch_single_status_row(selected_job_name, selected_row)
                    if not legacy_edit_flow and bool(selected_row['enabled']) and get_task_enabled(store, account_label, 'scheduled_start', default_enabled=settings.tasks.scheduled_start.enabled):
                        scoped_settings = copy.deepcopy(settings)
                        scoped_settings.tasks.scheduled_start.jobs = [copy.deepcopy(selected_job)]
                        task_scope = f'{account_label}:{selected_job_name}'
                        task_manager.submit(
                            'scheduled_auto_run',
                            scope=task_scope,
                            runner=lambda scoped_settings=scoped_settings, account_label=account_label, store=store: run_scheduled_start_cycle_fn(
                                settings=scoped_settings,
                                headed=args.headed,
                                state_file=args.state_file,
                                account_name=account_label,
                                force_run_now=True,
                                store=store,
                            ),
                            status_message='正在执行抢机器检查',
                        )
                        _nudge_background_tasks(task_manager)
                        transient_run_state[selected_row['job_name']] = _scheduled_run_pending_state(
                            selected_row,
                            trigger_label='修改规则后自动执行',
                            task_type='scheduled_auto_run',
                            task_scope=task_scope,
                        )
                        selected_row = _fetch_single_status_row(scheduled_job_identity(selected_job), selected_row)
                    request_reload_fn(store)
                    snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', f'all:{account_label}'))
                    _queue_scheduled_background_sync(scoped_args)
                    _refresh_status_snapshot_if_due(force=True, settle_seconds=0.01)
                    continue
                except _InteractiveCancel:
                    continue
                except (ValueError, StopIteration) as exc:
                    _print_execution_summary('更新失败', detail=str(exc))
            elif inner == '5':
                next_enabled = not bool(selected_row['enabled'])
                set_job_enabled_fn(store, account_label, selected_row['job_name'], next_enabled)
                if not next_enabled:
                    transient_run_state.pop(selected_row['job_name'], None)
                request_reload_fn(store)
                snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', f'all:{account_label}'))
                _refresh_status_snapshot_if_due(force=True, settle_seconds=0.01)
                selected_row = _fetch_single_status_row(selected_row['job_name'], selected_row)
                _queue_scheduled_background_sync(scoped_args)
                _print_execution_summary(
                    '已恢复任务' if next_enabled else '已暂停任务',
                    detail=f"job={selected_row['job_name']}\n后台状态协调已排队",
                )
            elif inner == '6':
                if not _confirm_action('删除任务', f'当前账号: {account_label}', f'job: {selected_row["job_name"]}'):
                    continue
                try:
                    transient_run_state.pop(selected_row['job_name'], None)
                    snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', f'job:{account_label}:{selected_row["job_name"]}'))
                    _persist_job_changes(
                        config_path=args.config,
                        settings=settings,
                        load_settings_fn=load_settings_fn,
                        validate_settings_fn=validate_settings_fn,
                        mutator=lambda jobs: jobs.__delitem__(
                            next(index for index, item in enumerate(jobs) if item.get('name') == selected_row['job_name'] or item.get('instance_id') == selected_row['job_name'])
                        ),
                    )
                    settings = load_settings_fn(args.config)
                    request_reload_fn(store)
                    snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', f'all:{account_label}'))
                    _queue_scheduled_background_sync(scoped_args)
                    _refresh_status_snapshot_if_due(force=True, settle_seconds=0.01)
                    _print_execution_summary('已删除任务', detail=f"job={selected_row['job_name']}\n后台状态协调已排队")
                    break
                except (ValueError, StopIteration) as exc:
                    _print_execution_summary('删除失败', detail=str(exc))
            elif inner == '0':
                break
            else:
                print('无效选择。')


def _keeper_menu(
    args: argparse.Namespace,
    *,
    settings: Settings,
    current_account: str | None,
    set_task_enabled_fn,
    request_reload_fn,
    store,
    keeper_probe_rows_fn,
    run_keeper_only_fn,
    command_history_fn,
    load_settings_fn,
    validate_settings_fn,
    task_manager: InteractiveTaskManager | None = None,
    snapshot_store: InteractiveSnapshotStore | None = None,
) -> None:
    owns_runtime = False
    if task_manager is None or snapshot_store is None:
        snapshot_store = InteractiveSnapshotStore()
        task_manager = InteractiveTaskManager(snapshot_store=snapshot_store, max_workers=_interactive_max_workers(settings))
        owns_runtime = True
    account_label = current_account or 'default'
    account_scope = current_account or 'default'
    probe_snapshot_key = _snapshot_key('keeper_probe', account_scope)
    selected_key = '1'
    
    def _queue_keeper_probe_refresh(*, settle_seconds: float = 0.0) -> None:
        _submit_snapshot_task(
            task_manager=task_manager,
            snapshot_store=snapshot_store,
            task_type='keeper_probe_refresh',
            scope=account_scope,
            snapshot_key=probe_snapshot_key,
            runner=lambda: keeper_probe_rows_fn(settings, store, account_name=account_label),
            status_message='正在刷新 Keeper 检测',
            replace_queued=True,
        )
        if settle_seconds > 0:
            _nudge_background_tasks(task_manager, settle_seconds=settle_seconds)
        else:
            task_manager.start_pending()

    def _current_probe_rows() -> list[dict[str, Any]]:
        task_manager.drain_completed()
        rows = snapshot_store.get_snapshot(probe_snapshot_key)
        return list(rows) if isinstance(rows, list) else []

    def _show_keeper_execution_results(*, trigger_label: str) -> None:
        task_manager.submit(
            'keeper_execute_run',
            scope=account_scope,
            runner=lambda: run_keeper_only_fn(
                settings=settings,
                headed=args.headed,
                account_name=account_label,
                store=store,
            ),
            status_message='正在执行 Keeper',
        )
        _nudge_background_tasks(task_manager, settle_seconds=0.01)
        post_selected_key = '0'

        def _keeper_execution_snapshot(preferred_key: str | None) -> tuple[str, list[MenuItem], str | None]:
            task_manager.drain_completed()
            execute_task = task_manager.get_task('keeper_execute_run', account_scope)
            status = _page_status_from_task_result(
                execute_task,
                success_message='本轮 Keeper 执行完成',
                idle_message='等待开始执行',
            )
            active_task = execute_task if execute_task is not None and execute_task.status in {'queued', 'running'} else None
            results = list(execute_task.payload) if execute_task is not None and isinstance(execute_task.payload, list) else []
            if active_task is not None:
                post_items = [MenuItem('0', '返回 Keeper 首页')]
            else:
                post_items = [MenuItem('1', '重新检测'), MenuItem('2', '查看最近 Keeper 记录'), MenuItem('0', '返回 Keeper 首页')]
            return (
                _render_keeper_execution_page(
                    results,
                    page_status_lines=_page_status_lines(status, active_task=active_task, progress_label='执行进度'),
                ),
                post_items,
                preferred_key or post_selected_key,
            )

        while True:
            execute_task = task_manager.get_task('keeper_execute_run', account_scope)
            execution_status = _page_status_from_task_result(
                execute_task,
                success_message='本轮 Keeper 执行完成',
                idle_message='等待开始执行',
            )
            active_execute_task = execute_task if execute_task is not None and execute_task.status in {'queued', 'running'} else None
            execution_results = list(execute_task.payload) if execute_task is not None and isinstance(execute_task.payload, list) else []
            if active_execute_task is not None:
                post_items = [MenuItem('0', '返回 Keeper 首页')]
            else:
                post_items = [MenuItem('1', '重新检测'), MenuItem('2', '查看最近 Keeper 记录'), MenuItem('0', '返回 Keeper 首页')]
            post = _choose_menu_with_refresh(
                _render_keeper_execution_page(
                    execution_results,
                    page_status_lines=_page_status_lines(
                        execution_status,
                        active_task=active_execute_task,
                        progress_label='执行进度',
                    ),
                ),
                post_items,
                default_key=_menu_default_key(post_items, post_selected_key),
                refresh_fn=lambda preferred_key: _keeper_execution_snapshot(preferred_key),
                refresh_revision_fn=lambda: _menu_refresh_revision(
                    task_manager=task_manager,
                    task_keys=[task_manager.task_key('keeper_execute_run', account_scope)],
                ),
                refresh_interval_seconds=1.0,
                on_rendered_fn=task_manager.start_pending,
                refresh_policy='always',
                pre_refresh_fn=task_manager.drain_completed,
            )
            post_selected_key = post
            execute_task = task_manager.get_task('keeper_execute_run', account_scope)
            if execute_task is not None and execute_task.status in {'queued', 'running'}:
                if post == '0':
                    break
                continue
            if post == '1':
                _queue_keeper_probe_refresh(settle_seconds=0.01)
                break
            if post == '2':
                code, output = _run_captured_action(
                    '最近 Keeper 记录',
                    lambda: command_history_fn(_copy_args(args, account=account_label, task='keeper', event_type=None, limit=20, json=False, headed=False)),
                )
                _show_result_screen('最近 Keeper 记录', output, code=code)
                continue
            if post == '0':
                return
            print('无效选择。')

    try:
        while True:
            items = [
                MenuItem('1', '查看本次 Keeper 计划'),
                MenuItem('2', '编辑 Keeper 规则'),
                MenuItem('3', '暂停/恢复 Keeper'),
                MenuItem('4', '立即执行一次 Keeper'),
                MenuItem('0', '返回首页'),
            ]
            choice = _choose_menu_with_refresh(
                _render_keeper_rules(settings, account_label, store),
                items,
                default_key=_menu_default_key(items, selected_key),
                refresh_fn=lambda preferred_key: (
                    _render_keeper_rules(settings, account_label, store),
                    items,
                    preferred_key or selected_key,
                ),
                refresh_interval_seconds=1.0,
                on_rendered_fn=task_manager.start_pending,
                refresh_policy='always',
                pre_refresh_fn=task_manager.drain_completed,
            )
            selected_key = choice
            if choice == '1':
                _queue_keeper_probe_refresh(settle_seconds=0.01)
                inner_selected_key = '1'

                def _keeper_probe_snapshot(preferred_key: str | None) -> tuple[str, list[MenuItem], str | None]:
                    rows = _current_probe_rows()
                    probe_task = task_manager.get_task('keeper_probe_refresh', account_scope)
                    status = _page_status_from_tasks(
                        snapshot_store=snapshot_store,
                        snapshot_key=probe_snapshot_key,
                        primary_task=probe_task,
                    )
                    inner_items = [MenuItem('1', '立即执行一次 Keeper'), MenuItem('2', '重新检测'), MenuItem('3', '查看全部实例状态'), MenuItem('0', '返回 Keeper 首页')]
                    status_lines = _page_status_lines(
                        status,
                        active_task=probe_task,
                        progress_label='检测进度',
                        show_progress=False,
                    )
                    if rows:
                        status_lines = [*status_lines, *_keeper_probe_schedule_lines(settings, store, account_name=account_label)]
                    return (
                        _render_keeper_probe_page(
                            rows,
                            page_status_lines=status_lines,
                        ),
                        inner_items,
                        preferred_key or inner_selected_key,
                    )

                while True:
                    probe_rows = _current_probe_rows()
                    probe_task = task_manager.get_task('keeper_probe_refresh', account_scope)
                    status = _page_status_from_tasks(
                        snapshot_store=snapshot_store,
                        snapshot_key=probe_snapshot_key,
                        primary_task=probe_task,
                    )
                    inner_items = [MenuItem('1', '立即执行一次 Keeper'), MenuItem('2', '重新检测'), MenuItem('3', '查看全部实例状态'), MenuItem('0', '返回 Keeper 首页')]
                    probe_status_lines = _page_status_lines(
                        status,
                        active_task=probe_task,
                        progress_label='检测进度',
                        show_progress=False,
                    )
                    if probe_rows:
                        probe_status_lines = [*probe_status_lines, *_keeper_probe_schedule_lines(settings, store, account_name=account_label)]
                    inner = _choose_menu_with_refresh(
                        _render_keeper_probe_page(
                            probe_rows,
                            page_status_lines=probe_status_lines,
                        ),
                        inner_items,
                        default_key=_menu_default_key(inner_items, inner_selected_key),
                        refresh_fn=lambda preferred_key: _keeper_probe_snapshot(preferred_key),
                        refresh_revision_fn=lambda: _menu_refresh_revision(
                            snapshot_store=snapshot_store,
                            snapshot_keys=[probe_snapshot_key],
                            task_manager=task_manager,
                            task_keys=[task_manager.task_key('keeper_probe_refresh', account_scope)],
                        ),
                        refresh_interval_seconds=1.0,
                        on_rendered_fn=task_manager.start_pending,
                        refresh_policy='always',
                        pre_refresh_fn=task_manager.drain_completed,
                    )
                    inner_selected_key = inner
                    probe_rows = _current_probe_rows()
                    if inner == '1':
                        ready_count = sum(1 for row in probe_rows if row.get('eligible'))
                        if not _confirm_action('立即执行一次 Keeper', f'当前账号: {account_label}', f'本次将处理 {ready_count} 台'):
                            continue
                        _show_keeper_execution_results(trigger_label='手动开始执行')
                    elif inner == '2':
                        _queue_keeper_probe_refresh(settle_seconds=0.01)
                        continue
                    elif inner == '3':
                        _browse_keeper_probe(
                            settings=settings,
                            store=store,
                            current_account=current_account,
                            keeper_probe_rows_fn=keeper_probe_rows_fn,
                            task_manager=task_manager,
                            snapshot_store=snapshot_store,
                        )
                        continue
                    elif inner == '0':
                        break
                    else:
                        print('无效选择。')
            elif choice == '2':
                try:
                    updated_keeper = _prompt_keeper_settings(settings.tasks.keeper)
                    _persist_keeper_changes(
                        config_path=args.config,
                        settings=settings,
                        load_settings_fn=load_settings_fn,
                        validate_settings_fn=validate_settings_fn,
                        keeper_settings=updated_keeper,
                    )
                    settings = load_settings_fn(args.config)
                    request_reload_fn(store)
                    _show_keeper_execution_results(trigger_label='修改规则后自动执行')
                except _InteractiveCancel:
                    continue
                except ValueError as exc:
                    _print_execution_summary('更新失败', detail=str(exc))
            elif choice == '3':
                next_enabled = not get_task_enabled(store, account_label, 'keeper', default_enabled=settings.tasks.keeper.enabled)
                set_task_enabled_fn(store, account_label, 'keeper', next_enabled)
                _print_execution_summary('已更新 Keeper 状态', detail=f'account={account_label} enabled={next_enabled}')
            elif choice == '4':
                _show_keeper_execution_results(trigger_label='手动开始执行')
            elif choice == '0':
                return
            else:
                print('无效选择。')
    finally:
        if owns_runtime:
            _nudge_background_tasks(task_manager, settle_seconds=0.01)
            task_manager.shutdown(wait=False)


def _account_menu(
    args: argparse.Namespace,
    *,
    settings: Settings,
    store,
    current_account: str | None,
    command_accounts_fn,
    command_login_fn,
    keeper_probe_rows_fn,
    scheduled_job_status_rows_fn,
    load_settings_fn: Callable[[str], Settings],
    validate_settings_fn: Callable[[Settings, str], list[str]],
    task_manager: InteractiveTaskManager,
    snapshot_store: InteractiveSnapshotStore,
) -> tuple[Settings, str | None]:
    selected_key = '1'
    while True:
        items = [
            MenuItem('1', '查看账号状态'),
            MenuItem('2', '切换到新账号'),
            MenuItem('3', '重新验证当前登录状态'),
            MenuItem('0', '返回首页'),
        ]
        choice = _choose_menu(
            '账号',
            items,
            default_key=_menu_default_key(items, selected_key),
        )
        selected_key = choice
        if choice == '1':
            current_account = _browse_account_detail(
                args=args,
                settings=settings,
                store=store,
                current_account=current_account,
                command_login_fn=command_login_fn,
                keeper_probe_rows_fn=keeper_probe_rows_fn,
                scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
                task_manager=task_manager,
                snapshot_store=snapshot_store,
            )
        elif choice == '2':
            settings, current_account = _switch_to_new_account(
                args=args,
                settings=settings,
                store=store,
                current_account=current_account,
                command_login_fn=command_login_fn,
                load_settings_fn=load_settings_fn,
                validate_settings_fn=validate_settings_fn,
            )
            for prefix in ('account_runtime:', 'diagnostics:', 'healthcheck:', 'scheduled_progress:', 'dashboard:'):
                snapshot_store.clear_prefix(prefix)
        elif choice == '3':
            current_account = _browse_account_detail(
                args=args,
                settings=settings,
                store=store,
                current_account=current_account,
                command_login_fn=command_login_fn,
                keeper_probe_rows_fn=keeper_probe_rows_fn,
                scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
                task_manager=task_manager,
                snapshot_store=snapshot_store,
                trigger_verify_on_open=True,
            )
        elif choice == '0':
            return settings, current_account
        else:
            print('无效选择。')


def _records_menu(
    args: argparse.Namespace,
    *,
    current_account: str | None,
    command_history_fn,
    command_auth_report_fn,
    settings: Settings,
    store,
    keeper_probe_rows_fn,
    scheduled_job_status_rows_fn,
) -> None:
    selected_key = '1'
    while True:
        items = [
            MenuItem('1', '查看最近记录'),
            MenuItem('2', '查看认证异常'),
            MenuItem('0', '返回首页'),
        ]
        choice = _choose_menu(
            _render_records_overview(settings, store, current_account=current_account),
            items,
            default_key=_menu_default_key(items, selected_key),
        )
        selected_key = choice
        scoped_args = _copy_args(args, account=current_account)
        if choice == '1':
            filters = _history_filter_wizard(settings, current_account)
            if filters is None:
                continue
            rows = store.read_history(
                account_name=filters.account,
                task_type=filters.task,
                event_type=filters.event_type,
                limit=filters.limit,
            )
            _browse_history_records(
                settings=settings,
                store=store,
                current_account=current_account,
                rows=rows,
                keeper_probe_rows_fn=keeper_probe_rows_fn,
                scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
            )
        elif choice == '2':
            filters = _auth_report_filter_wizard(settings, current_account)
            if filters is None:
                continue
            code, output = _run_captured_action(
                '认证异常',
                lambda: command_auth_report_fn(_copy_args(scoped_args, account=filters.account, limit=filters.limit, json=False, only_unmapped=filters.only_unmapped, only_likely_auth=filters.only_likely_auth, suggest_patch=False, apply_suggested_patch=False, headed=False)),
            )
            _show_result_screen('认证异常', output, code=code)
        elif choice == '0':
            return
        else:
            print('无效选择。')


def _diagnostics_menu(
    args: argparse.Namespace,
    *,
    current_account: str | None,
    command_list_instances_fn,
    command_healthcheck_fn,
    settings: Settings,
    store,
    keeper_probe_rows_fn,
    load_settings_fn,
    validate_settings_fn,
    task_manager: InteractiveTaskManager,
    snapshot_store: InteractiveSnapshotStore,
    clear_scope_snapshots_on_exit: bool = False,
    service_status_fn: Callable[[], dict[str, Any]] = read_launch_agent_status,
    service_start_fn: Callable[[], Any] = start_launch_agent,
    service_stop_fn: Callable[[], Any] = stop_launch_agent,
) -> None:
    account_scope = current_account or 'default'
    instance_snapshot_key = _snapshot_key('instances', account_scope)
    keeper_snapshot_key = _snapshot_key('keeper_probe', account_scope)
    healthcheck_snapshot_key = _snapshot_key('healthcheck', account_scope)
    config_snapshot_key = _snapshot_key('config_diagnostics', account_scope)
    selected_key = '1'

    def _queue_diagnostics_refresh(*, force_related: bool = False) -> None:
        if force_related or snapshot_store.get_snapshot(instance_snapshot_key) is None:
            _submit_snapshot_task(
                task_manager=task_manager,
                snapshot_store=snapshot_store,
                task_type='instances_refresh',
                scope=account_scope,
                snapshot_key=instance_snapshot_key,
                runner=lambda: _load_instance_rows_via_command(
                    args=args,
                    current_account=current_account,
                    command_list_instances_fn=command_list_instances_fn,
                ),
                status_message='正在刷新实例列表',
                replace_queued=True,
            )
        if force_related or snapshot_store.get_snapshot(keeper_snapshot_key) is None:
            _submit_snapshot_task(
                task_manager=task_manager,
                snapshot_store=snapshot_store,
                task_type='keeper_probe_refresh',
                scope=account_scope,
                snapshot_key=keeper_snapshot_key,
                runner=lambda: keeper_probe_rows_fn(settings, store, account_name=current_account),
                status_message='正在刷新 Keeper 探测',
                replace_queued=True,
            )
        task_manager.start_pending()

    def _diagnostics_body() -> str:
        task_manager.drain_completed()
        diagnostics_snapshot = _diagnostics_snapshot_payload(
            snapshot_store=snapshot_store,
            account_name=account_scope,
            task_manager=task_manager,
            store=store,
        )
        status = _diagnostics_page_status(
            snapshot_store=snapshot_store,
            account_scope=account_scope,
            instance_task=task_manager.get_task('instances_refresh', account_scope),
            keeper_task=task_manager.get_task('keeper_probe_refresh', account_scope),
            healthcheck_task=task_manager.get_task('healthcheck_run', account_scope),
        )
        return _render_diagnostics_page(
            _account_display_name(settings, current_account),
            diagnostics_snapshot,
            page_status_lines=_page_status_lines(status),
        )

    _queue_diagnostics_refresh()
    try:
        while True:
            items = [
                MenuItem('1', '查看实例'),
                MenuItem('2', '查看 Keeper 探测'),
                MenuItem('3', '健康自检'),
                MenuItem('4', '配置诊断'),
                MenuItem('5', '启动后台服务'),
                MenuItem('6', '停止后台服务'),
                MenuItem('7', '重启后台服务'),
                MenuItem('0', '返回首页'),
            ]
            choice = _choose_menu_with_refresh(
                _diagnostics_body(),
                items,
                default_key=_menu_default_key(items, selected_key),
                refresh_fn=lambda preferred_key: (_diagnostics_body(), items, preferred_key or selected_key),
                refresh_revision_fn=lambda: _menu_refresh_revision(
                    snapshot_store=snapshot_store,
                    snapshot_keys=[
                        instance_snapshot_key,
                        keeper_snapshot_key,
                        healthcheck_snapshot_key,
                        config_snapshot_key,
                    ],
                    task_manager=task_manager,
                    task_keys=[
                        task_manager.task_key('instances_refresh', account_scope),
                        task_manager.task_key('keeper_probe_refresh', account_scope),
                        task_manager.task_key('healthcheck_run', account_scope),
                    ],
                ),
                refresh_interval_seconds=1.0,
                on_rendered_fn=task_manager.start_pending,
                refresh_policy='always',
                pre_refresh_fn=task_manager.drain_completed,
            )
            selected_key = choice
            if choice == '1':
                _browse_instance_list(
                    args=args,
                    current_account=current_account,
                    settings=settings,
                    command_list_instances_fn=command_list_instances_fn,
                    task_manager=task_manager,
                    snapshot_store=snapshot_store,
                )
                _queue_diagnostics_refresh(force_related=False)
            elif choice == '2':
                _browse_keeper_probe(
                    settings=settings,
                    store=store,
                    current_account=current_account,
                    keeper_probe_rows_fn=keeper_probe_rows_fn,
                    task_manager=task_manager,
                    snapshot_store=snapshot_store,
                )
                _queue_diagnostics_refresh(force_related=False)
            elif choice == '3':
                _browse_healthcheck_detail(
                    args=args,
                    current_account=current_account,
                    command_healthcheck_fn=command_healthcheck_fn,
                    task_manager=task_manager,
                    snapshot_store=snapshot_store,
                )
                _queue_diagnostics_refresh(force_related=False)
            elif choice == '4':
                try:
                    body = _render_config_diagnostics(
                        settings=settings,
                        current_account=current_account,
                        config_path=args.config,
                        load_settings_fn=load_settings_fn,
                        validate_settings_fn=validate_settings_fn,
                    )
                    body_lines = [line.strip() for line in body.splitlines() if line.strip()]
                    _store_snapshot(
                        snapshot_store,
                        config_snapshot_key,
                        {
                            'status': '成功',
                            'summary': body_lines[0] if body_lines else '配置诊断完成',
                            'body': body,
                        },
                        status_message='最近更新',
                    )
                    _queue_diagnostics_refresh(force_related=False)
                    _show_result_screen('配置诊断', body)
                except ValueError as exc:
                    task_manager.record_resource_error(str(exc))
                    snapshot_store.record_failure(config_snapshot_key, _friendly_resource_error_message(str(exc)))
                    _queue_diagnostics_refresh(force_related=False)
                    _print_execution_summary('配置诊断失败', detail=_friendly_resource_error_message(str(exc)))
            elif choice == '5':
                service_status = service_status_fn() if callable(service_status_fn) else {}
                if not bool(service_status.get('installed')):
                    _print_execution_summary(
                        '后台服务未安装',
                        detail=f'请先执行: python main.py service-install --config {args.config}',
                    )
                    continue
                if bool(service_status.get('loaded')):
                    _append_interactive_service_log(args.config, f'LaunchAgent 已在运行 label={DEFAULT_SERVICE_LABEL}')
                    _record_interactive_service_event(store, action='start', message='LaunchAgent 已在运行')
                    _print_execution_summary('后台服务已在运行', detail=str(service_status.get('label') or ''))
                    continue
                code, detail = _normalize_service_action_result(service_start_fn())
                if code == 0:
                    _append_interactive_service_log(args.config, f'已启动 LaunchAgent label={DEFAULT_SERVICE_LABEL}')
                    _record_interactive_service_event(store, action='start', message='已启动 LaunchAgent')
                else:
                    _record_interactive_service_event(store, action='start', message='启动 LaunchAgent 失败', level='error', detail=detail or '')
                _print_execution_summary('已启动后台服务' if code == 0 else '启动后台服务失败', code=code, detail=detail or None)
            elif choice == '6':
                service_status = service_status_fn() if callable(service_status_fn) else {}
                if not bool(service_status.get('installed')):
                    _append_interactive_service_log(args.config, f'LaunchAgent 未安装 label={DEFAULT_SERVICE_LABEL}')
                    _record_interactive_service_event(store, action='stop', message='LaunchAgent 未安装')
                    _print_execution_summary('后台服务未安装')
                    continue
                if not bool(service_status.get('loaded')):
                    _append_interactive_service_log(args.config, f'LaunchAgent 已停止 label={DEFAULT_SERVICE_LABEL}')
                    _record_interactive_service_event(store, action='stop', message='LaunchAgent 已停止')
                    _print_execution_summary('后台服务未在运行', detail=str(service_status.get('label') or ''))
                    continue
                code, detail = _normalize_service_action_result(service_stop_fn())
                if code == 0:
                    _append_interactive_service_log(args.config, f'已停止 LaunchAgent label={DEFAULT_SERVICE_LABEL}')
                    _record_interactive_service_event(store, action='stop', message='已停止 LaunchAgent')
                else:
                    _record_interactive_service_event(store, action='stop', message='停止 LaunchAgent 失败', level='error', detail=detail or '')
                _print_execution_summary('已停止后台服务' if code == 0 else '停止后台服务失败', code=code, detail=detail or None)
            elif choice == '7':
                service_status = service_status_fn() if callable(service_status_fn) else {}
                if not bool(service_status.get('installed')):
                    _print_execution_summary(
                        '后台服务未安装',
                        detail=f'请先执行: python main.py service-install --config {args.config}',
                    )
                    continue
                if bool(service_status.get('loaded')):
                    stop_code, stop_detail = _normalize_service_action_result(service_stop_fn())
                    if stop_code != 0:
                        _record_interactive_service_event(store, action='restart', message='重启 LaunchAgent 失败', level='error', detail=stop_detail or '')
                        _print_execution_summary('重启后台服务失败', code=stop_code, detail=stop_detail or None)
                        continue
                code, detail = _normalize_service_action_result(service_start_fn())
                if code == 0:
                    _append_interactive_service_log(args.config, f'已重启 LaunchAgent label={DEFAULT_SERVICE_LABEL}')
                    _record_interactive_service_event(store, action='restart', message='已重启 LaunchAgent')
                else:
                    _record_interactive_service_event(store, action='restart', message='重启 LaunchAgent 失败', level='error', detail=detail or '')
                _print_execution_summary('已重启后台服务' if code == 0 else '重启后台服务失败', code=code, detail=detail or None)
            elif choice == '0':
                return
            else:
                print('无效选择。')
    finally:
        if clear_scope_snapshots_on_exit:
            _clear_diagnostics_scope_snapshots(snapshot_store, current_account=current_account)


def run_interactive(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings],
    validate_settings_fn: Callable[[Settings, str], list[str]],
    create_store_fn: Callable[[Settings], Any],
    render_dashboard_fn: Callable[[dict[str, Any]], str],
    build_dashboard_view_fn: Callable[..., dict[str, Any]],
    set_task_enabled_fn: Callable[..., None],
    set_job_enabled_fn: Callable[..., None],
    set_job_override_fn: Callable[..., None],
    clear_runtime_controls_fn: Callable[..., None],
    runtime_controls_snapshot_fn: Callable[..., dict[str, Any]],
    request_reload_fn: Callable[..., None],
    run_variant_fn: Callable[..., int],
    start_background_scheduled_fn: Callable[..., tuple[int, str]] | None,
    stop_background_polling_fn: Callable[..., tuple[int, str]] | None,
    run_keeper_only_fn: Callable[..., list[Any]],
    run_scheduled_start_cycle_fn: Callable[..., list[Any]],
    command_config_show_fn: Callable[..., int],
    command_config_resolve_fn: Callable[..., int],
    command_config_edit_fn: Callable[..., int],
    command_history_fn: Callable[..., int],
    command_keeper_probe_fn: Callable[..., int],
    command_auth_report_fn: Callable[..., int],
    command_list_instances_fn: Callable[..., int],
    command_accounts_fn: Callable[..., int],
    command_login_fn: Callable[..., int],
    command_healthcheck_fn: Callable[..., int],
    list_instances_panel_rows_fn: Callable[..., list[dict[str, Any]]],
    history_panel_rows_fn: Callable[..., list[Any]],
    auth_panel_rows_fn: Callable[..., list[Any]],
    keeper_probe_rows_fn: Callable[..., list[dict[str, Any]]],
    scheduled_job_status_rows_fn: Callable[..., list[dict[str, Any]]],
    scheduled_candidate_panel_data_fn: Callable[..., dict[str, Any] | None],
    render_candidate_explanation_fn: Callable[[dict[str, Any] | None], str],
) -> int:
    reset_thread_capture_state()
    settings = load_settings_fn(args.config)
    store = create_store_fn(settings)
    current_account = _pick_default_account(settings, getattr(args, 'account', None), store)
    selected_key = '1'
    snapshot_store = InteractiveSnapshotStore()
    task_manager = InteractiveTaskManager(snapshot_store=snapshot_store, max_workers=_interactive_max_workers(settings))
    _hide_cursor()
    try:
        while True:
            task_manager.drain_completed()
            settings = load_settings_fn(args.config)
            store = create_store_fn(settings)
            current_account = _pick_default_account(settings, current_account, store)
            dashboard_scope = current_account or 'default'
            dashboard_snapshot_key = _snapshot_key('dashboard', dashboard_scope)
            _submit_snapshot_task(
                task_manager=task_manager,
                snapshot_store=snapshot_store,
                task_type='dashboard_refresh',
                scope=dashboard_scope,
                snapshot_key=dashboard_snapshot_key,
                runner=lambda settings=settings, store=store, current_account=current_account: _dashboard_snapshot_view(
                    settings=settings,
                    store=store,
                    current_account=current_account,
                    scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
                    snapshot_store=snapshot_store,
                ),
                status_message='正在刷新首页概览',
            )
            task_manager.drain_completed()
            snapshot_view = snapshot_store.get_snapshot(dashboard_snapshot_key)
            if isinstance(snapshot_view, dict):
                view = copy.deepcopy(snapshot_view)
            else:
                view = _dashboard_placeholder_view(
                    settings=settings,
                    store=store,
                    current_account=current_account,
                    scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
                )
            view['current_account'] = _account_display_name(settings, current_account)
            dashboard_status = snapshot_store.page_status(
                dashboard_snapshot_key,
                task_manager.get_task('dashboard_refresh', dashboard_scope),
            )
            view['page_status_lines'] = _page_status_lines(dashboard_status)
            warning_text = ''
            current_row = view.get('current_account_row') or {}
            if str(current_row.get('status') or '') == 'not_configured':
                warning_text = '\n\n注意：当前账号未配置 token 或密码登录，很多功能会直接失败。请先到“账号”里切换或刷新登录。'
            items = [
                MenuItem('1', '抢机器'),
                MenuItem('2', 'Keeper'),
                MenuItem('3', '账号'),
                MenuItem('4', '诊断'),
                MenuItem('0', '退出'),
            ]
            choice = _choose_menu(
                render_dashboard_fn(view) + warning_text,
                items,
                default_key=_menu_default_key(items, selected_key),
                on_rendered_fn=task_manager.start_pending,
            )
            selected_key = choice
            if choice == '1':
                _scheduled_menu(
                    args,
                    settings=settings,
                    current_account=current_account,
                    run_variant_fn=run_variant_fn,
                    start_background_scheduled_fn=start_background_scheduled_fn,
                    stop_background_polling_fn=stop_background_polling_fn,
                    run_scheduled_start_cycle_fn=run_scheduled_start_cycle_fn,
                    set_job_enabled_fn=set_job_enabled_fn,
                    set_job_override_fn=set_job_override_fn,
                    request_reload_fn=request_reload_fn,
                    store=store,
                    scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
                    load_settings_fn=load_settings_fn,
                    validate_settings_fn=validate_settings_fn,
                    task_manager=task_manager,
                    snapshot_store=snapshot_store,
                )
            elif choice == '2':
                _keeper_menu(
                    args,
                    settings=settings,
                    current_account=current_account,
                    set_task_enabled_fn=set_task_enabled_fn,
                    request_reload_fn=request_reload_fn,
                    store=store,
                    keeper_probe_rows_fn=keeper_probe_rows_fn,
                    run_keeper_only_fn=run_keeper_only_fn,
                    command_history_fn=command_history_fn,
                    load_settings_fn=load_settings_fn,
                    validate_settings_fn=validate_settings_fn,
                    task_manager=task_manager,
                    snapshot_store=snapshot_store,
                )
            elif choice == '3':
                settings, current_account = _account_menu(
                    args,
                    settings=settings,
                    store=store,
                    current_account=current_account,
                    command_accounts_fn=command_accounts_fn,
                    command_login_fn=command_login_fn,
                    keeper_probe_rows_fn=keeper_probe_rows_fn,
                    scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
                    load_settings_fn=load_settings_fn,
                    validate_settings_fn=validate_settings_fn,
                    task_manager=task_manager,
                    snapshot_store=snapshot_store,
                )
            elif choice == '4':
                _diagnostics_menu(
                    args,
                    current_account=current_account,
                    command_list_instances_fn=command_list_instances_fn,
                    command_healthcheck_fn=command_healthcheck_fn,
                    settings=settings,
                    store=store,
                    keeper_probe_rows_fn=keeper_probe_rows_fn,
                    load_settings_fn=load_settings_fn,
                    validate_settings_fn=validate_settings_fn,
                    task_manager=task_manager,
                    snapshot_store=snapshot_store,
                    clear_scope_snapshots_on_exit=True,
                )
            elif choice in {'0', 'q', 'quit', 'exit'}:
                return 0
            else:
                print('无效选择。')
    finally:
        _nudge_background_tasks(task_manager, settle_seconds=0.01)
        task_manager.shutdown(wait=False)
        reset_thread_capture_state()
        _show_cursor()
