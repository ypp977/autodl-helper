from __future__ import annotations

from autodl_helper.config import Settings

from ..account_ops import _interactive_max_workers
from ..account_common import _copy_args, _run_captured_action
from ..dialogs import (
    MenuItem,
    _InteractiveCancel,
    _choose_menu,
    _choose_menu_with_refresh,
    _confirm_action,
    _menu_default_key,
    _prompt_scheduled_job,
)
from ..history_instance import _find_scheduled_job
from ..runtime import InteractiveSnapshotStore, InteractiveTaskManager, InteractiveTaskResult
from ..scheduled import _job_to_payload, _persist_job_changes, _persist_keeper_changes, _scheduled_run_pending_state, _scheduled_seed_status_rows
from ..screen_scheduled import (
    _build_scheduled_detail_menu_items,
    _render_scheduled_job_detail,
    _render_scheduled_job_picker,
    _scheduled_picker_item_label,
    _show_live_scheduled_status,
)
from ..service_ops import _coordinate_scheduled_background, _merge_scheduled_transient_state, _refresh_scheduled_transient_state, _submit_snapshot_task, get_task_enabled, read_daemon_status
from ..status_task import (
    _friendly_resource_error_message,
    _menu_refresh_revision,
    _nudge_background_tasks,
    _page_status_from_task_result,
    _page_status_from_tasks,
    _page_status_lines,
    _print_execution_summary,
    _snapshot_key,
    _store_snapshot,
)
from .rendering import _show_result_screen_for as _show_result_screen
from autodl_helper.runtime_control import scheduled_job_identity

__all__ = [
    'InteractiveSnapshotStore',
    'InteractiveTaskManager',
    'InteractiveTaskResult',
    'MenuItem',
    'Settings',
    '_InteractiveCancel',
    '_build_scheduled_detail_menu_items',
    '_choose_menu',
    '_choose_menu_with_refresh',
    '_confirm_action',
    '_coordinate_scheduled_background',
    '_copy_args',
    '_find_scheduled_job',
    '_friendly_resource_error_message',
    '_interactive_max_workers',
    '_job_to_payload',
    '_merge_scheduled_transient_state',
    '_menu_default_key',
    '_menu_refresh_revision',
    '_nudge_background_tasks',
    '_page_status_from_task_result',
    '_page_status_from_tasks',
    '_page_status_lines',
    '_persist_job_changes',
    '_persist_keeper_changes',
    '_print_execution_summary',
    '_prompt_scheduled_job',
    '_render_scheduled_job_detail',
    '_render_scheduled_job_picker',
    '_refresh_scheduled_transient_state',
    '_run_captured_action',
    '_scheduled_picker_item_label',
    '_scheduled_run_pending_state',
    '_scheduled_seed_status_rows',
    '_show_live_scheduled_status',
    '_show_result_screen',
    '_snapshot_key',
    '_store_snapshot',
    '_submit_snapshot_task',
    'get_task_enabled',
    'read_daemon_status',
    'scheduled_job_identity',
]
