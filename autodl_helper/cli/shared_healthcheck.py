from __future__ import annotations

from pathlib import Path
from typing import Callable

from autodl_helper.core.config import AccountSettings, Settings
from autodl_helper.core.store import SQLiteStore

from .shared_accounts import build_client, create_store, get_enabled_accounts
from .shared_settings import validate_settings


def probe_path_writable(path: str | Path) -> bool:
    probe_path = Path(path)
    try:
        probe_path.parent.mkdir(parents=True, exist_ok=True)
        with open(probe_path, 'a', encoding='utf-8'):
            pass
        return True
    except OSError:
        return False


def collect_healthcheck_errors(
    *,
    settings: Settings,
    state_file: str | Path,
    lock_file: str | Path,
    smoke: bool,
    headed: bool,
    permission_probe: Callable[[str | Path], bool] = probe_path_writable,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
    get_enabled_accounts_fn: Callable[[Settings], list[AccountSettings]] = get_enabled_accounts,
    create_store_fn: Callable[[Settings], SQLiteStore] = create_store,
    build_client_fn: Callable[..., object] = build_client,
) -> list[str]:
    errors = validate_settings_fn(settings, purpose='debug_health')
    for account in get_enabled_accounts_fn(settings):
        if not permission_probe(account.cache_file):
            errors.append(f'Auth cache file is not writable for account {account.name}: {account.cache_file}')
    if not permission_probe(settings.storage.database_file):
        errors.append(f'SQLite database is not writable: {settings.storage.database_file}')
    if not permission_probe(state_file):
        errors.append(f'State file is not writable: {state_file}')
    if not permission_probe(lock_file):
        errors.append(f'Lock file is not writable: {lock_file}')
    try:
        store = create_store_fn(settings)
        if store.schema_version() != SQLiteStore.SCHEMA_VERSION:
            errors.append(f'Unexpected SQLite schema version: {store.schema_version()}')
    except Exception as exc:
        errors.append(f'SQLite check failed: {exc}')
        store = None
    if smoke:
        for account in get_enabled_accounts_fn(settings):
            try:
                client = build_client_fn(settings, headed, account=account, store=store)
                client.list_instances()
            except Exception as exc:
                errors.append(f'Smoke check failed for account {account.name}: {exc}')
    return errors


__all__ = ["probe_path_writable", "collect_healthcheck_errors"]
