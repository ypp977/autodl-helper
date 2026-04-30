from __future__ import annotations

import argparse

from ...dialogs import MenuItem
from ...runtime import InteractiveSnapshotStore, InteractiveTaskManager
from ...support.snapshots import _browse_snapshot_list
from ...screen_scheduled import _keeper_probe_schedule_lines
from ...screen_support import _delegate
from ...service_ops import _load_instance_rows_via_command as _load_instance_rows_via_command_fallback
from ...shared import _instance_gpu_summary, _instance_idle_gpu_summary, _normalize_instance_status

from .views import (
    _render_instance_detail,
    _render_instance_list_page,
    _render_keeper_probe_detail,
    _render_keeper_probe_list_page,
)

__all__ = [
    "_browse_instance_list",
    "_browse_keeper_probe",
]

_load_instance_rows_via_command = _delegate('_load_instance_rows_via_command', _load_instance_rows_via_command_fallback)


def _browse_instance_list(*, args: argparse.Namespace, current_account: str | None, settings, command_list_instances_fn, task_manager: InteractiveTaskManager | None = None, snapshot_store: InteractiveSnapshotStore | None = None) -> None:
    _browse_snapshot_list(
        settings=settings,
        current_account=current_account,
        snapshot_namespace='instances',
        task_type='instances_refresh',
        status_message='正在刷新实例列表',
        task_runner=lambda: _load_instance_rows_via_command(args=args, current_account=current_account, command_list_instances_fn=command_list_instances_fn),
        render_page_fn=_render_instance_list_page,
        build_items_fn=lambda rows: [
            MenuItem(str(index), f"{row.get('name') or '-'} / {row.get('region') or '-'} / {_normalize_instance_status(row.get('status'))} / {_instance_gpu_summary(row)} / {_instance_idle_gpu_summary(row)} / {row.get('machine_alias') or '-'}")
            for index, row in enumerate(rows, start=1)
        ],
        detail_title_fn=lambda _row: '实例详情',
        detail_body_fn=lambda row, account_label: _render_instance_detail(row, account_label),
        task_manager=task_manager,
        snapshot_store=snapshot_store,
    )


def _browse_keeper_probe(*, settings, store, current_account: str | None, keeper_probe_rows_fn, task_manager: InteractiveTaskManager | None = None, snapshot_store: InteractiveSnapshotStore | None = None) -> None:
    _browse_snapshot_list(
        settings=settings,
        current_account=current_account,
        snapshot_namespace='keeper_probe',
        task_type='keeper_probe_refresh',
        status_message='正在刷新 Keeper 探测',
        task_runner=lambda: keeper_probe_rows_fn(settings, store, account_name=current_account),
        render_page_fn=_render_keeper_probe_list_page,
        build_items_fn=lambda rows: [MenuItem(str(index), f"{row.get('instance_id') or '-'} / {row.get('result') or '-'}") for index, row in enumerate(rows, start=1)],
        detail_title_fn=lambda _row: 'Keeper 检测详情',
        detail_body_fn=lambda row, account_label: _render_keeper_probe_detail(row, account_label),
        task_manager=task_manager,
        snapshot_store=snapshot_store,
        extra_status_lines_fn=lambda rows, _status, _active_task: [*_keeper_probe_schedule_lines(settings, store, account_name=current_account)] if rows else None,
        progress_label='检测进度',
        show_progress=False,
    )


_browse_instance_list = _delegate('_browse_instance_list', _browse_instance_list)
_browse_keeper_probe = _delegate('_browse_keeper_probe', _browse_keeper_probe)
