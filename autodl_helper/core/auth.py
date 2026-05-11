"""Core authentication exports."""

from autodl_helper.auth import (
    AuthError,
    RUNTIME_AUTHORIZATION,
    RUNTIME_AUTHORIZATIONS,
    RUNTIME_AUTH_VALIDATED_AT,
    FORCE_REFRESH_LAST_ATTEMPT_AT,
    FORCE_REFRESH_LAST_FAILURE_AT,
    alert_auth_failure,
    clear_runtime_authorization,
    fetch_token_via_playwright,
    get_runtime_authorization,
    inspect_auth_state,
    resolve_authorization,
    resolve_login_form,
    run_single_login_attempt,
    validate_authorization,
)
from autodl_helper.auth.cache import load_cached_authorization, read_auth_cache, write_auth_cache
from autodl_helper.auth.error_signals import AUTH_CODE_SIGNALS, AUTH_MESSAGE_SIGNALS
from autodl_helper.auth.errors import classify_auth_signal, extract_code_msg, is_business_auth_failure
from autodl_helper.auth.login import (
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
    find_first_visible_locator,
)
from autodl_helper.auth.policy import (
    DEFAULT_RUNTIME_AUTH_REVALIDATE_SECONDS,
    LIGHTWEIGHT_POLICIES,
    AuthRuntimePolicy,
    resolve_auth_runtime_policy,
)
