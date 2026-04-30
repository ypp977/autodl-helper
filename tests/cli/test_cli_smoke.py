from importlib import import_module


def test_cli_main_is_importable():
    cli = import_module('autodl_helper.cli')
    assert callable(cli.main)


def test_cli_main_can_run_with_tracemalloc_env(monkeypatch, tmp_path):
    cli = import_module('autodl_helper.cli')
    output = tmp_path / 'trace.log'
    monkeypatch.setenv('AUTODL_HELPER_TRACEMALLOC', '1')
    monkeypatch.setenv('AUTODL_HELPER_TRACEMALLOC_OUTPUT', str(output))

    assert cli.main(['--help']) == 0
    assert output.exists()
