from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any

RESET = '\033[0m'
DIM = '\033[38;5;245m'
BLUE = '\033[38;5;75m'
CYAN = '\033[38;5;80m'
GREEN = '\033[38;5;114m'
YELLOW = '\033[38;5;179m'
RED = '\033[38;5;174m'
ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
ISO_DATETIME_RE = re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?')


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
