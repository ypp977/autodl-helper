from __future__ import annotations

from typing import Any

from autodl_helper.auth import inspect_auth_state
from autodl_helper.config import Settings
from autodl_helper.runtime_control import get_task_enabled

from ...account_common import _mask_phone
from ...presentation import CYAN, _heading, _key_value, _section, _separator, _tone_chip

__all__ = [
    '_render_accounts_summary',
    '_render_account_detail',
]


def _render_accounts_summary(settings: Settings, store, *, current_account: str | None) -> str:
    lines = [_heading('账号状态', color=CYAN), _separator(), '']
    if not settings.accounts:
        return '账号状态\n\n未配置多账号。'
    for account in settings.accounts:
        state = inspect_auth_state(account.to_auth_settings(), store=store, account_name=account.name)
        marker = '当前' if account.name == current_account else '   '
        lines.append(
            f"[{marker}] {account.name} / {_mask_phone(account.autodl_phone)} "
            f"启用={'是' if account.enabled else '否'} "
            f"状态={state['status']} 来源={state['auth_source']}"
        )
    return '\n'.join(lines)


def _render_account_detail(
    settings: Settings,
    store,
    *,
    account_name: str,
    keeper_probe_rows_fn,
    scheduled_job_status_rows_fn,
    snapshot: dict[str, Any] | None = None,
    page_status_lines: list[str] | None = None,
) -> str:
    del keeper_probe_rows_fn, scheduled_job_status_rows_fn
    account = next((item for item in settings.accounts if item.name == account_name), None)
    if account is None:
        return f'账号详情\n\n账号不存在: {account_name}'
    runtime_snapshot = snapshot or {
        'account_name': account.name,
        'account_enabled': bool(account.enabled),
        'auth_status': '首次加载中',
        'auth_source': '-',
        'running_instances': 0,
        'expiring_soon': 0,
        'scheduled_jobs': 0,
        'paused_jobs': 0,
        'keeper_enabled': get_task_enabled(store, account.name, 'keeper', default_enabled=settings.tasks.keeper.enabled),
    }
    lines = [
        _heading(f'账号详情: {account.name}', color=CYAN),
        _separator(),
    ]
    if page_status_lines:
        lines.extend(page_status_lines)
    lines.extend([
        _section('[账号状态]'),
        _key_value('账号名', account.name),
        _key_value('登录状态', runtime_snapshot.get('auth_status') or '首次加载中'),
        _key_value('认证来源', runtime_snapshot.get('auth_source') or '-'),
        _key_value('是否启用', _tone_chip('启用', 'ok') if runtime_snapshot.get('account_enabled', True) else _tone_chip('停用', 'warn')),
        '',
        _section('[Helper 关注的数据]'),
        _key_value('运行中实例', runtime_snapshot['running_instances']),
        _key_value('一周内到期', runtime_snapshot['expiring_soon']),
        _key_value('Keeper', _tone_chip('启用', 'ok') if runtime_snapshot['keeper_enabled'] else _tone_chip('暂停', 'warn')),
        _key_value('抢机器任务', runtime_snapshot['scheduled_jobs']),
        _key_value('已暂停任务', runtime_snapshot['paused_jobs']),
    ])
    return '\n'.join(lines)
