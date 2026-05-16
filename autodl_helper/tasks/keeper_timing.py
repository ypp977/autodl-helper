from __future__ import annotations

from datetime import datetime, timedelta

from autodl_helper.core.api import ASIA_SHANGHAI
from autodl_helper.core.models import KeeperResult

SHUTDOWN_STATUSES = {"shutdown", "stopped", "off"}


def normalize_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(ASIA_SHANGHAI)
    if now.tzinfo is None:
        return ASIA_SHANGHAI.localize(now)
    return now


def parse_datetime(value: str) -> datetime | None:
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


def extract_time_field(item: dict, field_name: str) -> str:
    payload = item.get(field_name)
    if isinstance(payload, dict):
        if payload.get("Valid"):
            return str(payload.get("Time", "") or "").strip()
        return ""
    return str(payload or "").strip()


def duration_seconds(now: datetime, value: str) -> int | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


def format_datetime(value: datetime | None) -> str:
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
    stopped_at = extract_time_field(item, "stopped_at")
    release_source = "stopped_at" if stopped_at else "none"
    shutdown_anchor = parse_datetime(stopped_at)
    if shutdown_anchor is None and fallback_to_status_at and status in SHUTDOWN_STATUSES:
        shutdown_anchor = parse_datetime(status_at)
        if shutdown_anchor is not None:
            release_source = "fallback_status_at"
    if shutdown_anchor is None:
        return {"release_source": "none", "release_deadline": "", "next_keeper_time": ""}
    release_deadline_dt = shutdown_anchor + timedelta(hours=shutdown_release_after_hours)
    next_keeper_time_dt = release_deadline_dt - timedelta(hours=keeper_trigger_before_hours)
    return {
        "release_source": release_source,
        "release_deadline": format_datetime(release_deadline_dt),
        "next_keeper_time": format_datetime(next_keeper_time_dt),
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
    now = normalize_now(now)
    instance_id = str(item.get("uuid", "") or "").strip()
    status = str(item.get("status", "") or "").strip()
    release_at = str(item.get("release_at", "") or "").strip()
    status_at = str(item.get("status_at", "") or "").strip()
    started_at = extract_time_field(item, "started_at")
    stopped_at = extract_time_field(item, "stopped_at")

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

    started_duration_seconds = duration_seconds(now, started_at)
    shutdown_duration_seconds = duration_seconds(now, stopped_at)
    release_source = "stopped_at" if stopped_at else "none"
    shutdown_anchor = parse_datetime(stopped_at)

    if shutdown_anchor is None and fallback_to_status_at and status in SHUTDOWN_STATUSES:
        shutdown_anchor = parse_datetime(status_at)
        shutdown_duration_seconds = duration_seconds(now, status_at)
        if shutdown_anchor is not None:
            release_source = "fallback_status_at"

    if started_duration_seconds is None and fallback_to_status_at and status not in SHUTDOWN_STATUSES:
        started_duration_seconds = duration_seconds(now, status_at)

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
            release_deadline=format_datetime(release_deadline_dt),
            next_keeper_time=format_datetime(next_keeper_time_dt),
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
            release_deadline=format_datetime(release_deadline_dt),
            next_keeper_time=format_datetime(next_keeper_time_dt),
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
            release_deadline=format_datetime(release_deadline_dt),
            next_keeper_time=format_datetime(next_keeper_time_dt),
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
        release_deadline=format_datetime(release_deadline_dt),
        next_keeper_time=format_datetime(next_keeper_time_dt),
        seconds_until_release=seconds_until_release,
        seconds_until_keeper=seconds_until_keeper,
        started_duration_seconds=started_duration_seconds,
        shutdown_duration_seconds=shutdown_duration_seconds,
        eligible=True,
        result="ready",
        reason=reason,
    )
