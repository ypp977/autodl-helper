from importlib import import_module


def test_cli_support_modules_import():
    module_names = [
        'autodl_helper.cli.shared',
        'autodl_helper.cli.shared_accounts',
        'autodl_helper.cli.shared_edit',
        'autodl_helper.cli.shared_healthcheck',
        'autodl_helper.cli.shared_notifications',
        'autodl_helper.cli.shared_scheduled',
        'autodl_helper.cli.shared_settings',
        'autodl_helper.cli.handlers',
    ]

    for module_name in module_names:
        module = import_module(module_name)
        assert module is not None


def test_cli_support_exports_expected_helpers():
    shared = import_module('autodl_helper.cli.shared')
    shared_accounts = import_module('autodl_helper.cli.shared_accounts')
    shared_healthcheck = import_module('autodl_helper.cli.shared_healthcheck')
    shared_notifications = import_module('autodl_helper.cli.shared_notifications')
    shared_settings = import_module('autodl_helper.cli.shared_settings')

    assert hasattr(shared, 'select_accounts')
    assert hasattr(shared, 'create_store')
    assert hasattr(shared, 'build_notifiers')
    assert hasattr(shared_accounts, 'get_enabled_accounts')
    assert hasattr(shared_healthcheck, 'collect_healthcheck_errors')
    assert hasattr(shared_notifications, 'build_named_notifiers')
    assert hasattr(shared_settings, 'validate_settings')
    assert hasattr(shared_settings, 'compute_cycle_interval_seconds')
