from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"notifications": {}}
        try:
            raw = self.path.read_text()
            if not raw.strip():
                return {"notifications": {}}
            payload = json.loads(raw)
            if isinstance(payload, dict):
                payload.setdefault("notifications", {})
                return payload
        except Exception:
            pass
        return {"notifications": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True))

    def mark_notified(self, job_name: str, result: str, window_key: str) -> None:
        notifications = self.data.setdefault("notifications", {})
        job_bucket = notifications.setdefault(job_name, {})
        job_bucket[f"{result}:{window_key}"] = True
        self._save()

    def was_notified(self, job_name: str, result: str, window_key: str) -> bool:
        notifications = self.data.get("notifications", {})
        job_bucket = notifications.get(job_name, {})
        return job_bucket.get(f"{result}:{window_key}", False)
