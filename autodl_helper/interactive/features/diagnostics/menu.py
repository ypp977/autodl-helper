from __future__ import annotations

import argparse
from typing import Any, Callable

from autodl_helper.config import Settings

from ...support.delegates import _bind_app_globals
from ...support.keeper import (
    MenuItem,
    _choose_menu_with_refresh,
    _friendly_resource_error_message,
    _menu_default_key,
    _menu_refresh_revision,
    _page_status_lines,
    _print_execution_summary,
    _show_result_screen,
    _snapshot_key,
    _store_snapshot,
    _submit_snapshot_task,
)
from ...account_common import _account_display_name
from ...config_ops import _render_config_diagnostics
from ...screens import _browse_healthcheck_detail, _browse_instance_list, _diagnostics_snapshot_payload, _render_diagnostics_page
from ...service_ops import (
    _append_interactive_service_log,
    _clear_diagnostics_scope_snapshots,
    _load_instance_rows_via_command,
    _normalize_service_action_result,
    _record_interactive_service_event,
)
from ..instances.browse import _browse_keeper_probe
from .status import DEFAULT_SERVICE_LABEL, _diagnostics_page_status, _read_launch_agent_status_fallback, _start_launch_agent_fallback, _stop_launch_agent_fallback


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
    task_manager,
    snapshot_store,
    clear_scope_snapshots_on_exit: bool = False,
    service_status_fn: Callable[[], dict[str, Any]] = _read_launch_agent_status_fallback,
    service_start_fn: Callable[[], Any] = _start_launch_agent_fallback,
    service_stop_fn: Callable[[], Any] = _stop_launch_agent_fallback,
) -> None:
    _bind_app_globals(globals(), exclude={'_diagnostics_menu', '_diagnostics_page_status'})
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
                    _append_interactive_service_log(args.config, f'后台服务已在运行 label={DEFAULT_SERVICE_LABEL}')
                    _record_interactive_service_event(store, action='start', message='后台服务已在运行')
                    _print_execution_summary('后台服务已在运行', detail=str(service_status.get('label') or ''))
                    continue
                code, detail = _normalize_service_action_result(service_start_fn())
                if code == 0:
                    _append_interactive_service_log(args.config, f'已启动后台服务 label={DEFAULT_SERVICE_LABEL}')
                    _record_interactive_service_event(store, action='start', message='已启动后台服务')
                else:
                    _record_interactive_service_event(store, action='start', message='启动后台服务失败', level='error', detail=detail or '')
                _print_execution_summary('已启动后台服务' if code == 0 else '启动后台服务失败', code=code, detail=detail or None)
            elif choice == '6':
                service_status = service_status_fn() if callable(service_status_fn) else {}
                if not bool(service_status.get('installed')):
                    _append_interactive_service_log(args.config, f'后台服务未安装 label={DEFAULT_SERVICE_LABEL}')
                    _record_interactive_service_event(store, action='stop', message='后台服务未安装')
                    _print_execution_summary('后台服务未安装')
                    continue
                if not bool(service_status.get('loaded')):
                    _append_interactive_service_log(args.config, f'后台服务已停止 label={DEFAULT_SERVICE_LABEL}')
                    _record_interactive_service_event(store, action='stop', message='后台服务已停止')
                    _print_execution_summary('后台服务未在运行', detail=str(service_status.get('label') or ''))
                    continue
                code, detail = _normalize_service_action_result(service_stop_fn())
                if code == 0:
                    _append_interactive_service_log(args.config, f'已停止后台服务 label={DEFAULT_SERVICE_LABEL}')
                    _record_interactive_service_event(store, action='stop', message='已停止后台服务')
                else:
                    _record_interactive_service_event(store, action='stop', message='停止后台服务失败', level='error', detail=detail or '')
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
                        _record_interactive_service_event(store, action='restart', message='重启后台服务失败', level='error', detail=stop_detail or '')
                        _print_execution_summary('重启后台服务失败', code=stop_code, detail=stop_detail or None)
                        continue
                code, detail = _normalize_service_action_result(service_start_fn())
                if code == 0:
                    _append_interactive_service_log(args.config, f'已重启后台服务 label={DEFAULT_SERVICE_LABEL}')
                    _record_interactive_service_event(store, action='restart', message='已重启后台服务')
                else:
                    _record_interactive_service_event(store, action='restart', message='重启后台服务失败', level='error', detail=detail or '')
                _print_execution_summary('已重启后台服务' if code == 0 else '重启后台服务失败', code=code, detail=detail or None)
            elif choice == '0':
                return
            else:
                print('无效选择。')
    finally:
        if clear_scope_snapshots_on_exit:
            _clear_diagnostics_scope_snapshots(snapshot_store, current_account=current_account)


__all__ = ['_diagnostics_menu']
