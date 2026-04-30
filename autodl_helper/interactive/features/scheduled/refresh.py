from __future__ import annotations

import argparse
import copy
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from autodl_helper.config import Settings

from ...support.delegates import _bind_app_globals
from ...support.scheduled import (
    InteractiveSnapshotStore,
    InteractiveTaskManager,
    InteractiveTaskResult,
    MenuItem,
    _build_scheduled_detail_menu_items,
    _coordinate_scheduled_background,
    _friendly_resource_error_message,
    _merge_scheduled_transient_state,
    _nudge_background_tasks,
    _page_status_lines,
    _refresh_scheduled_transient_state,
    _render_scheduled_job_detail,
    _render_scheduled_job_picker,
    _scheduled_picker_item_label,
    _scheduled_seed_status_rows,
    _snapshot_key,
    _store_snapshot,
)


@dataclass
class ScheduledMenuState:
    args: argparse.Namespace
    settings: Settings
    current_account: str | None
    run_variant_fn: Callable[..., int]
    start_background_scheduled_fn: Callable[..., tuple[int, str]] | None
    stop_background_polling_fn: Callable[..., tuple[int, str]] | None
    run_scheduled_start_cycle_fn: Callable[..., list[Any]]
    set_job_enabled_fn: Callable[..., None]
    set_job_override_fn: Callable[..., None]
    request_reload_fn: Callable[..., None]
    store: Any
    scheduled_job_status_rows_fn: Callable[..., list[dict[str, Any]]]
    load_settings_fn: Callable[[str], Settings]
    validate_settings_fn: Callable[[Settings, str], list[str]]
    task_manager: InteractiveTaskManager
    snapshot_store: InteractiveSnapshotStore
    account_label: str = field(init=False)
    selected_key: str = '1'
    detail_selected_key: str = '1'
    transient_run_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    snapshot_key: str = field(init=False)
    status_refresh_scope: str = field(init=False)
    scheduled_status_retry_after: float = 0.0
    scheduled_status_last_submit_at: float = 0.0

    def __post_init__(self) -> None:
        self.account_label = self.current_account or 'default'
        self.snapshot_key = _snapshot_key('scheduled_status', self.account_label)
        self.status_refresh_scope = self.account_label


def _bind_globals() -> None:
    _bind_app_globals(globals(), exclude={'ScheduledMenuState', '_bind_globals'})


def _scheduled_status_task(state: ScheduledMenuState):
    return state.task_manager.get_task('scheduled_status_refresh', state.status_refresh_scope)


def _scheduled_status_page_lines(state: ScheduledMenuState) -> list[str]:
    return _page_status_lines(state.snapshot_store.page_status(state.snapshot_key, _scheduled_status_task(state)))


def _base_status_rows(state: ScheduledMenuState) -> list[dict[str, Any]]:
    rows = state.snapshot_store.get_snapshot(state.snapshot_key)
    if isinstance(rows, list) and rows:
        return list(rows)
    return _scheduled_seed_status_rows(state.settings, state.store, account_name=state.account_label)


def _scheduled_refresh_task_keys(state: ScheduledMenuState) -> list[str]:
    task_keys = [
        state.task_manager.task_key('scheduled_status_refresh', state.status_refresh_scope),
        state.task_manager.task_key('scheduled_background_sync', state.account_label),
    ]
    for overlay in state.transient_run_state.values():
        task_type = str(overlay.get('_task_type') or '').strip()
        task_scope = str(overlay.get('_task_scope') or '').strip()
        if task_type and task_scope:
            task_keys.append(state.task_manager.task_key(task_type, task_scope))
    return task_keys


def _refresh_status_backoff_state(state: ScheduledMenuState) -> None:
    state.task_manager.drain_completed()
    _refresh_scheduled_transient_state(state.transient_run_state, state.task_manager)
    entry = state.snapshot_store.get_entry(state.snapshot_key)
    if entry is not None and entry.error_message:
        if state.scheduled_status_retry_after <= 0.0:
            state.scheduled_status_retry_after = time.monotonic() + 3.0
    else:
        state.scheduled_status_retry_after = 0.0


def _queue_status_refresh(state: ScheduledMenuState, *, force: bool = False, settle_seconds: float = 0.0) -> bool:
    task = _scheduled_status_task(state)
    if task is not None and task.status in {'queued', 'running'}:
        if settle_seconds > 0:
            _nudge_background_tasks(state.task_manager, settle_seconds=settle_seconds)
        return False
    now = time.monotonic()
    if not force:
        if state.scheduled_status_retry_after and now < state.scheduled_status_retry_after:
            return False
        if now - state.scheduled_status_last_submit_at < 1.0:
            return False
    state.scheduled_status_last_submit_at = now

    def _on_success(task_result: InteractiveTaskResult) -> None:
        state.task_manager.clear_resource_error()
        state.scheduled_status_retry_after = 0.0
        _store_snapshot(state.snapshot_store, state.snapshot_key, task_result.payload, status_message='最近更新')

    def _on_error(task_result: InteractiveTaskResult) -> None:
        state.task_manager.record_resource_error(task_result.error_message)
        state.scheduled_status_retry_after = time.monotonic() + 3.0
        state.snapshot_store.record_failure(state.snapshot_key, _friendly_resource_error_message(task_result.error_message))

    state.task_manager.submit(
        'scheduled_status_refresh',
        scope=state.status_refresh_scope,
        runner=lambda: state.scheduled_job_status_rows_fn(state.settings, state.store, account_name=state.account_label),
        status_message='正在刷新抢机器规则',
        on_success=_on_success,
        on_error=_on_error,
        replace_queued=True,
    )
    if settle_seconds > 0:
        _nudge_background_tasks(state.task_manager, settle_seconds=settle_seconds)
    else:
        state.task_manager.start_pending()
    return True


def _refresh_status_snapshot_if_due(state: ScheduledMenuState, *, force: bool = False, settle_seconds: float = 0.0) -> None:
    _refresh_status_backoff_state(state)
    if not force:
        entry = state.snapshot_store.get_entry(state.snapshot_key)
        if state.task_manager.circuit_state().get('circuit_open') and entry is not None and entry.error_message:
            return
        if state.scheduled_status_retry_after and time.monotonic() < state.scheduled_status_retry_after:
            return
    _queue_status_refresh(state, force=force, settle_seconds=settle_seconds)


def _fetch_status_rows(
    state: ScheduledMenuState,
    *,
    job_name: str | None = None,
    force_refresh: bool = False,
    settle_seconds: float = 0.0,
) -> list[dict[str, Any]]:
    if force_refresh:
        _refresh_status_snapshot_if_due(state, force=True, settle_seconds=settle_seconds)
    _refresh_status_backoff_state(state)
    rows = _merge_scheduled_transient_state(copy.deepcopy(_base_status_rows(state)), state.transient_run_state)
    if job_name is not None:
        rows = [row for row in rows if str(row.get('job_name') or '') == job_name]
    return rows


def _fetch_live_status_rows(state: ScheduledMenuState, *, job_name: str | None = None) -> list[dict[str, Any]]:
    rows = state.scheduled_job_status_rows_fn(state.settings, state.store, account_name=state.account_label, job_name=job_name)
    _store_snapshot(state.snapshot_store, state.snapshot_key, rows, status_message='最近更新')
    _refresh_status_backoff_state(state)
    rows = _merge_scheduled_transient_state(copy.deepcopy(rows), state.transient_run_state)
    if job_name is not None:
        rows = [row for row in rows if str(row.get('job_name') or '') == job_name]
    return rows


def _fetch_single_status_row(state: ScheduledMenuState, job_name: str, fallback: dict[str, Any]) -> dict[str, Any]:
    rows = _fetch_status_rows(state, job_name=job_name)
    if rows:
        return rows[0]
    merged = dict(fallback)
    overlay = state.transient_run_state.get(job_name)
    if overlay:
        merged.update(overlay)
    return merged


def _queue_scheduled_background_sync(state: ScheduledMenuState, scoped_args: argparse.Namespace) -> None:
    state.task_manager.submit(
        'scheduled_background_sync',
        scope=state.account_label,
        runner=lambda scoped_args=scoped_args, settings=state.settings, store=state.store: _coordinate_scheduled_background(
            args=scoped_args,
            settings=state.settings,
            store=state.store,
            account_name=state.account_label,
            start_background_scheduled_fn=state.start_background_scheduled_fn,
            stop_background_polling_fn=state.stop_background_polling_fn,
        ),
        status_message='正在协调后台轮询',
    )
    state.task_manager.start_pending()


def _scheduled_picker_snapshot(state: ScheduledMenuState, preferred_key: str | None) -> tuple[str, list[MenuItem], str | None]:
    _refresh_status_snapshot_if_due(state, settle_seconds=0.01)
    refreshed_rows = _fetch_status_rows(state)
    items = [MenuItem(str(index), _scheduled_picker_item_label(row)) for index, row in enumerate(refreshed_rows, start=1)]
    items += [
        MenuItem('n', '新建任务'),
        MenuItem('s', '查看全部抢机进度'),
        MenuItem('0', '返回首页'),
    ]
    return (
        _render_scheduled_job_picker(
            state.settings,
            state.account_label,
            refreshed_rows,
            page_status_lines=_scheduled_status_page_lines(state),
        ),
        items,
        preferred_key or state.selected_key,
    )


def _scheduled_detail_snapshot(
    state: ScheduledMenuState,
    selected_row: dict[str, Any],
    selected_job,
    preferred_key: str | None,
    detail_selected_key: str,
) -> tuple[str, list[MenuItem], str | None]:
    _refresh_status_snapshot_if_due(state, settle_seconds=0.01)
    refreshed_row = _fetch_single_status_row(state, selected_row['job_name'], selected_row)
    detail_items = _build_scheduled_detail_menu_items(bool(refreshed_row['enabled']), bool(refreshed_row.get('daemon_running')))
    return (
        _render_scheduled_job_detail(
            selected_job,
            refreshed_row,
            state.account_label,
            page_status_lines=_scheduled_status_page_lines(state),
        ),
        detail_items,
        preferred_key or detail_selected_key,
    )


__all__ = [
    'ScheduledMenuState',
    '_scheduled_status_task',
    '_scheduled_status_page_lines',
    '_base_status_rows',
    '_scheduled_refresh_task_keys',
    '_refresh_status_backoff_state',
    '_queue_status_refresh',
    '_refresh_status_snapshot_if_due',
    '_fetch_status_rows',
    '_fetch_live_status_rows',
    '_fetch_single_status_row',
    '_queue_scheduled_background_sync',
    '_scheduled_picker_snapshot',
    '_scheduled_detail_snapshot',
]
