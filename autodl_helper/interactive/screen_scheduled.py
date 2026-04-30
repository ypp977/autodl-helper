
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from dataclasses import asdict, dataclass, is_dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

from autodl_helper.auth import clear_runtime_authorization, inspect_auth_state
from autodl_helper.auth_cache import write_auth_cache
from autodl_helper.config import (
    AccountSettings,
    KeeperSettings,
    ScheduledStartJob,
    ScheduledStartPriority,
    ScheduledStartSelector,
    Settings,
    read_raw_settings,
    write_raw_settings,
)
from autodl_helper.runtime_control import (
    get_task_enabled,
    read_config_reload_status,
    read_daemon_launch_status,
    read_daemon_status,
    scheduled_job_identity,
)
from autodl_helper.service_launchd import append_service_lifecycle_log
from autodl_helper.services.manager import service_status as _service_status
from autodl_helper.services.manager import start_service as _start_service
from autodl_helper.services.manager import stop_service as _stop_service

from .dialogs import (
    MenuItem,
    _InteractiveCancel,
    _choose_menu,
    _choose_menu_with_refresh,
    _clear_screen,
    _confirm_action,
    _decode_arrow_escape_sequence,
    _hide_cursor,
    _menu_default_key,
    _prompt,
    _prompt_int_with_default,
    _prompt_keeper_settings,
    _prompt_scheduled_job,
    _prompt_scheduled_time_settings,
    _prompt_with_default,
    _read_escape_sequence_blocking,
    _read_escape_sequence_with_deadline,
    _read_fd_char,
    _read_key_with_timeout,
    _repaint_screen,
    _render_menu,
    _supports_arrow_menu,
    _show_cursor,
    _split_csv,
    _update_menu_selection,
    _update_menu_title,
)
from .presentation import (
    BLUE,
    CYAN,
    DIM,
    GREEN,
    RED,
    YELLOW,
    _boxed_lines,
    _display_width,
    _format_hours_brief,
    _format_human_datetime,
    _format_minutes_brief,
    _format_relative_deadline,
    _heading,
    _humanize_datetime_text,
    _key_value,
    _pad_display,
    _parse_iso_datetime,
    _render_two_columns,
    _section,
    _separator,
    _strip_ansi,
    _style_text,
    _tone_chip,
)
from .runtime import (
    InteractivePageStatus,
    InteractiveSnapshotStore,
    InteractiveTaskManager,
    InteractiveTaskResult,
    capture_callable_output,
    reset_thread_capture_state,
)
from .shared import *  # noqa: F401,F403
from .account_ops import *  # noqa: F401,F403
from .account_common import *  # noqa: F401,F403
from .service_ops import *  # noqa: F401,F403
from .config_ops import *  # noqa: F401,F403
from .screen_support import *  # noqa: F401,F403

from .screen_support import _delegate

def _show_result_screen(title: str, body: str, *, code: int | None = None) -> None:
    tone = 'info'
    status_label = ''
    if code is not None:
        tone = 'ok' if code == 0 else 'bad'
        status_label = '成功' if code == 0 else '失败'
    body_lines = [line for line in body.splitlines()] or ['无输出。']
    title_block = _boxed_lines(title, [status_label] if status_label else ['结果详情'], tone=tone)
    content_block = _boxed_lines('详情', body_lines[:40], tone='muted')
    _choose_menu('\n'.join(title_block + [''] + content_block), [MenuItem('0', '返回')], default_key='0')

def _show_live_scheduled_status(
    *,
    job_name: str | None,
    fetch_rows_fn: Callable[[], list[dict[str, Any]]],
    poll_action_fn: Callable[[float | None], str] | None = None,
    refresh_interval_seconds: float = 3.0,
    task_manager: InteractiveTaskManager | None = None,
    snapshot_store: InteractiveSnapshotStore | None = None,
    current_account: str | None = None,
    clear_scope_snapshot_on_exit: bool = False,
    settings: Settings | None = None,
) -> None:
    owns_runtime = False
    if task_manager is None or snapshot_store is None:
        snapshot_store = InteractiveSnapshotStore()
        task_manager = InteractiveTaskManager(snapshot_store=snapshot_store, max_workers=_interactive_max_workers(settings))
        owns_runtime = True
    poll_action = poll_action_fn or _poll_live_action
    account_scope = current_account or 'default'
    scope = f'job:{account_scope}:{job_name}' if job_name else f'all:{account_scope}'
    snapshot_key = _snapshot_key('scheduled_progress', scope)

    def _queue_progress_refresh() -> None:
        _submit_snapshot_task(
            task_manager=task_manager,
            snapshot_store=snapshot_store,
            task_type='scheduled_progress_refresh',
            scope=scope,
            snapshot_key=snapshot_key,
            runner=lambda: _prepare_live_scheduled_rows(fetch_rows_fn()),
            status_message='正在刷新抢机进度',
            replace_queued=True,
        )
        _nudge_background_tasks(task_manager, settle_seconds=0.01)

    def _refresh_progress_now(previous_rows: list[dict[str, Any]] | None = None) -> None:
        refreshed_rows = _prepare_live_scheduled_rows(fetch_rows_fn(), previous_rows=previous_rows)
        _store_snapshot(snapshot_store, snapshot_key, refreshed_rows, status_message='最近更新')

    _refresh_progress_now()
    first_render = True
    try:
        while True:
            task_manager.drain_completed()
            task = task_manager.get_task('scheduled_progress_refresh', scope)
            rows_snapshot = snapshot_store.get_snapshot(snapshot_key)
            rows = list(rows_snapshot) if isinstance(rows_snapshot, list) else []
            status = _page_status_from_tasks(
                snapshot_store=snapshot_store,
                snapshot_key=snapshot_key,
                primary_task=task,
            )
            footer_text, wait_timeout = _scheduled_live_footer(rows, refresh_interval_seconds=refresh_interval_seconds)
            if first_render and wait_timeout is not None and task is None:
                status = InteractivePageStatus(
                    state='refreshing',
                    message='正在刷新抢机进度',
                    updated_at=status.updated_at,
                    error_message=status.error_message,
                )
            first_render = False
            _repaint_screen()
            render_status = getattr(
                sys.modules.get('autodl_helper.interactive_app') or sys.modules.get('autodl_helper.interactive.app'),
                '_render_scheduled_status',
                _render_scheduled_status,
            )
            try:
                body = render_status(
                    job_name,
                    rows,
                    page_status_lines=_page_status_lines(status, active_task=task, progress_label='刷新进度'),
                )
            except TypeError:
                body = render_status(job_name, rows)
            print(body)
            print('')
            print(_section(footer_text))
            action = poll_action(wait_timeout)
            if action == 'back':
                return
            if action == 'refresh':
                _refresh_progress_now(rows)
            elif action == 'stay' and wait_timeout is not None:
                _refresh_progress_now(rows)
    finally:
        if clear_scope_snapshot_on_exit:
            _clear_scheduled_progress_scope_snapshots(
                snapshot_store,
                current_account=current_account,
                job_name=job_name,
            )
        if owns_runtime:
            _nudge_background_tasks(task_manager, settle_seconds=0.01)
            task_manager.shutdown(wait=False)

def _render_keeper_rules(settings: Settings, account_name: str, store) -> str:
    enabled = get_task_enabled(store, account_name, 'keeper', default_enabled=settings.tasks.keeper.enabled)
    keeper = settings.tasks.keeper
    overview = _keeper_probe_overview([])
    schedule_lines = _keeper_probe_schedule_lines(settings, store, account_name=account_name)
    lines = [
        _heading('Keeper 规则确认', color=CYAN),
        _separator(),
        _section('[当前账号]'),
        _key_value('账号', account_name),
        _key_value('Keeper 状态', _tone_chip('运行中', 'ok') if enabled else _tone_chip('已暂停', 'warn')),
        *schedule_lines,
        _key_value('本次应接管', f"{overview['due']} 台"),
        _key_value('未到接管窗口', f"{overview['not_due']} 台"),
        _key_value('状态异常', f"{overview['abnormal']} 台"),
        _key_value('一周内接近释放', f"{overview['expiring']} 台"),
        '',
        _section('[规则详情]'),
        _key_value('最多保留', _format_hours_brief(keeper.shutdown_release_after_hours)),
        _key_value('释放前开始接管', _format_hours_brief(keeper.keeper_trigger_before_hours)),
        _key_value('检查频率', _format_minutes_brief(keeper.interval_minutes)),
    ]
    return '\n'.join(lines)

def _keeper_probe_schedule_lines(settings: Settings, store, *, account_name: str | None) -> list[str]:
    if store is None:
        return [
            _key_value('下次执行时间', '后台未运行'),
            _key_value('上次执行时间', '待首次执行'),
            _key_value('上次执行结果', '暂无结果'),
        ]
    scope = account_name or 'default'
    last_run_raw = str(store.get_runtime_value('last_run:keeper', '') or '').strip()
    last_run = _parse_iso_datetime(last_run_raw)
    last_run_text = _format_human_datetime(last_run_raw) if last_run_raw else '待首次执行'
    daemon_running = bool(read_daemon_status(store).get('running'))
    if not get_task_enabled(store, scope, 'keeper', default_enabled=settings.tasks.keeper.enabled):
        next_text = '未启用'
    elif not daemon_running:
        next_text = '后台未运行'
    elif last_run is None:
        next_text = '待首次执行'
    else:
        next_dt = last_run + timedelta(minutes=max(1, int(settings.tasks.keeper.interval_minutes or 1)))
        next_text = _format_human_datetime(next_dt.isoformat())
    last_result_text = _keeper_last_execution_summary(store, account_name=scope)
    return [
        _key_value('下次执行时间', next_text),
        _key_value('上次执行时间', last_run_text),
        _key_value('上次执行结果', last_result_text),
    ]

def _keeper_last_execution_summary(store, *, account_name: str) -> str:
    if store is None:
        return '暂无结果'
    history_rows = store.read_history(account_name=account_name, task_type='keeper', limit=100)
    if not history_rows:
        return '暂无结果'
    latest_dt = _parse_iso_datetime(str(history_rows[0].created_at or ''))
    if latest_dt is None:
        return '暂无结果'
    latest_payload = history_rows[0].payload if isinstance(history_rows[0].payload, dict) else {}
    latest_batch_id = str(latest_payload.get('batch_id') or '').strip()
    if latest_batch_id:
        batch = [
            row for row in history_rows
            if str((row.payload or {}).get('batch_id') or '').strip() == latest_batch_id
        ]
    else:
        latest_bucket = latest_dt.replace(microsecond=0)
        batch = []
        for row in history_rows:
            row_dt = _parse_iso_datetime(str(row.created_at or ''))
            if row_dt is None:
                continue
            if row_dt.replace(microsecond=0) == latest_bucket:
                batch.append(row)
    if not batch:
        return '暂无结果'
    success = sum(1 for row in batch if str(row.result or '') == 'keeper_executed')
    failed = sum(1 for row in batch if str(row.result or '') in {'keeper_failed_power_on', 'keeper_failed_power_off'})
    skipped = max(0, len(batch) - success - failed)
    return f'已处理 {success} 台 / 跳过 {skipped} 台 / 失败 {failed} 台'

def _keeper_probe_schedule_texts(settings: Settings, store, *, account_name: str | None) -> tuple[str, str]:
    lines = _keeper_probe_schedule_lines(settings, store, account_name=account_name)
    values: list[str] = []
    for line in lines:
        values.append(_strip_ansi(line).split(':', 1)[1].strip() if ':' in _strip_ansi(line) else '暂无结果')
    while len(values) < 2:
        values.append('暂无结果')
    return values[0], values[1]

def _keeper_probe_overview(rows: list[dict[str, Any]]) -> dict[str, int]:
    due = sum(1 for row in rows if bool(row.get('eligible')))
    not_due = sum(1 for row in rows if str(row.get('result') or '') == 'skip_not_due')
    abnormal = sum(1 for row in rows if str(row.get('result') or '') in {'skip_missing_shutdown_time', 'skip_missing_instance_id'})
    expiring = sum(1 for row in rows if _keeper_release_within_days(row, days=7))
    return {
        'due': due,
        'not_due': not_due,
        'abnormal': abnormal,
        'expiring': expiring,
    }

def _keeper_release_within_days(row: dict[str, Any], *, days: int) -> bool:
    deadline = _parse_iso_datetime(str(row.get('release_deadline') or ''))
    if deadline is None:
        return False
    now = datetime.now().astimezone()
    deadline = deadline.astimezone()
    return now <= deadline <= now + timedelta(days=days)

def _render_keeper_probe_page(rows: list[dict[str, Any]], *, page_status_lines: list[str] | None = None) -> str:
    will_run = [row for row in rows if row.get('eligible')]
    abnormal = [row for row in rows if str(row.get('result') or '') in {'skip_missing_shutdown_time', 'skip_missing_instance_id'}]
    lines = [
        _heading('Keeper 检测结果', color=CYAN),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        '',
        _section('[本次将执行]'),
    ])
    if will_run:
        for row in will_run:
            release_text = _format_relative_deadline(str(row.get('release_deadline') or '')) if row.get('release_deadline') else '待确认'
            keeper_text = _format_relative_deadline(str(row.get('next_keeper_time') or '')) if row.get('next_keeper_time') else '现在'
            card_lines = [
                _key_value('实例状态', row.get('status') or '待确认'),
                _key_value('当前阶段', _tone_chip('进入 Keeper 窗口', 'ok')),
                _key_value('下一步动作', '按顺序执行本轮 Keeper'),
                _key_value('距离释放', release_text),
                _key_value('距离接管', keeper_text),
            ]
            lines.extend(_boxed_lines(f"实例 {row['instance_id']}", card_lines, tone='ok'))
            lines.append('')
    else:
        lines.append('暂无需要执行的实例')
    if abnormal:
        lines.extend(['', _section('[状态异常]')])
        for row in abnormal[:10]:
            card_lines = [
                _key_value('实例状态', row.get('status') or '未知'),
                _key_value('当前阶段', _tone_chip(_keeper_result_label(str(row.get('result') or 'skip_missing_shutdown_time')), 'bad')),
                _key_value('下一步动作', _keeper_reason_label(str(row.get('reason') or 'missing_shutdown_time'))),
                _key_value('距离释放', _format_relative_deadline(str(row.get('release_deadline') or '')) if row.get('release_deadline') else '暂无结果'),
                _key_value('距离接管', _format_relative_deadline(str(row.get('next_keeper_time') or '')) if row.get('next_keeper_time') else '暂无结果'),
            ]
            lines.extend(_boxed_lines(f"实例 {row['instance_id']}", card_lines, tone='bad'))
            lines.append('')
    return '\n'.join(lines)

def _render_keeper_execution_page(
    results: list[Any],
    *,
    page_status_lines: list[str] | None = None,
) -> str:
    executed = [item for item in results if getattr(item, 'result', '') == 'keeper_executed']
    failed = [item for item in results if getattr(item, 'result', '') in {'keeper_failed_power_on', 'keeper_failed_power_off'}]
    skipped = [item for item in results if getattr(item, 'result', '') not in {'keeper_executed', 'keeper_failed_power_on', 'keeper_failed_power_off'}]
    lines = [
        _heading('Keeper 执行结果', color=CYAN),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        _key_value('已处理', len(executed)),
        _key_value('失败', len(failed)),
        _key_value('跳过', len(skipped)),
        '',
    ])

    def _render_section(title: str, items: list[Any], tone: str) -> None:
        if not items:
            return
        lines.extend([_section(title)])
        for item in items:
            result = str(getattr(item, 'result', '') or '')
            reason = str(getattr(item, 'reason', '') or '')
            summary = _humanize_datetime_text(str(getattr(item, 'summary', '') or '').strip())
            release_deadline = str(getattr(item, 'release_deadline', '') or '').strip()
            next_keeper_time = str(getattr(item, 'next_keeper_time', '') or '').strip()
            release_text = _format_relative_deadline(release_deadline) if release_deadline else '暂无结果'
            keeper_text = _format_relative_deadline(next_keeper_time) if next_keeper_time else '暂无结果'
            card_lines = [
                _key_value('实例状态', getattr(item, 'status', '') or '待确认'),
                _key_value('当前阶段', _tone_chip(_keeper_result_label(result or '暂无结果'), tone)),
                _key_value('下一步动作', _keeper_reason_label(reason or '暂无结果')),
                _key_value('距离释放', release_text),
                _key_value('距离接管', keeper_text),
            ]
            if summary:
                card_lines.append(_key_value('结果说明', summary))
            lines.extend(
                _boxed_lines(
                    f"实例 {getattr(item, 'instance_id', '未知实例')}",
                    card_lines,
                    tone=tone,
                )
            )
            lines.append('')

    _render_section('[已处理]', executed, 'ok')
    _render_section('[执行失败]', failed, 'bad')
    if not results:
        lines.append('暂无执行结果')
    elif not executed and not failed:
        lines.append('暂无需要执行的实例')
    return '\n'.join(lines)

def _render_scheduled_job_picker(
    settings: Settings,
    account_name: str,
    status_rows: list[dict[str, Any]],
    *,
    page_status_lines: list[str] | None = None,
) -> str:
    lines = [
        _heading('选择抢机器规则'),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        _key_value('当前账号', account_name),
        '',
        _section('[任务列表]'),
    ])
    return '\n'.join(lines)

def _render_scheduled_job_detail(
    job,
    status_row: dict[str, Any],
    account_name: str,
    *,
    page_status_lines: list[str] | None = None,
) -> str:
    target_summary = _job_target_summary(job)
    schedule_mode = getattr(job, 'schedule_mode', 'daily') or 'daily'
    runtime_label, runtime_tone = _scheduled_runtime_status_label(job, status_row)
    runtime_status = _tone_chip(runtime_label, runtime_tone)
    lines = [
        _heading('抢机器规则'),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        _section('[基本信息]'),
        _key_value('当前账号', account_name),
        _key_value('任务名称', scheduled_job_identity(job)),
        _key_value('目标时间', status_row["target_time"]),
        _key_value('提前启动', f'{status_row["advance_hours"]} 小时'),
        _key_value('执行计划', '单次' if schedule_mode == 'once' else '每天'),
        _key_value('任务状态', runtime_status),
        '',
    ])
    if status_row.get('last_run_label'):
        lines.extend([
            _section('[最近执行]'),
            _key_value('执行方式', status_row.get('last_run_trigger') or '-'),
            _key_value('本次结果', status_row.get('last_run_label') or '-'),
            _key_value('结果说明', status_row.get('last_run_summary') or '-'),
            '',
        ])
    lines.append(_section('[目标条件]'))
    if job.instance_id:
        lines.append(_key_value('目标方式', '固定实例'))
        lines.append(_key_value('目标实例', job.instance_id or '-'))
    else:
        lines.append(_key_value('目标方式', '按条件筛选候选机器'))
        lines.append(_key_value('筛选条件', target_summary))
    return '\n'.join(lines)

def _render_scheduled_run_results(job_name: str, results: list[Any]) -> str:
    lines = [_heading(f'抢机器执行结果: {job_name}'), _separator(), '']
    if not results:
        lines.append('- 本次没有产生新的执行结果（可能已被当前窗口成功记录跳过）')
    else:
        for item in results:
            lines.append(
                f"- result={_scheduled_result_label(getattr(item, 'result', '-'))} reason={_scheduled_reason_label(getattr(item, 'reason', '-'))} "
                f"instance={getattr(item, 'instance_id', '-') or '-'} summary={getattr(item, 'summary', '') or '-'}"
            )
    return '\n'.join(lines)

def _show_scheduled_run_results_screen(
    *,
    job_name: str,
    results: list[Any],
    fetch_rows_fn: Callable[[], list[dict[str, Any]]],
    back_label: str,
) -> None:
    post_selected_key = '1'
    while True:
        post_items = [MenuItem('1', '查看抢机进度'), MenuItem('0', back_label)]
        action = _choose_menu(
            _render_scheduled_run_results(job_name, results),
            post_items,
            default_key=_menu_default_key(post_items, post_selected_key),
        )
        post_selected_key = action
        if action == '1':
            _show_live_scheduled_status(job_name=job_name, fetch_rows_fn=fetch_rows_fn, settings=None)
        elif action == '0':
            break
        else:
            print('无效选择。')

def _render_scheduled_status(
    job_name: str | None,
    status_rows: list[dict[str, Any]],
    *,
    page_status_lines: list[str] | None = None,
) -> str:
    title = f'抢机进度: {job_name}' if job_name else '抢机进度'
    lines = [_heading(title), _separator()]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.append('')
    for row in status_rows:
        payload = row.get('latest_payload') or {}
        stage_label = str(row.get('_live_stage_label')) if '_live_stage_label' in row else None
        stage_tone = str(row.get('_live_stage_tone')) if '_live_stage_tone' in row else None
        if stage_label is None or stage_tone is None:
            stage_label, stage_tone = _scheduled_stage_label(row)
        execution_label = str(row.get('_live_execution_label')) if '_live_execution_label' in row else None
        execution_tone = str(row.get('_live_execution_tone')) if '_live_execution_tone' in row else None
        if execution_label is None or execution_tone is None:
            execution_label, execution_tone = _scheduled_execution_status(row)
        missing_reason_label = str(row.get('_live_missing_reason_label')) if '_live_missing_reason_label' in row else None
        missing_reason_tone = str(row.get('_live_missing_reason_tone')) if '_live_missing_reason_tone' in row else None
        if missing_reason_label is None or missing_reason_tone is None:
            missing_reason_label, missing_reason_tone = _scheduled_missing_check_reason(row)
        rule_match_label, rule_match_tone = _scheduled_rule_match_note(row)
        poll_text = str(row.get('_live_poll_text')) if '_live_poll_text' in row else None
        target_text = str(row.get('_live_target_text')) if '_live_target_text' in row else None
        if poll_text is None or target_text is None:
            poll_text, target_text = _scheduled_window_countdowns(row)
        next_action = str(row.get('_live_next_action') or '').strip() if '_live_next_action' in row else ''
        if not next_action:
            next_action = _scheduled_next_action(row)
        hit_text, waiting_text, dropped_text = _scheduled_candidate_groups(payload)
        card_lines = [
            _key_value('规则开关', _tone_chip('已启用', 'ok') if row['enabled'] else _tone_chip('已停用', 'warn')),
            _key_value('执行状态', _tone_chip(execution_label, execution_tone)),
            _key_value('当前阶段', _tone_chip(stage_label, stage_tone)),
            _key_value('下一步动作', next_action),
            _key_value('执行计划', '单次' if row.get('schedule_mode') == 'once' else '每天'),
            _key_value('目标方式', '固定实例' if row.get('target_mode') == 'instance' else '按条件筛选候选机器'),
            _key_value('目标条件', _scheduled_field_fallback(row.get('target_summary'), empty_text='未设置')),
            _key_value('目标时间', _scheduled_field_fallback(row.get('target_time'), empty_text='未设置')),
            _key_value('距离开始轮询', poll_text),
            _key_value('距离目标时间', target_text),
            _key_value('最近检查时间', _scheduled_latest_check_text(row)),
            _key_value('最近检查结果', _scheduled_latest_result_text(row)),
            _key_value('已命中', _scheduled_candidate_group_text(hit_text)),
            _key_value('等待中', _scheduled_candidate_group_text(waiting_text)),
            _key_value('被淘汰', _scheduled_candidate_group_text(dropped_text)),
        ]
        if missing_reason_label:
            card_lines.insert(8, _key_value('未检查原因', _tone_chip(missing_reason_label, missing_reason_tone)))
        if rule_match_label:
            card_lines.insert(12 if missing_reason_label else 11, _key_value('规则匹配状态', _tone_chip(rule_match_label, rule_match_tone)))
        if row.get('latest_summary'):
            card_lines.append(_key_value('结果说明', _sanitize_scheduled_summary(row['latest_summary'])))
        lines.extend(_boxed_lines(f"任务 {row['job_name']}", card_lines, tone=stage_tone))
        lines.append('')
    return '\n'.join(lines)

def _build_scheduled_detail_menu_items(enabled: bool, daemon_running: bool) -> list[MenuItem]:
    return [
        MenuItem('1', '立即执行一轮' if enabled else '恢复并执行一轮'),
        MenuItem('2', '查看抢机进度'),
        MenuItem('4', '修改规则'),
        MenuItem('5', '暂停任务' if enabled else '恢复任务'),
        MenuItem('6', '删除任务'),
        MenuItem('0', '返回规则列表'),
    ]

_choose_menu = _delegate('_choose_menu', _choose_menu)
_choose_menu_with_refresh = _delegate('_choose_menu_with_refresh', _choose_menu_with_refresh)
_menu_default_key = _delegate('_menu_default_key', _menu_default_key)
read_daemon_status = _delegate('read_daemon_status', read_daemon_status)
read_daemon_launch_status = _delegate('read_daemon_launch_status', read_daemon_launch_status)
read_config_reload_status = _delegate('read_config_reload_status', read_config_reload_status)
_poll_live_action = _delegate('_poll_live_action', _poll_live_action)

__all__ = [
    '_show_result_screen',
    '_show_live_scheduled_status',
    '_render_keeper_rules',
    '_keeper_probe_schedule_lines',
    '_keeper_last_execution_summary',
    '_keeper_probe_schedule_texts',
    '_keeper_probe_overview',
    '_keeper_release_within_days',
    '_render_keeper_probe_page',
    '_render_keeper_execution_page',
    '_render_scheduled_job_picker',
    '_render_scheduled_job_detail',
    '_render_scheduled_run_results',
    '_show_scheduled_run_results_screen',
    '_render_scheduled_status',
    '_build_scheduled_detail_menu_items',
]

_show_live_scheduled_status = _delegate('_show_live_scheduled_status', _show_live_scheduled_status)
_render_keeper_rules = _delegate('_render_keeper_rules', _render_keeper_rules)
_keeper_probe_schedule_lines = _delegate('_keeper_probe_schedule_lines', _keeper_probe_schedule_lines)
_keeper_last_execution_summary = _delegate('_keeper_last_execution_summary', _keeper_last_execution_summary)
_keeper_probe_schedule_texts = _delegate('_keeper_probe_schedule_texts', _keeper_probe_schedule_texts)
_keeper_probe_overview = _delegate('_keeper_probe_overview', _keeper_probe_overview)
_keeper_release_within_days = _delegate('_keeper_release_within_days', _keeper_release_within_days)
_render_keeper_probe_page = _delegate('_render_keeper_probe_page', _render_keeper_probe_page)
_render_keeper_execution_page = _delegate('_render_keeper_execution_page', _render_keeper_execution_page)
_render_scheduled_job_picker = _delegate('_render_scheduled_job_picker', _render_scheduled_job_picker)
_render_scheduled_job_detail = _delegate('_render_scheduled_job_detail', _render_scheduled_job_detail)
_render_scheduled_run_results = _delegate('_render_scheduled_run_results', _render_scheduled_run_results)
_show_scheduled_run_results_screen = _delegate('_show_scheduled_run_results_screen', _show_scheduled_run_results_screen)
_build_scheduled_detail_menu_items = _delegate('_build_scheduled_detail_menu_items', _build_scheduled_detail_menu_items)
