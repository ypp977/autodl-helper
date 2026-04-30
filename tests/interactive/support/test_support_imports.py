from importlib import import_module


def test_interactive_support_modules_import():
    module_names = [
        'autodl_helper.interactive.support',
        'autodl_helper.interactive.support.delegates',
        'autodl_helper.interactive.support.keeper',
        'autodl_helper.interactive.support.rendering',
        'autodl_helper.interactive.support.snapshots',
        'autodl_helper.interactive.support.scheduled',
        'autodl_helper.interactive.support.services',
    ]

    for module_name in module_names:
        module = import_module(module_name)
        assert module is not None


def test_interactive_support_exports_expected_symbols():
    support = import_module('autodl_helper.interactive.support')
    delegates = import_module('autodl_helper.interactive.support.delegates')
    keeper = import_module('autodl_helper.interactive.support.keeper')
    snapshots = import_module('autodl_helper.interactive.support.snapshots')
    scheduled = import_module('autodl_helper.interactive.support.scheduled')
    rendering = import_module('autodl_helper.interactive.support.rendering')
    services = import_module('autodl_helper.interactive.support.services')
    from autodl_helper.interactive.support import _account_label, _browse_snapshot_list, _render_scoped_list_page, _show_result_screen_for

    assert hasattr(support, '_delegate')
    assert hasattr(support, '_browse_snapshot_list')
    assert hasattr(support, '_render_scoped_list_page')
    assert hasattr(delegates, '_resolve_app_target')
    assert hasattr(keeper, '_show_result_screen')
    assert hasattr(scheduled, '_show_result_screen')
    assert hasattr(snapshots, '_choose_menu_with_refresh')
    assert hasattr(rendering, '_show_result_screen_for')
    assert hasattr(services, 'read_launch_agent_status')
    assert _browse_snapshot_list is support._browse_snapshot_list
    assert _account_label is support._account_label
    assert _render_scoped_list_page is support._render_scoped_list_page
    assert _show_result_screen_for is support._show_result_screen_for
