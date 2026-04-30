from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

from ..dialogs import MenuItem, _choose_menu_with_refresh as _dialog_choose_menu_with_refresh, _menu_default_key as _dialog_menu_default_key
from ..runtime import InteractivePageStatus, InteractiveSnapshotStore, InteractiveTaskManager, InteractiveTaskResult
from ..service_ops import _submit_snapshot_task
from ..status_task import _interactive_max_workers, _menu_refresh_revision, _nudge_background_tasks, _page_status_from_tasks, _page_status_lines, _snapshot_key
from .delegates import _delegate
from .rendering import _account_label, _show_result_screen_for

if TYPE_CHECKING:
    from autodl_helper.config import Settings


_choose_menu_with_refresh = _delegate('_choose_menu_with_refresh', _dialog_choose_menu_with_refresh)
_menu_default_key = _delegate('_menu_default_key', _dialog_menu_default_key)


def _browse_snapshot_list(
    *,
    settings: Settings,
    current_account: str | None,
    snapshot_namespace: str,
    task_type: str,
    status_message: str,
    task_runner: Callable[[], Any],
    render_page_fn: Callable[[str, list[dict[str, Any]], list[str] | None], str],
    build_items_fn: Callable[[list[dict[str, Any]]], list[MenuItem]],
    detail_title_fn: Callable[[dict[str, Any]], str],
    detail_body_fn: Callable[[dict[str, Any], str], str],
    task_manager: InteractiveTaskManager | None = None,
    snapshot_store: InteractiveSnapshotStore | None = None,
    extra_status_lines_fn: Callable[[list[dict[str, Any]], InteractivePageStatus, InteractiveTaskResult | None], list[str] | None] | None = None,
    progress_label: str = '任务进度',
    show_task_stage: bool = True,
    show_progress: bool = False,
    show_hint: bool = True,
    loading_item_text: str = '加载中…',
    return_item_text: str = '返回诊断',
    refresh_interval_seconds: float = 1.0,
) -> None:
    owns_runtime = False
    if task_manager is None or snapshot_store is None:
        snapshot_store = InteractiveSnapshotStore()
        task_manager = InteractiveTaskManager(snapshot_store=snapshot_store, max_workers=_interactive_max_workers(settings))
        owns_runtime = True

    account = _account_label(settings, current_account)
    scope = current_account or 'default'
    snapshot_id = _snapshot_key(snapshot_namespace, scope)

    _submit_snapshot_task(
        task_manager=task_manager,
        snapshot_store=snapshot_store,
        task_type=task_type,
        scope=scope,
        snapshot_key=snapshot_id,
        runner=task_runner,
        status_message=status_message,
    )
    _nudge_background_tasks(task_manager)
    selected_key = '1'

    def _current_rows() -> list[dict[str, Any]]:
        rows = snapshot_store.get_snapshot(snapshot_id)
        return list(rows) if isinstance(rows, list) else []

    def _menu_snapshot(preferred_key: str | None) -> tuple[str, list[MenuItem], str | None]:
        task_manager.drain_completed()
        rows = _current_rows()
        primary_task = task_manager.get_task(task_type, scope)
        status = _page_status_from_tasks(
            snapshot_store=snapshot_store,
            snapshot_key=snapshot_id,
            primary_task=primary_task,
        )
        page_status_lines = _page_status_lines(
            status,
            active_task=primary_task,
            progress_label=progress_label,
            show_task_stage=show_task_stage,
            show_progress=show_progress,
            show_hint=show_hint,
        )
        if extra_status_lines_fn is not None:
            extra_lines = extra_status_lines_fn(rows, status, primary_task)
            if extra_lines:
                page_status_lines = [*page_status_lines, *extra_lines]
        items = build_items_fn(rows)
        if not items:
            items.append(MenuItem('r', loading_item_text))
        items.append(MenuItem('0', return_item_text))
        title = render_page_fn(account, rows, page_status_lines=page_status_lines)
        keep_key = preferred_key if preferred_key and any(item.key == preferred_key for item in items) else _menu_default_key(items, '1')
        return title, items, keep_key

    try:
        while True:
            title, items, default_key = _menu_snapshot(selected_key)
            choice = _choose_menu_with_refresh(
                title,
                items,
                default_key=default_key,
                refresh_fn=lambda preferred_key: _menu_snapshot(preferred_key),
                refresh_revision_fn=lambda: _menu_refresh_revision(
                    snapshot_store=snapshot_store,
                    snapshot_keys=[snapshot_id],
                    task_manager=task_manager,
                    task_keys=[task_manager.task_key(task_type, scope)],
                ),
                refresh_interval_seconds=refresh_interval_seconds,
                on_rendered_fn=task_manager.start_pending,
                refresh_policy='always',
                pre_refresh_fn=task_manager.drain_completed,
            )
            if choice == '0':
                return
            if choice == 'r':
                continue
            if not choice.isdigit():
                continue
            selected_key = choice
            rows = _current_rows()
            if not rows or int(choice) - 1 >= len(rows):
                continue
            row = rows[int(choice) - 1]
            _show_result_screen_for(detail_title_fn(row), detail_body_fn(row, account))
    finally:
        if owns_runtime:
            _nudge_background_tasks(task_manager, settle_seconds=0.01)
            task_manager.shutdown(wait=False)


__all__ = [
    '_choose_menu_with_refresh',
    '_menu_default_key',
    '_browse_snapshot_list',
    '_snapshot_key',
    '_page_status_lines',
    '_page_status_from_tasks',
    '_menu_refresh_revision',
    '_nudge_background_tasks',
    '_interactive_max_workers',
]
