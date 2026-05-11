"""Stable core import surface for AutoDL helper internals.

This package exposes the real implementation entrypoints for modules that used
legacy top-level shims. New internal imports should prefer these modules.
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

_CORE_MODULES = {
    "api": "autodl_helper.core.api",
    "auth": "autodl_helper.core.auth",
    "config": "autodl_helper.core.config",
    "models": "autodl_helper.core.models",
    "store": "autodl_helper.core.store",
}


def __getattr__(name: str) -> ModuleType:
    target = _CORE_MODULES.get(name)
    if target is None:
        raise AttributeError(name)
    module = import_module(target)
    globals()[name] = module
    return module


__all__ = [*_CORE_MODULES]
