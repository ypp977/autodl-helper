from __future__ import annotations

from dataclasses import dataclass

from ..config import AuthSettings


DEFAULT_RUNTIME_AUTH_REVALIDATE_SECONDS = 300


@dataclass(frozen=True)
class AuthRuntimePolicy:
    mode: str
    runtime_auth_revalidate_seconds: int
    force_refresh_min_interval_seconds: int
    auth_failure_backoff_seconds: int


LIGHTWEIGHT_POLICIES: dict[str, AuthRuntimePolicy] = {
    "off": AuthRuntimePolicy(
        mode="off",
        runtime_auth_revalidate_seconds=0,
        force_refresh_min_interval_seconds=0,
        auth_failure_backoff_seconds=0,
    ),
    "normal": AuthRuntimePolicy(
        mode="normal",
        runtime_auth_revalidate_seconds=60,
        force_refresh_min_interval_seconds=90,
        auth_failure_backoff_seconds=30,
    ),
    "aggressive": AuthRuntimePolicy(
        mode="aggressive",
        runtime_auth_revalidate_seconds=180,
        force_refresh_min_interval_seconds=180,
        auth_failure_backoff_seconds=60,
    ),
}


def resolve_auth_runtime_policy(settings: AuthSettings) -> AuthRuntimePolicy:
    base = LIGHTWEIGHT_POLICIES.get(settings.lightweight_mode, LIGHTWEIGHT_POLICIES["off"])
    return AuthRuntimePolicy(
        mode=base.mode,
        runtime_auth_revalidate_seconds=settings.runtime_auth_revalidate_seconds or base.runtime_auth_revalidate_seconds,
        force_refresh_min_interval_seconds=settings.force_refresh_min_interval_seconds or base.force_refresh_min_interval_seconds,
        auth_failure_backoff_seconds=settings.auth_failure_backoff_seconds or base.auth_failure_backoff_seconds,
    )
