from __future__ import annotations

import argparse
import copy
import re
from dataclasses import asdict
from typing import Any, Callable

from autodl_helper.auth_policy import resolve_auth_runtime_policy
from autodl_helper.config import AccountSettings, LIGHTWEIGHT_MODES, Settings

from .shared_accounts import get_enabled_accounts


def compute_cycle_interval_seconds(settings: Settings) -> int:
    candidates: list[int] = []
    if settings.tasks.keeper.enabled:
        candidates.append(max(60, settings.tasks.keeper.interval_minutes * 60))
    if settings.tasks.scheduled_start.enabled and settings.tasks.scheduled_start.jobs:
        candidates.append(max(5, settings.tasks.scheduled_start.poll_interval_seconds))
    return min(candidates) if candidates else 3600


def compute_dispatch_interval_seconds(settings: Settings) -> int:
    return 5


def compute_interval_for_mode(settings: Settings, mode: str) -> int:
    if mode == 'keeper':
        return max(60, settings.tasks.keeper.interval_minutes * 60)
    if mode == 'scheduled_start':
        return max(5, settings.tasks.scheduled_start.poll_interval_seconds)
    return compute_cycle_interval_seconds(settings)


def _sync_primary_auth(settings: Settings) -> None:
    if not settings.accounts:
        return
    enabled = [account for account in settings.accounts if account.enabled]
    primary = enabled[0] if enabled else settings.accounts[0]
    settings.auth = primary.to_auth_settings()


def _resolve_account_override_targets(settings: Settings, account_name: str | None) -> list[AccountSettings]:
    if not settings.accounts:
        return []
    if account_name:
        matches = [account for account in settings.accounts if account.name == account_name]
        if not matches:
            raise ValueError(f'Account not found or disabled: {account_name}')
        return matches
    return list(settings.accounts)


def _resolve_job_override_targets(settings: Settings, job_name: str | None, *, require_single: bool) -> list[Any]:
    jobs = list(settings.tasks.scheduled_start.jobs)
    if not jobs:
        return []
    if job_name:
        matches = [job for job in jobs if job.name == job_name or job.instance_id == job_name]
        if not matches:
            raise ValueError(f'scheduled-start job not found: {job_name}')
        settings.tasks.scheduled_start.jobs = matches
        return matches
    if require_single:
        if len(jobs) != 1:
            raise ValueError('检测到多个 scheduled-start jobs，请使用 --scheduled-job 指定要覆盖的任务。')
        return jobs
    return jobs


def apply_cli_overrides(args: argparse.Namespace, settings: Settings) -> Settings:
    effective = copy.deepcopy(settings)

    keeper = effective.tasks.keeper
    if getattr(args, 'shutdown_release_after_hours', None) is not None:
        keeper.shutdown_release_after_hours = args.shutdown_release_after_hours
    if getattr(args, 'keeper_trigger_before_hours', None) is not None:
        keeper.keeper_trigger_before_hours = args.keeper_trigger_before_hours
    if getattr(args, 'start_cooldown_minutes', None) is not None:
        keeper.start_cooldown_minutes = args.start_cooldown_minutes
    if getattr(args, 'stop_cooldown_minutes', None) is not None:
        keeper.stop_cooldown_minutes = args.stop_cooldown_minutes
    if getattr(args, 'fallback_to_status_at', None) is not None:
        keeper.fallback_to_status_at = bool(args.fallback_to_status_at)

    scheduled = effective.tasks.scheduled_start
    if getattr(args, 'scheduled_poll_interval', None) is not None:
        scheduled.poll_interval_seconds = args.scheduled_poll_interval

    if getattr(args, 'scheduled_job', None):
        _resolve_job_override_targets(effective, args.scheduled_job, require_single=False)

    job_target_time = getattr(args, 'target_time', None)
    job_advance_hours = getattr(args, 'advance_hours', None)
    if job_target_time is not None or job_advance_hours is not None:
        targets = _resolve_job_override_targets(effective, getattr(args, 'scheduled_job', None), require_single=True)
        for job in targets:
            if job_target_time is not None:
                job.target_time = job_target_time
            if job_advance_hours is not None:
                job.advance_hours = job_advance_hours

    auth_override_fields = (
        'lightweight_mode',
        'runtime_auth_revalidate_seconds',
        'force_refresh_min_interval_seconds',
        'auth_failure_backoff_seconds',
    )
    if any(getattr(args, field, None) is not None for field in auth_override_fields):
        for account in _resolve_account_override_targets(effective, getattr(args, 'account', None)):
            if getattr(args, 'lightweight_mode', None) is not None:
                account.lightweight_mode = args.lightweight_mode
            if getattr(args, 'runtime_auth_revalidate_seconds', None) is not None:
                account.runtime_auth_revalidate_seconds = args.runtime_auth_revalidate_seconds
            if getattr(args, 'force_refresh_min_interval_seconds', None) is not None:
                account.force_refresh_min_interval_seconds = args.force_refresh_min_interval_seconds
            if getattr(args, 'auth_failure_backoff_seconds', None) is not None:
                account.auth_failure_backoff_seconds = args.auth_failure_backoff_seconds
        _sync_primary_auth(effective)

    return effective



TIME_RE = re.compile(r'^(?:[01]\d|2[0-3]):[0-5]\d$')


def validate_settings(
    settings: Settings,
    purpose: str = 'all',
    *,
    get_enabled_accounts_fn: Callable[[Settings], list[AccountSettings]] = get_enabled_accounts,
    time_pattern=TIME_RE,
    lightweight_modes=LIGHTWEIGHT_MODES,
) -> list[str]:
    errors: list[str] = []
    enabled_accounts = get_enabled_accounts_fn(settings)
    if not enabled_accounts:
        errors.append('At least one enabled account is required.')
    for account in enabled_accounts:
        if purpose != 'test-notify' and not account.authorization and not (account.autodl_phone and account.autodl_password):
            errors.append(f'account {account.name}: Either Authorization or both AUTODL_PHONE and AUTODL_PASSWORD are required.')
        if account.cache_max_age_seconds <= 0:
            errors.append(f'account {account.name}: auth.cache_max_age_seconds must be a positive integer.')
        if account.lightweight_mode not in lightweight_modes:
            errors.append(f'account {account.name}: lightweight_mode must be one of {sorted(lightweight_modes)}.')
        if account.runtime_auth_revalidate_seconds < 0:
            errors.append(f'account {account.name}: runtime_auth_revalidate_seconds must be zero or a positive integer.')
        if account.force_refresh_min_interval_seconds < 0:
            errors.append(f'account {account.name}: force_refresh_min_interval_seconds must be zero or a positive integer.')
        if account.auth_failure_backoff_seconds < 0:
            errors.append(f'account {account.name}: auth_failure_backoff_seconds must be zero or a positive integer.')

    keeper = settings.tasks.keeper
    if purpose in {'all', 'run-daemon', 'run-all', 'run-keeper', 'validate', 'healthcheck'}:
        if keeper.shutdown_release_after_hours <= 0:
            errors.append('keeper.shutdown_release_after_hours must be a positive integer.')
        if keeper.keeper_trigger_before_hours < 0:
            errors.append('keeper.keeper_trigger_before_hours must be zero or a positive integer.')
        if keeper.start_cooldown_minutes < 0:
            errors.append('keeper.start_cooldown_minutes must be zero or a positive integer.')
        if keeper.stop_cooldown_minutes < 0:
            errors.append('keeper.stop_cooldown_minutes must be zero or a positive integer.')

    if not settings.storage.database_file:
        errors.append('storage.database_file is required.')

    scheduled = settings.tasks.scheduled_start
    if purpose in {'all', 'run-daemon', 'run-all', 'run-scheduled-start', 'validate', 'healthcheck'} and scheduled.enabled:
        if scheduled.poll_interval_seconds < 5:
            errors.append('scheduled_start.poll_interval_seconds must be at least 5.')
        if not scheduled.jobs:
            errors.append('scheduled_start.jobs must not be empty when scheduled_start is enabled.')
        for index, job in enumerate(scheduled.jobs, start=1):
            label = job.name or job.instance_id or f'job#{index}'
            if bool(job.instance_id) == bool(job.selector):
                errors.append(f'scheduled_start job {label} must provide exactly one of instance_id or selector.')
            if not time_pattern.match(job.target_time):
                errors.append(f'scheduled_start job {label} target_time must be HH:MM.')
            if job.advance_hours <= 0:
                errors.append(f'scheduled_start job {label} advance_hours must be a positive integer.')
            if job.selector is not None:
                if not job.selector.gpu_model:
                    errors.append(f'scheduled_start job {label} selector.gpu_model is required.')
                if job.selector.gpu_count <= 0:
                    errors.append(f'scheduled_start job {label} selector.gpu_count must be a positive integer.')
                if job.priority:
                    for entry in job.priority:
                        if not (entry.instance_id or entry.region or entry.machine_alias):
                            errors.append(f'scheduled_start job {label} priority entries must define at least one matcher.')

    if purpose in {'all', 'run-daemon', 'run-all', 'run-keeper', 'run-scheduled-start', 'validate', 'test-notify', 'healthcheck'} and settings.notifications.pushplus.enabled and not settings.notifications.pushplus.token:
        errors.append('pushplus is enabled but token is missing.')
    if purpose in {'all', 'run-daemon', 'run-all', 'run-keeper', 'run-scheduled-start', 'validate', 'test-notify', 'healthcheck'} and settings.notifications.serverchan.enabled and not settings.notifications.serverchan.token:
        errors.append('serverchan is enabled but token is missing.')
    if purpose in {'all', 'run-daemon', 'run-all', 'run-keeper', 'run-scheduled-start', 'validate', 'test-notify', 'healthcheck'} and settings.notifications.email.enabled:
        email = settings.notifications.email
        if not email.smtp_host or not email.username or not email.password or not email.to:
            errors.append('email is enabled but smtp_host/username/password/to are incomplete.')

    return errors

def serialize_settings(settings: Settings, *, resolved: bool = False, account_name: str | None = None) -> dict[str, Any]:
    display = copy.deepcopy(settings)
    if account_name:
        display.accounts = _resolve_account_override_targets(display, account_name)
        _sync_primary_auth(display)
    payload = asdict(display)
    if resolved:
        payload['auth']['resolved_auth_runtime_policy'] = asdict(resolve_auth_runtime_policy(display.auth))
        for account_payload, account in zip(payload.get('accounts', []), display.accounts):
            account_payload['resolved_auth_runtime_policy'] = asdict(resolve_auth_runtime_policy(account.to_auth_settings()))
    if payload['auth'].get('authorization'):
        payload['auth']['authorization'] = '<redacted>'
    if payload['auth'].get('autodl_password'):
        payload['auth']['autodl_password'] = '<redacted>'
    for account_payload in payload.get('accounts', []):
        if account_payload.get('authorization'):
            account_payload['authorization'] = '<redacted>'
        if account_payload.get('autodl_password'):
            account_payload['autodl_password'] = '<redacted>'
    notifications = payload.get('notifications', {})
    for channel in ('pushplus', 'serverchan'):
        channel_payload = notifications.get(channel, {})
        if channel_payload.get('token'):
            channel_payload['token'] = '<redacted>'
    email_payload = notifications.get('email', {})
    if email_payload.get('password'):
        email_payload['password'] = '<redacted>'
    return payload


__all__ = [
    "compute_cycle_interval_seconds",
    "compute_dispatch_interval_seconds",
    "compute_interval_for_mode",
    "_sync_primary_auth",
    "_resolve_account_override_targets",
    "_resolve_job_override_targets",
    "apply_cli_overrides",
    "serialize_settings",
    "validate_settings",
]
