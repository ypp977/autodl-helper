from __future__ import annotations

import contextlib
import io
import logging
import builtins
import os
import resource
import sys
import threading
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures.thread import _worker
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable

MAX_CAPTURED_OUTPUT_CHARS = 8 * 1024
FD_WARN_THRESHOLD = 60.0
FD_FUSE_THRESHOLD = 80.0
RESOURCE_CIRCUIT_COOLDOWN_SECONDS = 30.0
HIGH_RISK_TASK_TYPES = {
    'login_verify_run',
    'healthcheck_run',
}
RESOURCE_GATED_TASK_TYPES = HIGH_RISK_TASK_TYPES | {
    'account_refresh',
    'instances_refresh',
    'keeper_probe_refresh',
    'diagnostics_refresh',
    'scheduled_progress_refresh',
    'scheduled_status_refresh',
}


@dataclass(frozen=True)
class InteractiveTaskResult:
    task_type: str
    scope: str
    task_key: str
    status: str
    started_at: str = ''
    finished_at: str = ''
    status_message: str = ''
    error_message: str = ''
    payload: Any = None
    captured_output: str = ''


@dataclass(frozen=True)
class InteractivePageStatus:
    state: str
    message: str
    updated_at: str = ''
    error_message: str = ''


@dataclass
class _SnapshotEntry:
    payload: Any = None
    updated_at: str = ''
    status_message: str = ''
    error_message: str = ''


class InteractiveSnapshotStore:
    def __init__(self, *, namespace_limits: dict[str, int] | None = None) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, _SnapshotEntry] = {}
        self._entry_revisions: dict[str, int] = {}
        self._revision = 0
        self._namespace_limits = dict(
            namespace_limits
            or {
                'dashboard': 2,
                'account_runtime': 4,
                'diagnostics': 4,
                'healthcheck': 4,
                'config_diagnostics': 4,
                'instances': 4,
                'keeper_probe': 4,
                'scheduled_status': 4,
                'scheduled_progress': 16,
            }
        )

    @staticmethod
    def _namespace_of(key: str) -> str:
        return str(key).split(':', 1)[0]

    def _enforce_namespace_limit_locked(self, namespace: str) -> None:
        limit = self._namespace_limits.get(namespace)
        if limit is None or limit <= 0:
            return
        matching_keys = [key for key in self._entries if self._namespace_of(key) == namespace]
        while len(matching_keys) > limit:
            oldest_key = matching_keys.pop(0)
            self._entries.pop(oldest_key, None)

    def get_snapshot(self, key: str) -> Any:
        with self._lock:
            entry = self._entries.get(key)
            return None if entry is None else entry.payload

    def revision(self) -> int:
        with self._lock:
            return int(self._revision)

    def entry_revision(self, key: str) -> int:
        with self._lock:
            return int(self._entry_revisions.get(key, 0))

    def get_entry(self, key: str) -> _SnapshotEntry | None:
        with self._lock:
            entry = self._entries.get(key)
            return None if entry is None else _SnapshotEntry(
                payload=entry.payload,
                updated_at=entry.updated_at,
                status_message=entry.status_message,
                error_message=entry.error_message,
            )

    def set_snapshot(self, key: str, payload: Any, *, status_message: str = '') -> None:
        with self._lock:
            self._entries[key] = _SnapshotEntry(
                payload=payload,
                updated_at=datetime.now().astimezone().isoformat(),
                status_message=status_message,
                error_message='',
            )
            self._revision += 1
            self._entry_revisions[key] = self._revision
            self._enforce_namespace_limit_locked(self._namespace_of(key))

    def record_failure(self, key: str, error_message: str) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = _SnapshotEntry(
                    payload=None,
                    updated_at='',
                    status_message='',
                    error_message=str(error_message or ''),
                )
                self._revision += 1
                self._entry_revisions[key] = self._revision
                return
            entry.error_message = str(error_message or '')
            self._revision += 1
            self._entry_revisions[key] = self._revision

    def clear_prefix(self, prefix: str) -> None:
        with self._lock:
            for key in [item for item in self._entries if item.startswith(prefix)]:
                self._entries.pop(key, None)
                self._revision += 1
                self._entry_revisions[key] = self._revision

    def page_status(self, key: str, task: InteractiveTaskResult | None = None) -> InteractivePageStatus:
        with self._lock:
            entry = self._entries.get(key)
            if task is not None and task.status in {'queued', 'running'}:
                if entry and entry.updated_at:
                    return InteractivePageStatus(
                        state='refreshing',
                        message='正在刷新',
                        updated_at=entry.updated_at,
                        error_message='',
                    )
                return InteractivePageStatus(
                    state='loading',
                    message='首次加载中',
                    updated_at='',
                    error_message='',
                )
            if entry and entry.error_message:
                return InteractivePageStatus(
                    state='failed',
                    message='刷新失败（保留上次结果）' if entry.updated_at else '刷新失败',
                    updated_at=entry.updated_at,
                    error_message=entry.error_message,
                )
            if entry and entry.updated_at:
                return InteractivePageStatus(
                    state='ready',
                    message=entry.status_message or '最近更新',
                    updated_at=entry.updated_at,
                    error_message='',
                )
            return InteractivePageStatus(
                state='idle',
                message='首次加载中',
                updated_at='',
                error_message='',
            )


@dataclass
class _PendingTask:
    result: InteractiveTaskResult
    future: Future | None = None
    runner: Callable[[], Any] | None = None
    on_success: Callable[[InteractiveTaskResult], None] | None = None
    on_error: Callable[[InteractiveTaskResult], None] | None = None


class _ThreadCaptureStream:
    def __init__(self, fallback: Any) -> None:
        self._fallback = fallback
        self._lock = threading.RLock()
        self._buffers: dict[int, io.StringIO] = {}

    def register(self, buffer: io.StringIO) -> None:
        with self._lock:
            self._buffers[threading.get_ident()] = buffer

    def unregister(self) -> None:
        with self._lock:
            self._buffers.pop(threading.get_ident(), None)

    def _current_buffer(self) -> io.StringIO | None:
        with self._lock:
            return self._buffers.get(threading.get_ident())

    def write(self, data: str) -> int:
        buffer = self._current_buffer()
        if buffer is not None:
            return buffer.write(data)
        try:
            return self._fallback.write(data)
        except ValueError:
            return len(data)

    def flush(self) -> None:
        buffer = self._current_buffer()
        if buffer is not None:
            buffer.flush()
            return
        try:
            self._fallback.flush()
        except ValueError:
            return

    def isatty(self) -> bool:
        return bool(getattr(self._fallback, 'isatty', lambda: False)())

    def fileno(self) -> int:
        return int(self._fallback.fileno())

    @property
    def encoding(self) -> str | None:
        return getattr(self._fallback, 'encoding', None)

    @property
    def errors(self) -> str | None:
        return getattr(self._fallback, 'errors', None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fallback, name)


_stream_proxy_lock = threading.RLock()
_stdout_proxy: _ThreadCaptureStream | None = None
_stderr_proxy: _ThreadCaptureStream | None = None
_stream_capture_count = 0
_original_stdout: Any = None
_original_stderr: Any = None
_print_capture_count = 0
_original_print: Any = None
_print_buffers: dict[int, io.StringIO] = {}


def _thread_capturing_print(*args: Any, **kwargs: Any) -> None:
    buffer = _print_buffers.get(threading.get_ident())
    if buffer is None or kwargs.get('file') not in {None, sys.stdout}:
        try:
            _original_print(*args, **kwargs)
        except ValueError:
            target = kwargs.get('file', sys.stdout)
            if not bool(getattr(target, 'closed', False)):
                raise
            return
        return
    local_kwargs = dict(kwargs)
    local_kwargs['file'] = buffer
    _original_print(*args, **local_kwargs)


def _ensure_thread_capture_streams() -> tuple[_ThreadCaptureStream, _ThreadCaptureStream]:
    global _stdout_proxy, _stderr_proxy, _stream_capture_count, _original_stdout, _original_stderr
    with _stream_proxy_lock:
        if _stream_capture_count == 0:
            _original_stdout = sys.stdout
            _original_stderr = sys.stderr
            _stdout_proxy = _ThreadCaptureStream(sys.stdout)
            _stderr_proxy = _ThreadCaptureStream(sys.stderr)
            sys.stdout = _stdout_proxy
            sys.stderr = _stderr_proxy
        _stream_capture_count += 1
        assert _stdout_proxy is not None
        assert _stderr_proxy is not None
        return _stdout_proxy, _stderr_proxy


def _release_thread_capture_streams() -> None:
    global _stream_capture_count, _stdout_proxy, _stderr_proxy, _original_stdout, _original_stderr, _print_capture_count, _original_print
    with _stream_proxy_lock:
        if _stream_capture_count <= 0:
            return
        _stream_capture_count -= 1
        if _stream_capture_count != 0:
            return
        if _original_stdout is not None and sys.stdout is _stdout_proxy:
            sys.stdout = _original_stdout if not bool(getattr(_original_stdout, 'closed', False)) else sys.__stdout__
        if _original_stderr is not None and sys.stderr is _stderr_proxy:
            sys.stderr = _original_stderr if not bool(getattr(_original_stderr, 'closed', False)) else sys.__stderr__
        if _print_capture_count > 0 and _original_print is not None:
            builtins.print = _original_print
            _print_capture_count = 0
            _print_buffers.clear()
        _stdout_proxy = None
        _stderr_proxy = None
        _original_stdout = None
        _original_stderr = None


def reset_thread_capture_state() -> None:
    global _stream_capture_count, _stdout_proxy, _stderr_proxy, _original_stdout, _original_stderr, _print_capture_count, _original_print
    with _stream_proxy_lock:
        if _stdout_proxy is not None and sys.stdout is _stdout_proxy:
            sys.stdout = _original_stdout if _original_stdout is not None and not bool(getattr(_original_stdout, 'closed', False)) else sys.__stdout__
        if _stderr_proxy is not None and sys.stderr is _stderr_proxy:
            sys.stderr = _original_stderr if _original_stderr is not None and not bool(getattr(_original_stderr, 'closed', False)) else sys.__stderr__
        if _original_print is not None and builtins.print is _thread_capturing_print:
            builtins.print = _original_print
        _stream_capture_count = 0
        _print_capture_count = 0
        _print_buffers.clear()
        _stdout_proxy = None
        _stderr_proxy = None
        _original_stdout = None
        _original_stderr = None
        _original_print = None


def _capture_callable_output(action: Callable[[], Any]) -> tuple[Any, str]:
    stdout_proxy, stderr_proxy = _ensure_thread_capture_streams()
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    root_logger = logging.getLogger()
    thread_id = threading.get_ident()
    capture_handler = logging.StreamHandler(stderr_buffer)
    capture_handler.setLevel(logging.NOTSET)

    class _ThreadFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return getattr(record, 'thread', None) != thread_id

    suppress_current_thread = _ThreadFilter()
    try:
        stdout_proxy.register(stdout_buffer)
        stderr_proxy.register(stderr_buffer)
        with _stream_proxy_lock:
            global _print_capture_count, _original_print
            if _print_capture_count == 0:
                _original_print = builtins.print
                builtins.print = _thread_capturing_print
            _print_capture_count += 1
            _print_buffers[thread_id] = stdout_buffer
        for handler in root_logger.handlers:
            handler.addFilter(suppress_current_thread)
        root_logger.addHandler(capture_handler)
        result = action()
    finally:
        root_logger.removeHandler(capture_handler)
        capture_handler.close()
        for handler in root_logger.handlers:
            with contextlib.suppress(Exception):
                handler.removeFilter(suppress_current_thread)
        with _stream_proxy_lock:
            _print_buffers.pop(thread_id, None)
            if _print_capture_count > 0:
                _print_capture_count -= 1
                if _print_capture_count == 0 and _original_print is not None:
                    builtins.print = _original_print
        stdout_proxy.unregister()
        stderr_proxy.unregister()
        _release_thread_capture_streams()
    stdout_text = stdout_buffer.getvalue().strip()
    stderr_text = stderr_buffer.getvalue().strip()
    output = '\n\n'.join(part for part in [stdout_text, stderr_text] if part)
    if len(output) > MAX_CAPTURED_OUTPUT_CHARS:
        output = output[: MAX_CAPTURED_OUTPUT_CHARS - 3] + '...'
    return result, output


def capture_callable_output(action: Callable[[], Any]) -> tuple[Any, str]:
    return _capture_callable_output(action)


def _fd_usage_snapshot() -> dict[str, Any]:
    try:
        soft_limit, _hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    except Exception:
        soft_limit = 0
    current = -1
    for candidate in ('/dev/fd', '/proc/self/fd'):
        try:
            current = len(os.listdir(candidate))
            break
        except Exception:
            continue
    usage_percent = 0.0
    if soft_limit and soft_limit > 0 and current >= 0:
        usage_percent = round((float(current) / float(soft_limit)) * 100.0, 1)
    return {
        'current': current,
        'soft_limit': int(soft_limit or 0),
        'usage_percent': usage_percent,
    }


def _resource_error_message(error_message: str) -> bool:
    text = str(error_message or '').lower()
    return any(
        marker in text
        for marker in (
            'too many open files',
            'unable to open database file',
            'resource temporarily unavailable',
            'resource exhausted',
        )
    )


class _DaemonThreadPoolExecutor(ThreadPoolExecutor):
    def _adjust_thread_count(self) -> None:
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = '%s_%d' % (self._thread_name_prefix or self, num_threads)
            thread = threading.Thread(
                name=thread_name,
                target=_worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._create_worker_context(),
                    self._work_queue,
                ),
            )
            thread.daemon = True
            thread.start()
            self._threads.add(thread)


class InteractiveTaskManager:
    def __init__(
        self,
        *,
        snapshot_store: InteractiveSnapshotStore,
        max_workers: int = 2,
        fd_warn_threshold: float = FD_WARN_THRESHOLD,
        fd_fuse_threshold: float = FD_FUSE_THRESHOLD,
        circuit_cooldown_seconds: float = RESOURCE_CIRCUIT_COOLDOWN_SECONDS,
    ) -> None:
        self.snapshot_store = snapshot_store
        self._max_workers = max_workers
        self._fd_warn_threshold = float(fd_warn_threshold)
        self._fd_fuse_threshold = float(fd_fuse_threshold)
        self._circuit_cooldown_seconds = float(circuit_cooldown_seconds)
        self._lock = threading.RLock()
        self._executor = _DaemonThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='interactive-runtime')
        self._tasks: dict[str, InteractiveTaskResult] = {}
        self._task_revisions: dict[str, int] = {}
        self._revision = 0
        self._pending: dict[str, _PendingTask] = {}
        self._queued: dict[str, _PendingTask] = {}
        self._closed = False
        self._circuit_until: datetime | None = None
        self._circuit_reason = ''
        self._half_open_probe_task_key: str | None = None
        self._half_open_ready = False

    @staticmethod
    def task_key(task_type: str, scope: str) -> str:
        return f'{task_type}:{scope}'

    def _mark_task_revision_locked(self, task_key: str) -> None:
        self._revision += 1
        self._task_revisions[task_key] = self._revision

    def revision(self) -> int:
        with self._lock:
            return int(self._revision)

    def task_revision(self, task_key: str) -> int:
        with self._lock:
            return int(self._task_revisions.get(task_key, 0))

    def _open_circuit_locked(self, reason: str) -> None:
        self._circuit_until = datetime.now().astimezone() + timedelta(seconds=max(0.01, self._circuit_cooldown_seconds))
        self._circuit_reason = str(reason or '资源熔断中')
        self._half_open_probe_task_key = None
        self._half_open_ready = False

    def _refresh_circuit_locked(self) -> None:
        fd_stats = _fd_usage_snapshot()
        if fd_stats['soft_limit'] > 0 and fd_stats['usage_percent'] >= self._fd_fuse_threshold:
            self._open_circuit_locked(
                f'资源熔断中：文件描述符占用过高 ({fd_stats["current"]} / {fd_stats["soft_limit"]}, {fd_stats["usage_percent"]}%)'
            )
            return
        if self._circuit_until is not None and datetime.now().astimezone() >= self._circuit_until:
            self._circuit_until = None
            self._circuit_reason = ''
            self._half_open_probe_task_key = None
            self._half_open_ready = True

    def _reject_task(
        self,
        *,
        task_type: str,
        scope: str,
        status_message: str,
        error_message: str,
        on_error: Callable[[InteractiveTaskResult], None] | None,
    ) -> InteractiveTaskResult:
        result = InteractiveTaskResult(
            task_type=task_type,
            scope=scope,
            task_key=self.task_key(task_type, scope),
            status='failed',
            status_message=status_message,
            error_message=error_message,
            finished_at=datetime.now().astimezone().isoformat(),
        )
        self._tasks[result.task_key] = result
        self._mark_task_revision_locked(result.task_key)
        if on_error is not None:
            on_error(result)
        return result

    def submit(
        self,
        task_type: str,
        *,
        scope: str,
        runner: Callable[[], Any],
        status_message: str = '',
        on_success: Callable[[InteractiveTaskResult], None] | None = None,
        on_error: Callable[[InteractiveTaskResult], None] | None = None,
        replace_queued: bool = False,
    ) -> InteractiveTaskResult:
        task_key = self.task_key(task_type, scope)
        with self._lock:
            if self._closed:
                raise RuntimeError('InteractiveTaskManager is closed')
            self._refresh_circuit_locked()
            is_high_risk = task_type in HIGH_RISK_TASK_TYPES
            is_resource_gated = task_type in RESOURCE_GATED_TASK_TYPES
            if is_resource_gated and self._circuit_until is not None and datetime.now().astimezone() < self._circuit_until:
                return self._reject_task(
                    task_type=task_type,
                    scope=scope,
                    status_message=status_message,
                    error_message=self._circuit_reason or '资源熔断中，暂不启动高风险后台任务',
                    on_error=on_error,
                )
            if is_high_risk and self._half_open_probe_task_key is not None:
                return self._reject_task(
                    task_type=task_type,
                    scope=scope,
                    status_message=status_message,
                    error_message='资源熔断半开中，等待试探任务完成',
                    on_error=on_error,
                )
            existing = self._tasks.get(task_key)
            if existing is not None and existing.status == 'running':
                return existing
            if existing is not None and existing.status == 'queued' and not replace_queued:
                return existing
            result = InteractiveTaskResult(
                task_type=task_type,
                scope=scope,
                task_key=task_key,
                status='queued',
                status_message=status_message,
            )
            self._tasks[task_key] = result
            self._mark_task_revision_locked(task_key)
            self._queued[task_key] = _PendingTask(
                result=result,
                runner=runner,
                on_success=on_success,
                on_error=on_error,
            )
            if is_high_risk and self._half_open_ready:
                self._half_open_probe_task_key = task_key
            return result

    def get_task(self, task_type: str, scope: str) -> InteractiveTaskResult | None:
        with self._lock:
            return self._tasks.get(self.task_key(task_type, scope))

    def drain_completed(self) -> None:
        completed: list[str] = []
        with self._lock:
            pending_items = list(self._pending.items())
        for task_key, pending in pending_items:
            future = pending.future
            if future is None or not future.done():
                continue
            completed.append(task_key)
            try:
                payload, captured_output = future.result()
                with self._lock:
                    current = self._tasks.get(task_key) or pending.result
                    final = replace(
                        current,
                        status='succeeded',
                        finished_at=datetime.now().astimezone().isoformat(),
                        payload=payload,
                        captured_output=captured_output,
                    )
                    self._tasks[task_key] = final
                    self._mark_task_revision_locked(task_key)
                if pending.on_success is not None:
                    pending.on_success(final)
                with self._lock:
                    if task_key == self._half_open_probe_task_key:
                        self._half_open_probe_task_key = None
                        self._circuit_until = None
                        self._circuit_reason = ''
                        self._half_open_ready = False
            except Exception as exc:
                with self._lock:
                    current = self._tasks.get(task_key) or pending.result
                    final = replace(
                        current,
                        status='failed',
                        finished_at=datetime.now().astimezone().isoformat(),
                        error_message=str(exc),
                    )
                    self._tasks[task_key] = final
                    self._mark_task_revision_locked(task_key)
                    if task_key == self._half_open_probe_task_key or _resource_error_message(str(exc)):
                        self._open_circuit_locked(f'资源熔断中：{exc}')
                if pending.on_error is not None:
                    pending.on_error(final)
        if completed:
            with self._lock:
                for task_key in completed:
                    self._pending.pop(task_key, None)
        self.start_pending()

    def start_pending(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    return
                if len(self._pending) >= self._max_workers or not self._queued:
                    return
                task_key = next(iter(self._queued))
                pending = self._queued.pop(task_key)
                runner = pending.runner
                if runner is None:
                    continue

                def wrapped(task_key: str = task_key, runner: Callable[[], Any] = runner) -> tuple[Any, str]:
                    with self._lock:
                        current = self._tasks[task_key]
                        self._tasks[task_key] = replace(
                            current,
                            status='running',
                            started_at=datetime.now().astimezone().isoformat(),
                        )
                        self._mark_task_revision_locked(task_key)
                    return _capture_callable_output(runner)

                pending.future = self._executor.submit(wrapped)
                self._pending[task_key] = pending

    def runtime_stats(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_circuit_locked()
            running_by_type: dict[str, int] = {}
            queued_by_type: dict[str, int] = {}
            oldest_running_age_seconds = 0
            now = datetime.now().astimezone()
            for task in self._tasks.values():
                if task.status == 'running':
                    running_by_type[task.task_type] = running_by_type.get(task.task_type, 0) + 1
                    if task.started_at:
                        with contextlib.suppress(Exception):
                            started = datetime.fromisoformat(task.started_at)
                            oldest_running_age_seconds = max(
                                oldest_running_age_seconds,
                                int(max(0.0, (now - started).total_seconds())),
                            )
                elif task.status == 'queued':
                    queued_by_type[task.task_type] = queued_by_type.get(task.task_type, 0) + 1
            fd_stats = _fd_usage_snapshot()
            return {
                'max_workers': self._max_workers,
                'running_count': sum(running_by_type.values()),
                'queued_count': sum(queued_by_type.values()),
                'running_by_type': running_by_type,
                'queued_by_type': queued_by_type,
                'oldest_running_age_seconds': oldest_running_age_seconds,
                'fd_current': fd_stats['current'],
                'fd_soft_limit': fd_stats['soft_limit'],
                'fd_usage_percent': fd_stats['usage_percent'],
            }

    def circuit_state(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_circuit_locked()
            circuit_open = self._circuit_until is not None and datetime.now().astimezone() < self._circuit_until
            return {
                'circuit_open': circuit_open,
                'circuit_reason': self._circuit_reason,
                'circuit_until': self._circuit_until.isoformat() if circuit_open and self._circuit_until is not None else '',
                'fd_warn_threshold': self._fd_warn_threshold,
                'fd_fuse_threshold': self._fd_fuse_threshold,
            }

    def record_resource_error(self, error_message: str) -> None:
        if not _resource_error_message(error_message):
            return
        with self._lock:
            self._open_circuit_locked(f'资源熔断中：{error_message}')

    def clear_resource_error(self) -> None:
        with self._lock:
            if self._circuit_until is None:
                self._circuit_reason = ''

    def shutdown(self, *, wait: bool = False) -> None:
        with self._lock:
            self._closed = True
            self._queued.clear()
        self._executor.shutdown(wait=wait, cancel_futures=True)
