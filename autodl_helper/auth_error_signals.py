from __future__ import annotations

from typing import Any

AUTH_CODE_SIGNALS = {
    "credentialrejected",
    "unauthorized",
    "forbidden",
    "invalid_token",
    "token_expired",
    "token_invalid",
    "login_required",
    "not_login",
    "not_logged_in",
    "auth_failed",
}

AUTH_MESSAGE_SIGNALS = (
    "credential rejected",
    "unauthorized",
    "authorization",
    "auth",
    "login",
    "token",
    "expired",
    "expire",
    "invalid",
    "signin",
    "sign in",
    "重新登录",
    "未登录",
    "登录失效",
    "登录过期",
    "鉴权失败",
)


def extract_code_msg(payload: dict[str, Any]) -> tuple[str, str]:
    code = str(payload.get("code", "") or "").strip()
    msg = str(payload.get("msg", "") or "").strip()
    return code, msg


def classify_auth_signal(code: str, msg: str) -> tuple[bool, str]:
    code_lower = str(code or "").strip().lower()
    msg_lower = str(msg or "").strip().lower()
    if not code_lower and not msg_lower:
        return False, "none"
    if code_lower == "success":
        return False, "success"
    if code_lower in AUTH_CODE_SIGNALS:
        return True, "code"
    combined = f"{code_lower} {msg_lower}".strip()
    if any(signal in combined for signal in AUTH_MESSAGE_SIGNALS):
        return True, "message"
    return False, "unmapped"


def is_business_auth_failure(payload: dict[str, Any]) -> bool:
    code, msg = extract_code_msg(payload)
    matched, _source = classify_auth_signal(code, msg)
    return matched
