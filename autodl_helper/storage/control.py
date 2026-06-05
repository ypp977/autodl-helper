from __future__ import annotations

from typing import Any, Protocol

from .records import scheduled_job_control_row, task_control_row, utc_now_iso


class _StoreConnection(Protocol):
    def execute(self, sql: str, parameters: Any = ...) -> Any: ...


class ControlStoreMixin:
    def init_schema(self) -> None:
        raise NotImplementedError

    def connect(self) -> Any:
        raise NotImplementedError

    def set_runtime_value(self, key: str, value: str) -> None:
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_control(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, utc_now_iso()),
            )

    def set_runtime_values(self, values: dict[str, str]) -> None:
        if not values:
            return
        self.init_schema()
        now = utc_now_iso()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO runtime_control(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                [(str(key), str(value), now) for key, value in values.items()],
            )

    def get_runtime_value(self, key: str, default: str = '') -> str:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute('SELECT value FROM runtime_control WHERE key = ?', (key,)).fetchone()
            return str(row['value']) if row is not None else default

    def get_runtime_snapshot(self) -> dict[str, str]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute('SELECT key, value FROM runtime_control').fetchall()
            return {str(row['key']): str(row['value']) for row in rows}

    def set_task_control(self, account_name: str, task_type: str, *, enabled: bool, source: str) -> None:
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO task_control(account_name, task_type, enabled, source, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_name, task_type) DO UPDATE SET
                    enabled = excluded.enabled,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (account_name, task_type, 1 if enabled else 0, source, utc_now_iso()),
            )

    def get_task_control(self, account_name: str, task_type: str) -> bool | None:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                'SELECT enabled FROM task_control WHERE account_name = ? AND task_type = ?',
                (account_name, task_type),
            ).fetchone()
            if row is None:
                return None
            return bool(row['enabled'])

    def list_task_controls(self, *, account_name: str | None = None) -> list[dict[str, Any]]:
        self.init_schema()
        query = 'SELECT account_name, task_type, enabled, source, updated_at FROM task_control'
        params: list[Any] = []
        if account_name:
            query += ' WHERE account_name = ?'
            params.append(account_name)
        query += ' ORDER BY account_name, task_type'
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [task_control_row(row) for row in rows]

    def upsert_scheduled_job_control(
        self,
        account_name: str,
        job_name: str,
        *,
        enabled: bool,
        target_time_override: str = '',
        advance_hours_override: float | None = None,
        source: str,
    ) -> None:
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_job_control(account_name, job_name, enabled, target_time_override, advance_hours_override, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_name, job_name) DO UPDATE SET
                    enabled = excluded.enabled,
                    target_time_override = excluded.target_time_override,
                    advance_hours_override = excluded.advance_hours_override,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    account_name,
                    job_name,
                    1 if enabled else 0,
                    target_time_override,
                    advance_hours_override,
                    source,
                    utc_now_iso(),
                ),
            )

    def get_scheduled_job_control(self, account_name: str, job_name: str) -> dict[str, Any] | None:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT account_name, job_name, enabled, target_time_override, advance_hours_override, source, updated_at
                FROM scheduled_job_control
                WHERE account_name = ? AND job_name = ?
                """,
                (account_name, job_name),
            ).fetchone()
            if row is None:
                return None
            return scheduled_job_control_row(row)

    def list_scheduled_job_controls(self, *, account_name: str | None = None) -> list[dict[str, Any]]:
        self.init_schema()
        query = """
            SELECT account_name, job_name, enabled, target_time_override, advance_hours_override, source, updated_at
            FROM scheduled_job_control
        """
        params: list[Any] = []
        if account_name:
            query += ' WHERE account_name = ?'
            params.append(account_name)
        query += ' ORDER BY account_name, job_name'
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [scheduled_job_control_row(row) for row in rows]
