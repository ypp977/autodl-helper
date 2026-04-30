from __future__ import annotations

from ...support.delegates import _bind_app_globals
from ...support.keeper import _InteractiveCancel, _persist_keeper_changes, _print_execution_summary, _prompt_keeper_settings


def _edit_keeper_rules(
    *,
    args,
    settings,
    account_label: str,
    store,
    load_settings_fn,
    validate_settings_fn,
    request_reload_fn,
):
    _bind_app_globals(globals(), exclude={'_edit_keeper_rules'})
    try:
        updated_keeper = _prompt_keeper_settings(settings.tasks.keeper)
        _persist_keeper_changes(
            config_path=args.config,
            settings=settings,
            load_settings_fn=load_settings_fn,
            validate_settings_fn=validate_settings_fn,
            keeper_settings=updated_keeper,
        )
        settings = load_settings_fn(args.config)
        request_reload_fn(store)
        return settings, updated_keeper
    except _InteractiveCancel:
        return settings, None
    except ValueError as exc:
        _print_execution_summary('更新失败', detail=str(exc))
        return settings, None
