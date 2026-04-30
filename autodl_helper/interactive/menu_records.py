from __future__ import annotations

import argparse
from typing import Any, Callable

from autodl_helper.config import Settings
from .app_runtime import _delegate
from .dialogs import MenuItem, _choose_menu as _dialog_choose_menu, _menu_default_key as _dialog_menu_default_key
from .shared import _copy_args, _run_captured_action
from .screens import _auth_report_filter_wizard, _browse_history_records, _history_filter_wizard, _render_records_overview


_choose_menu = _delegate('_choose_menu', _dialog_choose_menu)
_menu_default_key = _delegate('_menu_default_key', _dialog_menu_default_key)


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
            from .shared import _show_result_screen
            _show_result_screen('认证异常', output, code=code)
        elif choice == '0':
            return
        else:
            print('无效选择。')


__all__ = ['_records_menu']
