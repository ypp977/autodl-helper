from __future__ import annotations

import argparse

from ...dialogs import MenuItem, _choose_menu as _dialog_choose_menu, _choose_menu_with_refresh as _dialog_choose_menu_with_refresh, _menu_default_key as _dialog_menu_default_key
from ...runtime import InteractiveSnapshotStore, InteractiveTaskManager
from ...service_ops import _submit_snapshot_task
from ...screen_support import _delegate
from ...screen_scheduled import _show_result_screen
from ...screen_support import _resolve_app_target
from ...status_task import (
    _friendly_resource_error_message,
    _menu_refresh_revision,
    _nudge_background_tasks,
    _page_status_from_tasks,
    _page_status_lines,
    _snapshot_key,
    _store_snapshot,
)

from ...account_ops import _account_runtime_snapshot, _enabled_account_names, _login_verify_snapshot
from .views import _render_account_detail

__all__ = [
    '_browse_account_detail',
]

_choose_menu = _delegate('_choose_menu', _dialog_choose_menu)
_choose_menu_with_refresh = _delegate('_choose_menu_with_refresh', _dialog_choose_menu_with_refresh)
_menu_default_key = _delegate('_menu_default_key', _dialog_menu_default_key)


def _show_result_screen_for(title: str, body: str, *, code: int | None = None) -> None:
    result_screen = _resolve_app_target('_show_result_screen', _show_result_screen)
    result_screen(title, body, code=code)


def _browse_account_detail(
    *,
    args: argparse.Namespace,
    settings,
    store,
    current_account: str | None,
    command_login_fn,
    keeper_probe_rows_fn,
    scheduled_job_status_rows_fn,
    task_manager: InteractiveTaskManager,
    snapshot_store: InteractiveSnapshotStore,
    trigger_verify_on_open: bool = False,
) -> str | None:
    result_screen = _show_result_screen_for
    enabled_accounts = _enabled_account_names(settings)
    selected_account = current_account or (enabled_accounts[0] if enabled_accounts else None)
    if selected_account is None:
        result_screen('账号详情', '当前没有可用账号。')
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
