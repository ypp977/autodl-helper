from __future__ import annotations

import ctypes
import errno
import os
import signal


def _windows_pid_exists(pid: int) -> bool:
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    handle = kernel32.OpenProcess(process_query_limited_information | synchronize, False, int(pid))
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == 5


def _posix_pid_exists(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        return False
    return True


def pid_exists(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if os.name == 'nt':
        return _windows_pid_exists(int(pid))
    return _posix_pid_exists(int(pid))


def terminate_pid(pid: int) -> None:
    os.kill(int(pid), signal.SIGTERM)
