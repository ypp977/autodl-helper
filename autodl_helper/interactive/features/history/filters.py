from __future__ import annotations

from types import SimpleNamespace

from autodl_helper.config import Settings

from ...account_common import _account_display_name
from ...dialogs import MenuItem, _choose_menu, _prompt_int_with_default
from ...account_ops import _enabled_account_names
from ...presentation import CYAN, _heading, _key_value, _separator

__all__ = [
    '_choose_account_scope',
    '_history_filter_wizard',
    '_auth_report_filter_wizard',
    '_instances_filter_wizard',
    '_keeper_probe_filter_wizard',
    '_healthcheck_filter_wizard',
]


def _choose_account_scope(settings: Settings, current_account: str | None, *, title: str, allow_all: bool = True) -> str | None:
    items: list[MenuItem] = []
    default_key = '1'
    if allow_all:
        items.append(MenuItem('a', '全部账号'))
        default_key = 'a'
    for index, name in enumerate(_enabled_account_names(settings), start=1):
        items.append(MenuItem(str(index), _account_display_name(settings, name)))
        if name == current_account:
            default_key = str(index)
    items.append(MenuItem('0', '返回'))
    choice = _choose_menu(title, items, default_key=default_key)
    if choice == '0':
        return None
    if choice == 'a':
        return None
    if choice.isdigit():
        names = _enabled_account_names(settings)
        if 1 <= int(choice) <= len(names):
            return names[int(choice) - 1]
    raise ValueError('无效账号选择。')


def _history_filter_wizard(settings: Settings, current_account: str | None) -> SimpleNamespace | None:
    draft = {
        'account': current_account,
        'task': None,
        'limit': 20,
    }
    while True:
        lines = [
            _heading('最近记录筛选向导', color=CYAN),
            _separator(),
            _key_value('账号范围', _account_display_name(settings, draft['account']) if draft['account'] else '全部账号'),
            _key_value('任务类型', draft['task'] or '全部'),
            _key_value('数量限制', draft['limit']),
        ]
        choice = _choose_menu(
            '\n'.join(lines),
            [
                MenuItem('1', '选择账号范围'),
                MenuItem('2', '选择任务类型'),
                MenuItem('3', '修改数量限制'),
                MenuItem('c', '查看记录'),
                MenuItem('0', '取消'),
            ],
            default_key='1',
        )
        if choice == '1':
            draft['account'] = _choose_account_scope(settings, draft['account'], title=_heading('选择账号范围', color=CYAN), allow_all=True)
        elif choice == '2':
            task_choice = _choose_menu(
                _heading('选择任务类型', color=CYAN),
                [MenuItem('a', '全部'), MenuItem('1', 'keeper'), MenuItem('2', 'scheduled_start'), MenuItem('3', 'service'), MenuItem('0', '返回')],
                default_key='a',
            )
            if task_choice == 'a':
                draft['task'] = None
            elif task_choice == '1':
                draft['task'] = 'keeper'
            elif task_choice == '2':
                draft['task'] = 'scheduled_start'
            elif task_choice == '3':
                draft['task'] = 'service'
        elif choice == '3':
            draft['limit'] = _prompt_int_with_default('limit', draft['limit'])
        elif choice == 'c':
            draft['event_type'] = None
            return SimpleNamespace(**draft)
        elif choice == '0':
            return None


def _auth_report_filter_wizard(settings: Settings, current_account: str | None) -> SimpleNamespace | None:
    draft = {
        'account': current_account,
        'limit': 20,
        'only_unmapped': False,
        'only_likely_auth': False,
    }
    while True:
        lines = [
            _heading('认证异常筛选向导', color=CYAN),
            _separator(),
            _key_value('账号范围', _account_display_name(settings, draft['account']) if draft['account'] else '全部账号'),
            _key_value('数量限制', draft['limit']),
            _key_value('仅未覆盖', '是' if draft['only_unmapped'] else '否'),
            _key_value('仅疑似认证错误', '是' if draft['only_likely_auth'] else '否'),
        ]
        choice = _choose_menu(
            '\n'.join(lines),
            [
                MenuItem('1', '选择账号范围'),
                MenuItem('2', '修改数量限制'),
                MenuItem('3', '切换仅未覆盖'),
                MenuItem('4', '切换仅疑似认证错误'),
                MenuItem('c', '查看异常'),
                MenuItem('0', '取消'),
            ],
            default_key='1',
        )
        if choice == '1':
            draft['account'] = _choose_account_scope(settings, draft['account'], title=_heading('选择账号范围', color=CYAN), allow_all=True)
        elif choice == '2':
            draft['limit'] = _prompt_int_with_default('limit', draft['limit'])
        elif choice == '3':
            draft['only_unmapped'] = not draft['only_unmapped']
        elif choice == '4':
            draft['only_likely_auth'] = not draft['only_likely_auth']
        elif choice == 'c':
            return SimpleNamespace(**draft)
        elif choice == '0':
            return None


def _instances_filter_wizard(settings: Settings, current_account: str | None) -> SimpleNamespace | None:
    draft = {'account': current_account}
    while True:
        lines = [
            _heading('实例列表筛选向导', color=CYAN),
            _separator(),
            _key_value('账号范围', _account_display_name(settings, draft['account']) if draft['account'] else '全部账号'),
        ]
        choice = _choose_menu(
            '\n'.join(lines),
            [MenuItem('1', '选择账号范围'), MenuItem('c', '查看实例'), MenuItem('0', '取消')],
            default_key='1',
        )
        if choice == '1':
            draft['account'] = _choose_account_scope(settings, draft['account'], title=_heading('选择账号范围', color=CYAN), allow_all=True)
        elif choice == 'c':
            return SimpleNamespace(**draft)
        elif choice == '0':
            return None


def _keeper_probe_filter_wizard(settings: Settings, current_account: str | None) -> SimpleNamespace | None:
    draft = {'account': current_account, 'only_eligible': False}
    while True:
        lines = [
            _heading('Keeper 探测筛选向导', color=CYAN),
            _separator(),
            _key_value('账号范围', _account_display_name(settings, draft['account']) if draft['account'] else '全部账号'),
            _key_value('只看可执行实例', '是' if draft['only_eligible'] else '否'),
        ]
        choice = _choose_menu(
            '\n'.join(lines),
            [MenuItem('1', '选择账号范围'), MenuItem('2', '切换只看可执行实例'), MenuItem('c', '查看探测'), MenuItem('0', '取消')],
            default_key='1',
        )
        if choice == '1':
            draft['account'] = _choose_account_scope(settings, draft['account'], title=_heading('选择账号范围', color=CYAN), allow_all=True)
        elif choice == '2':
            draft['only_eligible'] = not draft['only_eligible']
        elif choice == 'c':
            return SimpleNamespace(**draft)
        elif choice == '0':
            return None


def _healthcheck_filter_wizard() -> SimpleNamespace | None:
    draft = {'smoke': False}
    while True:
        lines = [
            _heading('健康检查向导', color=CYAN),
            _separator(),
            _key_value('附带登录/实例烟雾测试', '是' if draft['smoke'] else '否'),
        ]
        choice = _choose_menu(
            '\n'.join(lines),
            [MenuItem('1', '切换烟雾测试'), MenuItem('c', '开始检查'), MenuItem('0', '取消')],
            default_key='1',
        )
        if choice == '1':
            draft['smoke'] = not draft['smoke']
        elif choice == 'c':
            return SimpleNamespace(**draft)
        elif choice == '0':
            return None
