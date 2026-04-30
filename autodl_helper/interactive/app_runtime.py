from __future__ import annotations

import argparse
import copy
from typing import Any, Callable

from autodl_helper.config import Settings
from .dialogs import MenuItem, _choose_menu as _dialog_choose_menu, _menu_default_key as _dialog_menu_default_key
from .runtime import InteractiveSnapshotStore, InteractiveTaskManager, reset_thread_capture_state
from .screens import _account_display_name, _dashboard_placeholder_view, _dashboard_snapshot_view
from .service_ops import _submit_snapshot_task as _service_submit_snapshot_task
from .shared import (
    _interactive_max_workers,
    _nudge_background_tasks as _shared_nudge_background_tasks,
    _page_status_lines,
    _pick_default_account,
    _snapshot_key,
)
from .support import delegates as _delegates

_resolve_app_module = _delegates._resolve_app_module
_resolve_app_target = _delegates._resolve_app_target
_delegate = _delegates._delegate
_bind_app_globals = _delegates._bind_app_globals


_choose_menu = _delegate('_choose_menu', _dialog_choose_menu)
_menu_default_key = _delegate('_menu_default_key', _dialog_menu_default_key)
_scheduled_menu = _delegate('_scheduled_menu', lambda *args, **kwargs: None)
_keeper_menu = _delegate('_keeper_menu', lambda *args, **kwargs: None)
_account_menu = _delegate('_account_menu', lambda *args, **kwargs: None)
_diagnostics_menu = _delegate('_diagnostics_menu', lambda *args, **kwargs: None)
_submit_snapshot_task = _delegate('_submit_snapshot_task', _service_submit_snapshot_task)
_nudge_background_tasks = _delegate('_nudge_background_tasks', _shared_nudge_background_tasks)


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
    global _SERVICE_CONFIG_PATH
    _SERVICE_CONFIG_PATH = args.config
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
                _account_menu(
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


def _hide_cursor():
    from .dialogs import _hide_cursor as _impl

    return _impl()


def _show_cursor():
    from .dialogs import _show_cursor as _impl

    return _impl()


__all__ = ['run_interactive']
