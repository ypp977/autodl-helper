"""autodl-helper package metadata and legacy module aliases."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

__version__ = "0.1.0"

_LEGACY_MODULE_ALIASES = {
    "api": "autodl_helper.api.client",
    "auth": "autodl_helper.auth",
    "cli": "autodl_helper.cli.app",
    "cli_handlers": "autodl_helper.cli.handlers",
    "cli_parser": "autodl_helper.cli.parser",
    "cli_renderers": "autodl_helper.cli.renderers",
    "config": "autodl_helper.config.loader",
    "interactive_actions": "autodl_helper.interactive.actions",
    "interactive_app": "autodl_helper.interactive.app",
    "interactive_runtime": "autodl_helper.interactive.runtime",
    "interactive_views": "autodl_helper.interactive.views",
    "notify": "autodl_helper.notify.notifier",
    "service_launchd": "autodl_helper.service_launchd",
}


def __getattr__(name: str) -> ModuleType:
    target = _LEGACY_MODULE_ALIASES.get(name)
    if target is None:
        raise AttributeError(name)
    module = import_module(target)
    globals()[name] = module
    return module


__all__ = ["__version__", *_LEGACY_MODULE_ALIASES]
