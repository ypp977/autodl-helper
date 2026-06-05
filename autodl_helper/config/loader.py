from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

LIGHTWEIGHT_MODES = {"off", "normal", "aggressive"}


@dataclass
class AuthSettings:
    authorization: str = ""
    autodl_phone: str = ""
    autodl_password: str = ""
    login_retries: int = 3
    login_timeout_ms: int = 15000
    post_login_wait_seconds: int = 8
    cache_file: str = ".autodl-helper-auth.json"
    cache_max_age_seconds: int = 86400
    lightweight_mode: str = "off"
    runtime_auth_revalidate_seconds: int = 0
    force_refresh_min_interval_seconds: int = 0
    auth_failure_backoff_seconds: int = 0


@dataclass
class AccountSettings:
    name: str = "default"
    enabled: bool = True
    authorization: str = ""
    autodl_phone: str = ""
    autodl_password: str = ""
    login_retries: int = 3
    login_timeout_ms: int = 15000
    post_login_wait_seconds: int = 8
    cache_file: str = ".autodl-helper-auth.json"
    cache_max_age_seconds: int = 86400
    lightweight_mode: str = "off"
    runtime_auth_revalidate_seconds: int = 0
    force_refresh_min_interval_seconds: int = 0
    auth_failure_backoff_seconds: int = 0

    def to_auth_settings(self) -> AuthSettings:
        return AuthSettings(
            authorization=self.authorization,
            autodl_phone=self.autodl_phone,
            autodl_password=self.autodl_password,
            login_retries=self.login_retries,
            login_timeout_ms=self.login_timeout_ms,
            post_login_wait_seconds=self.post_login_wait_seconds,
            cache_file=self.cache_file,
            cache_max_age_seconds=self.cache_max_age_seconds,
            lightweight_mode=self.lightweight_mode,
            runtime_auth_revalidate_seconds=self.runtime_auth_revalidate_seconds,
            force_refresh_min_interval_seconds=self.force_refresh_min_interval_seconds,
            auth_failure_backoff_seconds=self.auth_failure_backoff_seconds,
        )


@dataclass
class StorageSettings:
    database_file: str = "data/autodl-helper.db"


@dataclass
class KeeperSettings:
    enabled: bool = True
    min_day: int = 7
    shutdown_release_after_hours: int = 360
    keeper_trigger_before_hours: int = 6
    interval_minutes: int = 60
    power_on_wait_seconds: int = 60
    power_off_wait_seconds: int = 5
    start_cooldown_minutes: int = 60
    stop_cooldown_minutes: int = 360
    fallback_to_status_at: bool = True


@dataclass
class ScheduledStartSelector:
    regions: list[str] = field(default_factory=list)
    gpu_model: str = ""
    gpu_count: int = 1
    charge_types: list[str] = field(default_factory=list)


@dataclass
class ScheduledStartPriority:
    instance_id: str = ""
    region: str = ""
    machine_alias: str = ""


@dataclass
class ScheduledStartJob:
    enabled: bool = True
    instance_id: str = ""
    name: str = ""
    target_time: str = "14:00"
    advance_hours: float = 2
    schedule_mode: str = "daily"
    weekdays: list[int] = field(default_factory=list)
    run_date: str = ""
    timezone: str = "Asia/Shanghai"
    selector: ScheduledStartSelector | None = None
    priority: list[ScheduledStartPriority] = field(default_factory=list)


@dataclass
class ScheduledStartSettings:
    enabled: bool = False
    poll_interval_seconds: int = 300
    jobs: list[ScheduledStartJob] = field(default_factory=list)


@dataclass
class TaskSettings:
    keeper: KeeperSettings = field(default_factory=KeeperSettings)
    scheduled_start: ScheduledStartSettings = field(default_factory=ScheduledStartSettings)


@dataclass
class NotificationChannelSettings:
    enabled: bool = False
    token: str = ""


@dataclass
class EmailSettings:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    username: str = ""
    password: str = ""
    to: list[str] = field(default_factory=list)


@dataclass
class NotificationSettings:
    pushplus: NotificationChannelSettings = field(default_factory=NotificationChannelSettings)
    serverchan: NotificationChannelSettings = field(default_factory=NotificationChannelSettings)
    email: EmailSettings = field(default_factory=EmailSettings)


@dataclass
class InteractiveSettings:
    max_workers: int = 6


@dataclass
class Settings:
    auth: AuthSettings = field(default_factory=AuthSettings)
    accounts: list[AccountSettings] = field(default_factory=list)
    storage: StorageSettings = field(default_factory=StorageSettings)
    tasks: TaskSettings = field(default_factory=TaskSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    interactive: InteractiveSettings = field(default_factory=InteractiveSettings)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def read_raw_settings(config_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else Path("config.yaml")
    return _read_yaml(path)


def write_raw_settings(config_path: str | Path, payload: dict[str, Any]) -> None:
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp', delete=False) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(text)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _normalize_lightweight_mode(raw_value: Any) -> str:
    if isinstance(raw_value, bool):
        return "normal" if raw_value else "off"
    value = str(raw_value or "").strip().lower()
    return value or "off"


def _resolve_path(base_dir: Path, raw_value: str, default_name: str) -> str:
    raw_value = str(raw_value or "").strip()
    if not raw_value:
        return str((base_dir / default_name).resolve())
    path = Path(raw_value)
    if not path.is_absolute():
        path = base_dir / path
    return str(path.resolve())


def _parse_scheduled_job(job: dict[str, Any]) -> ScheduledStartJob:
    selector_payload = job.get("selector")
    selector = ScheduledStartSelector(**selector_payload) if selector_payload else None
    priority = [ScheduledStartPriority(**item) for item in job.get("priority", [])]
    return ScheduledStartJob(
        enabled=bool(job.get("enabled", True)),
        instance_id=job.get("instance_id", ""),
        name=job.get("name", ""),
        target_time=job.get("target_time", "14:00"),
        advance_hours=float(job.get("advance_hours", 2)),
        schedule_mode=str(job.get("schedule_mode", "daily") or "daily"),
        weekdays=[int(day) for day in (job.get("weekdays") or [])],
        run_date=str(job.get("run_date", "") or ""),
        timezone=job.get("timezone", "Asia/Shanghai"),
        selector=selector,
        priority=priority,
    )


def _account_default_cache_name(name: str) -> str:
    safe_name = (name or "default").strip() or "default"
    return f".cache/{safe_name}-auth.json"


def _build_account(
    *,
    base_dir: Path,
    payload: dict[str, Any],
    inherited_auth_payload: dict[str, Any] | None = None,
    use_env: bool,
    index: int,
    legacy_default_cache: bool,
) -> AccountSettings:
    inherited_auth_payload = inherited_auth_payload or {}
    name = str(payload.get("name", "")).strip() or ("default" if index == 0 else f"account-{index + 1}")
    merged = {**inherited_auth_payload, **payload}
    default_cache_name = ".autodl-helper-auth.json" if legacy_default_cache and index == 0 else _account_default_cache_name(name)
    authorization = merged.get("authorization", "")
    phone = merged.get("autodl_phone", "")
    password = merged.get("autodl_password", "")
    if use_env:
        authorization = os.getenv("Authorization", authorization)
        phone = os.getenv("AUTODL_PHONE", phone)
        password = os.getenv("AUTODL_PASSWORD", password)
    cache_file_raw = os.getenv("AUTODL_AUTH_CACHE_FILE", merged.get("cache_file", "")) if use_env else merged.get("cache_file", "")
    lightweight_mode_raw = merged.get("lightweight_mode", "off")
    return AccountSettings(
        name=name,
        enabled=bool(merged.get("enabled", True)),
        authorization=str(authorization or "").strip(),
        autodl_phone=str(phone or "").strip(),
        autodl_password=str(password or "").strip(),
        login_retries=_env_int("AUTODL_LOGIN_RETRIES", int(merged.get("login_retries", 3))) if use_env else int(merged.get("login_retries", 3)),
        login_timeout_ms=_env_int("AUTODL_LOGIN_TIMEOUT_MS", int(merged.get("login_timeout_ms", 15000))) if use_env else int(merged.get("login_timeout_ms", 15000)),
        post_login_wait_seconds=_env_int("AUTODL_POST_LOGIN_WAIT_SECONDS", int(merged.get("post_login_wait_seconds", 8))) if use_env else int(merged.get("post_login_wait_seconds", 8)),
        cache_file=_resolve_path(base_dir, cache_file_raw, default_cache_name),
        cache_max_age_seconds=_env_int("AUTODL_AUTH_CACHE_MAX_AGE_SECONDS", int(merged.get("cache_max_age_seconds", 86400))) if use_env else int(merged.get("cache_max_age_seconds", 86400)),
        lightweight_mode=_normalize_lightweight_mode(lightweight_mode_raw),
        runtime_auth_revalidate_seconds=int(merged.get("runtime_auth_revalidate_seconds", 0) or 0),
        force_refresh_min_interval_seconds=int(merged.get("force_refresh_min_interval_seconds", 0) or 0),
        auth_failure_backoff_seconds=int(merged.get("auth_failure_backoff_seconds", 0) or 0),
    )


def _primary_auth_from_accounts(accounts: list[AccountSettings]) -> AuthSettings:
    primary = next((account for account in accounts if account.enabled), accounts[0] if accounts else AccountSettings())
    return primary.to_auth_settings()


def load_settings(config_path: str | Path | None = None) -> Settings:
    path = Path(config_path) if config_path else Path("config.yaml")
    dotenv_path = path.parent / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path=dotenv_path)
    payload = _read_yaml(path)

    auth_payload = payload.get("auth", {})
    tasks_payload = payload.get("tasks", {})
    keeper_payload = tasks_payload.get("keeper", {})
    scheduled_payload = tasks_payload.get("scheduled_start", {})
    notifications_payload = payload.get("notifications", {})
    email_payload = notifications_payload.get("email", {})
    storage_payload = payload.get("storage", {})
    interactive_payload = payload.get("interactive", {})

    raw_accounts = payload.get("accounts") or []
    if raw_accounts:
        use_env = len(raw_accounts) == 1
        accounts = [
            _build_account(
                base_dir=path.parent,
                payload=account_payload,
                inherited_auth_payload=auth_payload,
                use_env=use_env,
                index=index,
                legacy_default_cache=False,
            )
            for index, account_payload in enumerate(raw_accounts)
        ]
    else:
        accounts = [
            _build_account(
                base_dir=path.parent,
                payload=auth_payload,
                inherited_auth_payload={},
                use_env=True,
                index=0,
                legacy_default_cache=True,
            )
        ]

    jobs = [_parse_scheduled_job(job) for job in scheduled_payload.get("jobs", [])]

    return Settings(
        auth=_primary_auth_from_accounts(accounts),
        accounts=accounts,
        storage=StorageSettings(
            database_file=_resolve_path(
                path.parent,
                os.getenv("AUTODL_DB_PATH", storage_payload.get("database_file", "")),
                "data/autodl-helper.db",
            )
        ),
        tasks=TaskSettings(
            keeper=KeeperSettings(
                enabled=keeper_payload.get("enabled", True),
                min_day=int(os.getenv("MIN_DAY", keeper_payload.get("min_day", 7))),
                shutdown_release_after_hours=int(keeper_payload.get("shutdown_release_after_hours", 360)),
                keeper_trigger_before_hours=int(keeper_payload.get("keeper_trigger_before_hours", 6)),
                interval_minutes=int(keeper_payload.get("interval_minutes", 60)),
                power_on_wait_seconds=int(keeper_payload.get("power_on_wait_seconds", 60)),
                power_off_wait_seconds=int(keeper_payload.get("power_off_wait_seconds", 5)),
                start_cooldown_minutes=int(keeper_payload.get("start_cooldown_minutes", 60)),
                stop_cooldown_minutes=int(keeper_payload.get("stop_cooldown_minutes", 360)),
                fallback_to_status_at=bool(keeper_payload.get("fallback_to_status_at", True)),
            ),
            scheduled_start=ScheduledStartSettings(
                enabled=scheduled_payload.get("enabled", False),
                poll_interval_seconds=int(scheduled_payload.get("poll_interval_seconds", 300)),
                jobs=jobs,
            ),
        ),
        notifications=NotificationSettings(
            pushplus=NotificationChannelSettings(
                enabled=notifications_payload.get("pushplus", {}).get("enabled", False),
                token=os.getenv("PUSHPLUS_TOKEN", notifications_payload.get("pushplus", {}).get("token", "")).strip(),
            ),
            serverchan=NotificationChannelSettings(
                enabled=notifications_payload.get("serverchan", {}).get("enabled", False),
                token=os.getenv("SERVERCHAN_SENDKEY", notifications_payload.get("serverchan", {}).get("token", "")).strip(),
            ),
            email=EmailSettings(
                enabled=email_payload.get("enabled", False),
                smtp_host=os.getenv("SMTP_HOST", email_payload.get("smtp_host", "")).strip(),
                smtp_port=int(os.getenv("SMTP_PORT", email_payload.get("smtp_port", 465))),
                username=os.getenv("SMTP_USERNAME", email_payload.get("username", "")).strip(),
                password=os.getenv("SMTP_PASSWORD", email_payload.get("password", "")).strip(),
                to=email_payload.get("to", []),
            ),
        ),
        interactive=InteractiveSettings(
            max_workers=max(1, int(interactive_payload.get("max_workers", 6) or 6)),
        ),
    )
