from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class KeeperResult:
    instance_id: str
    status: str
    release_at: str
    release_source: str
    started_at: str
    stopped_at: str
    status_at: str
    release_deadline: str
    next_keeper_time: str
    seconds_until_release: int | None
    seconds_until_keeper: int | None
    started_duration_seconds: int | None
    shutdown_duration_seconds: int | None
    eligible: bool
    result: str
    reason: str
    event_type: str = ''
    severity: str = 'info'
    summary: str = ''


@dataclass
class ScheduledStartCandidateDetail:
    instance_id: str
    label: str
    status: str
    start_mode: str
    gpu_idle_num: int | None
    reason: str
    reason_label: str
    region_name: str = ''
    machine_alias: str = ''
    matched_priority_index: int | None = None
    matched_priority_rule: str = ''
    selected: bool = False


@dataclass
class ScheduledStartResult:
    result: str
    reason: str
    instance_id: str
    status: str
    gpu_idle_num: int | None
    start_mode: str
    target_time: str
    deadline: str
    selector_summary: str = ''
    candidate_count: int = 0
    candidate_details: list[ScheduledStartCandidateDetail] = field(default_factory=list)
    selected_instance_id: str = ''
    selected_instance_label: str = ''
    event_type: str = ''
    severity: str = 'info'
    summary: str = ''

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.result == other
        return super().__eq__(other)


@dataclass
class HistoryRecord:
    created_at: str
    account_name: str
    task_type: str
    result: str
    reason: str
    instance_id: str
    payload: dict[str, Any]
    event_type: str = ''
    severity: str = 'info'
    summary: str = ''


@dataclass
class AuthEventSummary:
    code: str
    msg: str
    count: int
    last_seen_at: str
    accounts: list[str]
    mapped: bool
    matched_by: str
