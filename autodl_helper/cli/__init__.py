from __future__ import annotations

from importlib import import_module
from typing import Any


class _LazySymbol:
    def __init__(self, name: str) -> None:
        self._name = name

    def _resolve(self) -> Any:
        module = import_module('.app', __name__)
        return getattr(module, self._name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._resolve()(*args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._resolve(), item)

    def __repr__(self) -> str:
        return f'<lazy symbol {self._name}>'


main = _LazySymbol('main')


def __getattr__(name: str) -> Any:
    if name.startswith('__'):
        raise AttributeError(name)
    value = _LazySymbol(name)
    globals()[name] = value
    return value
