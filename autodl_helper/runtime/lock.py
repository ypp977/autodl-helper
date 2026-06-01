from __future__ import annotations

import os
from pathlib import Path

from .pid import pid_exists


class LockAcquisitionError(RuntimeError):
    pass


def _pid_is_running(pid: int) -> bool:
    return pid_exists(pid)


class FileLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        for attempt in range(2):
            try:
                self._fd = os.open(str(self.path), flags)
                os.write(self._fd, str(os.getpid()).encode('utf-8'))
                return
            except FileExistsError as exc:
                if attempt == 0 and self._recover_stale_lock():
                    continue
                raise LockAcquisitionError(f'lock already exists: {self.path}') from exc

    def _recover_stale_lock(self) -> bool:
        try:
            raw = self.path.read_text(encoding='utf-8').strip()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if not raw:
            self.path.unlink(missing_ok=True)
            return True
        try:
            pid = int(raw)
        except ValueError:
            self.path.unlink(missing_ok=True)
            return True
        if _pid_is_running(pid):
            return False
        self.path.unlink(missing_ok=True)
        return True

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self.path.exists():
            self.path.unlink()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
