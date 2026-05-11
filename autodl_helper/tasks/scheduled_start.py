from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from autodl_helper.core.config import ScheduledStartPriority, ScheduledStartSelector
from autodl_helper.events import enrich_scheduled_result
from autodl_helper.core.models import ScheduledStartCandidateDetail, ScheduledStartResult
from autodl_helper.state import StateStore

logger = logging.getLogger(__name__)
_GPU_COUNT_RE = re.compile(r'[*×x]\s*(\d+)\s*卡', re.IGNORECASE)


@dataclass
class ScheduledStartJobRuntime:
    job_name: str
    instance_id: str = ""
    target_time: str = "14:00"
    advance_hours: int = 2
    schedule_mode: str = "daily"
    weekdays: list[int] = field(default_factory=list)
    timezone: str = "Asia/Shanghai"
    poll_interval_seconds: int = 300
    selector: ScheduledStartSelector | None = None
    priority: list[ScheduledStartPriority] = field(default_factory=list)

    def window_key(self, now: datetime) -> str:
        if self.schedule_mode == 'weekly':
            iso = now.isocalendar()
            return f'{iso.year}-W{iso.week:02d}-{now.isoweekday()}'
        return now.date().isoformat()

    def target_datetime(self, now: datetime) -> datetime:
        tz = ZoneInfo(self.timezone)
        hh, mm = map(int, self.target_time.split(':'))
        return datetime.combine(now.date(), time(hh, mm), tzinfo=tz)

    def scheduled_today(self, now: datetime) -> bool:
        if self.schedule_mode != 'weekly':
            return True
        if not self.weekdays:
            return True
        return now.isoweekday() in set(self.weekdays)

    def selector_summary(self) -> str:
        if self.selector is None:
            return ""
        parts = []
        if self.selector.regions:
            parts.append(f"regions={','.join(self.selector.regions)}")
        if self.selector.gpu_model:
            parts.append(f"gpu_model={self.selector.gpu_model}")
        if self.selector.gpu_count:
            parts.append(f"gpu_count={self.selector.gpu_count}")
        if self.selector.charge_types:
            parts.append(f"charge_types={','.join(self.selector.charge_types)}")
        return '; '.join(parts)


def _status(instance: dict) -> str:
    return (instance.get('status') or '').lower()


def _instance_label(instance: dict, fallback: str) -> str:
    return f"{instance.get('region_name', '')} {instance.get('machine_alias', '')}".strip() or fallback


def _parse_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def _gpu_idle_num(instance: dict) -> int | None:
    return _parse_int(instance.get('gpu_idle_num'))


def _gpu_count(instance: dict) -> int | None:
    gpu_all_num = _parse_int(instance.get('gpu_all_num'))
    if gpu_all_num is not None:
        return gpu_all_num
    for source in (instance.get('machine_alias', ''), instance.get('spec', '')):
        match = _GPU_COUNT_RE.search(str(source))
        if match:
            return int(match.group(1))
    return None


def _is_shutdown(instance: dict) -> bool:
    return _status(instance) in {'shutdown', 'stopped', 'off'}


def _supports_gpu_start(instance: dict) -> bool:
    gpu_idle = _gpu_idle_num(instance)
    return gpu_idle is not None and gpu_idle > 0


def _is_running_with_gpu(instance: dict) -> bool:
    return _status(instance) in {'running', 'on'} and (instance.get('start_mode') or '').lower() == 'gpu'


def _matches_priority(instance: dict, rule: ScheduledStartPriority) -> bool:
    if rule.instance_id and instance.get('uuid') != rule.instance_id:
        return False
    if rule.region and instance.get('region_name') != rule.region:
        return False
    if rule.machine_alias and instance.get('machine_alias') != rule.machine_alias:
        return False
    return True


def _priority_sort_key(instance: dict, priority: list[ScheduledStartPriority]) -> tuple[object, ...]:
    priority_index = len(priority)
    for index, rule in enumerate(priority):
        if _matches_priority(instance, rule):
            priority_index = index
            break
    return (
        priority_index,
        str(instance.get('region_name', '')),
        str(instance.get('machine_alias', '')),
        str(instance.get('uuid', '')),
    )


def _priority_match(instance: dict, priority: list[ScheduledStartPriority]) -> tuple[int | None, str]:
    for index, rule in enumerate(priority, start=1):
        if _matches_priority(instance, rule):
            parts = []
            if rule.instance_id:
                parts.append(f'instance_id={rule.instance_id}')
            if rule.region:
                parts.append(f'region={rule.region}')
            if rule.machine_alias:
                parts.append(f'machine_alias={rule.machine_alias}')
            return index, ', '.join(parts) or '-'
    return None, ''


def _matches_selector(instance: dict, selector: ScheduledStartSelector) -> bool:
    if selector.regions and instance.get('region_name') not in selector.regions:
        return False
    if selector.charge_types and instance.get('charge_type') not in selector.charge_types:
        return False
    if selector.gpu_model:
        haystack = ' '.join(str(instance.get(field, '')) for field in ('machine_alias', 'spec')).casefold()
        if selector.gpu_model.casefold() not in haystack:
            return False
    if selector.gpu_count:
        candidate_gpu_count = _gpu_count(instance)
        if candidate_gpu_count != selector.gpu_count:
            return False
    return True


def _find_instance(instances: list[dict], instance_id: str):
    for item in instances:
        if item.get('uuid') == instance_id:
            return item
    return None


def _build_result(
    *,
    job: ScheduledStartJobRuntime,
    result: str,
    reason: str,
    now: datetime,
    instance: dict | None = None,
    candidate_count: int = 0,
    candidate_details: list[ScheduledStartCandidateDetail] | None = None,
    selected_instance_id: str = "",
    selected_instance_label: str = "",
) -> ScheduledStartResult:
    status = _status(instance or {})
    start_mode = "" if instance is None else str(instance.get('start_mode') or '')
    instance_id = selected_instance_id or (str(instance.get('uuid')) if instance else job.instance_id)
    return enrich_scheduled_result(
        ScheduledStartResult(
            result=result,
            reason=reason,
            instance_id=instance_id or job.instance_id,
            status=status,
            gpu_idle_num=_gpu_idle_num(instance or {}),
            start_mode=start_mode,
            target_time=job.target_time,
            deadline=job.target_datetime(now).isoformat(),
            selector_summary=job.selector_summary(),
            candidate_count=candidate_count,
            candidate_details=candidate_details or [],
            selected_instance_id=selected_instance_id or instance_id or "",
            selected_instance_label=selected_instance_label,
        )
    )


def _debug_log(result: ScheduledStartResult) -> None:
    logger.info(
        'scheduled_start instance_id=%s status=%s gpu_idle_num=%s start_mode=%s result=%s reason=%s candidate_count=%s selector=%s selected_instance=%s candidate_details=%s',
        result.instance_id,
        result.status,
        '' if result.gpu_idle_num is None else result.gpu_idle_num,
        result.start_mode,
        result.result,
        result.reason,
        result.candidate_count,
        result.selector_summary,
        result.selected_instance_id,
        _candidate_details_digest(result.candidate_details),
    )


def _notify_once(*, notifier, state_store: StateStore, job_name: str, result: str, key: str, title: str, message: str) -> None:
    if state_store.was_notified(job_name, result, key):
        return
    notifier.notify_task_result(task_type='scheduled_start', title=title, message=message)
    state_store.mark_notified(job_name, result, key)


def _format_notification_message(result: ScheduledStartResult) -> str:
    lines = [
        f"instance_id: {result.instance_id}",
        f"status: {result.status}",
        f"gpu_idle_num: {'' if result.gpu_idle_num is None else result.gpu_idle_num}",
        f"start_mode: {result.start_mode}",
        f"target_time: {result.target_time}",
        f"deadline: {result.deadline}",
        f"result: {result.result}",
        f"reason: {result.reason}",
    ]
    if result.selector_summary:
        lines.append(f"selector: {result.selector_summary}")
        lines.append(f"candidate_count: {result.candidate_count}")
        if result.selected_instance_id:
            lines.append(f"selected_instance_id: {result.selected_instance_id}")
        if result.selected_instance_label:
            lines.append(f"selected_instance_label: {result.selected_instance_label}")
    if result.candidate_details:
        lines.append('candidate_details:')
        for index, item in enumerate(result.candidate_details, start=1):
            selected = 'yes' if item.selected else 'no'
            gpu_idle = '' if item.gpu_idle_num is None else item.gpu_idle_num
            lines.append(
                f"  {index}. instance_id={item.instance_id} label={item.label} status={item.status} "
                f"start_mode={item.start_mode} gpu_idle_num={gpu_idle} reason={item.reason_label} "
                f"reason_code={item.reason} selected={selected}"
            )
    return '\n'.join(lines)


def _candidate_reason_label(reason: str) -> str:
    return {
        'running_with_gpu': '实例已在 GPU 模式运行',
        'eligible': '可尝试开机',
        'gpu_idle_zero': 'GPU 空闲数为 0',
        'missing_gpu_idle_num': '缺少 gpu_idle_num',
        'running_without_gpu': '实例已运行但不是 GPU 模式',
        'not_shutdown': '实例当前不处于关机状态',
    }.get(reason, reason or '-')


def _candidate_reason(instance: dict) -> str:
    status = _status(instance)
    if _is_running_with_gpu(instance):
        return 'running_with_gpu'
    if status in {'running', 'on'} and not _is_running_with_gpu(instance):
        return 'running_without_gpu'
    if not _is_shutdown(instance):
        return 'not_shutdown'
    gpu_idle = _gpu_idle_num(instance)
    if gpu_idle is None:
        return 'missing_gpu_idle_num'
    if gpu_idle <= 0:
        return 'gpu_idle_zero'
    return 'eligible'


def _build_candidate_details(
    matched: list[dict],
    *,
    job_name: str,
    priority: list[ScheduledStartPriority],
    selected_instance_id: str = '',
) -> list[ScheduledStartCandidateDetail]:
    details: list[ScheduledStartCandidateDetail] = []
    for item in matched:
        reason = _candidate_reason(item)
        matched_priority_index, matched_priority_rule = _priority_match(item, priority)
        details.append(
            ScheduledStartCandidateDetail(
                instance_id=str(item.get('uuid', '')),
                label=_instance_label(item, job_name),
                status=_status(item),
                start_mode=str(item.get('start_mode') or ''),
                gpu_idle_num=_gpu_idle_num(item),
                reason=reason,
                reason_label=_candidate_reason_label(reason),
                region_name=str(item.get('region_name', '')),
                machine_alias=str(item.get('machine_alias', '')),
                matched_priority_index=matched_priority_index,
                matched_priority_rule=matched_priority_rule,
                selected=bool(selected_instance_id) and str(item.get('uuid', '')) == selected_instance_id,
            )
        )
    return details


def _candidate_details_digest(candidate_details: list[ScheduledStartCandidateDetail]) -> str:
    if not candidate_details:
        return '-'
    parts = []
    for item in candidate_details[:3]:
        marker = '*' if item.selected else ''
        parts.append(f"{marker}{item.instance_id}:{item.reason}")
    if len(candidate_details) > 3:
        parts.append(f"...+{len(candidate_details) - 3}")
    return ','.join(parts)


def _select_candidate(job: ScheduledStartJobRuntime, instances: list[dict]) -> tuple[list[dict], dict | None]:
    if job.selector is not None:
        matched = [item for item in instances if _matches_selector(item, job.selector)]
    elif job.instance_id:
        matched = [item for item in instances if item.get('uuid') == job.instance_id]
    else:
        matched = []

    matched = sorted(matched, key=lambda item: _priority_sort_key(item, job.priority))

    running_candidates = [item for item in matched if _is_running_with_gpu(item)]
    if running_candidates:
        return matched, running_candidates[0]

    eligible = [item for item in matched if _candidate_reason(item) == 'eligible']
    if eligible:
        return matched, eligible[0]
    return matched, None


def run_scheduled_start_job(
    *,
    client,
    notifier,
    state_store: StateStore,
    job: ScheduledStartJobRuntime,
    now: datetime,
    force_run_now: bool = False,
) -> ScheduledStartResult:
    tz = ZoneInfo(job.timezone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    target_dt = job.target_datetime(now)
    window_start = target_dt - timedelta(hours=job.advance_hours)
    key = job.window_key(now)

    if not job.scheduled_today(now) and not force_run_now:
        result = _build_result(job=job, result='outside_window', reason='not_scheduled_today', now=now)
        _debug_log(result)
        return result

    if now < window_start and not force_run_now:
        result = _build_result(job=job, result='outside_window', reason='outside_window', now=now)
        _debug_log(result)
        return result

    instances = client.list_instances()
    matched, selected = _select_candidate(job, instances)

    if not matched:
        result_name = 'deadline_failed' if (now > target_dt and job.selector is not None) else ('instance_missing' if now > target_dt else 'waiting_for_instance')
        result = _build_result(
            job=job,
            result=result_name,
            reason='selector_no_match' if job.selector else 'instance_missing',
            now=now,
            candidate_count=0,
            candidate_details=[],
        )
        if now > target_dt:
            _notify_once(
                notifier=notifier,
                state_store=state_store,
                job_name=job.job_name,
                result='failure',
                key=key,
                title=f'{job.job_name} startup failed',
                message=_format_notification_message(result),
            )
        _debug_log(result)
        return result

    if selected and _is_running_with_gpu(selected):
        selected_id = str(selected.get('uuid', ''))
        result = _build_result(
            job=job,
            result='already_running',
            reason='already_running',
            now=now,
            instance=selected,
            candidate_count=len(matched),
            candidate_details=_build_candidate_details(matched, job_name=job.job_name, priority=job.priority, selected_instance_id=selected_id),
            selected_instance_id=selected_id,
            selected_instance_label=_instance_label(selected, job.job_name),
        )
        _notify_once(
            notifier=notifier,
            state_store=state_store,
            job_name=job.job_name,
            result='success',
            key=key,
            title=f'{job.job_name} already running',
            message=_format_notification_message(result),
        )
        _debug_log(result)
        return result

    if selected is None:
        reasons = {_candidate_reason(item) for item in matched}
        if len(matched) == 1:
            reason = next(iter(reasons))
        else:
            reason = 'no_eligible_candidate'
        result_name = 'deadline_failed' if now > target_dt else 'waiting_for_gpu'
        result = _build_result(
            job=job,
            result=result_name,
            reason='deadline_missed' if now > target_dt else reason,
            now=now,
            instance=matched[0],
            candidate_count=len(matched),
            candidate_details=_build_candidate_details(matched, job_name=job.job_name, priority=job.priority),
        )
        if now > target_dt:
            _notify_once(
                notifier=notifier,
                state_store=state_store,
                job_name=job.job_name,
                result='failure',
                key=key,
                title=f'{job.job_name} startup failed',
                message=_format_notification_message(result),
            )
        _debug_log(result)
        return result

    if now > target_dt:
        selected_id = str(selected.get('uuid', ''))
        result = _build_result(
            job=job,
            result='deadline_failed',
            reason='deadline_missed',
            now=now,
            instance=selected,
            candidate_count=len(matched),
            candidate_details=_build_candidate_details(matched, job_name=job.job_name, priority=job.priority, selected_instance_id=selected_id),
            selected_instance_id=selected_id,
            selected_instance_label=_instance_label(selected, job.job_name),
        )
        _notify_once(
            notifier=notifier,
            state_store=state_store,
            job_name=job.job_name,
            result='failure',
            key=key,
            title=f'{job.job_name} startup failed',
            message=_format_notification_message(result),
        )
        _debug_log(result)
        return result

    selected_id = str(selected.get('uuid', ''))
    selected_label = _instance_label(selected, job.job_name)
    if client.open_machine(selected_id, payload='gpu'):
        refreshed_instances = client.list_instances()
        refreshed = _find_instance(refreshed_instances, selected_id) or selected
        if _is_running_with_gpu(refreshed):
            result = _build_result(
                job=job,
                result='started',
                reason='started',
                now=now,
                instance=refreshed,
                candidate_count=len(matched),
                candidate_details=_build_candidate_details(matched, job_name=job.job_name, priority=job.priority, selected_instance_id=selected_id),
                selected_instance_id=selected_id,
                selected_instance_label=selected_label,
            )
            _notify_once(
                notifier=notifier,
                state_store=state_store,
                job_name=job.job_name,
                result='success',
                key=key,
                title=f'{job.job_name} startup succeeded',
                message=_format_notification_message(result),
            )
            _debug_log(result)
            return result
        if _status(refreshed) in {'running', 'on'}:
            result = _build_result(
                job=job,
                result='started_without_gpu',
                reason='running_without_gpu',
                now=now,
                instance=refreshed,
                candidate_count=len(matched),
                candidate_details=_build_candidate_details(matched, job_name=job.job_name, priority=job.priority, selected_instance_id=selected_id),
                selected_instance_id=selected_id,
                selected_instance_label=selected_label,
            )
            _debug_log(result)
            return result

    result = _build_result(
        job=job,
        result='retrying',
        reason='retrying',
        now=now,
        instance=selected,
        candidate_count=len(matched),
        candidate_details=_build_candidate_details(matched, job_name=job.job_name, priority=job.priority, selected_instance_id=selected_id),
        selected_instance_id=selected_id,
        selected_instance_label=selected_label,
    )
    _debug_log(result)
    return result
