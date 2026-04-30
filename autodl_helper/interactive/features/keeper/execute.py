from __future__ import annotations

import argparse

from ...support.delegates import _bind_app_globals
from ...support.keeper import (
    InteractiveSnapshotStore,
    InteractiveTaskManager,
    MenuItem,
    Settings,
    _choose_menu_with_refresh,
    _confirm_action,
    _copy_args,
    _menu_default_key,
    _menu_refresh_revision,
    _nudge_background_tasks,
    _page_status_from_task_result,
    _page_status_from_tasks,
    _page_status_lines,
    _print_execution_summary,
    _render_keeper_execution_page,
    _run_captured_action,
    _show_result_screen,
    _submit_snapshot_task,
)


def _show_keeper_execution_results(
    *,
    args: argparse.Namespace,
    settings: Settings,
    account_label: str,
    account_scope: str,
    store,
    task_manager: InteractiveTaskManager,
    snapshot_store: InteractiveSnapshotStore,
    run_keeper_only_fn,
    command_history_fn,
    queue_keeper_probe_refresh_fn,
) -> None:
    _bind_app_globals(globals(), exclude={'_show_keeper_execution_results'})
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
            queue_keeper_probe_refresh_fn(settle_seconds=0.01)
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
