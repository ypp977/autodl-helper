from importlib import import_module


def test_cli_main_is_importable():
    cli = import_module('autodl_helper.cli')
    assert callable(cli.main)
