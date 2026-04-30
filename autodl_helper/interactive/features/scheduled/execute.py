from __future__ import annotations

import copy
from typing import Any

from ....runtime_control import get_task_enabled, scheduled_job_identity
from ...support.delegates import _bind_app_globals
from ...support.scheduled import _nudge_background_tasks, _print_execution_summary, _scheduled_run_pending_state, _show_live_scheduled_status
from .refresh import ScheduledMenuState, _fetch_live_status_rows


def _bind_globals() -> None:
    _bind_app_globals(globals(), exclude={'_bind_globals'})


def _schedule_auto_run(
    state: ScheduledMenuState,
    *,
    job,
    selected_row: dict[str, Any],
    trigger_label: str,
    task_type: str,
    task_scope: str,
    force_run_now: bool = False,
) -> None:
    scoped_settings = copy.deepcopy(state.settings)
    scoped_settings.tasks.scheduled_start.jobs = [copy.deepcopy(job)]
    state.task_manager.submit(
        task_type,
        scope=task_scope,
        runner=lambda scoped_settings=scoped_settings, account_label=state.account_label, store=state.store: state.run_scheduled_start_cycle_fn(
            settings=scoped_settings,
            headed=state.args.headed,
            state_file=state.args.state_file,
            account_name=state.account_label,
            force_run_now=force_run_now,
            store=state.store,
        ),
        status_message='正在执行抢机器检查',
    )
    _nudge_background_tasks(state.task_manager)
    state.transient_run_state[selected_row['job_name']] = _scheduled_run_pending_state(
        selected_row,
        trigger_label=trigger_label,
        task_type=task_type,
        task_scope=task_scope,
    )


def _maybe_queue_auto_run_for_job(
    state: ScheduledMenuState,
    *,
    job,
    selected_row: dict[str, Any],
    trigger_label: str,
    force_run_now: bool = True,
) -> None:
    if not get_task_enabled(state.store, state.account_label, 'scheduled_start', default_enabled=state.settings.tasks.scheduled_start.enabled):
        return
    task_scope = f'{state.account_label}:{scheduled_job_identity(job)}'
    _schedule_auto_run(
        state,
        job=job,
        selected_row=selected_row,
        trigger_label=trigger_label,
        task_type='scheduled_auto_run',
        task_scope=task_scope,
        force_run_now=force_run_now,
    )


def _show_all_scheduled_progress(state: ScheduledMenuState) -> None:
    _show_live_scheduled_status(
        job_name=None,
        fetch_rows_fn=lambda: _fetch_live_status_rows(state),
        task_manager=state.task_manager,
        snapshot_store=state.snapshot_store,
        current_account=state.account_label,
        clear_scope_snapshot_on_exit=True,
        settings=state.settings,
    )


def _show_job_progress(state: ScheduledMenuState, job_name: str) -> None:
    _show_live_scheduled_status(
        job_name=job_name,
        fetch_rows_fn=lambda: _fetch_live_status_rows(state, job_name=job_name),
        task_manager=state.task_manager,
        snapshot_store=state.snapshot_store,
        current_account=state.account_label,
        clear_scope_snapshot_on_exit=True,
    )


def _run_manual_scheduled_job(state: ScheduledMenuState, selected_row: dict[str, Any], selected_job: Any | None = None) -> None:
    task_scope = f'{state.account_label}:{selected_row["job_name"]}'
    _schedule_auto_run(
        state,
        job=selected_job or selected_row,
        selected_row=selected_row,
        trigger_label='手动立即执行',
        task_type='scheduled_manual_run',
        task_scope=task_scope,
        force_run_now=False,
    )


__all__ = [
    '_schedule_auto_run',
    '_maybe_queue_auto_run_for_job',
    '_show_all_scheduled_progress',
    '_show_job_progress',
    '_run_manual_scheduled_job',
]
