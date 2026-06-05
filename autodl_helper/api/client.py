from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import pytz
import requests

from ..auth.errors import extract_code_msg, is_business_auth_failure
from ..security import redact_sensitive, redact_text

INSTANCE_URL = "https://www.autodl.com/api/v1/instance"
POWER_ON_URL = "https://www.autodl.com/api/v1/instance/power_on"
POWER_OFF_URL = "https://www.autodl.com/api/v1/instance/power_off"
ASIA_SHANGHAI = pytz.timezone("Asia/Shanghai")

logger = logging.getLogger(__name__)


def build_headers(authorization: str) -> dict[str, str]:
    return {
        "Authorization": authorization,
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    }


@dataclass
class AutoDLClient:
    authorization: str
    min_day: int
    request_timeout: int = 30
    session: Any = field(default_factory=requests.Session)
    auth_refresh_callback: Callable[[], str] | None = None
    auth_failure_event_callback: Callable[[dict[str, Any]], None] | None = None
    last_power_on_response: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    last_power_off_response: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def _refresh_authorization(self) -> bool:
        if self.auth_refresh_callback is None:
            return False
        new_authorization = self.auth_refresh_callback()
        if not new_authorization:
            return False
        self.authorization = new_authorization
        logger.info("Authorization refreshed for AutoDL client")
        return True

    def _emit_auth_failure_event(self, payload: dict[str, Any]) -> None:
        if self.auth_failure_event_callback is None:
            return
        try:
            self.auth_failure_event_callback(payload)
        except Exception:  # pragma: no cover - logging callback must not break request flow
            logger.exception("记录 AutoDL 鉴权失败事件时出错")

    def post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        refreshed = False
        while True:
            response = self.session.post(
                url=url,
                headers=build_headers(self.authorization),
                json=body,
                timeout=self.request_timeout,
            )
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code in {401, 403}:
                    self._emit_auth_failure_event({"code": str(status_code), "msg": "http authorization failure", "url": url})
                if not refreshed and status_code in {401, 403} and self._refresh_authorization():
                    refreshed = True
                    continue
                raise
            payload = response.json()
            code, msg = extract_code_msg(payload)
            if str(code or '').strip().lower() != 'success' and (code or msg):
                self._emit_auth_failure_event(payload)
            if is_business_auth_failure(payload):
                logger.warning("AutoDL 业务层鉴权失败: code=%s msg=%s", redact_text(code, max_length=120), redact_text(msg))
                if not refreshed and self._refresh_authorization():
                    refreshed = True
                    continue
            return payload

    def open_machine(self, instance_uuid: str, payload: str = "non_gpu") -> bool:
        body = {"instance_uuid": str(instance_uuid), "payload": payload}
        result = self.post_json(POWER_ON_URL, body)
        self.last_power_on_response = result
        logger.info("uuid=%s power_on response=%s", instance_uuid, redact_sensitive(result))
        return result.get("code") == "Success"

    def close_machine(self, instance_uuid: str) -> bool:
        payload = {"instance_uuid": str(instance_uuid)}
        result = self.post_json(POWER_OFF_URL, payload)
        self.last_power_off_response = result
        logger.info("uuid=%s power_off response=%s", instance_uuid, redact_sensitive(result))
        return result.get("code") == "Success"

    def list_instances(self, page: int = 1, page_size: int = 100) -> list[dict[str, Any]]:
        if page != 1:
            result = self._list_instances_page(page=page, page_size=page_size)
            return _extract_instance_rows(result)

        rows: list[dict[str, Any]] = []
        current_page = 1
        max_pages = 100
        while current_page <= max_pages:
            result = self._list_instances_page(page=current_page, page_size=page_size)
            current_rows = _extract_instance_rows(result)
            rows.extend(current_rows)
            next_page = _next_instance_page(
                result,
                current_page=current_page,
                page_size=page_size,
                fetched_count=len(rows),
                current_count=len(current_rows),
            )
            if next_page is None:
                break
            current_page = next_page
        return rows

    def _list_instances_page(self, *, page: int, page_size: int) -> dict[str, Any]:
        body = {
            "date_from": "",
            "date_to": "",
            "page_index": page,
            "page_size": page_size,
            "status": [],
            "charge_type": [],
        }
        result = self.post_json(INSTANCE_URL, body)
        if result.get("code") != "Success":
            raise RuntimeError(f"failed to list instances: {redact_text(redact_sensitive(result))}")
        return result

    @staticmethod
    def running_days(status_at: str, now: datetime | None = None) -> int:
        now = now or datetime.now(ASIA_SHANGHAI)
        status_at_time = datetime.fromisoformat(status_at)
        return (now - status_at_time).days

    @staticmethod
    def days_until_release(release_at: str, now: datetime | None = None) -> int:
        now = now or datetime.now(ASIA_SHANGHAI)
        release_at_time = datetime.fromisoformat(release_at.replace(" ", "T"))
        return (release_at_time - now).days


def _instance_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data", {})
    return data if isinstance(data, dict) else {}


def _extract_instance_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _instance_data(payload).get("list", [])
    return rows if isinstance(rows, list) else []


def _metadata_int(data: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = data.get(key)
        if value in {None, ""}:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _next_instance_page(
    payload: dict[str, Any],
    *,
    current_page: int,
    page_size: int,
    fetched_count: int,
    current_count: int,
) -> int | None:
    if current_count <= 0:
        return None
    data = _instance_data(payload)
    next_page = _metadata_int(data, "next_page", "nextPage", "next_page_index")
    if next_page is not None and next_page > current_page:
        return next_page
    has_next = data.get("has_next", data.get("hasNext"))
    if isinstance(has_next, bool):
        return current_page + 1 if has_next else None
    page_total = _metadata_int(data, "page_total", "pageTotal", "total_page", "total_pages")
    if page_total is not None:
        return current_page + 1 if current_page < page_total else None
    total = _metadata_int(data, "total", "count", "total_count", "totalCount")
    if total is not None and fetched_count < total:
        return current_page + 1
    return None
