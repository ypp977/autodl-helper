from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _top_level_defs(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return names


def _import_specs(path: Path) -> list[tuple[str, str, int]]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    specs: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                specs.append(('import', alias.name, 0))
        elif isinstance(node, ast.ImportFrom):
            specs.append(('from', node.module or '', int(node.level or 0)))
    return specs


def _assert_no_forbidden_imports(path: Path, forbidden: set[str], forbidden_relative: set[str]) -> None:
    offenders: list[str] = []
    for kind, module, level in _import_specs(path):
        if kind == 'import':
            if module in forbidden:
                offenders.append(module)
        elif kind == 'from':
            if module in forbidden:
                offenders.append(module)
            if level and module in forbidden_relative:
                offenders.append('.' * level + module)
    assert not offenders, f'{path} imports forbidden entrypoints: {offenders}'


def test_cli_facades_stay_thin():
    thin_files = [
        ROOT / 'autodl_helper/cli/shared.py',
    ]

    for path in thin_files:
        assert _top_level_defs(path) == [], f'{path} should remain a thin façade'


def test_cli_shared_and_command_layers_do_not_import_cli_entrypoints():
    forbidden = {'autodl_helper.cli.app'}
    forbidden_relative = {'app'}
    paths = [
        *ROOT.joinpath('autodl_helper/cli').glob('shared*.py'),
        *ROOT.joinpath('autodl_helper/cli/commands').rglob('*.py'),
    ]

    for path in paths:
        if path.name in {'app.py'}:
            continue
        _assert_no_forbidden_imports(path, forbidden, forbidden_relative)
