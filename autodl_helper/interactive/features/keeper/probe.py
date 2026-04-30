from __future__ import annotations

from typing import Any

from ...support.delegates import _bind_app_globals
from ...support.keeper import (
    InteractiveSnapshotStore,
    InteractiveTaskManager,
    Settings,
    _interactive_max_workers,
    _nudge_background_tasks,
    _snapshot_key,
    _submit_snapshot_task,
)


def _queue_keeper_probe_refresh(
    *,
    task_manager: InteractiveTaskManager,
    snapshot_store: InteractiveSnapshotStore,
    account_scope: str,
    account_label: str,
    settings: Settings,
    store,
    keeper_probe_rows_fn,
    settle_seconds: float = 0.0,
) -> None:
    _bind_app_globals(globals(), exclude={'_queue_keeper_probe_refresh', '_current_probe_rows'})
    probe_snapshot_key = _snapshot_key('keeper_probe', account_scope)
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


def _current_probe_rows(
    *,
    task_manager: InteractiveTaskManager,
    snapshot_store: InteractiveSnapshotStore,
    account_scope: str,
) -> list[dict[str, Any]]:
    _bind_app_globals(globals(), exclude={'_queue_keeper_probe_refresh', '_current_probe_rows'})
    probe_snapshot_key = _snapshot_key('keeper_probe', account_scope)
    task_manager.drain_completed()
    rows = snapshot_store.get_snapshot(probe_snapshot_key)
    return list(rows) if isinstance(rows, list) else []
