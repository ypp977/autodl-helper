from __future__ import annotations


_RESULT_LABELS = {
    'keeper_executed': '已执行保活',
    'keeper_failed_power_off': '关机失败',
    'keeper_failed_power_on': '开机失败',
    'ready': '可执行',
    'skip_already_executed_in_cycle': '跳过',
    'skip_missing_instance_id': '跳过',
    'skip_missing_shutdown_time': '跳过',
    'skip_not_due': '跳过',
    'skip_recently_started': '跳过',
    'skip_recently_stopped': '跳过',
    'skip_running': '跳过',
}

_REASON_LABELS = {
    'already_executed_in_release_cycle': '该释放周期已执行过保活',
    'auth_failed': '授权失效或接口拒绝登录态',
    'before_next_keeper_time': '未到保活窗口',
    'fallback_status_at_ready': '使用状态时间兜底，已到保活窗口',
    'fallback_status_at_recently_started': '使用状态时间兜底，仍在开机冷却',
    'fallback_status_at_recently_stopped': '使用状态时间兜底，仍在关机冷却',
    'insufficient_balance': '余额不足或账户额度不足',
    'invalid_instance_state': '实例状态不允许当前操作',
    'keeper_window_reached': '已到保活窗口',
    'missing_instance_id': '实例缺少 uuid',
    'missing_shutdown_time': '缺少关机时间，无法计算释放窗口',
    'power_off_api_rejected': '关机接口拒绝',
    'power_off_exception': '关机接口异常',
    'power_off_failed': '关机失败',
    'power_off_timeout': '关机接口超时',
    'power_on_api_rejected': '开机接口拒绝',
    'power_on_exception': '开机接口异常',
    'power_on_failed': '开机失败',
    'power_on_timeout': '开机接口超时',
    'quota_limited': '资源配额或库存限制',
    'started_within_cooldown': '仍在开机冷却',
    'stopped_within_cooldown': '仍在关机冷却',
}

_REASON_CATEGORIES = {
    'auth_failed': 'auth',
    'insufficient_balance': 'billing',
    'invalid_instance_state': 'instance_state',
    'power_off_api_rejected': 'api_rejected',
    'power_off_exception': 'exception',
    'power_off_failed': 'api_rejected',
    'power_off_timeout': 'timeout',
    'power_on_api_rejected': 'api_rejected',
    'power_on_exception': 'exception',
    'power_on_failed': 'api_rejected',
    'power_on_timeout': 'timeout',
    'quota_limited': 'quota',
}


def keeper_result_label(result: str) -> str:
    return _RESULT_LABELS.get(str(result or ''), str(result or '-') or '-')


def keeper_reason_label(reason: str) -> str:
    return _REASON_LABELS.get(str(reason or ''), str(reason or '-') or '-')


def keeper_failure_category(reason: str) -> str:
    return _REASON_CATEGORIES.get(str(reason or ''), 'other')


def normalize_keeper_failure_reason(
    *,
    action: str,
    failure_kind: str,
    response_code: str = '',
    response_msg: str = '',
) -> str:
    action = str(action or '').strip()
    failure_kind = str(failure_kind or '').strip()
    if failure_kind in {'timeout', 'exception'}:
        return f'{action}_{failure_kind}'

    text = f'{response_code} {response_msg}'.lower()
    if any(marker in text for marker in ('unauthorized', 'forbidden', 'login', 'auth', 'token', '未登录', '登录', '授权')):
        return 'auth_failed'
    if any(marker in text for marker in ('balance', 'insufficient', 'arrears', '欠费', '余额', '额度不足')):
        return 'insufficient_balance'
    if any(marker in text for marker in ('quota', 'limit', 'stock', 'sold out', 'no gpu', '库存', '配额', '资源不足')):
        return 'quota_limited'
    if any(marker in text for marker in ('state', 'status', 'running', 'stopped', 'shutdown', 'not allow', '状态', '不允许')):
        return 'invalid_instance_state'
    if action in {'power_on', 'power_off'}:
        return f'{action}_api_rejected'
    return failure_kind or 'unknown_failure'
