from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..account_common import _account_display_name
from ..dialogs import MenuItem
from ..presentation import CYAN, _heading, _key_value, _section, _separator
from .delegates import _resolve_app_target

if TYPE_CHECKING:
    from autodl_helper.config import Settings


def _account_label(settings: Settings, current_account: str | None) -> str:
    return _account_display_name(settings, current_account)


def _show_result_screen_for(title: str, body: str, *, code: int | None = None) -> None:
    from ..screen_scheduled import _show_result_screen as _fallback

    result_screen = _resolve_app_target('_show_result_screen', _fallback)
    result_screen(title, body, code=code)


def _render_scoped_list_page(
    title: str,
    *,
    account_label: str,
    metric_items: list[tuple[str, Any]],
    page_status_lines: list[str] | None = None,
    section_title: str = '[选择实例查看详情]',
    color: str = CYAN,
) -> str:
    lines = [
        _heading(title, color=color),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.append(_key_value('查看账号', account_label))
    for label, value in metric_items:
        lines.append(_key_value(label, value))
    lines.extend(['', _section(section_title)])
    return '\n'.join(lines)


__all__ = ['_account_label', '_show_result_screen_for', '_render_scoped_list_page', 'MenuItem']
