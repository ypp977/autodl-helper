from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from ..auth.errors import classify_auth_signal
from ..runtime.events import KEEPER_EVENT_TYPES, KEEPER_SEVERITY, SCHEDULED_EVENT_TYPES, SCHEDULED_SEVERITY
from .control import ControlStoreMixin
from .models import AuthEventSummary, HistoryRecord
from .records import (
    dump_payload,
    keeper_history_record,
    legacy_scheduled_payload_matches,
    load_payload,
    scheduled_candidate_row,
    scheduled_history_record,
    scheduled_job_name_variants,
    service_history_record,
    utc_now_iso,
)


class _ClosingSQLiteConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_val, exc_tb):  # type: ignore[override]
        try:
            return super().__exit__(exc_type, exc_val, exc_tb)
        finally:
            self.close()


class SQLiteStore(ControlStoreMixin):
    SCHEMA_VERSION = 3
    CONNECT_RETRY_ATTEMPTS = 3
    CONNECT_RETRY_DELAY_SECONDS = 0.01

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._schema_initialized = False

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(self.CONNECT_RETRY_ATTEMPTS):
            try:
                conn = sqlite3.connect(self.path, factory=_ClosingSQLiteConnection)
                conn.row_factory = sqlite3.Row
                return conn
            except sqlite3.OperationalError as exc:
                last_error = exc
                if 'unable to open database file' not in str(exc).lower() or attempt == self.CONNECT_RETRY_ATTEMPTS - 1:
                    break
                time.sleep(self.CONNECT_RETRY_DELAY_SECONDS)
        assert last_error is not None
        message = f'数据库打开失败（可能为文件描述符耗尽或资源熔断）: {last_error}; path={self.path}'
        raise sqlite3.OperationalError(message) from last_error

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
        existing = {row['name'] for row in rows}
        if column not in existing:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {ddl}')

    def init_schema(self) -> None:
        if self._schema_initialized:
            return
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accounts (
                    name TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_cache (
                    account_name TEXT PRIMARY KEY,
                    authorization TEXT NOT NULL,
                    cached_at INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS keeper_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    release_deadline TEXT NOT NULL,
                    result TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    event_type TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL DEFAULT 'info',
                    summary TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_keeper_cycle
                    ON keeper_history(account_name, instance_id, release_deadline, result);
                CREATE TABLE IF NOT EXISTS scheduled_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT NOT NULL,
                    job_name TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    window_key TEXT NOT NULL,
                    result TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    event_type TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL DEFAULT 'info',
                    summary TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_window
                    ON scheduled_history(account_name, job_name, window_key, result);
                CREATE TABLE IF NOT EXISTS event_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    code TEXT NOT NULL,
                    msg TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_event_log_task_created_at
                    ON event_log(task_type, created_at DESC);
                CREATE TABLE IF NOT EXISTS runtime_control (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_control (
                    account_name TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_name, task_type)
                );
                CREATE TABLE IF NOT EXISTS scheduled_job_control (
                    account_name TEXT NOT NULL,
                    job_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    target_time_override TEXT NOT NULL DEFAULT '',
                    advance_hours_override INTEGER,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_name, job_name)
                );
                """
            )
            self._ensure_column(conn, 'keeper_history', 'event_type', "event_type TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, 'keeper_history', 'severity', "severity TEXT NOT NULL DEFAULT 'info'")
            self._ensure_column(conn, 'keeper_history', 'summary', "summary TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, 'scheduled_history', 'event_type', "event_type TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, 'scheduled_history', 'severity', "severity TEXT NOT NULL DEFAULT 'info'")
            self._ensure_column(conn, 'scheduled_history', 'summary', "summary TEXT NOT NULL DEFAULT ''")
            conn.execute(
                'INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)',
                ('schema_version', str(self.SCHEMA_VERSION)),
            )
        self._schema_initialized = True

    def schema_version(self) -> int:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute('SELECT value FROM schema_meta WHERE key = ?', ('schema_version',)).fetchone()
            return int(row['value']) if row else 0

    def register_accounts(self, accounts: list[Any]) -> None:
        self.init_schema()
        now = utc_now_iso()
        with self.connect() as conn:
            for account in accounts:
                conn.execute(
                    """
                    INSERT INTO accounts(name, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        enabled = excluded.enabled,
                        updated_at = excluded.updated_at
                    """,
                    (account.name, 1 if account.enabled else 0, now, now),
                )

    def get_auth_cache(self, account_name: str) -> dict[str, Any] | None:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                'SELECT authorization, cached_at FROM auth_cache WHERE account_name = ?',
                (account_name,),
            ).fetchone()
            if row is None:
                return None
            return {'authorization': row['authorization'], 'cached_at': row['cached_at']}

    def set_auth_cache(self, account_name: str, authorization: str, cached_at: int) -> None:
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO auth_cache(account_name, authorization, cached_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_name) DO UPDATE SET
                    authorization = excluded.authorization,
                    cached_at = excluded.cached_at,
                    updated_at = excluded.updated_at
                """,
                (account_name, authorization, cached_at, utc_now_iso()),
            )

    def was_keeper_executed_in_cycle(self, account_name: str, instance_id: str, release_deadline: str) -> bool:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM keeper_history
                WHERE account_name = ? AND instance_id = ? AND release_deadline = ? AND result = 'keeper_executed'
                LIMIT 1
                """,
                (account_name, instance_id, release_deadline),
            ).fetchone()
            return row is not None

    def add_keeper_history(
        self,
        account_name: str,
        instance_id: str,
        release_deadline: str,
        result: str,
        reason: str,
        payload: dict[str, Any],
        event_type: str = '',
        severity: str = 'info',
        summary: str = '',
    ) -> None:
        self.init_schema()
        event_type = event_type or str(payload.get('event_type', '') or KEEPER_EVENT_TYPES.get(result, ''))
        severity = severity or str(payload.get('severity', '') or KEEPER_SEVERITY.get(result, 'info'))
        summary = summary or str(payload.get('summary', '') or '')
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO keeper_history(account_name, instance_id, release_deadline, result, reason, event_type, severity, summary, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_name,
                    instance_id,
                    release_deadline,
                    result,
                    reason,
                    event_type,
                    severity,
                    summary,
                    utc_now_iso(),
                    dump_payload(payload),
                ),
            )

    def add_scheduled_history(
        self,
        account_name: str,
        job_name: str,
        instance_id: str,
        window_key: str,
        result: str,
        reason: str,
        payload: dict[str, Any],
        event_type: str = '',
        severity: str = 'info',
        summary: str = '',
    ) -> None:
        self.init_schema()
        event_type = event_type or str(payload.get('event_type', '') or SCHEDULED_EVENT_TYPES.get(result, ''))
        if result == 'deadline_failed' and reason == 'selector_no_match' and not payload.get('event_type'):
            event_type = 'scheduled.failed.selector_no_match'
        severity = severity or str(payload.get('severity', '') or SCHEDULED_SEVERITY.get(result, 'info'))
        summary = summary or str(payload.get('summary', '') or '')
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_history(account_name, job_name, instance_id, window_key, result, reason, event_type, severity, summary, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_name,
                    job_name,
                    instance_id,
                    window_key,
                    result,
                    reason,
                    event_type,
                    severity,
                    summary,
                    utc_now_iso(),
                    dump_payload(payload),
                ),
            )

    def add_event(self, account_name: str, task_type: str, level: str, message: str, *, code: str = '', msg: str = '', payload: dict[str, Any] | None = None) -> None:
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO event_log(account_name, task_type, level, message, code, msg, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (account_name, task_type, level, message, code, msg, utc_now_iso(), dump_payload(payload)),
            )

    def read_history(
        self,
        *,
        account_name: str | None = None,
        task_type: str | None = None,
        event_type: str | None = None,
        limit: int = 20,
    ) -> list[HistoryRecord]:
        self.init_schema()
        with self.connect() as conn:
            records: list[HistoryRecord] = []

            if task_type in {None, 'keeper'}:
                keeper_rows = conn.execute(
                    """
                    SELECT created_at, account_name, result, reason, instance_id, event_type, severity, summary, payload
                    FROM keeper_history
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (max(limit * 4, 50),),
                ).fetchall()
                for row in keeper_rows:
                    if account_name and row['account_name'] != account_name:
                        continue
                    if event_type and (row['event_type'] or '') != event_type:
                        continue
                    records.append(keeper_history_record(row))

            if task_type in {None, 'scheduled_start'}:
                scheduled_rows = conn.execute(
                    """
                    SELECT created_at, account_name, result, reason, instance_id, event_type, severity, summary, payload
                    FROM scheduled_history
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (max(limit * 4, 50),),
                ).fetchall()
                for row in scheduled_rows:
                    if account_name and row['account_name'] != account_name:
                        continue
                    if event_type and (row['event_type'] or '') != event_type:
                        continue
                    records.append(scheduled_history_record(row))

            if task_type in {None, 'service'}:
                service_rows = conn.execute(
                    """
                    SELECT created_at, account_name, level, message, code, msg, payload
                    FROM event_log
                    WHERE task_type = 'service'
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (max(limit * 4, 50),),
                ).fetchall()
                for row in service_rows:
                    payload = load_payload(row['payload'])
                    payload_event_type = str(payload.get('action') or '')
                    if account_name and row['account_name'] not in {'', account_name}:
                        continue
                    if event_type and payload_event_type != event_type and str(row['code'] or '') != event_type:
                        continue
                    records.append(service_history_record(row))

            records.sort(key=lambda item: item.created_at, reverse=True)
            return records[:limit]

    def summarize_auth_failures(self, *, account_name: str | None = None, limit: int = 50) -> list[AuthEventSummary]:
        self.init_schema()
        clauses = ["task_type = 'auth'"]
        params: list[Any] = []
        if account_name:
            clauses.append('account_name = ?')
            params.append(account_name)
        query = f"""
            SELECT code, msg, COUNT(*) AS hit_count, MAX(created_at) AS last_seen_at,
                   GROUP_CONCAT(DISTINCT account_name) AS account_names
            FROM event_log
            WHERE {' AND '.join(clauses)}
            GROUP BY code, msg
            ORDER BY hit_count DESC, last_seen_at DESC
            LIMIT ?
        """
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            summaries: list[AuthEventSummary] = []
            for row in rows:
                code = row['code'] or ''
                msg = row['msg'] or ''
                mapped, matched_by = classify_auth_signal(code, msg)
                accounts = [part for part in str(row['account_names'] or '').split(',') if part]
                summaries.append(
                    AuthEventSummary(
                        code=code,
                        msg=msg,
                        count=int(row['hit_count'] or 0),
                        last_seen_at=str(row['last_seen_at'] or ''),
                        accounts=accounts,
                        mapped=mapped,
                        matched_by=matched_by,
                    )
                )
            return summaries

    def read_scheduled_candidates(
        self,
        *,
        account_name: str | None = None,
        job_name: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        self.init_schema()
        clauses: list[str] = []
        params: list[Any] = []
        if account_name:
            clauses.append('account_name = ?')
            params.append(account_name)
        if job_name:
            variants = scheduled_job_name_variants(str(job_name), account_name=account_name)
            placeholders = ', '.join('?' for _ in variants)
            clauses.append(f'job_name IN ({placeholders})')
            params.extend(variants)
        query = """
            SELECT created_at, account_name, job_name, instance_id, result, reason, event_type, severity, summary, payload
            FROM scheduled_history
        """
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY created_at DESC LIMIT ?'
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [scheduled_candidate_row(row) for row in rows]

    def has_scheduled_success(
        self,
        *,
        account_name: str,
        job_name: str,
        window_key: str,
        job_signature: str | None = None,
        legacy_match_payload: dict[str, Any] | None = None,
    ) -> bool:
        self.init_schema()
        variants = scheduled_job_name_variants(job_name, account_name=account_name)
        placeholders = ', '.join('?' for _ in variants)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT payload
                FROM scheduled_history
                WHERE account_name = ?
                  AND job_name IN ({placeholders})
                  AND window_key = ?
                  AND result IN ('started', 'already_running', 'power_on_submitted')
                ORDER BY id DESC
                LIMIT 20
                """,
                [account_name, *variants, window_key],
            ).fetchall()
            if not rows:
                return False
            if not job_signature:
                return True
            for row in rows:
                payload = load_payload(row['payload'])
                if str(payload.get('job_signature') or '').strip() == job_signature:
                    return True
                if not str(payload.get('job_signature') or '').strip() and legacy_match_payload and legacy_scheduled_payload_matches(payload, legacy_match_payload):
                    return True
            return False
