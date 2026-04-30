from __future__ import annotations

import argparse
from ...support.delegates import _bind_app_globals
from ...support.scheduled import (
    _build_scheduled_detail_menu_items,
    MenuItem,
    _choose_menu,
    _confirm_action,
    _copy_args,
    _menu_default_key,
    _menu_refresh_revision,
    _render_scheduled_job_detail,
    _render_scheduled_job_picker,
    _scheduled_picker_item_label,
)
from ...history_instance import _find_scheduled_job
from .editor import _create_scheduled_job, _delete_scheduled_job, _edit_scheduled_job, _toggle_scheduled_job
from .execute import _run_manual_scheduled_job, _show_all_scheduled_progress, _show_job_progress
from .refresh import (
    ScheduledMenuState,
    _fetch_single_status_row,
    _fetch_status_rows,
    _scheduled_picker_snapshot,
    _scheduled_refresh_task_keys,
    _scheduled_status_page_lines,
    _refresh_status_snapshot_if_due,
)


def _bind_globals() -> None:
    _bind_app_globals(globals(), exclude={'_scheduled_menu', '_bind_globals'})


def _scheduled_menu(
    args: argparse.Namespace,
    *,
    settings,
    current_account: str | None,
    run_variant_fn,
    start_background_scheduled_fn,
    stop_background_polling_fn,
    run_scheduled_start_cycle_fn,
    set_job_enabled_fn,
    set_job_override_fn,
    request_reload_fn,
    store,
    scheduled_job_status_rows_fn,
    load_settings_fn,
    validate_settings_fn,
    task_manager,
    snapshot_store,
) -> None:
    _bind_globals()
    state = ScheduledMenuState(
        args=args,
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

    _refresh_status_snapshot_if_due(state, force=True, settle_seconds=0.01)

    def _scheduled_detail_snapshot_local(preferred_key: str | None):
        _refresh_status_snapshot_if_due(state, settle_seconds=0.01)
        refreshed_rows = _fetch_status_rows(state)
        selected_row = refreshed_rows[int(state.selected_key) - 1] if state.selected_key.isdigit() and 1 <= int(state.selected_key) <= len(refreshed_rows) else refreshed_rows[0]
        selected_job = _find_scheduled_job(state.settings, selected_row['job_name'])
        detail_items = _build_scheduled_detail_menu_items(bool(selected_row['enabled']), bool(selected_row.get('daemon_running')))
        return (
            _render_scheduled_job_detail(
                selected_job,
                selected_row,
                state.account_label,
                page_status_lines=_scheduled_status_page_lines(state),
            ),
            detail_items,
            preferred_key or state.detail_selected_key,
        )

    while True:
        status_rows = _fetch_status_rows(state)
        items = [MenuItem(str(index), _scheduled_picker_item_label(row)) for index, row in enumerate(status_rows, start=1)]
        items += [
            MenuItem('n', '新建任务'),
            MenuItem('s', '查看全部抢机进度'),
            MenuItem('0', '返回首页'),
        ]
        choice = _choose_menu(
            _render_scheduled_job_picker(
                state.settings,
                state.account_label,
                status_rows,
                page_status_lines=_scheduled_status_page_lines(state),
            ),
            items,
            default_key=_menu_default_key(items, state.selected_key),
            refresh_fn=lambda preferred_key: _scheduled_picker_snapshot(state, preferred_key),
            refresh_revision_fn=lambda: _menu_refresh_revision(
                snapshot_store=state.snapshot_store,
                snapshot_keys=[state.snapshot_key],
                task_manager=state.task_manager,
                task_keys=_scheduled_refresh_task_keys(state),
            ),
            refresh_interval_seconds=1.0,
            on_rendered_fn=state.task_manager.start_pending,
        )
        state.selected_key = choice
        scoped_args = _copy_args(args, account=state.account_label)
        if choice == '0':
            return
        if choice.lower() == 'n':
            selected_key = _create_scheduled_job(state, scoped_args)
            if selected_key:
                state.selected_key = selected_key
            continue
        if choice.lower() == 's':
            _show_all_scheduled_progress(state)
            continue
        current_status_rows = _fetch_status_rows(state)
        if not choice.isdigit() or not (1 <= int(choice) <= len(current_status_rows)):
            print('无效选择。')
            continue
        selected_row = current_status_rows[int(choice) - 1]
        selected_job = _find_scheduled_job(state.settings, selected_row['job_name'])
        state.detail_selected_key = '1'

        while True:
            detail_items = _build_scheduled_detail_menu_items(bool(selected_row['enabled']), bool(selected_row.get('daemon_running')))
            detail_status_row = dict(selected_row)
            inner = _choose_menu(
                _render_scheduled_job_detail(
                    selected_job,
                    detail_status_row,
                    state.account_label,
                    page_status_lines=_scheduled_status_page_lines(state),
                ),
                detail_items,
                default_key=_menu_default_key(detail_items, state.detail_selected_key),
                refresh_fn=lambda preferred_key: _scheduled_detail_snapshot_local(preferred_key),
                refresh_revision_fn=lambda: _menu_refresh_revision(
                    snapshot_store=state.snapshot_store,
                    snapshot_keys=[state.snapshot_key],
                    task_manager=state.task_manager,
                    task_keys=_scheduled_refresh_task_keys(state),
                ),
                refresh_interval_seconds=1.0,
                on_rendered_fn=state.task_manager.start_pending,
            )
            state.detail_selected_key = inner
            if inner == '1':
                if not _confirm_action(
                    '立即执行一轮' if bool(selected_row['enabled']) else '恢复并执行一轮',
                    f'当前账号: {state.account_label}',
                    f'job: {selected_row["job_name"]}',
                    f'时间窗口: {selected_row["target_time"]} / 提前{selected_row["advance_hours"]}h',
                ):
                    continue
                if not bool(selected_row['enabled']):
                    set_job_enabled_fn(state.store, state.account_label, selected_row['job_name'], True)
                    _refresh_status_snapshot_if_due(state, force=True, settle_seconds=0.01)
                    selected_row = _fetch_single_status_row(state, selected_row['job_name'], selected_row)
                _run_manual_scheduled_job(state, selected_row, selected_job)
                selected_row = _fetch_single_status_row(state, selected_row['job_name'], selected_row)
            elif inner == '2':
                _show_job_progress(state, selected_row['job_name'])
            elif inner in {'3', '4'}:
                updated = _edit_scheduled_job(state, selected_row, selected_job)
                if updated is None:
                    continue
                selected_row, selected_job = updated
                continue
            elif inner == '5':
                updated_row = _toggle_scheduled_job(state, selected_row)
                if updated_row is not None:
                    selected_row = updated_row
            elif inner == '6':
                if _delete_scheduled_job(state, selected_row):
                    break
            elif inner == '0':
                break
            else:
                print('无效选择。')


__all__ = ['_scheduled_menu']
