from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import pytz
import requests

from ..auth.errors import extract_code_msg, is_business_auth_failure

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
                logger.warning("AutoDL 业务层鉴权失败: code=%s msg=%s", code, msg)
                if not refreshed and self._refresh_authorization():
                    refreshed = True
                    continue
            return payload

    def open_machine(self, instance_uuid: str, payload: str = "non_gpu") -> bool:
        body = {"instance_uuid": str(instance_uuid), "payload": payload}
        result = self.post_json(POWER_ON_URL, body)
        logger.info("uuid=%s power_on response=%s", instance_uuid, result)
        return result.get("code") == "Success"

    def close_machine(self, instance_uuid: str) -> bool:
        payload = {"instance_uuid": str(instance_uuid)}
        result = self.post_json(POWER_OFF_URL, payload)
        logger.info("uuid=%s power_off response=%s", instance_uuid, result)
        return result.get("code") == "Success"

    def list_instances(self, page: int = 1, page_size: int = 100) -> list[dict[str, Any]]:
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
            raise RuntimeError(f"failed to list instances: {result}")
        return result.get("data", {}).get("list", [])

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
