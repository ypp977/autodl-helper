from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / 'autodl_helper'


def _top_level_defs(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return names


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix('')
    parts = list(relative.parts)
    if parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts)


def _resolve_from_import(path: Path, module: str, level: int) -> str:
    if level == 0:
        return module

    current_parts = _module_name(path).split('.')
    if path.name != '__init__.py':
        current_parts = current_parts[:-1]
    if level > 1:
        current_parts = current_parts[: -(level - 1)]
    if module:
        current_parts.extend(module.split('.'))
    return '.'.join(part for part in current_parts if part)


def _import_specs(path: Path) -> list[tuple[str, str, int, int, bool]]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    specs: list[tuple[str, str, int, int, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                specs.append(('import', alias.name, 0, node.lineno, False))
        elif isinstance(node, ast.ImportFrom):
            is_star = any(alias.name == '*' for alias in node.names)
            specs.append(('from', node.module or '', int(node.level or 0), node.lineno, is_star))
    return specs


def _autodl_python_files() -> list[Path]:
    return sorted(
        path
        for path in PACKAGE_ROOT.rglob('*.py')
        if '__pycache__' not in path.parts
    )


def _assert_no_forbidden_imports(paths: list[Path], forbidden_prefixes: tuple[str, ...]) -> None:
    offenders: list[str] = []
    for path in paths:
        for kind, module, level, lineno, _is_star in _import_specs(path):
            imported = module if kind == 'import' else _resolve_from_import(path, module, level)
            if any(
                imported == prefix or imported.startswith(f'{prefix}.')
                for prefix in forbidden_prefixes
            ):
                offenders.append(f'{path.relative_to(ROOT)}:{lineno} imports {imported}')
    assert not offenders, 'forbidden architecture imports:\n' + '\n'.join(offenders)


def test_autodl_helper_does_not_use_import_star():
    offenders: list[str] = []
    for path in _autodl_python_files():
        for kind, module, level, lineno, is_star in _import_specs(path):
            if kind == 'from' and is_star:
                imported = _resolve_from_import(path, module, level)
                offenders.append(f'{path.relative_to(ROOT)}:{lineno} from {imported} import *')
    assert not offenders, 'wildcard imports are forbidden:\n' + '\n'.join(offenders)


def test_core_tasks_and_services_do_not_import_cli_or_ui():
    paths = [
        *PACKAGE_ROOT.joinpath('core').rglob('*.py'),
        *PACKAGE_ROOT.joinpath('tasks').rglob('*.py'),
        *PACKAGE_ROOT.joinpath('services').rglob('*.py'),
    ]
    _assert_no_forbidden_imports(paths, ('autodl_helper.cli', 'autodl_helper.ui'))


def test_ui_does_not_import_cli_app():
    paths = [*PACKAGE_ROOT.joinpath('ui').rglob('*.py')]
    _assert_no_forbidden_imports(paths, ('autodl_helper.cli.app',))


def test_refactor_facades_stay_thin():
    thin_files = [
        ROOT / 'autodl_helper/cli/shared.py',
    ]

    for path in thin_files:
        assert _top_level_defs(path) == [], f'{path} should remain a thin façade'


def test_cli_boundary_modules_do_not_grow_business_helpers():
    boundary_modules = [
        ROOT / 'autodl_helper/cli/shared.py',
    ]

    for path in boundary_modules:
        assert _top_level_defs(path) == [], f'{path} should delegate, not carry business helpers'


def test_cli_app_keeps_command_implementations_delegated():
    path = ROOT / 'autodl_helper/cli/app.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    offenders: list[str] = []

    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith('_command_'):
            continue
        statements = [stmt for stmt in node.body if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Constant)]
        has_impl_return = any(
            isinstance(stmt, ast.Return)
            and isinstance(stmt.value, ast.Call)
            and (
                isinstance(stmt.value.func, ast.Name)
                and stmt.value.func.id.endswith('_impl')
            )
            for stmt in statements
        )
        forbidden_nodes = (ast.For, ast.While, ast.Try, ast.ClassDef, ast.With, ast.AsyncWith)
        if len(statements) > 2 or not has_impl_return or any(isinstance(child, forbidden_nodes) for child in ast.walk(node)):
            offenders.append(node.name)

    assert not offenders, 'cli/app.py command wrappers must stay thin delegators: ' + ', '.join(offenders)
