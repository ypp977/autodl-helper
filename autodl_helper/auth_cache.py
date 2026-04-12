from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from autodl_helper.config import AuthSettings

logger = logging.getLogger(__name__)


def read_auth_cache(path: str | Path) -> dict[str, object] | None:
    cache_path = Path(path)
    if not cache_path.exists():
        return None
    try:
        return json.loads(cache_path.read_text())
    except Exception as exc:
        logger.warning("读取 auth cache 失败: %s", exc)
        return None


def write_auth_cache(path: str | Path, authorization: str, *, cached_at: int | None = None) -> None:
    cache_path = Path(path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    payload = {
        "authorization": authorization,
        "cached_at": cached_at if cached_at is not None else int(time.time()),
    }
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    try:
        os.chmod(temp_path, 0o600)
    except OSError:
        logger.warning("无法设置 auth cache 临时文件权限: %s", temp_path)
    os.replace(temp_path, cache_path)
    try:
        os.chmod(cache_path, 0o600)
    except OSError:
        logger.warning("无法设置 auth cache 文件权限: %s", cache_path)


def load_cached_authorization(settings: AuthSettings, *, store=None, account_name: str = "default") -> tuple[str | None, bool]:
    payload: dict[str, object] | None = None
    if store is not None:
        payload = store.get_auth_cache(account_name)
    if not payload:
        payload = read_auth_cache(settings.cache_file)
    if not payload:
        return None, False
    authorization = str(payload.get("authorization", "") or "").strip()
    cached_at = int(payload.get("cached_at", 0) or 0)
    expired = bool(cached_at) and (time.time() - cached_at > settings.cache_max_age_seconds)
    return authorization or None, expired
