from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Callable

from autodl_helper.core.config import Settings, load_settings

from ..shared_settings import apply_cli_overrides, serialize_settings, validate_settings


def command_init(
    args: argparse.Namespace,
    *,
    validate_config_fn: Callable[[argparse.Namespace], int] | None = None,
    launch_interactive_fn: Callable[[argparse.Namespace], int] | None = None,
    input_fn: Callable[[str], str] | None = None,
    cwd: str | Path | None = None,
) -> int:
    root = Path(cwd).resolve() if cwd is not None else Path.cwd()
    config_path = Path(args.config).expanduser()
    if not config_path.is_absolute():
        config_path = (root / config_path).resolve()

    package_root = Path(__file__).resolve().parents[3]
    env_template_path = (root / '.env.template') if (root / '.env.template').exists() else (package_root / '.env.template')
    env_path = root / '.env'
    config_template_path = (root / 'config.example.yaml') if (root / 'config.example.yaml').exists() else (package_root / 'config.example.yaml')

    if validate_config_fn is None:
        validate_config_fn = command_validate_config
    if input_fn is None:
        input_fn = input

    print('[1/4] Environment')
    print(f'Python: {sys.executable}')
    print(f'pip: {shutil.which("pip") or "not found"}')
    print(f'playwright: {shutil.which("playwright") or "not found"}')

    def _should_overwrite(dst: Path, label: str) -> bool:
        if getattr(args, 'force', False):
            return True
        if getattr(args, 'yes', False):
            return False
        answer = str(input_fn(f'{label} already exists. Overwrite from template? [y/N]: ')).strip().lower()
        return answer in {'y', 'yes'}

    def _sync_file(*, src: Path, dst: Path, label: str) -> None:
        if not src.exists():
            print(f'Missing template: {src.name}', file=sys.stderr)
            raise FileNotFoundError(src)
        if dst.exists():
            if not _should_overwrite(dst, label):
                print(f'Kept existing {label}.')
                return
            action = 'Overwrote'
        else:
            action = 'Created'
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        print(f'{action} {label} from template.')

    print('[2/4] Bootstrap files')
    try:
        _sync_file(src=env_template_path, dst=env_path, label='.env')
        _sync_file(src=config_template_path, dst=config_path, label=config_path.name)
    except FileNotFoundError:
        return 1

    print('[3/4] Validate config')
    validation_code = validate_config_fn(argparse.Namespace(config=str(config_path)))
    if validation_code != 0:
        print('Configuration validation failed.', file=sys.stderr)
        return int(validation_code or 1)

    print('[4/4] Ready')
    print('Bootstrap complete.')
    print('Next:')
    print(f'  python main.py ui --config {config_path.name}')
    print(f'  python main.py login --config {config_path.name} --account <account-name>')
    print(f'  python main.py service install --config {config_path.name}')

    if launch_interactive_fn is not None and not getattr(args, 'yes', False):
        answer = str(input_fn('Launch UI now? [y/N]: ')).strip().lower()
        if answer in {'y', 'yes'}:
            interactive_args = argparse.Namespace(**vars(args))
            interactive_args.config = str(config_path)
            return int(launch_interactive_fn(interactive_args) or 0)
    return 0


def command_validate_config(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
) -> int:
    try:
        settings = apply_cli_overrides(args, load_settings_fn(args.config))
        errors = validate_settings_fn(settings, purpose='validate')
        if errors:
            print('Configuration invalid:', file=sys.stderr)
            for error in errors:
                print(f'- {error}', file=sys.stderr)
            return 1
        print('Configuration valid.')
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def command_config_show(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
) -> int:
    try:
        settings = load_settings_fn(args.config)
        print(json.dumps(serialize_settings(settings, resolved=False, account_name=getattr(args, 'account', None)), ensure_ascii=False, indent=2))
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def command_config_resolve(
    args: argparse.Namespace,
    *,
    load_settings_fn: Callable[[str], Settings] = load_settings,
    validate_settings_fn: Callable[[Settings, str], list[str]] = validate_settings,
) -> int:
    try:
        settings = apply_cli_overrides(args, load_settings_fn(args.config))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    errors = validate_settings_fn(settings, purpose='validate')
    if errors:
        print('Configuration invalid:', file=sys.stderr)
        for error in errors:
            print(f'- {error}', file=sys.stderr)
        return 1
    print(json.dumps(serialize_settings(settings, resolved=True, account_name=getattr(args, 'account', None)), ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "command_init",
    "command_validate_config",
    "command_config_show",
    "command_config_resolve",
]
