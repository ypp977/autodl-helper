from __future__ import annotations

import unicodedata

RESET = '\033[0m'
BOLD = '\033[1m'
DIM = '\033[2m'
GREEN = '\033[38;5;34m'
YELLOW = '\033[38;5;136m'
RED = '\033[38;5;160m'
CYAN = '\033[38;5;37m'
BLUE = '\033[38;5;32m'


def color(text: str, ansi: str, *, enabled: bool = True) -> str:
    return f'{ansi}{text}{RESET}' if enabled else text


def display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {'F', 'W'} else 1
    return width


def pad_display(text: str, width: int) -> str:
    return text + ' ' * max(0, width - display_width(text))


def render_header(title: str, *, color_enabled: bool = False) -> str:
    return color(f'== {title} ==', BOLD + CYAN, enabled=color_enabled)


def render_section(title: str, *, color_enabled: bool = False) -> str:
    return color(f'[{title}]', BOLD + BLUE, enabled=color_enabled)


def render_status(label: str, value: str, ansi: str, *, color_enabled: bool = True) -> str:
    name = color(label, DIM, enabled=color_enabled)
    dot = color('●', ansi, enabled=color_enabled)
    text = color(value, ansi, enabled=color_enabled)
    return f'{name} {dot} {text}'


def render_metric(label: str, value: str, *, ansi: str = BLUE, color_enabled: bool = True) -> str:
    return color(f'{label} {value}', ansi, enabled=color_enabled)


def render_metric_row(
    items: list[tuple[str, str, str]],
    *,
    separator: str = '  |  ',
    cell_width: int = 22,
    color_enabled: bool = True,
) -> str:
    cells: list[str] = []
    for index, (label, value, ansi) in enumerate(items):
        raw = f'{label} {value}'
        if index < len(items) - 1:
            raw = pad_display(raw, cell_width)
        cells.append(color(raw, ansi, enabled=color_enabled))
    return separator.join(cells)


def render_menu(items: list[tuple[str, str]]) -> str:
    return '\n'.join(f'{key}. {label}' for key, label in items)


def clear_screen(*, enabled: bool = True) -> None:
    if enabled:
        print('\033[2J\033[H', end='')


def render_rule(width: int = 72, *, color_enabled: bool = True) -> str:
    return color('─' * width, DIM, enabled=color_enabled)


def render_notice(message: str, *, color_enabled: bool = True) -> str:
    is_bad = any(token in message for token in ('失败', '错误', '无效', '操作失败'))
    ansi = RED if is_bad else GREEN
    return color(f'提示: {message}', ansi, enabled=color_enabled)


def print_numbered_menu(items: list[tuple[str, str]]) -> None:
    for key, label in items:
        print(f'  {color(key + ".", BOLD + CYAN)} {label}')


def print_menu_groups(groups: list[tuple[str, list[tuple[str, str]]]]) -> None:
    for title, items in groups:
        print(f'  {color(f"[{title}]", DIM)}')
        for key, label in items:
            print(f'    {color(key, BOLD + CYAN)}  {label}')
