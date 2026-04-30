from __future__ import annotations

import sys
from typing import Any


_APP_MODULE_NAMES = ('autodl_helper.interactive.app', 'autodl_helper.interactive_app')


def _resolve_app_module():
    for module_name in _APP_MODULE_NAMES:
        app_module = sys.modules.get(module_name)
        if app_module is not None:
            return app_module
    return None


def _resolve_app_target(name: str, fallback):
    for module_name in _APP_MODULE_NAMES:
        app_module = sys.modules.get(module_name)
        if app_module is None:
            continue
        target = getattr(app_module, name, None)
        if target is None or target is fallback or type(target).__name__ == '_Proxy':
            continue
        return target
    return fallback


class _Proxy:
    __slots__ = ('_name', '_fallback')

    def __init__(self, name: str, fallback):
        self._name = name
        self._fallback = fallback

    def _target(self):
        return _resolve_app_target(self._name, self._fallback)

    def __call__(self, *args, **kwargs):
        return self._target()(*args, **kwargs)

    def __getattr__(self, attr):
        return getattr(self._target(), attr)


def _delegate(name: str, fallback):
    return _Proxy(name, fallback)


def _bind_app_globals(target_globals: dict[str, Any], *, exclude: set[str] | None = None) -> None:
    app_module = _resolve_app_module()
    if app_module is None:
        return
    excluded = exclude or set()
    for name, value in app_module.__dict__.items():
        if name.startswith('__') or name in excluded:
            continue
        target_globals[name] = value


__all__ = ['_resolve_app_module', '_resolve_app_target', '_delegate', '_bind_app_globals']
