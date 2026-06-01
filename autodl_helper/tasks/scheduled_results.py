from __future__ import annotations


_RESULT_LABELS = {
    'already_running': '已在运行',
    'deadline_failed': '失败',
    'error': '错误',
    'failure': '失败',
    'instance_missing': '失败',
    'outside_window': '跳过',
    'power_on_submitted': '已提交开机',
    'retrying': '等待',
    'started': '已开机',
    'started_without_gpu': '等待',
    'success': '已开机',
    'waiting_for_gpu': '等待',
    'waiting_for_instance': '等待',
    'window_already_succeeded': '当前窗口已完成',
}

_REASON_LABELS = {
    'already_running': '实例已在运行',
    'deadline_failed': '已过截止时间',
    'deadline_missed': '已过截止时间',
    'gpu_idle_zero': '候选暂不可抢',
    'instance_missing': '暂无可用目标',
    'no_eligible_candidate': '候选暂不可抢',
    'not_scheduled_today': '今日不执行',
    'outside_window': '未到执行窗口',
    'power_on_submitted': '已提交开机',
    'retrying': '候选暂不可抢',
    'running_without_gpu': '候选暂不可抢',
    'scheduled_disabled': '配置未启用',
    'selector_no_match': '暂无可用目标',
    'started': '实例已在运行',
    'task_paused': '任务已暂停',
    'window_already_succeeded': '当前窗口已完成',
}

_CANDIDATE_REASON_LABELS = {
    'eligible': '可尝试开机',
    'gpu_idle_zero': 'GPU 空闲数为 0',
    'missing_gpu_idle_num': '缺少 gpu_idle_num',
    'not_shutdown': '实例当前不处于关机状态',
    'running_with_gpu': '实例已在 GPU 模式运行',
    'running_without_gpu': '实例已运行但不是 GPU 模式',
}


def scheduled_result_label(result: str) -> str:
    return _RESULT_LABELS.get(str(result or ''), str(result or '-') or '-')


def scheduled_reason_label(reason: str) -> str:
    return _REASON_LABELS.get(str(reason or ''), str(reason or '-') or '-')


def scheduled_candidate_reason_label(reason: str) -> str:
    return _CANDIDATE_REASON_LABELS.get(str(reason or ''), str(reason or '-') or '-')
