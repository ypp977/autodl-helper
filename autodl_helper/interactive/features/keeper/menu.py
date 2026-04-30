from __future__ import annotations

import argparse
from typing import Any

from ...support.delegates import _bind_app_globals
from ...support.keeper import (
    InteractiveSnapshotStore,
    InteractiveTaskManager,
    MenuItem,
    Settings,
    _InteractiveCancel,
    _choose_menu_with_refresh,
    _confirm_action,
    _copy_args,
    _interactive_max_workers,
    _keeper_probe_schedule_lines,
    _menu_default_key,
    _menu_refresh_revision,
    _nudge_background_tasks,
    _page_status_from_task_result,
    _page_status_from_tasks,
    _page_status_lines,
    _persist_keeper_changes,
    _print_execution_summary,
    _prompt_keeper_settings,
    _render_keeper_execution_page,
    _render_keeper_probe_page,
    _render_keeper_rules,
    _run_captured_action,
    _show_result_screen,
    _snapshot_key,
    _submit_snapshot_task,
    get_task_enabled,
)
from ..instances.browse import _browse_keeper_probe


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
    _bind_app_globals(globals(), exclude={'_scheduled_menu', '_keeper_menu'})

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
