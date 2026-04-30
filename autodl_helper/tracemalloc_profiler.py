from __future__ import annotations

import contextlib
import os
import threading
import tracemalloc
from dataclasses import dataclass
from pathlib import Path
from typing import IO


def _truthy(value: str | None) -> bool:
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        return max(minimum, int(raw))
    except Exception:
        return default


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        return max(minimum, float(raw))
    except Exception:
        return default


@dataclass(frozen=True)
class TracemallocConfig:
    enabled: bool
    traceback_limit: int = 25
    top_limit: int = 20
    interval_seconds: float = 30.0
    output_path: str = ''

    @classmethod
    def from_env(cls) -> 'TracemallocConfig':
        return cls(
            enabled=_truthy(os.environ.get('AUTODL_HELPER_TRACEMALLOC')),
            traceback_limit=_env_int('AUTODL_HELPER_TRACEMALLOC_TRACEBACK_LIMIT', 25),
            top_limit=_env_int('AUTODL_HELPER_TRACEMALLOC_TOP_LIMIT', 20),
            interval_seconds=_env_float('AUTODL_HELPER_TRACEMALLOC_INTERVAL_SECONDS', 30.0),
            output_path=str(os.environ.get('AUTODL_HELPER_TRACEMALLOC_OUTPUT') or '').strip(),
        )


class TracemallocProfiler:
    def __init__(self, config: TracemallocConfig) -> None:
        self.config = config
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._stream: IO[str] | None = None
        self._owns_stream = False
        self._started_here = False

    def __enter__(self) -> 'TracemallocProfiler':
        if not self.config.enabled:
            return self
        if not tracemalloc.is_tracing():
            tracemalloc.start(self.config.traceback_limit)
            self._started_here = True
        self._stream = self._open_stream()
        self._emit_snapshot('start')
        self._thread = threading.Thread(target=self._run, name='autodl-tracemalloc', daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.config.enabled:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.config.interval_seconds + 1.0))
        self._emit_snapshot('stop')
        if self._owns_stream and self._stream is not None:
            with contextlib.suppress(Exception):
                self._stream.close()
        if self._started_here and tracemalloc.is_tracing():
            tracemalloc.stop()
        self._started_here = False
        self._stream = None
        self._owns_stream = False

    def _open_stream(self) -> IO[str]:
        if not self.config.output_path:
            import sys

            return sys.stderr
        path = Path(self.config.output_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._owns_stream = True
        return path.open('a', encoding='utf-8')

    def _run(self) -> None:
        while not self._stop_event.wait(self.config.interval_seconds):
            self._emit_snapshot('tick')

    def _emit_snapshot(self, stage: str) -> None:
        stream = self._stream
        if stream is None:
            return
        current, peak = tracemalloc.get_traced_memory()
        snapshot = tracemalloc.take_snapshot()
        top_stats = snapshot.statistics('lineno')[: self.config.top_limit]
        lines = [f'[tracemalloc:{stage}] current={current} peak={peak} top={len(top_stats)}']
        for index, stat in enumerate(top_stats, start=1):
            frame = stat.traceback[0]
            lines.append(f'#{index} {frame.filename}:{frame.lineno} size={stat.size} count={stat.count}')
        stream.write('\n'.join(lines) + '\n')
        stream.flush()


def profiler_from_env() -> TracemallocProfiler:
    return TracemallocProfiler(TracemallocConfig.from_env())


__all__ = ['TracemallocConfig', 'TracemallocProfiler', 'profiler_from_env']
