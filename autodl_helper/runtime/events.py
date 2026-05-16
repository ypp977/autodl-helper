from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from autodl_helper.tasks.keeper_results import keeper_reason_label

if TYPE_CHECKING:
    from ..storage.models import KeeperResult, ScheduledStartResult


KEEPER_EVENT_TYPES = {
    'skip_not_due': 'keeper.not_due',
    'skip_recently_started': 'keeper.cooldown.started',
    'skip_recently_stopped': 'keeper.cooldown.stopped',
    'skip_missing_shutdown_time': 'keeper.skip.missing_shutdown_time',
    'skip_missing_instance_id': 'keeper.skip.missing_instance_id',
    'skip_already_executed_in_cycle': 'keeper.skip.already_executed',
    'ready': 'keeper.window.reached',
    'keeper_executed': 'keeper.executed',
    'keeper_failed_power_on': 'keeper.failed.power_on',
    'keeper_failed_power_off': 'keeper.failed.power_off',
}

SCHEDULED_EVENT_TYPES = {
    'outside_window': 'scheduled.wait.window',
    'waiting_for_instance': 'scheduled.wait.instance',
    'waiting_for_gpu': 'scheduled.wait.gpu',
    'retrying': 'scheduled.starting',
    'already_running': 'scheduled.already_running',
    'started': 'scheduled.started',
    'started_without_gpu': 'scheduled.started_without_gpu',
    'instance_missing': 'scheduled.failed.instance_missing',
    'deadline_failed': 'scheduled.failed.deadline_missed',
}


KEEPER_SEVERITY = {
    'skip_not_due': 'info',
    'skip_recently_started': 'info',
    'skip_recently_stopped': 'info',
    'skip_missing_shutdown_time': 'warning',
    'skip_missing_instance_id': 'warning',
    'skip_already_executed_in_cycle': 'info',
    'ready': 'info',
    'keeper_executed': 'success',
    'keeper_failed_power_on': 'error',
    'keeper_failed_power_off': 'error',
}

SCHEDULED_SEVERITY = {
    'outside_window': 'info',
    'waiting_for_instance': 'info',
    'waiting_for_gpu': 'info',
    'retrying': 'warning',
    'already_running': 'success',
    'started': 'success',
    'started_without_gpu': 'warning',
    'instance_missing': 'error',
    'deadline_failed': 'error',
}


def _format_keeper_summary(result: 'KeeperResult') -> str:
    status = result.status or '-'
    deadline = result.release_deadline or '-'
    next_keeper = result.next_keeper_time or '-'
    if result.result == 'keeper_executed':
        return f'已执行 keeper；状态={status}；释放时间={deadline}；下次keeper={next_keeper}'
    if result.result == 'skip_already_executed_in_cycle':
        return f'当前释放周期已执行；释放时间={deadline}'
    if result.result in {'keeper_failed_power_on', 'keeper_failed_power_off'}:
        extra = []
        extra.append(f'原因={keeper_reason_label(result.reason)}')
        if result.response_code or result.response_msg:
            extra.append(f'接口返回={result.response_code or "-"}:{result.response_msg or "-"}')
        suffix = f'；{"；".join(extra)}'
        return f'keeper 执行失败；状态={status}；释放时间={deadline}{suffix}'
    if result.result == 'skip_not_due':
        return f'未到 keeper 窗口；下次keeper={next_keeper}；释放时间={deadline}'
    if result.result == 'skip_recently_started':
        return f'启动冷却中；最近启动时间={result.started_at or "-"}'
    if result.result == 'skip_recently_stopped':
        return f'关机冷却中；最近关机时间={result.stopped_at or "-"}'
    if result.result == 'skip_missing_shutdown_time':
        return '缺少关机时间，无法计算 keeper 窗口'
    if result.result == 'skip_missing_instance_id':
        return '实例缺少 uuid，已跳过'
    return f'keeper 状态={result.result}；释放时间={deadline}'


def _format_scheduled_summary(result: 'ScheduledStartResult') -> str:
    deadline = result.deadline or '-'
    target_time = result.target_time or '-'
    selected = result.selected_instance_id or result.instance_id or '-'
    candidate_brief = ''
    if getattr(result, 'candidate_details', None):
        parts = []
        for item in result.candidate_details[:3]:
            marker = '*' if item.selected else ''
            parts.append(f"{marker}{item.instance_id}({item.reason})")
        candidate_brief = f"；候选={','.join(parts)}"
        if len(result.candidate_details) > 3:
            candidate_brief += f"...+{len(result.candidate_details) - 3}"
    if result.result == 'started':
        return f'已发起 GPU 开机；实例={selected}；目标时间={target_time}；deadline={deadline}{candidate_brief}'
    if result.result == 'already_running':
        return f'实例已在 GPU 运行；实例={selected}；目标时间={target_time}{candidate_brief}'
    if result.result == 'outside_window':
        return f'未到抢机窗口；目标时间={target_time}；deadline={deadline}'
    if result.result == 'waiting_for_instance':
        return f'等待候选实例出现；目标时间={target_time}；deadline={deadline}'
    if result.result == 'waiting_for_gpu':
        return f'候选存在但暂不可抢；候选数={result.candidate_count}；目标时间={target_time}{candidate_brief}'
    if result.result == 'started_without_gpu':
        return f'平台启动后未进入 GPU 模式；实例={selected}{candidate_brief}'
    if result.result == 'instance_missing':
        return f'目标实例不存在；目标时间={target_time}；deadline={deadline}'
    if result.result == 'deadline_failed':
        return f'已超过 deadline 仍未成功；目标时间={target_time}；deadline={deadline}{candidate_brief}'
    if result.result == 'retrying':
        return f'开机已提交，等待下轮确认；实例={selected}{candidate_brief}'
    return f'scheduled-start 状态={result.result}；目标时间={target_time}'


def enrich_keeper_result(result: 'KeeperResult') -> 'KeeperResult':
    event_type = KEEPER_EVENT_TYPES.get(result.result, 'keeper.unknown')
    severity = KEEPER_SEVERITY.get(result.result, 'info')
    summary = _format_keeper_summary(result)
    return replace(result, event_type=event_type, severity=severity, summary=summary)


def enrich_scheduled_result(result: 'ScheduledStartResult') -> 'ScheduledStartResult':
    event_type = SCHEDULED_EVENT_TYPES.get(result.result, 'scheduled.unknown')
    if result.result == 'deadline_failed' and result.reason == 'selector_no_match':
        event_type = 'scheduled.failed.selector_no_match'
    severity = SCHEDULED_SEVERITY.get(result.result, 'info')
    summary = _format_scheduled_summary(result)
    return replace(result, event_type=event_type, severity=severity, summary=summary)
