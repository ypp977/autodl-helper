from __future__ import annotations

import sys
from types import ModuleType

from autodl_helper.interactive import dialogs
from autodl_helper.interactive import app_runtime
from autodl_helper.interactive.support import delegates


def test_resolve_app_target_skips_proxy_objects(monkeypatch):
    app_module = ModuleType('autodl_helper.interactive.app')

    class _Proxy:
        pass

    app_module.target = _Proxy()
    monkeypatch.setitem(sys.modules, 'autodl_helper.interactive.app', app_module)

    assert delegates._resolve_app_target('target', fallback='fallback') == 'fallback'

    app_module.target = 'live-target'
    assert delegates._resolve_app_target('target', fallback='fallback') == 'live-target'


def test_delegate_tracks_live_app_updates(monkeypatch):
    app_module = ModuleType('autodl_helper.interactive.app')
    app_module.answer = lambda: 'live'
    monkeypatch.setitem(sys.modules, 'autodl_helper.interactive.app', app_module)

    proxy = delegates._delegate('answer', lambda: 'fallback')

    assert proxy() == 'live'

    app_module.answer = lambda: 'updated'
    assert proxy() == 'updated'


def test_bind_app_globals_copies_public_symbols(monkeypatch):
    app_module = ModuleType('autodl_helper.interactive.app')
    app_module.answer = 42
    app_module._private = 'ignore-me'
    app_module.__hidden__ = 'ignore-me-too'
    monkeypatch.setitem(sys.modules, 'autodl_helper.interactive.app', app_module)

    target_globals = {'existing': True}
    delegates._bind_app_globals(target_globals, exclude={'existing'})

    assert target_globals['existing'] is True
    assert target_globals['answer'] == 42
    assert target_globals['_private'] == 'ignore-me'
    assert '__hidden__' not in target_globals


def test_app_runtime_uses_shared_delegate_helpers():
    assert app_runtime._resolve_app_module is delegates._resolve_app_module
    assert app_runtime._resolve_app_target is delegates._resolve_app_target
    assert app_runtime._delegate is delegates._delegate
    assert app_runtime._bind_app_globals is delegates._bind_app_globals


def test_delegate_returns_module_level_proxy_type():
    proxy_a = delegates._delegate('a', lambda: 'a')
    proxy_b = delegates._delegate('b', lambda: 'b')

    assert type(proxy_a) is type(proxy_b)


def test_dialogs_delegate_caches_proxy(monkeypatch):
    dialogs._DELEGATE_CACHE.clear()
    proxy_a = dialogs._delegate('_show_cursor')
    proxy_b = dialogs._delegate('_show_cursor')

    assert proxy_a is proxy_b
