from __future__ import annotations

from autodl_helper.tasks.keeper_results import keeper_reason_label, keeper_result_label
from autodl_helper.tasks.scheduled_results import (
    scheduled_candidate_reason_label,
    scheduled_reason_label,
    scheduled_result_label,
)


def test_keeper_result_and_reason_labels_are_shared():
    assert keeper_result_label('keeper_failed_power_on') == '开机失败'
    assert keeper_result_label('skip_not_due') == '跳过'
    assert keeper_reason_label('power_on_timeout') == '开机接口超时'
    assert keeper_result_label('unknown_code') == 'unknown_code'


def test_scheduled_result_and_reason_labels_are_shared():
    assert scheduled_result_label('deadline_failed') == '失败'
    assert scheduled_reason_label('selector_no_match') == '暂无可用目标'
    assert scheduled_reason_label('not_scheduled_today') == '今日不执行'
    assert scheduled_candidate_reason_label('running_without_gpu') == '实例已运行但不是 GPU 模式'
    assert scheduled_result_label('unknown_code') == 'unknown_code'
