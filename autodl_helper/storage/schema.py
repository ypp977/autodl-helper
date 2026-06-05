from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 3

SCHEMA_SQL = """
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
                advance_hours_override REAL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_name, job_name)
);
"""

COMPAT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ('keeper_history', 'event_type', "event_type TEXT NOT NULL DEFAULT ''"),
    ('keeper_history', 'severity', "severity TEXT NOT NULL DEFAULT 'info'"),
    ('keeper_history', 'summary', "summary TEXT NOT NULL DEFAULT ''"),
    ('scheduled_history', 'event_type', "event_type TEXT NOT NULL DEFAULT ''"),
    ('scheduled_history', 'severity', "severity TEXT NOT NULL DEFAULT 'info'"),
    ('scheduled_history', 'summary', "summary TEXT NOT NULL DEFAULT ''"),
)


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
    existing = {row['name'] for row in rows}
    if column not in existing:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {ddl}')


def initialize_schema(conn: sqlite3.Connection, *, schema_version: int = SCHEMA_VERSION) -> None:
    conn.executescript(SCHEMA_SQL)
    for table, column, ddl in COMPAT_COLUMNS:
        ensure_column(conn, table, column, ddl)
    conn.execute(
        'INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)',
        ('schema_version', str(schema_version)),
    )
