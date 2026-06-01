from __future__ import annotations

from typing import Any, Callable

from autodl_helper.core.api import AutoDLClient
from autodl_helper.core.auth import inspect_auth_state, resolve_authorization
from autodl_helper.core.config import AccountSettings, Settings
from autodl_helper.core.store import SQLiteStore
from autodl_helper.security import redact_sensitive, redact_text


def get_enabled_accounts(settings: Settings) -> list[AccountSettings]:
    if settings.accounts:
        return [account for account in settings.accounts if account.enabled]
    return [
        AccountSettings(
            name='default',
            enabled=True,
            authorization=settings.auth.authorization,
            autodl_phone=settings.auth.autodl_phone,
            autodl_password=settings.auth.autodl_password,
            login_retries=settings.auth.login_retries,
            login_timeout_ms=settings.auth.login_timeout_ms,
            post_login_wait_seconds=settings.auth.post_login_wait_seconds,
            cache_file=settings.auth.cache_file,
            cache_max_age_seconds=settings.auth.cache_max_age_seconds,
            lightweight_mode=settings.auth.lightweight_mode,
            runtime_auth_revalidate_seconds=settings.auth.runtime_auth_revalidate_seconds,
            force_refresh_min_interval_seconds=settings.auth.force_refresh_min_interval_seconds,
            auth_failure_backoff_seconds=settings.auth.auth_failure_backoff_seconds,
        )
    ]


def select_accounts(
    settings: Settings,
    account_name: str | None = None,
    *,
    require_explicit_for_multi: bool = False,
    get_enabled_accounts_fn: Callable[[Settings], list[AccountSettings]] = get_enabled_accounts,
) -> list[AccountSettings]:
    accounts = get_enabled_accounts_fn(settings)
    if not accounts:
        raise ValueError('At least one enabled account is required.')
    if account_name:
        selected = [account for account in accounts if account.name == account_name]
        if not selected:
            raise ValueError(f'Account not found or disabled: {account_name}')
        return selected
    if require_explicit_for_multi and len(accounts) > 1:
        raise ValueError('检测到多个启用账号，请使用 --account 明确指定账号。')
    return accounts


def create_store(
    settings: Settings,
    store_cls: type[SQLiteStore] = SQLiteStore,
    *,
    get_enabled_accounts_fn: Callable[[Settings], list[AccountSettings]] = get_enabled_accounts,
) -> SQLiteStore:
    store = store_cls(settings.storage.database_file)
    store.init_schema()
    store.register_accounts(settings.accounts or get_enabled_accounts_fn(settings))
    return store


def _account_status_label(status: str) -> str:
    mapping = {
        'logged_in': '已登录(runtime)',
        'cached': '已缓存登录',
        'token_configured': '已配置 token',
        'login_ready': '可密码登录',
        'not_configured': '未配置登录信息',
    }
    return mapping.get(status, status or '-')


def _account_source_label(source: str) -> str:
    mapping = {
        'runtime': 'runtime',
        'sqlite-cache': 'sqlite-cache',
        'file-cache': 'file-cache',
        'config': 'config',
        'password-login-ready': 'password',
        'missing': '-',
    }
    return mapping.get(source, source or '-')


def account_status_rows(
    settings: Settings,
    store: SQLiteStore,
    *,
    account_name: str | None = None,
    select_accounts_fn: Callable[..., list[AccountSettings]] = select_accounts,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for account in select_accounts_fn(settings, account_name):
        state = inspect_auth_state(account.to_auth_settings(), store=store, account_name=account.name)
        rows.append(
            {
                'account_name': account.name,
                'enabled': account.enabled,
                'status': state['status'],
                'status_label': _account_status_label(str(state['status'])),
                'auth_source': state['auth_source'],
                'auth_source_label': _account_source_label(str(state['auth_source'])),
                'cached_at': state['cached_at'],
                'cached_at_iso': state['cached_at_iso'],
                'has_credentials': state['has_credentials'],
                'has_config_token': state['has_config_token'],
                'cache_file': state['cache_file'],
                'lightweight_mode': state['lightweight_mode'],
            }
        )
    return rows


def record_auth_event(store: SQLiteStore | None, account_name: str, payload: dict[str, object]) -> None:
    if store is None:
        return
    sanitized = redact_sensitive(dict(payload))
    code = redact_text(sanitized.get('code', ''), max_length=120)
    msg = redact_text(sanitized.get('msg', ''), max_length=500)
    store.add_event(
        account_name,
        'auth',
        'warning',
        '命中鉴权失败刷新判定',
        code=code,
        msg=msg,
        payload=sanitized,
    )


def create_client(
    settings: Settings,
    headed: bool,
    account: AccountSettings | None = None,
    store: SQLiteStore | None = None,
    *,
    get_enabled_accounts_fn: Callable[[Settings], list[AccountSettings]] = get_enabled_accounts,
    resolve_authorization_fn: Callable[..., str] = resolve_authorization,
    client_cls: type[AutoDLClient] = AutoDLClient,
):
    selected_account = account or get_enabled_accounts_fn(settings)[0]
    auth_settings = selected_account.to_auth_settings()
    authorization = resolve_authorization_fn(
        auth_settings,
        headed=headed,
        store=store,
        account_name=selected_account.name,
    )
    return client_cls(
        authorization=authorization,
        min_day=settings.tasks.keeper.min_day,
        auth_refresh_callback=lambda: resolve_authorization_fn(
            auth_settings,
            headed=headed,
            force_refresh=True,
            store=store,
            account_name=selected_account.name,
        ),
        auth_failure_event_callback=lambda payload: record_auth_event(store, selected_account.name, payload),
    )


def build_client(
    settings: Settings,
    headed: bool,
    account: AccountSettings | None = None,
    store: SQLiteStore | None = None,
    *,
    create_client_fn: Callable[..., object] = create_client,
):
    try:
        return create_client_fn(settings, headed, account=account, store=store)
    except TypeError:
        return create_client_fn(settings, headed)


__all__ = [
    "get_enabled_accounts",
    "select_accounts",
    "create_store",
    "_account_status_label",
    "_account_source_label",
    "account_status_rows",
    "record_auth_event",
    "create_client",
    "build_client",
]
