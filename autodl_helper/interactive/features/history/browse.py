from __future__ import annotations

from typing import TYPE_CHECKING

from ...dialogs import MenuItem, _choose_menu, _menu_default_key
from ...history_instance import _find_scheduled_job, _history_record_subject, _render_instance_reference
from ....runtime_control import scheduled_job_identity
from ...presentation import CYAN, _heading
from ...screen_scheduled import _render_keeper_rules, _render_scheduled_job_detail
from ...screen_scheduled import _show_result_screen
from ...screen_support import _resolve_app_target

from ..accounts.views import _render_account_detail
from .views import _render_history_record_detail

if TYPE_CHECKING:
    from autodl_helper.config import Settings
    from autodl_helper.models import HistoryRecord

__all__ = [
    '_browse_history_records',
]


def _show_result_screen_for(title: str, body: str, *, code: int | None = None) -> None:
    result_screen = _resolve_app_target('_show_result_screen', _show_result_screen)
    result_screen(title, body, code=code)


def _browse_history_records(
    *,
    settings: Settings,
    store,
    current_account: str | None,
    rows: list[HistoryRecord],
    keeper_probe_rows_fn,
    scheduled_job_status_rows_fn,
) -> str | None:
    result_screen = _show_result_screen_for
    if not rows:
        result_screen('最近记录', '没有符合条件的记录。')
        return current_account
    selected_key = '1'
    while True:
        items = [
            MenuItem(str(index), f"{row.created_at} | {row.account_name} | {row.task_type} | {_history_record_subject(row)}")
            for index, row in enumerate(rows, start=1)
        ] + [MenuItem('0', '返回')]
        choice = _choose_menu(_heading('最近记录列表', color=CYAN), items, default_key=_menu_default_key(items, selected_key))
        if choice == '0':
            return current_account
        if not choice.isdigit():
            continue
        selected_key = choice
        row = rows[int(choice) - 1]
        detail_selected_key = '1'
        while True:
            detail_items = [
                MenuItem('1', '查看关联账号'),
                MenuItem('2', '查看关联任务'),
                MenuItem('3', '查看关联实例'),
                MenuItem('0', '返回记录列表'),
            ]
            action = _choose_menu(
                _render_history_record_detail(row),
                detail_items,
                default_key=_menu_default_key(detail_items, detail_selected_key),
            )
            detail_selected_key = action
            if action == '1':
                result_screen(
                    '关联账号',
                    _render_account_detail(
                        settings,
                        store,
                        account_name=row.account_name,
                        keeper_probe_rows_fn=keeper_probe_rows_fn,
                        scheduled_job_status_rows_fn=scheduled_job_status_rows_fn,
                    ),
                )
            elif action == '2':
                if row.task_type == 'keeper':
                    result_screen('关联任务', _render_keeper_rules(settings, row.account_name, store))
                else:
                    try:
                        job = _find_scheduled_job(settings, row.payload.get('job_name') or row.instance_id or _history_record_subject(row))
                        status_rows = [{
                            'job_name': scheduled_job_identity(job),
                            'target_time': row.payload.get('target_time') or job.target_time,
                            'advance_hours': row.payload.get('advance_hours') or job.advance_hours,
                            'enabled': True,
                        }]
                        result_screen('关联任务', _render_scheduled_job_detail(job, status_rows[0], row.account_name))
                    except Exception:
                        result_screen('关联任务', '当前配置里找不到这条任务规则，可能已经被删除或改名。')
            elif action == '3':
                result_screen('关联实例', _render_instance_reference(row))
            elif action == '0':
                break
