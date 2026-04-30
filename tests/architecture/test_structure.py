from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


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


def test_refactor_facades_stay_thin():
    thin_files = [
        ROOT / 'autodl_helper/interactive/browse_instances.py',
        ROOT / 'autodl_helper/interactive/browse_records.py',
        ROOT / 'autodl_helper/interactive/menu_keeper.py',
        ROOT / 'autodl_helper/interactive/menu_scheduled.py',
        ROOT / 'autodl_helper/interactive/menu_diagnostics.py',
        ROOT / 'autodl_helper/interactive/support/keeper.py',
        ROOT / 'autodl_helper/interactive/support/scheduled.py',
        ROOT / 'autodl_helper/interactive/screens.py',
        ROOT / 'autodl_helper/cli/handlers.py',
        ROOT / 'autodl_helper/cli/shared.py',
    ]

    for path in thin_files:
        assert _top_level_defs(path) == [], f'{path} should remain a thin façade'


def test_no_import_star_back_to_interactive_app():
    paths = [
        *ROOT.joinpath('autodl_helper/interactive/features').rglob('*.py'),
        *ROOT.joinpath('autodl_helper/interactive/support').rglob('*.py'),
        ROOT / 'autodl_helper/interactive/browse_instances.py',
        ROOT / 'autodl_helper/interactive/browse_records.py',
        ROOT / 'autodl_helper/interactive/screens.py',
    ]

    for path in paths:
        text = path.read_text(encoding='utf-8')
        assert 'from .app import *' not in text
        assert 'from ..app import *' not in text
