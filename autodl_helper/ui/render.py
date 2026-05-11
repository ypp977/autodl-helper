from __future__ import annotations

RESET = '\033[0m'
BOLD = '\033[1m'
DIM = '\033[2m'
GREEN = '\033[38;5;114m'
YELLOW = '\033[38;5;179m'
RED = '\033[38;5;174m'
CYAN = '\033[38;5;80m'
BLUE = '\033[38;5;75m'


def color(text: str, ansi: str, *, enabled: bool = True) -> str:
    return f'{ansi}{text}{RESET}' if enabled else text


def render_header(title: str, *, color_enabled: bool = False) -> str:
    return color(f'== {title} ==', BOLD + CYAN, enabled=color_enabled)


def render_section(title: str, *, color_enabled: bool = False) -> str:
    return color(f'[{title}]', BOLD + BLUE, enabled=color_enabled)


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
