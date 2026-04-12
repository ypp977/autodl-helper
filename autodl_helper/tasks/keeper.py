from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

from autodl_helper.api import ASIA_SHANGHAI
from autodl_helper.events import enrich_keeper_result
from autodl_helper.models import KeeperResult

logger = logging.getLogger(__name__)

SHUTDOWN_STATUSES = {"shutdown", "stopped", "off"}


def _normalize_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(ASIA_SHANGHAI)
    if now.tzinfo is None:
        return ASIA_SHANGHAI.localize(now)
    return now


def _parse_datetime(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return ASIA_SHANGHAI.localize(parsed)
    return parsed


def _extract_time_field(item: dict, field_name: str) -> str:
    payload = item.get(field_name)
    if isinstance(payload, dict):
        if payload.get("Valid"):
            return str(payload.get("Time", "") or "").strip()
        return ""
    return str(payload or "").strip()


def _duration_seconds(now: datetime, value: str) -> int | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def format_duration_seconds(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    days, remain = divmod(seconds, 24 * 60 * 60)
    hours, remain = divmod(remain, 60 * 60)
    minutes, secs = divmod(remain, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def compute_keeper_schedule(
    *,
    item: dict,
    shutdown_release_after_hours: int,
    keeper_trigger_before_hours: int,
    fallback_to_status_at: bool,
) -> dict[str, str]:
    status = str(item.get("status", "") or "").strip()
    status_at = str(item.get("status_at", "") or "").strip()
    stopped_at = _extract_time_field(item, "stopped_at")
    release_source = "stopped_at" if stopped_at else "none"
    shutdown_anchor = _parse_datetime(stopped_at)
    if shutdown_anchor is None and fallback_to_status_at and status in SHUTDOWN_STATUSES:
        shutdown_anchor = _parse_datetime(status_at)
        if shutdown_anchor is not None:
            release_source = "fallback_status_at"
    if shutdown_anchor is None:
        return {"release_source": "none", "release_deadline": "", "next_keeper_time": ""}
    release_deadline_dt = shutdown_anchor + timedelta(hours=shutdown_release_after_hours)
    next_keeper_time_dt = release_deadline_dt - timedelta(hours=keeper_trigger_before_hours)
    return {
        "release_source": release_source,
        "release_deadline": _format_datetime(release_deadline_dt),
        "next_keeper_time": _format_datetime(next_keeper_time_dt),
    }


def evaluate_keeper_instance(
    *,
    client,
    item: dict,
    shutdown_release_after_hours: int,
    keeper_trigger_before_hours: int,
    start_cooldown_minutes: int,
    stop_cooldown_minutes: int,
    fallback_to_status_at: bool,
    now: datetime | None = None,
) -> KeeperResult:
    now = _normalize_now(now)
    instance_id = str(item.get("uuid", "") or "").strip()
    status = str(item.get("status", "") or "").strip()
    release_at = str(item.get("release_at", "") or "").strip()
    status_at = str(item.get("status_at", "") or "").strip()
    started_at = _extract_time_field(item, "started_at")
    stopped_at = _extract_time_field(item, "stopped_at")

    if not instance_id:
        return KeeperResult(
            instance_id="",
            status=status,
            release_at=release_at,
            release_source="none",
            started_at=started_at,
            stopped_at=stopped_at,
            status_at=status_at,
            release_deadline="",
            next_keeper_time="",
            seconds_until_release=None,
            seconds_until_keeper=None,
            started_duration_seconds=None,
            shutdown_duration_seconds=None,
            eligible=False,
            result="skip_missing_instance_id",
            reason="missing_instance_id",
        )

    started_duration_seconds = _duration_seconds(now, started_at)
    shutdown_duration_seconds = _duration_seconds(now, stopped_at)
    release_source = "stopped_at" if stopped_at else "none"
    shutdown_anchor = _parse_datetime(stopped_at)

    if shutdown_anchor is None and fallback_to_status_at and status in SHUTDOWN_STATUSES:
        shutdown_anchor = _parse_datetime(status_at)
        shutdown_duration_seconds = _duration_seconds(now, status_at)
        if shutdown_anchor is not None:
            release_source = "fallback_status_at"

    if started_duration_seconds is None and fallback_to_status_at and status not in SHUTDOWN_STATUSES:
        started_duration_seconds = _duration_seconds(now, status_at)

    if shutdown_anchor is None:
        return KeeperResult(
            instance_id=instance_id,
            status=status,
            release_at=release_at,
            release_source="none",
            started_at=started_at,
            stopped_at=stopped_at,
            status_at=status_at,
            release_deadline="",
            next_keeper_time="",
            seconds_until_release=None,
            seconds_until_keeper=None,
            started_duration_seconds=started_duration_seconds,
            shutdown_duration_seconds=shutdown_duration_seconds,
            eligible=False,
            result="skip_missing_shutdown_time",
            reason="missing_shutdown_time",
        )

    release_deadline_dt = shutdown_anchor + timedelta(hours=shutdown_release_after_hours)
    next_keeper_time_dt = release_deadline_dt - timedelta(hours=keeper_trigger_before_hours)
    seconds_until_release = max(0, int((release_deadline_dt - now).total_seconds()))
    seconds_until_keeper = int((next_keeper_time_dt - now).total_seconds())

    if seconds_until_keeper > 0:
        return KeeperResult(
            instance_id=instance_id,
            status=status,
            release_at=release_at,
            release_source=release_source,
            started_at=started_at,
            stopped_at=stopped_at,
            status_at=status_at,
            release_deadline=_format_datetime(release_deadline_dt),
            next_keeper_time=_format_datetime(next_keeper_time_dt),
            seconds_until_release=seconds_until_release,
            seconds_until_keeper=seconds_until_keeper,
            started_duration_seconds=started_duration_seconds,
            shutdown_duration_seconds=shutdown_duration_seconds,
            eligible=False,
            result="skip_not_due",
            reason="before_next_keeper_time",
        )

    start_cooldown_seconds = max(0, start_cooldown_minutes) * 60
    stop_cooldown_seconds = max(0, stop_cooldown_minutes) * 60

    if shutdown_duration_seconds is not None and shutdown_duration_seconds < stop_cooldown_seconds:
        reason = "fallback_status_at_recently_stopped" if release_source == "fallback_status_at" else "stopped_within_cooldown"
        return KeeperResult(
            instance_id=instance_id,
            status=status,
            release_at=release_at,
            release_source=release_source,
            started_at=started_at,
            stopped_at=stopped_at,
            status_at=status_at,
            release_deadline=_format_datetime(release_deadline_dt),
            next_keeper_time=_format_datetime(next_keeper_time_dt),
            seconds_until_release=seconds_until_release,
            seconds_until_keeper=seconds_until_keeper,
            started_duration_seconds=started_duration_seconds,
            shutdown_duration_seconds=shutdown_duration_seconds,
            eligible=False,
            result="skip_recently_stopped",
            reason=reason,
        )

    if started_duration_seconds is not None and started_duration_seconds < start_cooldown_seconds:
        reason = "fallback_status_at_recently_started" if release_source == "fallback_status_at" else "started_within_cooldown"
        return KeeperResult(
            instance_id=instance_id,
            status=status,
            release_at=release_at,
            release_source=release_source,
            started_at=started_at,
            stopped_at=stopped_at,
            status_at=status_at,
            release_deadline=_format_datetime(release_deadline_dt),
            next_keeper_time=_format_datetime(next_keeper_time_dt),
            seconds_until_release=seconds_until_release,
            seconds_until_keeper=seconds_until_keeper,
            started_duration_seconds=started_duration_seconds,
            shutdown_duration_seconds=shutdown_duration_seconds,
            eligible=False,
            result="skip_recently_started",
            reason=reason,
        )

    reason = "fallback_status_at_ready" if release_source == "fallback_status_at" else "keeper_window_reached"
    return KeeperResult(
        instance_id=instance_id,
        status=status,
        release_at=release_at,
        release_source=release_source,
        started_at=started_at,
        stopped_at=stopped_at,
        status_at=status_at,
        release_deadline=_format_datetime(release_deadline_dt),
        next_keeper_time=_format_datetime(next_keeper_time_dt),
        seconds_until_release=seconds_until_release,
        seconds_until_keeper=seconds_until_keeper,
        started_duration_seconds=started_duration_seconds,
        shutdown_duration_seconds=shutdown_duration_seconds,
        eligible=True,
        result="ready",
        reason=reason,
    )


def _log_keeper_result(account_name: str, result: KeeperResult) -> None:
    logger.info(
        "keeper检查 账号=%s 实例=%s 状态=%s 判断来源=%s 预计释放时间=%s 下次keeper时间=%s 关机时长=%s 启动后时长=%s keeper达标=%s 结果=%s 原因=%s",
        account_name,
        result.instance_id,
        result.status,
        result.release_source,
        result.release_deadline or "-",
        result.next_keeper_time or "-",
        format_duration_seconds(result.shutdown_duration_seconds),
        format_duration_seconds(result.started_duration_seconds),
        result.eligible,
        result.result,
        result.reason,
    )


def _store_keeper_history(store, account_name: str, result: KeeperResult, *, batch_id: str | None = None) -> None:
    payload = dict(result.__dict__)
    if batch_id:
        payload['batch_id'] = batch_id
    try:
        store.add_keeper_history(
            account_name,
            result.instance_id,
            result.release_deadline,
            result.result,
            result.reason,
            payload,
            result.event_type,
            result.severity,
            result.summary,
        )
    except TypeError:
        store.add_keeper_history(account_name, result.instance_id, result.release_deadline, result.result, result.reason, payload)


def run_keeper_cycle(
    *,
    client,
    shutdown_release_after_hours: int,
    keeper_trigger_before_hours: int,
    now: datetime | None = None,
    power_on_wait_seconds: int = 60,
    power_off_wait_seconds: int = 5,
    start_cooldown_minutes: int = 60,
    stop_cooldown_minutes: int = 360,
    fallback_to_status_at: bool = True,
    store=None,
    account_name: str = "default",
) -> list[KeeperResult]:
    results: list[KeeperResult] = []
    now = _normalize_now(now)
    batch_id = uuid4().hex

    for item in client.list_instances():
        result = evaluate_keeper_instance(
            client=client,
            item=item,
            shutdown_release_after_hours=shutdown_release_after_hours,
            keeper_trigger_before_hours=keeper_trigger_before_hours,
            start_cooldown_minutes=start_cooldown_minutes,
            stop_cooldown_minutes=stop_cooldown_minutes,
            fallback_to_status_at=fallback_to_status_at,
            now=now,
        )

        result = enrich_keeper_result(result)

        if result.result == "ready" and store is not None and result.release_deadline:
            if store.was_keeper_executed_in_cycle(account_name, result.instance_id, result.release_deadline):
                result = enrich_keeper_result(replace(
                    result,
                    eligible=False,
                    result="skip_already_executed_in_cycle",
                    reason="already_executed_in_release_cycle",
                ))

        if result.result == "ready":
            power_on_ok = client.open_machine(result.instance_id)
            if not power_on_ok:
                result = enrich_keeper_result(replace(result, eligible=False, result="keeper_failed_power_on", reason="power_on_failed"))
                if store is not None:
                    _store_keeper_history(store, account_name, result, batch_id=batch_id)
                results.append(result)
                _log_keeper_result(account_name, result)
                continue

            time.sleep(power_on_wait_seconds)

            power_off_ok = client.close_machine(result.instance_id)
            if not power_off_ok:
                result = enrich_keeper_result(replace(result, eligible=False, result="keeper_failed_power_off", reason="power_off_failed"))
                if store is not None:
                    _store_keeper_history(store, account_name, result, batch_id=batch_id)
                results.append(result)
                _log_keeper_result(account_name, result)
                continue

            time.sleep(power_off_wait_seconds)
            result = enrich_keeper_result(replace(result, result="keeper_executed"))

        result = enrich_keeper_result(result)
        if store is not None:
            _store_keeper_history(store, account_name, result, batch_id=batch_id)
        results.append(result)
        _log_keeper_result(account_name, result)

    return results
