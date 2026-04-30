from __future__ import annotations

import copy
from typing import Any

from ....runtime_control import read_daemon_status, scheduled_job_identity
from ...support.delegates import _bind_app_globals
from ...support.scheduled import _InteractiveCancel, _confirm_action, _job_to_payload, _persist_job_changes, _print_execution_summary, _prompt_scheduled_job, _snapshot_key
from ...history_instance import _find_scheduled_job
from .execute import _maybe_queue_auto_run_for_job
from .refresh import (
    ScheduledMenuState,
    _fetch_single_status_row,
    _fetch_status_rows,
    _queue_scheduled_background_sync,
    _refresh_status_snapshot_if_due,
)


def _bind_globals() -> None:
    _bind_app_globals(globals(), exclude={'_bind_globals'})


def _create_scheduled_job(state: ScheduledMenuState, scoped_args) -> str | None:
    try:
        new_job = _prompt_scheduled_job()
        if not new_job.name and not new_job.instance_id:
            raise ValueError('任务至少要有 name 或 instance_id。')
        _persist_job_changes(
            config_path=state.args.config,
            settings=state.settings,
            load_settings_fn=state.load_settings_fn,
            validate_settings_fn=state.validate_settings_fn,
            mutator=lambda jobs: jobs.append(_job_to_payload(new_job)),
        )
        state.settings = state.load_settings_fn(state.args.config)
        selected_row = {
            'job_name': scheduled_job_identity(new_job),
            'enabled': True,
            'target_time': new_job.target_time,
            'advance_hours': new_job.advance_hours,
            'schedule_mode': getattr(new_job, 'schedule_mode', 'daily') or 'daily',
            'timezone': getattr(new_job, 'timezone', 'Asia/Shanghai') or 'Asia/Shanghai',
            'daemon_running': bool(read_daemon_status(state.store).get('running')),
        }
        _maybe_queue_auto_run_for_job(
            state,
            job=new_job,
            selected_row=selected_row,
            trigger_label='新建规则后自动执行',
            force_run_now=True,
        )
        state.request_reload_fn(state.store)
        state.snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', f'all:{state.account_label}'))
        _queue_scheduled_background_sync(state, scoped_args)
        _refresh_status_snapshot_if_due(state, force=True, settle_seconds=0.01)
        _print_execution_summary('已创建抢机器任务', detail=f'job={scheduled_job_identity(new_job)}\n后台任务已排队执行')
        for index, row in enumerate(_fetch_status_rows(state), start=1):
            if row['job_name'] == scheduled_job_identity(new_job):
                return str(index)
        return None
    except _InteractiveCancel:
        return None
    except ValueError as exc:
        _print_execution_summary('创建失败', detail=str(exc))
        return None


def _edit_scheduled_job(state: ScheduledMenuState, selected_row: dict[str, Any], selected_job: Any) -> tuple[dict[str, Any], Any] | None:
    legacy_edit_flow = False
    try:
        updated_job = _prompt_scheduled_job(selected_job)
        _persist_job_changes(
            config_path=state.args.config,
            settings=state.settings,
            load_settings_fn=state.load_settings_fn,
            validate_settings_fn=state.validate_settings_fn,
            mutator=lambda jobs: jobs.__setitem__(
                next(index for index, item in enumerate(jobs) if item.get('name') == selected_row['job_name'] or item.get('instance_id') == selected_row['job_name']),
                _job_to_payload(updated_job),
            ),
        )
        state.settings = state.load_settings_fn(state.args.config)
        selected_job = _find_scheduled_job(state.settings, updated_job.name or updated_job.instance_id)
        selected_job_name = scheduled_job_identity(selected_job)
        if selected_job_name != selected_row['job_name']:
            state.snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', f'job:{state.account_label}:{selected_row["job_name"]}'))
        state.snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', f'job:{state.account_label}:{selected_job_name}'))
        current_control = state.store.get_scheduled_job_control(state.account_label, selected_job_name) or {}
        if (
            not legacy_edit_flow
            and not bool(current_control.get('enabled', True))
            and str(current_control.get('source') or '') == 'scheduled_once_complete'
        ):
            state.set_job_enabled_fn(state.store, state.account_label, selected_job_name, True)
        _refresh_status_snapshot_if_due(state, force=True, settle_seconds=0.01)
        selected_row = _fetch_single_status_row(state, selected_job_name, selected_row)
        if not legacy_edit_flow and bool(selected_row['enabled']):
            _maybe_queue_auto_run_for_job(
                state,
                job=selected_job,
                selected_row=selected_row,
                trigger_label='修改规则后自动执行',
                force_run_now=True,
            )
            selected_row = _fetch_single_status_row(state, scheduled_job_identity(selected_job), selected_row)
        state.request_reload_fn(state.store)
        state.snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', f'all:{state.account_label}'))
        _queue_scheduled_background_sync(state, state.args)
        _refresh_status_snapshot_if_due(state, force=True, settle_seconds=0.01)
        return selected_row, selected_job
    except _InteractiveCancel:
        return None
    except (ValueError, StopIteration) as exc:
        _print_execution_summary('更新失败', detail=str(exc))
        return None


def _toggle_scheduled_job(state: ScheduledMenuState, selected_row: dict[str, Any]) -> dict[str, Any] | None:
    next_enabled = not bool(selected_row['enabled'])
    state.set_job_enabled_fn(state.store, state.account_label, selected_row['job_name'], next_enabled)
    if not next_enabled:
        state.transient_run_state.pop(selected_row['job_name'], None)
    state.request_reload_fn(state.store)
    state.snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', f'all:{state.account_label}'))
    _refresh_status_snapshot_if_due(state, force=True, settle_seconds=0.01)
    selected_row = _fetch_single_status_row(state, selected_row['job_name'], selected_row)
    _queue_scheduled_background_sync(state, state.args)
    _print_execution_summary(
        '已恢复任务' if next_enabled else '已暂停任务',
        detail=f'job={selected_row["job_name"]}\n后台状态协调已排队',
    )
    return selected_row


def _delete_scheduled_job(state: ScheduledMenuState, selected_row: dict[str, Any]) -> bool:
    if not _confirm_action('删除任务', f'当前账号: {state.account_label}', f'job: {selected_row["job_name"]}'):
        return False
    try:
        state.transient_run_state.pop(selected_row['job_name'], None)
        state.snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', f'job:{state.account_label}:{selected_row["job_name"]}'))
        _persist_job_changes(
            config_path=state.args.config,
            settings=state.settings,
            load_settings_fn=state.load_settings_fn,
            validate_settings_fn=state.validate_settings_fn,
            mutator=lambda jobs: jobs.__delitem__(
                next(index for index, item in enumerate(jobs) if item.get('name') == selected_row['job_name'] or item.get('instance_id') == selected_row['job_name'])
            ),
        )
        state.settings = state.load_settings_fn(state.args.config)
        state.request_reload_fn(state.store)
        state.snapshot_store.clear_prefix(_snapshot_key('scheduled_progress', f'all:{state.account_label}'))
        _queue_scheduled_background_sync(state, state.args)
        _refresh_status_snapshot_if_due(state, force=True, settle_seconds=0.01)
        _print_execution_summary('已删除任务', detail=f'job={selected_row["job_name"]}\n后台状态协调已排队')
        return True
    except (ValueError, StopIteration) as exc:
        _print_execution_summary('删除失败', detail=str(exc))
        return False


__all__ = [
    '_create_scheduled_job',
    '_edit_scheduled_job',
    '_toggle_scheduled_job',
    '_delete_scheduled_job',
]
