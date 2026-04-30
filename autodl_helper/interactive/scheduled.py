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

if TYPE_CHECKING:
    from autodl_helper.models import HistoryRecord

DEFAULT_SERVICE_LABEL = 'autodl-helper'
_SERVICE_CONFIG_PATH = 'config.yaml'


from .status_task import (
    _friendly_resource_error_message,
    _is_scheduled_once_complete_result,
    _is_scheduled_once_terminal_result,
    datetime,
)

def _serialize_priority(priority: list[ScheduledStartPriority]) -> str:
    parts: list[str] = []
    for item in priority:
        fields: list[str] = []
        if item.instance_id:
            fields.append(f'iid={item.instance_id}')
        if item.region:
            fields.append(f'region={item.region}')
        if item.machine_alias:
            fields.append(f'alias={item.machine_alias}')
        if fields:
            parts.append(';'.join(fields))
    return ' | '.join(parts)

def _parse_priority(raw: str) -> list[ScheduledStartPriority]:
    if not raw.strip():
        return []
    items: list[ScheduledStartPriority] = []
    for chunk in raw.split('|'):
        payload: dict[str, str] = {}
        for field in chunk.split(';'):
            if '=' not in field:
                continue
            key, value = field.split('=', 1)
            payload[key.strip()] = value.strip()
        items.append(
            ScheduledStartPriority(
                instance_id=payload.get('iid', ''),
                region=payload.get('region', ''),
                machine_alias=payload.get('alias', ''),
            )
        )
    return [item for item in items if item.instance_id or item.region or item.machine_alias]

def _job_to_payload(job: ScheduledStartJob) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'instance_id': job.instance_id,
        'name': job.name,
        'target_time': job.target_time,
        'advance_hours': job.advance_hours,
        'schedule_mode': getattr(job, 'schedule_mode', 'daily') or 'daily',
        'timezone': job.timezone,
    }
    if job.selector is not None:
        payload['selector'] = asdict(job.selector)
    return payload

def _job_target_summary(job: ScheduledStartJob) -> str:
    if job.instance_id:
        return f'固定实例={job.instance_id}'
    if job.selector is None:
        return '-'
    parts = []
    if job.selector.regions:
        parts.append(f"地区={','.join(job.selector.regions)}")
    if job.selector.gpu_model:
        parts.append(f'GPU={job.selector.gpu_model}')
    if job.selector.gpu_count:
        parts.append(f'数量={job.selector.gpu_count}')
    return '；'.join(parts) or '-'

def _scheduled_result_label(value: str) -> str:
    return {
        'started': '已发起开机',
        'already_running': '实例已在运行',
        'outside_window': '未到轮询窗口',
        'waiting_for_gpu': '有候选但暂时不可抢',
        'waiting_for_instance': '还没等到目标实例',
        'no_eligible_candidate': '有候选但当前都不可开机',
        'selector_no_match': '当前没有命中筛选条件的候选',
        'instance_missing': '实例不存在',
        'started_without_gpu': '已开机但未进 GPU 模式',
        'power_on_submitted': '开机请求已提交',
        'deadline_failed': '超过截止时间仍失败',
    }.get(value, value or '-')

def _scheduled_reason_label(value: str) -> str:
    return {
        'started': '已提交开机动作',
        'already_running': '实例已在 GPU 模式运行',
        'outside_window': '当前还没到轮询窗口',
        'no_eligible_candidate': '有匹配候选，但都暂时不满足开机条件',
        'selector_no_match': '当前没有任何候选命中筛选条件',
        'waiting_for_gpu': '当前候选暂时不可开机',
        'waiting_for_instance': '当前还没有等到目标实例',
        'running_with_gpu': '实例已在 GPU 模式运行',
        'gpu_idle_zero': '空闲 GPU 数量为 0',
        'eligible': '候选满足条件，等待执行',
        'instance_missing': '目标实例不存在',
        'power_on_submitted': '平台已接受开机请求',
        'started_without_gpu': '实例已开机但未进入 GPU 模式',
        'deadline_failed': '超过截止时间仍未成功',
        'deadline_missed': '超过目标截止时间',
    }.get(value, value or '-')

def _scheduled_summary_replacements() -> dict[str, str]:
    return {
        'started': _scheduled_result_label('started'),
        'already_running': _scheduled_result_label('already_running'),
        'outside_window': _scheduled_result_label('outside_window'),
        'waiting_for_gpu': _scheduled_result_label('waiting_for_gpu'),
        'waiting_for_instance': _scheduled_result_label('waiting_for_instance'),
        'no_eligible_candidate': _scheduled_result_label('no_eligible_candidate'),
        'selector_no_match': _scheduled_result_label('selector_no_match'),
        'instance_missing': _scheduled_result_label('instance_missing'),
        'started_without_gpu': _scheduled_result_label('started_without_gpu'),
        'power_on_submitted': _scheduled_result_label('power_on_submitted'),
        'deadline_failed': _scheduled_result_label('deadline_failed'),
        'running_with_gpu': _scheduled_reason_label('running_with_gpu'),
        'gpu_idle_zero': _scheduled_reason_label('gpu_idle_zero'),
        'eligible': _scheduled_reason_label('eligible'),
        'deadline_missed': _scheduled_reason_label('deadline_missed'),
        'shutdown': _normalize_instance_status('shutdown'),
        'stopped': _normalize_instance_status('stopped'),
        'running': _normalize_instance_status('running'),
        'booting': _normalize_instance_status('booting'),
        'starting': _normalize_instance_status('starting'),
        'pending': _normalize_instance_status('pending'),
        'stopping': _normalize_instance_status('stopping'),
        'gpu': _normalize_start_mode('gpu'),
        'non_gpu': _normalize_start_mode('non_gpu'),
    }

def _sanitize_scheduled_summary(text: Any) -> str:
    raw = str(text or '').strip()
    if not raw:
        return '-'
    sanitized = raw
    for source, target in sorted(_scheduled_summary_replacements().items(), key=lambda item: len(item[0]), reverse=True):
        sanitized = re.sub(rf'(?<![A-Za-z0-9_]){re.escape(source)}(?![A-Za-z0-9_])', target, sanitized)
    return _humanize_datetime_text(sanitized)

def _keeper_result_label(value: str) -> str:
    return {
        'ready': '可执行保活',
        'keeper_executed': '已执行保活',
        'keeper_failed_power_on': '开机失败',
        'keeper_failed_power_off': '关机失败',
        'skip_not_due': '未到保活窗口',
        'skip_recently_stopped': '最近关机，处于冷却期',
        'skip_recently_started': '最近开机，处于冷却期',
        'skip_missing_shutdown_time': '缺少关机时间',
        'skip_already_executed_in_cycle': '本周期已执行过',
        'skip_missing_instance_id': '缺少实例 ID',
    }.get(value, value or '-')

def _keeper_reason_label(value: str) -> str:
    return {
        'before_next_keeper_time': '还没到下次保活时间',
        'stopped_within_cooldown': '最近关机时间未超过冷却窗口',
        'started_within_cooldown': '最近启动时间未超过冷却窗口',
        'fallback_status_at_recently_stopped': '只能用 status_at 兜底，且仍在关机冷却窗口',
        'fallback_status_at_recently_started': '只能用 status_at 兜底，且仍在开机冷却窗口',
        'fallback_status_at_ready': '只能用 status_at 兜底，但已到保活窗口',
        'keeper_window_reached': '已到保活执行窗口',
        'missing_shutdown_time': '没有可用的关机时间',
        'already_executed_in_release_cycle': '本轮释放周期里已经执行过',
        'power_on_failed': '开机接口执行失败',
        'power_off_failed': '关机接口执行失败',
        'missing_instance_id': '实例缺少 uuid',
    }.get(value, value or '-')

def _ensure_jobs_payload(raw_payload: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    tasks_payload = raw_payload.setdefault('tasks', {})
    scheduled_payload = tasks_payload.setdefault('scheduled_start', {})
    jobs_payload = scheduled_payload.setdefault('jobs', [])
    if not jobs_payload and settings.tasks.scheduled_start.jobs:
        jobs_payload.extend(_job_to_payload(job) for job in settings.tasks.scheduled_start.jobs)
    return jobs_payload

def _persist_job_changes(
    *,
    config_path: str,
    settings: Settings,
    load_settings_fn: Callable[[str], Settings],
    validate_settings_fn: Callable[[Settings, str], list[str]],
    mutator: Callable[[list[dict[str, Any]]], None],
) -> None:
    raw_payload = read_raw_settings(config_path)
    original_payload = copy.deepcopy(raw_payload)
    tasks_payload = raw_payload.setdefault('tasks', {})
    scheduled_payload = tasks_payload.setdefault('scheduled_start', {})
    jobs_payload = _ensure_jobs_payload(raw_payload, settings)
    mutator(jobs_payload)
    if jobs_payload:
        scheduled_payload['enabled'] = True
    else:
        scheduled_payload['enabled'] = False
    write_raw_settings(config_path, raw_payload)
    updated_settings = load_settings_fn(config_path)
    errors = validate_settings_fn(updated_settings, purpose='validate')
    if errors:
        write_raw_settings(config_path, original_payload)
        raise ValueError('配置写回失败: ' + '; '.join(errors))

def _persist_keeper_changes(
    *,
    config_path: str,
    settings: Settings,
    load_settings_fn: Callable[[str], Settings],
    validate_settings_fn: Callable[[Settings, str], list[str]],
    keeper_settings: KeeperSettings,
) -> None:
    raw_payload = read_raw_settings(config_path)
    original_payload = copy.deepcopy(raw_payload)
    tasks_payload = raw_payload.setdefault('tasks', {})
    tasks_payload['keeper'] = {
        'enabled': keeper_settings.enabled,
        'min_day': keeper_settings.min_day,
        'shutdown_release_after_hours': keeper_settings.shutdown_release_after_hours,
        'keeper_trigger_before_hours': keeper_settings.keeper_trigger_before_hours,
        'interval_minutes': keeper_settings.interval_minutes,
        'power_on_wait_seconds': keeper_settings.power_on_wait_seconds,
        'power_off_wait_seconds': keeper_settings.power_off_wait_seconds,
        'start_cooldown_minutes': keeper_settings.start_cooldown_minutes,
        'stop_cooldown_minutes': keeper_settings.stop_cooldown_minutes,
        'fallback_to_status_at': keeper_settings.fallback_to_status_at,
    }
    write_raw_settings(config_path, raw_payload)
    updated_settings = load_settings_fn(config_path)
    errors = validate_settings_fn(updated_settings, purpose='validate')
    if errors:
        write_raw_settings(config_path, original_payload)
        raise ValueError('配置写回失败: ' + '; '.join(errors))

def _normalize_charge_type(value: Any) -> str:
    if isinstance(value, list):
        parts = [_normalize_charge_type(item) for item in value if str(item).strip()]
        return ', '.join(parts) if parts else '-'
    text = str(value or '').strip()
    mapping = {
        'payg': '按量计费',
        'pay_as_you_go': '按量计费',
        'day': '包日计费',
        'package_day': '包日计费',
        'monthly': '包月计费',
        'month': '包月计费',
    }
    return mapping.get(text.lower(), text or '-')

def _normalize_instance_status(value: Any) -> str:
    text = str(value or '').strip().lower()
    mapping = {
        'running': '运行中',
        'on': '运行中',
        'shutdown': '已关机',
        'stopped': '已关机',
        'off': '已关机',
        'booting': '启动中',
        'starting': '启动中',
        'pending': '启动中',
        'stopping': '关机中',
    }
    return mapping.get(text, str(value or '-').strip() or '-')

def _normalize_start_mode(value: Any) -> str:
    text = str(value or '').strip().lower()
    mapping = {
        'gpu': 'GPU 模式',
        'non_gpu': '非 GPU 模式',
        'cpu': '非 GPU 模式',
    }
    return mapping.get(text, str(value or '-').strip() or '-')

def _scheduled_picker_item_label(row: dict[str, Any]) -> str:
    status_label = str(row.get('task_status_label') or ('已启用' if row.get('enabled') else '已暂停'))
    base = f"{row['job_name']}  {row['target_time']} 提前{row['advance_hours']}h  {status_label}"
    last_run_label = str(row.get('last_run_label') or '').strip()
    if not last_run_label:
        return base
    last_run_summary = str(row.get('last_run_summary') or '').strip()
    if last_run_summary:
        if len(last_run_summary) > 18:
            last_run_summary = last_run_summary[:15] + '...'
        return f'{base}  / 最近执行: {last_run_label} ({last_run_summary})'
    return f'{base}  / 最近执行: {last_run_label}'

def _scheduled_runtime_status_label(job, status_row: dict[str, Any]) -> tuple[str, str]:
    if status_row.get('task_status_label'):
        return str(status_row.get('task_status_label') or ''), str(status_row.get('task_status_tone') or 'info')
    latest_result = str(status_row.get('latest_result') or '')
    schedule_mode = getattr(job, 'schedule_mode', 'daily') or 'daily'
    if schedule_mode == 'once' and latest_result in {'started', 'already_running', 'power_on_submitted'}:
        return '单次已完成', 'ok'
    if not status_row.get('enabled', True):
        return '已暂停', 'warn'
    if bool(status_row.get('daemon_running')):
        return '轮询中', 'ok'
    return '等待执行', 'info'

def _scheduled_seed_status_rows(
    settings: Settings,
    store,
    *,
    account_name: str,
) -> list[dict[str, Any]]:
    task_enabled = get_task_enabled(store, account_name, 'scheduled_start', default_enabled=settings.tasks.scheduled_start.enabled)
    daemon_running = bool(read_daemon_status(store).get('running'))
    rows: list[dict[str, Any]] = []
    for job in settings.tasks.scheduled_start.jobs:
        identity = scheduled_job_identity(job)
        control = store.get_scheduled_job_control(account_name, identity) or {}
        enabled = bool(task_enabled) and bool(control.get('enabled', True))
        row = {
            'job_name': identity,
            'enabled': enabled,
            'target_time': str(control.get('target_time_override') or job.target_time),
            'advance_hours': control.get('advance_hours_override')
            if control.get('advance_hours_override') is not None
            else job.advance_hours,
            'schedule_mode': str(getattr(job, 'schedule_mode', 'daily') or 'daily'),
            'timezone': getattr(job, 'timezone', 'Asia/Shanghai') or 'Asia/Shanghai',
            'latest_result': '',
            'latest_reason': '',
            'latest_summary': '',
            'latest_created_at': '',
            'latest_payload': {},
            'latest_instance_id': '',
            'has_history': False,
            'latest_matches_current_rule': False,
            'target_mode': 'instance' if job.instance_id else 'selector',
            'target_summary': _job_target_summary(job),
            'daemon_running': daemon_running,
            'last_run_trigger': '',
            'last_run_label': '',
            'last_run_summary': '',
        }
        row.update(_scheduled_runtime_status_fields(row))
        rows.append(row)
    return rows

def _account_has_enabled_scheduled_jobs(settings: Settings, store, *, account_name: str) -> bool:
    if not get_task_enabled(store, account_name, 'scheduled_start', default_enabled=settings.tasks.scheduled_start.enabled):
        return False
    for job in settings.tasks.scheduled_start.jobs:
        control = store.get_scheduled_job_control(account_name, scheduled_job_identity(job)) or {}
        if bool(control.get('enabled', True)):
            return True
    return False

def _scheduled_run_result_summary(results: list[Any], *, trigger_label: str) -> dict[str, str]:
    if not results:
        return {
            'last_run_trigger': trigger_label,
            'last_run_label': '本次没有产生新的执行结果',
            'last_run_summary': '可能已被当前窗口成功记录跳过',
        }
    item = results[-1]
    summary = _sanitize_scheduled_summary(getattr(item, 'summary', '') or '')
    if summary == '-':
        summary = _scheduled_reason_label(str(getattr(item, 'reason', '') or ''))
    return {
        'last_run_trigger': trigger_label,
        'last_run_label': _scheduled_result_label(str(getattr(item, 'result', '') or '')),
        'last_run_summary': summary,
    }

def _scheduled_runtime_status_fields(row: dict[str, Any]) -> dict[str, str]:
    latest_result = str(row.get('latest_result') or '')
    schedule_mode = str(row.get('schedule_mode') or 'daily')
    if schedule_mode == 'once' and _is_scheduled_once_complete_result(latest_result):
        return {'task_status_label': '单次已完成', 'task_status_tone': 'ok'}
    if not row.get('enabled', True):
        return {'task_status_label': '已暂停', 'task_status_tone': 'warn'}
    if bool(row.get('daemon_running')):
        return {'task_status_label': '轮询中', 'task_status_tone': 'ok'}
    return {'task_status_label': '等待执行', 'task_status_tone': 'info'}

def _scheduled_result_payload(item: Any) -> dict[str, Any]:
    candidate_details: list[dict[str, Any]] = []
    for detail in list(getattr(item, 'candidate_details', []) or []):
        if isinstance(detail, dict):
            candidate_details.append(dict(detail))
        elif is_dataclass(detail):
            candidate_details.append(asdict(detail))
    return {
        'candidate_count': int(getattr(item, 'candidate_count', 0) or 0),
        'candidate_details': candidate_details,
        'selected_instance_id': str(getattr(item, 'selected_instance_id', '') or ''),
        'selected_instance_label': str(getattr(item, 'selected_instance_label', '') or ''),
        'selector_summary': str(getattr(item, 'selector_summary', '') or ''),
        'status': str(getattr(item, 'status', '') or ''),
    }

def _scheduled_run_result_state(base_row: dict[str, Any], results: list[Any], *, trigger_label: str) -> dict[str, Any]:
    state: dict[str, Any] = _scheduled_run_result_summary(results, trigger_label=trigger_label)
    if not results:
        return state
    item = results[-1]
    latest_result = str(getattr(item, 'result', '') or '')
    state.update(
        {
            'latest_result': latest_result,
            'latest_reason': str(getattr(item, 'reason', '') or ''),
            'latest_summary': str(getattr(item, 'summary', '') or ''),
            'latest_created_at': datetime.now().astimezone().isoformat(),
            'latest_payload': _scheduled_result_payload(item),
            'latest_instance_id': str(getattr(item, 'instance_id', '') or ''),
            'has_history': True,
            'latest_matches_current_rule': True,
        }
    )
    if str(base_row.get('schedule_mode') or 'daily') == 'once' and _is_scheduled_once_terminal_result(latest_result):
        state['enabled'] = False
    merged_row = dict(base_row)
    merged_row.update(state)
    state.update(_scheduled_runtime_status_fields(merged_row))
    return state

def _scheduled_run_pending_state(
    base_row: dict[str, Any],
    *,
    trigger_label: str,
    task_type: str,
    task_scope: str,
) -> dict[str, Any]:
    return {
        '_task_type': task_type,
        '_task_scope': task_scope,
        '_base_row': dict(base_row),
        '_trigger_label': trigger_label,
        'last_run_trigger': trigger_label,
        'last_run_label': '排队中',
        'last_run_summary': '后台任务排队中',
    }

def _scheduled_stage_label(row: dict[str, Any]) -> tuple[str, str]:
    result = str(row.get('latest_result') or '')
    if row.get('schedule_mode') == 'once' and _is_scheduled_once_complete_result(result):
        mapping = {
            'started': ('已发起开机', 'ok'),
            'power_on_submitted': ('已提交开机请求', 'ok'),
            'already_running': ('已抢到机器', 'ok'),
        }
        return mapping[result]
    if not row.get('enabled', True):
        return '已暂停', 'warn'
    if result == 'outside_window':
        phase_label, phase_tone, _ = _scheduled_window_phase(row)
        return phase_label, phase_tone
    mapping = {
        'started': ('已发起开机', 'ok'),
        'power_on_submitted': ('已提交开机请求', 'ok'),
        'already_running': ('已抢到机器', 'ok'),
        'waiting_for_gpu': ('等待可开机候选', 'info'),
        'waiting_for_instance': ('等待候选出现', 'info'),
        'selector_no_match': ('等待候选出现', 'info'),
        'deadline_failed': ('已超时', 'bad'),
        'instance_missing': ('规则失效', 'bad'),
        'started_without_gpu': ('已开机但未进入 GPU', 'warn'),
    }
    if result in mapping:
        return mapping[result]
    if result:
        return _scheduled_result_label(result), 'info'
    phase = _scheduled_window_phase(row)
    return phase[0], phase[1]

def _scheduled_execution_status(row: dict[str, Any]) -> tuple[str, str]:
    result = str(row.get('latest_result') or '')
    if not result:
        return '暂无检查记录', 'muted'
    if str(row.get('schedule_mode') or 'daily') == 'once' and _is_scheduled_once_complete_result(result):
        return '单次已完成', 'ok'
    if result in {'started', 'power_on_submitted', 'already_running'}:
        return '最近检查成功', 'ok'
    if result in {'deadline_failed', 'instance_missing'}:
        return '最近检查失败', 'bad'
    return '最近检查已执行', 'info'

def _scheduled_missing_check_reason(row: dict[str, Any]) -> tuple[str, str]:
    result = str(row.get('latest_result') or '')
    if result:
        return '', 'muted'
    if bool(row.get('has_history')) and not bool(row.get('latest_matches_current_rule')):
        return '等待新规则首次检查', 'info'
    phase_label, _, _ = _scheduled_window_phase(row)
    if phase_label == '等待抢机窗口':
        return '尚未到首次轮询', 'info'
    if bool(row.get('daemon_running')):
        return '轮询未落库', 'warn'
    return '后台未启动', 'warn'

def _scheduled_rule_match_note(row: dict[str, Any]) -> tuple[str, str]:
    if bool(row.get('has_history')) and not bool(row.get('latest_matches_current_rule')):
        return '最近检查来自旧规则', 'warn'
    return '', 'muted'

def _scheduled_next_action(row: dict[str, Any]) -> str:
    result = str(row.get('latest_result') or '')
    if row.get('schedule_mode') == 'once' and _is_scheduled_once_complete_result(result):
        if result == 'already_running':
            return '机器已可用，可以直接使用'
        return '等待实例启动完成，随后直接使用'
    if not row.get('enabled', True):
        return '先恢复任务，再继续轮询'
    if result == 'outside_window':
        return '等待进入下一次抢机窗口'
    if result in {'started', 'power_on_submitted'}:
        return '等待实例启动完成，随后继续刷新'
    if result == 'already_running':
        return '机器已可用，可以直接使用'
    if result in {'waiting_for_gpu', 'no_eligible_candidate'}:
        return '继续轮询候选，等待可开机资源'
    if result == 'selector_no_match':
        return '继续等待命中筛选条件的候选'
    if result == 'waiting_for_instance':
        return '继续等待匹配机器出现'
    if result == 'deadline_failed':
        return '调整目标时间或筛选条件后重试'
    if result == 'instance_missing':
        return '检查实例 ID，或改用筛选条件'
    if result == 'started_without_gpu':
        return '继续观察机器状态，必要时手动检查'
    phase = _scheduled_window_phase(row)
    return phase[2]

def _scheduled_candidate_summary(payload: dict[str, Any], row: dict[str, Any]) -> str:
    details = payload.get('candidate_details')
    if isinstance(details, list) and details:
        selected = next((item for item in details if isinstance(item, dict) and item.get('selected')), None)
        if isinstance(selected, dict):
            selected_id = selected.get('instance_id') or payload.get('selected_instance_id') or row.get('latest_instance_id') or '-'
            selected_status = _normalize_instance_status(selected.get('status') or payload.get('status') or '-')
            return f'已选中 {selected_id} / {selected_status}'
        fragments: list[str] = []
        for item in details[:3]:
            if not isinstance(item, dict):
                continue
            instance_id = item.get('instance_id') or '-'
            status = _normalize_instance_status(item.get('status') or '-')
            reason = _scheduled_reason_label(str(item.get('reason') or '-'))
            fragments.append(f'{instance_id}({status}/{reason})')
        if fragments:
            suffix = f' ...+{len(details) - 3}' if len(details) > 3 else ''
            return f'{len(details)} 个候选；' + '；'.join(fragments) + suffix
    candidate_count = payload.get('candidate_count')
    if isinstance(candidate_count, int) and candidate_count > 0:
        return f'{candidate_count} 个候选，等待更明确结果'
    selector_summary = str(payload.get('selector_summary') or '')
    if selector_summary:
        return f'当前没有候选；规则={selector_summary}'
    return '当前没有候选机器'

def _scheduled_candidate_groups(payload: dict[str, Any]) -> tuple[str, str, str]:
    details = payload.get('candidate_details')
    if not isinstance(details, list) or not details:
        return '-', '-', '-'
    hit: list[str] = []
    waiting: list[str] = []
    dropped: list[str] = []
    waiting_reasons = {'eligible', 'running_with_gpu', 'started', 'power_on_submitted', 'already_running'}
    for item in details:
        if not isinstance(item, dict):
            continue
        instance_id = str(item.get('instance_id') or '-')
        reason = str(item.get('reason') or '')
        if item.get('selected'):
            hit.append(instance_id)
        elif reason in waiting_reasons:
            waiting.append(instance_id)
        else:
            dropped.append(instance_id)
    def fmt(items: list[str]) -> str:
        if not items:
            return '-'
        return ' / '.join(items[:3]) + (f' ...+{len(items) - 3}' if len(items) > 3 else '')
    return fmt(hit), fmt(waiting), fmt(dropped)

def _scheduled_window_phase(row: dict[str, Any]) -> tuple[str, str, str]:
    target_time = str(row.get('target_time') or '').strip()
    if not target_time or ':' not in target_time:
        return '等待首次检查', 'info', '等待调度器开始轮询'
    try:
        hour, minute = [int(part) for part in target_time.split(':', 1)]
    except ValueError:
        return '等待首次检查', 'info', '等待调度器开始轮询'
    timezone_name = str(row.get('timezone') or 'Asia/Shanghai')
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        zone = ZoneInfo('Asia/Shanghai')
    now = datetime.now(zone)
    target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_dt <= now:
        target_dt += timedelta(days=1)
    start_dt = target_dt - timedelta(hours=int(row.get('advance_hours') or 0))
    if now < start_dt:
        return '等待抢机窗口', 'info', f'约 {_format_relative_deadline(start_dt.isoformat())} 后开始轮询'
    if now < target_dt:
        return '正在轮询候选', 'ok', f'继续轮询，距离目标时间还有 {_format_relative_deadline(target_dt.isoformat())}'
    return '等待下一轮窗口', 'info', '当前窗口已结束，等待下一次抢机时间'

def _scheduled_window_countdowns(row: dict[str, Any]) -> tuple[str, str]:
    target_time = str(row.get('target_time') or '').strip()
    if not target_time or ':' not in target_time:
        return '待计算', '待计算'
    try:
        hour, minute = [int(part) for part in target_time.split(':', 1)]
    except ValueError:
        return '待计算', '待计算'
    timezone_name = str(row.get('timezone') or 'Asia/Shanghai')
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        zone = ZoneInfo('Asia/Shanghai')
    now = datetime.now(zone)
    target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_dt <= now:
        target_dt += timedelta(days=1)
    start_dt = target_dt - timedelta(hours=int(row.get('advance_hours') or 0))
    start_text = '已经开始轮询' if now >= start_dt else _format_relative_deadline(start_dt.isoformat())
    target_text = _format_relative_deadline(target_dt.isoformat())
    return start_text, target_text

def _scheduled_latest_check_text(row: dict[str, Any]) -> str:
    created_at = str(row.get('latest_created_at') or '')
    if created_at:
        return _format_human_datetime(created_at)
    if bool(row.get('has_history')) and not bool(row.get('latest_matches_current_rule')):
        return '待同步最近检查记录'
    return '待首次检查'

def _scheduled_latest_matching_check_text(row: dict[str, Any]) -> str:
    created_at = str(row.get('latest_matching_created_at') or '')
    if created_at:
        return _format_human_datetime(created_at)
    if bool(row.get('has_history')):
        return '待当前规则首次检查'
    return '待首次检查'

def _scheduled_latest_result_text(row: dict[str, Any]) -> str:
    result = str(row.get('latest_result') or '')
    if result:
        return _scheduled_result_label(result)
    if bool(row.get('has_history')) and not bool(row.get('latest_matches_current_rule')):
        return '最近检查来自旧规则'
    return '待首次检查'

def _scheduled_field_fallback(value: str | None, *, empty_text: str) -> str:
    rendered = str(value or '').strip()
    return rendered or empty_text

def _scheduled_candidate_group_text(value: str) -> str:
    rendered = str(value or '').strip()
    if not rendered or rendered == '-':
        return '暂无'
    return rendered


__all__ = [
    "_serialize_priority",
    "_parse_priority",
    "_job_to_payload",
    "_job_target_summary",
    "_scheduled_result_label",
    "_scheduled_reason_label",
    "_scheduled_summary_replacements",
    "_sanitize_scheduled_summary",
    "_keeper_result_label",
    "_keeper_reason_label",
    "_ensure_jobs_payload",
    "_persist_job_changes",
    "_persist_keeper_changes",
    "_normalize_charge_type",
    "_normalize_instance_status",
    "_normalize_start_mode",
    "_scheduled_picker_item_label",
    "_scheduled_runtime_status_label",
    "_scheduled_seed_status_rows",
    "_account_has_enabled_scheduled_jobs",
    "_scheduled_run_result_summary",
    "_scheduled_runtime_status_fields",
    "_scheduled_result_payload",
    "_scheduled_run_result_state",
    "_scheduled_run_pending_state",
    "_scheduled_stage_label",
    "_scheduled_execution_status",
    "_scheduled_missing_check_reason",
    "_scheduled_rule_match_note",
    "_scheduled_next_action",
    "_scheduled_candidate_summary",
    "_scheduled_candidate_groups",
    "_scheduled_window_phase",
    "_scheduled_window_countdowns",
    "_scheduled_latest_check_text",
    "_scheduled_latest_matching_check_text",
    "_scheduled_latest_result_text",
    "_scheduled_field_fallback",
    "_scheduled_candidate_group_text",
]
