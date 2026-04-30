from __future__ import annotations

import argparse
from dataclasses import asdict
from typing import Any

from autodl_helper.config import Settings


def _has_config_edit_args(args: argparse.Namespace) -> bool:
    keys = (
        'shutdown_release_after_hours',
        'keeper_trigger_before_hours',
        'start_cooldown_minutes',
        'stop_cooldown_minutes',
        'fallback_to_status_at',
        'scheduled_poll_interval',
        'scheduled_job',
        'target_time',
        'advance_hours',
        'lightweight_mode',
        'runtime_auth_revalidate_seconds',
        'force_refresh_min_interval_seconds',
        'auth_failure_backoff_seconds',
    )
    return any(getattr(args, key, None) is not None for key in keys)


def _prompt_optional_text(prompt: str) -> str | None:
    value = input(prompt).strip()
    return value or None


def _prompt_optional_int(prompt: str) -> int | None:
    value = input(prompt).strip()
    return int(value) if value else None


def _prompt_optional_bool(prompt: str) -> bool | None:
    value = input(prompt).strip().lower()
    if not value:
        return None
    if value in {'y', 'yes', 'true', '1'}:
        return True
    if value in {'n', 'no', 'false', '0'}:
        return False
    raise ValueError(f'Invalid boolean input: {value}')


def collect_config_edit_args(args: argparse.Namespace) -> argparse.Namespace:
    if _has_config_edit_args(args):
        return args
    args.lightweight_mode = _prompt_optional_text('lightweight_mode (off/normal/aggressive, blank=skip): ')
    args.runtime_auth_revalidate_seconds = _prompt_optional_int('runtime_auth_revalidate_seconds (blank=skip): ')
    args.force_refresh_min_interval_seconds = _prompt_optional_int('force_refresh_min_interval_seconds (blank=skip): ')
    args.auth_failure_backoff_seconds = _prompt_optional_int('auth_failure_backoff_seconds (blank=skip): ')
    args.shutdown_release_after_hours = _prompt_optional_int('shutdown_release_after_hours (blank=skip): ')
    args.keeper_trigger_before_hours = _prompt_optional_int('keeper_trigger_before_hours (blank=skip): ')
    args.start_cooldown_minutes = _prompt_optional_int('start_cooldown_minutes (blank=skip): ')
    args.stop_cooldown_minutes = _prompt_optional_int('stop_cooldown_minutes (blank=skip): ')
    args.fallback_to_status_at = _prompt_optional_bool('fallback_to_status_at (y/n, blank=skip): ')
    args.scheduled_poll_interval = _prompt_optional_int('scheduled_poll_interval (blank=skip): ')
    args.scheduled_job = _prompt_optional_text('scheduled_job name/instance_id (blank=skip): ')
    args.target_time = _prompt_optional_text('target_time (HH:MM, blank=skip): ')
    args.advance_hours = _prompt_optional_int('advance_hours (blank=skip): ')
    return args


def _ensure_account_payloads(payload: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    accounts_payload = payload.get('accounts')
    if isinstance(accounts_payload, list) and accounts_payload:
        return accounts_payload
    payload['accounts'] = [
        {
            'name': account.name,
            'enabled': account.enabled,
            'authorization': account.authorization,
            'autodl_phone': account.autodl_phone,
            'autodl_password': account.autodl_password,
            'cache_file': account.cache_file,
            'cache_max_age_seconds': account.cache_max_age_seconds,
            'lightweight_mode': account.lightweight_mode,
            'runtime_auth_revalidate_seconds': account.runtime_auth_revalidate_seconds,
            'force_refresh_min_interval_seconds': account.force_refresh_min_interval_seconds,
            'auth_failure_backoff_seconds': account.auth_failure_backoff_seconds,
        }
        for account in settings.accounts
    ]
    return payload['accounts']


def _select_account_payloads(payload: dict[str, Any], settings: Settings, account_name: str | None) -> list[dict[str, Any]]:
    accounts_payload = _ensure_account_payloads(payload, settings)
    if account_name:
        matches = [item for item in accounts_payload if str(item.get('name', '')).strip() == account_name]
        if not matches:
            raise ValueError(f'Account not found or disabled: {account_name}')
        return matches
    return accounts_payload


def _ensure_task_payload(payload: dict[str, Any], task_name: str) -> dict[str, Any]:
    tasks_payload = payload.setdefault('tasks', {})
    task_payload = tasks_payload.setdefault(task_name, {})
    return task_payload


def _select_job_payloads(payload: dict[str, Any], settings: Settings, job_name: str | None, *, require_single: bool) -> list[dict[str, Any]]:
    scheduled_payload = _ensure_task_payload(payload, 'scheduled_start')
    jobs_payload = scheduled_payload.setdefault('jobs', [])
    if not jobs_payload and settings.tasks.scheduled_start.jobs:
        for job in settings.tasks.scheduled_start.jobs:
            jobs_payload.append(
                {
                    'instance_id': job.instance_id,
                    'name': job.name,
                    'target_time': job.target_time,
                    'advance_hours': job.advance_hours,
                    'timezone': job.timezone,
                    **({'selector': asdict(job.selector)} if job.selector is not None else {}),
                    **({'priority': [asdict(item) for item in job.priority]} if job.priority else {}),
                }
            )
    if job_name:
        matches = [item for item in jobs_payload if item.get('name') == job_name or item.get('instance_id') == job_name]
        if not matches:
            raise ValueError(f'scheduled-start job not found: {job_name}')
        return matches
    if require_single:
        if len(jobs_payload) != 1:
            raise ValueError('检测到多个 scheduled-start jobs，请使用 --scheduled-job 指定要改写的任务。')
        return jobs_payload
    return jobs_payload


__all__ = [
    "_has_config_edit_args",
    "_prompt_optional_text",
    "_prompt_optional_int",
    "_prompt_optional_bool",
    "collect_config_edit_args",
    "_ensure_account_payloads",
    "_select_account_payloads",
    "_ensure_task_payload",
    "_select_job_payloads",
]
