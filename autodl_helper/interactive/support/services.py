from __future__ import annotations

from typing import Any

from autodl_helper.services.manager import service_status as _service_status
from autodl_helper.services.manager import start_service as _start_service
from autodl_helper.services.manager import stop_service as _stop_service

DEFAULT_SERVICE_LABEL = 'autodl-helper'
_SERVICE_CONFIG_PATH = 'config.yaml'


def read_launch_agent_status(config_path: str | None = None) -> dict[str, Any]:
    return _service_status(config_path=config_path or _SERVICE_CONFIG_PATH)


def start_launch_agent(config_path: str | None = None):
    return _start_service(config_path=config_path or _SERVICE_CONFIG_PATH)


def stop_launch_agent(config_path: str | None = None):
    return _stop_service(config_path=config_path or _SERVICE_CONFIG_PATH)


__all__ = [
    'DEFAULT_SERVICE_LABEL',
    'read_launch_agent_status',
    'start_launch_agent',
    'stop_launch_agent',
]
