from __future__ import annotations

from autodl_helper.config import Settings
from autodl_helper.runtime_control import scheduled_job_identity

from ...account_common import _account_display_name
from ...history_instance import (
    _history_brief_line,
    _history_record_subject,
    _history_record_summary,
    _is_failure_record,
    _is_success_record,
)
from ...presentation import CYAN, _boxed_lines, _format_human_datetime, _heading, _humanize_datetime_text, _key_value, _section, _separator, _tone_chip
from ...scheduled import _keeper_reason_label, _keeper_result_label, _scheduled_reason_label, _scheduled_result_label

__all__ = [
    '_render_history_record_detail',
    '_render_records_overview',
    '_render_config_summary',
]


def _render_history_record_detail(row) -> str:
    lines = [
        _heading('记录详情', color=CYAN),
        _separator(),
        _key_value('时间', _format_human_datetime(row.created_at)),
        _key_value('账号', row.account_name),
        _key_value('任务', row.task_type),
        _key_value('事件', row.event_type or '-'),
        _key_value('级别', row.severity or '-'),
        _key_value('对象', _history_record_subject(row)),
        _key_value(
            '结果',
            row.result
            if row.task_type == 'service'
            else (_keeper_result_label(row.result) if row.task_type == 'keeper' else _scheduled_result_label(row.result)),
        ),
        _key_value(
            '原因',
            (row.reason or '-')
            if row.task_type == 'service'
            else (_keeper_reason_label(row.reason) if row.task_type == 'keeper' else _scheduled_reason_label(row.reason)),
        ),
        '',
        _section('[摘要]'),
        _history_record_summary(row),
    ]
    return '\n'.join(lines)


def _render_records_overview(settings: Settings, store, *, current_account: str | None) -> str:
    account_label = _account_display_name(settings, current_account)
    rows = store.read_history(account_name=current_account, limit=30)
    recent_success = next((row for row in rows if _is_success_record(row)), None)
    recent_failure = next((row for row in rows if _is_failure_record(row)), None)
    auth_summaries = store.summarize_auth_failures(account_name=current_account, limit=3)

    success_lines = [
        _key_value('账号范围', account_label),
        _key_value('最近一条', _history_brief_line(recent_success) if recent_success else '暂无成功记录'),
    ]
    if recent_success is not None:
        success_lines.append(_key_value('结果', _scheduled_result_label(recent_success.result) if recent_success.task_type == 'scheduled_start' else _keeper_result_label(recent_success.result)))

    failure_lines = [
        _key_value('账号范围', account_label),
        _key_value('最近一条', _history_brief_line(recent_failure) if recent_failure else '暂无失败记录'),
    ]
    if recent_failure is not None:
        failure_lines.append(_key_value('原因', _scheduled_reason_label(recent_failure.reason) if recent_failure.task_type == 'scheduled_start' else _keeper_reason_label(recent_failure.reason)))

    anomaly_lines = [_key_value('账号范围', account_label)]
    if auth_summaries:
        top = auth_summaries[0]
        message = top.msg or top.code or '未知异常'
        if len(message) > 40:
            message = message[:37] + '...'
        anomaly_lines.extend([
            _key_value('最近一条', message),
            _key_value('出现次数', top.count),
        ])
    else:
        anomaly_lines.append(_key_value('最近一条', '暂无认证异常'))

    blocks = [
        _heading('运行记录', color=CYAN),
        _separator(),
        '',
        *_boxed_lines('最近成功', success_lines, tone='ok'),
        '',
        *_boxed_lines('最近失败', failure_lines, tone='bad'),
        '',
        *_boxed_lines('最近异常', anomaly_lines, tone='warn'),
    ]
    return '\n'.join(blocks)


def _render_config_summary(settings: Settings, *, current_account: str | None) -> str:
    current_label = _account_display_name(settings, current_account)
    keeper_days, keeper_hours = divmod(int(settings.tasks.keeper.shutdown_release_after_hours), 24)
    keeper_limit = f'{keeper_days}天' if keeper_days and not keeper_hours else (f'{keeper_days}天 {keeper_hours}小时' if keeper_days else f'{keeper_hours}小时')
    lines = [
        _heading('配置概览'),
        _separator(),
        '',
        _key_value('当前账号', current_label),
        '',
        _section('[Keeper]'),
        _key_value('状态', _tone_chip('运行中', 'ok') if settings.tasks.keeper.enabled else _tone_chip('已暂停', 'warn')),
        _key_value('最长保留时间', keeper_limit),
        '',
        _section('[抢机器任务]'),
        _key_value('状态', _tone_chip('运行中', 'ok') if settings.tasks.scheduled_start.enabled else _tone_chip('已暂停', 'warn')),
        _key_value('任务数量', len(settings.tasks.scheduled_start.jobs)),
    ]
    for job in settings.tasks.scheduled_start.jobs:
        target = job.instance_id or (
            f"GPU={job.selector.gpu_model} x{job.selector.gpu_count}" if job.selector else '-'
        )
        lines.append(f"  • {_tone_chip('运行中' if settings.tasks.scheduled_start.enabled else '已暂停', 'ok' if settings.tasks.scheduled_start.enabled else 'warn')} {scheduled_job_identity(job)} / {job.target_time} / 提前{job.advance_hours}h / {target}")
    return '\n'.join(lines)
