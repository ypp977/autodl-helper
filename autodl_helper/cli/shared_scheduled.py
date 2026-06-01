from __future__ import annotations

import logging
from datetime import datetime, timedelta

from autodl_helper.tasks.scheduled_results import scheduled_reason_label, scheduled_result_label

logger = logging.getLogger(__name__)


def _scheduled_start_reason_label(reason: str) -> str:
    return scheduled_reason_label(reason)


def _format_scheduled_window(*, target_time: str, advance_hours: int, now: datetime) -> str:
    try:
        hh, mm = map(int, target_time.split(':'))
        target_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        window_start = target_dt - timedelta(hours=max(0, advance_hours))
        return f'{window_start.strftime("%H:%M")}-{target_dt.strftime("%H:%M")}'
    except Exception:
        return '-'


def _format_next_check(*, now: datetime, poll_interval_seconds: int) -> str:
    try:
        return (now + timedelta(seconds=max(1, poll_interval_seconds))).strftime('%H:%M:%S')
    except Exception:
        return '-'


def _format_local_time_label(value: str) -> str:
    raw = (value or '').strip()
    if not raw:
        return '-'
    try:
        parsed = datetime.fromisoformat(raw.replace(' ', 'T'))
    except ValueError:
        return raw
    return parsed.strftime('%m-%d %H:%M:%S')


def _format_keeper_window(*, next_keeper_time: str, release_deadline: str) -> str:
    start = _format_local_time_label(next_keeper_time)
    end = _format_local_time_label(release_deadline)
    if start == '-' and end == '-':
        return '-'
    return f'{start} ~ {end}'


def _format_schedule_label(schedule_mode: str, weekdays: list[int] | None = None) -> str:
    if schedule_mode == 'once':
        return '单次'
    if schedule_mode == 'weekly':
        labels = {1: '周一', 2: '周二', 3: '周三', 4: '周四', 5: '周五', 6: '周六', 7: '周日'}
        days = ','.join(labels.get(int(day), str(day)) for day in sorted(set(weekdays or [])))
        return f'每周{days}' if days else '每周'
    return '每天'


def _log_scheduled_start_summary(
    *,
    account_name: str,
    job_name: str,
    target_time: str,
    advance_hours: int,
    schedule_mode: str,
    weekdays: list[int] | None = None,
    poll_interval_seconds: int,
    status: str,
    reason: str,
    now: datetime,
    instance_id: str = '',
    candidate_count: int | None = None,
) -> None:
    status_label = scheduled_result_label(status if status != 'skip' else 'outside_window')
    fields = [
        f'账号={account_name}',
        f'任务={job_name}',
        f'目标={target_time}',
        f'计划={_format_schedule_label(schedule_mode, weekdays)}',
        f'间隔={poll_interval_seconds}秒',
        f'当前窗口={_format_scheduled_window(target_time=target_time, advance_hours=advance_hours, now=now)}',
        f'下次检查={_format_next_check(now=now, poll_interval_seconds=poll_interval_seconds)}',
        f'结果={status_label}',
        f'原因={_scheduled_start_reason_label(reason)}',
    ]
    if instance_id:
        fields.append(f'实例={instance_id}')
    if candidate_count is not None:
        fields.append(f'候选数={candidate_count}')
    logger.info('[抢机检查] %s', ' '.join(fields))


__all__ = [
    "_scheduled_start_reason_label",
    "_format_scheduled_window",
    "_format_next_check",
    "_format_local_time_label",
    "_format_keeper_window",
    "_log_scheduled_start_summary",
]
