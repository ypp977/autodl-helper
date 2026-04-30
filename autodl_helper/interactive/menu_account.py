from __future__ import annotations

import argparse
from typing import Any, Callable

from autodl_helper.config import Settings
from .account_ops import _switch_to_new_account
from .dialogs import MenuItem, _choose_menu as _dialog_choose_menu, _menu_default_key as _dialog_menu_default_key
from .app_runtime import _delegate
from .screens import _browse_account_detail
from .runtime import InteractiveSnapshotStore, InteractiveTaskManager


_choose_menu = _delegate('_choose_menu', _dialog_choose_menu)
_menu_default_key = _delegate('_menu_default_key', _dialog_menu_default_key)


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


__all__ = ['_account_menu']
