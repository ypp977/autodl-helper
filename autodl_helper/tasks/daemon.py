from __future__ import annotations

from typing import Any, Callable

DaemonRunner = Callable[[Any, str], int]


def run_daemon_command(args: Any, *, run_variant_fn: DaemonRunner) -> int:
    """Run all daemon tasks through an injected adapter.

    tasks/ stays independent from CLI/UI; the CLI passes its command adapter in.
    """
    return run_variant_fn(args, 'all')


__all__ = ['DaemonRunner', 'run_daemon_command']
