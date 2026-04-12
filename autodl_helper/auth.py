from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from autodl_helper.api import INSTANCE_URL, build_headers
from autodl_helper.auth_cache import load_cached_authorization, read_auth_cache, write_auth_cache
from autodl_helper.auth_login import (
    CAPTCHA_SELECTORS,
    CAPTCHA_TEXT_HINTS,
    DEFAULT_LOGIN_RETRIES,
    DEFAULT_LOGIN_TIMEOUT_MS,
    DEFAULT_POST_LOGIN_WAIT_SECONDS,
    LOGIN_BLOCKER_TEXT_HINTS,
    LOGIN_BUTTON_SELECTORS,
    LOGIN_URL,
    PASSWORD_INPUT_SELECTORS,
    PASSPORT_URL,
    PHONE_INPUT_SELECTORS,
    build_browser_launch_kwargs,
    describe_login_page,
    detect_login_blocker,
    fetch_token_via_playwright as _fetch_token_via_playwright,
    find_first_visible_locator,
    resolve_login_form as _resolve_login_form,
    run_single_login_attempt as _run_single_login_attempt,
)
from autodl_helper.auth_policy import (
    DEFAULT_RUNTIME_AUTH_REVALIDATE_SECONDS,
    LIGHTWEIGHT_POLICIES,
    AuthRuntimePolicy,
    resolve_auth_runtime_policy,
)
from autodl_helper.config import AuthSettings

logger = logging.getLogger(__name__)


class AuthError(RuntimeError):
    pass


RUNTIME_AUTHORIZATION: Optional[str] = None
RUNTIME_AUTHORIZATIONS: dict[str, str] = {}
RUNTIME_AUTH_VALIDATED_AT: dict[str, int] = {}
FORCE_REFRESH_LAST_ATTEMPT_AT: dict[str, int] = {}
FORCE_REFRESH_LAST_FAILURE_AT: dict[str, int] = {}


def _get_runtime_authorization(account_name: str) -> str:
    if account_name == "default":
        return (RUNTIME_AUTHORIZATION or "").strip()
    return (RUNTIME_AUTHORIZATIONS.get(account_name) or "").strip()


def get_runtime_authorization(account_name: str = "default") -> str:
    return _get_runtime_authorization(account_name)


def clear_runtime_authorization(account_name: str = "default") -> None:
    _set_runtime_authorization(account_name, "")
    RUNTIME_AUTH_VALIDATED_AT.pop(account_name, None)
    FORCE_REFRESH_LAST_ATTEMPT_AT.pop(account_name, None)
    FORCE_REFRESH_LAST_FAILURE_AT.pop(account_name, None)


def _set_runtime_authorization(account_name: str, authorization: str) -> None:
    global RUNTIME_AUTHORIZATION
    previous = _get_runtime_authorization(account_name)
    authorization = (authorization or "").strip()
    if authorization:
        RUNTIME_AUTHORIZATIONS[account_name] = authorization
    else:
        RUNTIME_AUTHORIZATIONS.pop(account_name, None)
    if account_name == "default":
        RUNTIME_AUTHORIZATION = authorization or None
    if not authorization or authorization != previous:
        RUNTIME_AUTH_VALIDATED_AT.pop(account_name, None)


def _mark_runtime_authorization_valid(account_name: str) -> None:
    RUNTIME_AUTH_VALIDATED_AT[account_name] = int(time.time())


def _is_runtime_authorization_recently_valid(account_name: str, authorization: str, *, reuse_seconds: int = DEFAULT_RUNTIME_AUTH_REVALIDATE_SECONDS) -> bool:
    if not authorization or _get_runtime_authorization(account_name) != authorization:
        return False
    validated_at = int(RUNTIME_AUTH_VALIDATED_AT.get(account_name, 0) or 0)
    if validated_at <= 0:
        return False
    return (time.time() - validated_at) < max(0, reuse_seconds)


def _recent_force_refresh_attempt(account_name: str, *, min_interval_seconds: int) -> bool:
    last_attempt_at = int(FORCE_REFRESH_LAST_ATTEMPT_AT.get(account_name, 0) or 0)
    if last_attempt_at <= 0:
        return False
    return (time.time() - last_attempt_at) < max(0, min_interval_seconds)


def _recent_force_refresh_failure(account_name: str, *, backoff_seconds: int) -> bool:
    last_failure_at = int(FORCE_REFRESH_LAST_FAILURE_AT.get(account_name, 0) or 0)
    if last_failure_at <= 0:
        return False
    return (time.time() - last_failure_at) < max(0, backoff_seconds)


def alert_auth_failure(message: str) -> None:
    border = "=" * 72
    logger.error(border)
    logger.error("AUTODL TOKEN 获取失败")
    logger.error(message)
    logger.error(border)
    print(f"\n{border}\nAUTODL TOKEN 获取失败\n{message}\n{border}\n", file=sys.stderr)


def validate_authorization(authorization: str, request_timeout: int = 30) -> bool:
    if not authorization:
        return False
    body = {
        "date_from": "",
        "date_to": "",
        "page_index": 1,
        "page_size": 1,
        "status": [],
        "charge_type": [],
    }
    try:
        response = requests.post(
            url=INSTANCE_URL,
            headers=build_headers(authorization),
            json=body,
            timeout=request_timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Authorization 校验请求失败: %s", exc)
        return False
    return response.json().get("code") == "Success"


def resolve_login_form(page, timeout_ms: int):
    return _resolve_login_form(page, timeout_ms, AuthError)


def run_single_login_attempt(
    phone: str,
    password: str,
    headed: bool,
    timeout_ms: int,
    post_login_wait_seconds: int,
) -> str:
    return _run_single_login_attempt(
        phone=phone,
        password=password,
        headed=headed,
        timeout_ms=timeout_ms,
        post_login_wait_seconds=post_login_wait_seconds,
        auth_error_cls=AuthError,
    )


def fetch_token_via_playwright(
    phone: str,
    password: str,
    headed: bool,
    timeout_ms: int,
    max_retries: int,
    post_login_wait_seconds: int,
) -> str:
    return _fetch_token_via_playwright(
        phone=phone,
        password=password,
        headed=headed,
        timeout_ms=timeout_ms,
        max_retries=max_retries,
        post_login_wait_seconds=post_login_wait_seconds,
        auth_error_cls=AuthError,
    )


def resolve_authorization(
    settings: AuthSettings,
    headed: bool = False,
    force_refresh: bool = False,
    *,
    store=None,
    account_name: str = "default",
) -> str:
    policy = resolve_auth_runtime_policy(settings)
    if not force_refresh:
        authorization = _get_runtime_authorization(account_name)
        if _is_runtime_authorization_recently_valid(
            account_name,
            authorization,
            reuse_seconds=policy.runtime_auth_revalidate_seconds,
        ):
            return authorization
        if validate_authorization(authorization):
            _set_runtime_authorization(account_name, authorization)
            _mark_runtime_authorization_valid(account_name)
            return authorization

        cached_authorization, cache_expired = load_cached_authorization(
            settings,
            store=store,
            account_name=account_name,
        )
        if cached_authorization and validate_authorization(cached_authorization):
            _set_runtime_authorization(account_name, cached_authorization)
            _mark_runtime_authorization_valid(account_name)
            cached_at = int(time.time())
            if store is not None:
                store.set_auth_cache(account_name, cached_authorization, cached_at)
            if cache_expired:
                write_auth_cache(settings.cache_file, cached_authorization, cached_at=cached_at)
            return cached_authorization

        authorization = settings.authorization.strip()
        if validate_authorization(authorization):
            _set_runtime_authorization(account_name, authorization)
            _mark_runtime_authorization_valid(account_name)
            cached_at = int(time.time())
            if store is not None:
                store.set_auth_cache(account_name, authorization, cached_at)
            return authorization

    if force_refresh and (policy.force_refresh_min_interval_seconds > 0 or policy.auth_failure_backoff_seconds > 0):
        runtime_authorization = _get_runtime_authorization(account_name)
        if runtime_authorization and policy.force_refresh_min_interval_seconds > 0 and _recent_force_refresh_attempt(
            account_name,
            min_interval_seconds=policy.force_refresh_min_interval_seconds,
        ):
            logger.info("轻量模式=%s：跳过高频 Playwright 强刷，复用现有 runtime token account=%s", policy.mode, account_name)
            return runtime_authorization
        if policy.auth_failure_backoff_seconds > 0 and _recent_force_refresh_failure(account_name, backoff_seconds=policy.auth_failure_backoff_seconds):
            raise AuthError(
                f"鉴权刷新仍在退避窗口内，暂不再次拉起 Playwright。account={account_name} mode={policy.mode} backoff={policy.auth_failure_backoff_seconds}s"
            )

    if not settings.autodl_phone or not settings.autodl_password:
        raise AuthError("Authorization 无效，且缺少 AUTODL_PHONE / AUTODL_PASSWORD 配置")

    FORCE_REFRESH_LAST_ATTEMPT_AT[account_name] = int(time.time())
    try:
        authorization = fetch_token_via_playwright(
            phone=settings.autodl_phone,
            password=settings.autodl_password,
            headed=headed,
            timeout_ms=settings.login_timeout_ms,
            max_retries=settings.login_retries,
            post_login_wait_seconds=settings.post_login_wait_seconds,
        )
    except Exception:
        FORCE_REFRESH_LAST_FAILURE_AT[account_name] = int(time.time())
        raise
    cached_at = int(time.time())
    _set_runtime_authorization(account_name, authorization)
    _mark_runtime_authorization_valid(account_name)
    FORCE_REFRESH_LAST_FAILURE_AT.pop(account_name, None)
    if store is not None:
        store.set_auth_cache(account_name, authorization, cached_at)
    write_auth_cache(settings.cache_file, authorization, cached_at=cached_at)
    return authorization


def inspect_auth_state(
    settings: AuthSettings,
    *,
    store=None,
    account_name: str = "default",
) -> dict[str, Any]:
    runtime_authorization = _get_runtime_authorization(account_name)
    store_payload = store.get_auth_cache(account_name) if store is not None else None
    file_payload = read_auth_cache(settings.cache_file)
    config_authorization = str(settings.authorization or "").strip()
    has_credentials = bool(settings.autodl_phone and settings.autodl_password)

    cached_authorization = ""
    cached_at = 0
    cache_source = "none"
    if store_payload and str(store_payload.get("authorization", "") or "").strip():
        cached_authorization = str(store_payload.get("authorization", "") or "").strip()
        cached_at = int(store_payload.get("cached_at", 0) or 0)
        cache_source = "sqlite-cache"
    elif file_payload and str(file_payload.get("authorization", "") or "").strip():
        cached_authorization = str(file_payload.get("authorization", "") or "").strip()
        cached_at = int(file_payload.get("cached_at", 0) or 0)
        cache_source = "file-cache"

    auth_source = "missing"
    if runtime_authorization:
        auth_source = "runtime"
    elif cached_authorization:
        auth_source = cache_source
    elif config_authorization:
        auth_source = "config"
    elif has_credentials:
        auth_source = "password-login-ready"

    status = "not_configured"
    if runtime_authorization:
        status = "logged_in"
    elif cached_authorization:
        status = "cached"
    elif config_authorization:
        status = "token_configured"
    elif has_credentials:
        status = "login_ready"

    runtime_validated_at = int(RUNTIME_AUTH_VALIDATED_AT.get(account_name, 0) or 0)
    return {
        "account_name": account_name,
        "status": status,
        "auth_source": auth_source,
        "has_runtime_token": bool(runtime_authorization),
        "has_cached_token": bool(cached_authorization),
        "has_config_token": bool(config_authorization),
        "has_credentials": has_credentials,
        "cache_file": settings.cache_file,
        "cached_at": cached_at,
        "cached_at_iso": datetime.fromtimestamp(cached_at, tz=timezone.utc).astimezone().isoformat(timespec="seconds") if cached_at else "",
        "runtime_validated_at": runtime_validated_at,
        "runtime_validated_at_iso": datetime.fromtimestamp(runtime_validated_at, tz=timezone.utc).astimezone().isoformat(timespec="seconds") if runtime_validated_at else "",
        "lightweight_mode": settings.lightweight_mode,
    }
