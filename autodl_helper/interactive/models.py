from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuntimeStatusView:
    running: bool
    mode: str = ''
    pid: int | None = None
    last_seen_at: str = ''


@dataclass
class CandidateSummaryView:
    job_name: str = ''
    selected_instance_id: str = ''
    candidate_count: int = 0
    top_reasons: list[str] = field(default_factory=list)


@dataclass
class DashboardView:
    runtime_status: RuntimeStatusView
    enabled_accounts: int
    keeper_enabled: bool
    scheduled_enabled: bool
    paused_task_count: int
    paused_job_count: int
    instance_rows: list[dict[str, Any]] = field(default_factory=list)
    recent_history: list[Any] = field(default_factory=list)
    recent_auth_rows: list[Any] = field(default_factory=list)
    candidate_summary: CandidateSummaryView = field(default_factory=CandidateSummaryView)
