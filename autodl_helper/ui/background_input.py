from __future__ import annotations

import queue
import threading
from typing import Any


class BackgroundInputTask:
    def __init__(self, input_fn: Any, prompt: str):
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        self._thread = threading.Thread(target=self._run, args=(input_fn, prompt), name='ui-menu-input', daemon=True)
        self._thread.start()

    def _run(self, input_fn: Any, prompt: str) -> None:
        try:
            self._queue.put(('ok', input_fn(prompt)))
        except Exception as exc:
            self._queue.put(('error', exc))

    def done(self) -> bool:
        return not self._queue.empty()

    def result(self) -> str:
        status, payload = self._queue.get_nowait()
        if status == 'error':
            raise payload
        return str(payload)
