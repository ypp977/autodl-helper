from __future__ import annotations

from typing import Any

from autodl_helper.services.manager import service_status as _service_status, start_service as _start_service, stop_service as _stop_service

from ...support.delegates import _bind_app_globals
from ...runtime import InteractivePageStatus
from ...status_task import _page_status_from_snapshot_keys, _snapshot_key


DEFAULT_SERVICE_LABEL = 'autodl-helper'


def _read_launch_agent_status_fallback():
    return _service_status(config_path='config.yaml')


def _start_launch_agent_fallback():
    return _start_service(config_path='config.yaml')


def _stop_launch_agent_fallback():
    return _stop_service(config_path='config.yaml')


def _diagnostics_page_status(
    *,
    snapshot_store,
    account_scope: str,
    instance_task,
    keeper_task,
    healthcheck_task,
):
    _bind_app_globals(globals(), exclude={'_diagnostics_page_status'})
    sources = [
        (
            _snapshot_key('instances', account_scope),
            '最近实例更新',
            '实例刷新失败（保留上次结果）',
            '实例刷新失败',
        ),
        (
            _snapshot_key('keeper_probe', account_scope),
            '最近 Keeper 更新',
            'Keeper 刷新失败（保留上次结果）',
            'Keeper 刷新失败',
        ),
        (
            _snapshot_key('healthcheck', account_scope),
            '最近健康自检更新',
            '健康自检刷新失败（保留上次结果）',
            '健康自检刷新失败',
        ),
        (
            _snapshot_key('config_diagnostics', account_scope),
            '最近配置诊断更新',
            '配置诊断刷新失败（保留上次结果）',
            '配置诊断刷新失败',
        ),
    ]
    status = _page_status_from_snapshot_keys(
        snapshot_store=snapshot_store,
        snapshot_keys=[key for key, *_ in sources],
        primary_task=instance_task,
        secondary_tasks=[keeper_task, healthcheck_task],
    )
    if any(task is not None and task.status in {'queued', 'running'} for task in (instance_task, keeper_task, healthcheck_task)):
        return status

    latest_ready: tuple[str, Any] | None = None
    latest_failed: tuple[str, Any] | None = None
    for key, ready_message, failed_keep_message, failed_message in sources:
        entry = snapshot_store.get_entry(key)
        if entry is None:
            continue
        if entry.updated_at:
            if latest_ready is None or str(entry.updated_at) >= str(latest_ready[1].updated_at):
                latest_ready = (ready_message, entry)
        if entry.error_message:
            failed_label = failed_keep_message if entry.updated_at else failed_message
            if latest_failed is None:
                latest_failed = (failed_label, entry)
            elif entry.updated_at and (not latest_failed[1].updated_at or str(entry.updated_at) >= str(latest_failed[1].updated_at)):
                latest_failed = (failed_label, entry)

    if latest_failed is not None and (latest_ready is None or str(latest_failed[1].updated_at or '') >= str(latest_ready[1].updated_at or '')):
        return InteractivePageStatus(
            state='failed',
            message=latest_failed[0],
            updated_at=str(latest_failed[1].updated_at or ''),
            error_message=str(latest_failed[1].error_message or ''),
        )
    if latest_ready is not None:
        return InteractivePageStatus(
            state='ready',
            message=latest_ready[0],
            updated_at=str(latest_ready[1].updated_at or ''),
            error_message='',
        )
    return status


__all__ = [
    'DEFAULT_SERVICE_LABEL',
    '_diagnostics_page_status',
    '_read_launch_agent_status_fallback',
    '_start_launch_agent_fallback',
    '_stop_launch_agent_fallback',
]
