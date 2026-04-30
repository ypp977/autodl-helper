from __future__ import annotations

from types import SimpleNamespace

from autodl_helper.interactive.dialogs import MenuItem
from autodl_helper.interactive.support import snapshots


class _FakeSnapshotStore:
    def __init__(self) -> None:
        self._snapshots: dict[str, object] = {}

    def set_snapshot(self, key: str, value: object) -> None:
        self._snapshots[key] = value

    def get_snapshot(self, key: str):
        return self._snapshots.get(key)

    def page_status(self, snapshot_key, primary_task=None):
        return SimpleNamespace(state='ready', message='', updated_at=None, error_message='')


class _FakeTaskManager:
    def task_key(self, task_type: str, scope: str) -> str:
        return f"{task_type}:{scope}"

    def drain_completed(self) -> None:
        return None

    def get_task(self, task_type: str, scope: str):
        return None

    def start_pending(self) -> None:
        return None

    def shutdown(self, wait: bool = False) -> None:
        return None


def test_browse_snapshot_list_routes_selected_row_to_result_screen(monkeypatch):
    shown: list[tuple[str, str, int | None]] = []
    choices = iter(['1', '0'])

    monkeypatch.setattr(snapshots, '_account_label', lambda settings, current_account: 'main')

    def fake_submit_snapshot_task(*, snapshot_store, snapshot_key, runner, **kwargs):
        snapshot_store.set_snapshot(snapshot_key, runner())

    monkeypatch.setattr(snapshots, '_submit_snapshot_task', fake_submit_snapshot_task)
    monkeypatch.setattr(snapshots, '_choose_menu_with_refresh', lambda *args, **kwargs: next(choices))
    monkeypatch.setattr(
        snapshots,
        '_show_result_screen_for',
        lambda title, body, *, code=None: shown.append((title, body, code)),
    )

    snapshot_store = _FakeSnapshotStore()
    task_manager = _FakeTaskManager()

    snapshots._browse_snapshot_list(
        settings=object(),
        current_account='main',
        snapshot_namespace='instance',
        task_type='instance_refresh',
        status_message='刷新中',
        task_runner=lambda: [{'name': 'row-a'}],
        render_page_fn=lambda account, rows, page_status_lines=None: f'{account}:{rows[0]["name"]}',
        build_items_fn=lambda rows: [MenuItem('1', rows[0]['name'])],
        detail_title_fn=lambda row: row['name'],
        detail_body_fn=lambda row, account: f'{account}:{row["name"]}',
        task_manager=task_manager,
        snapshot_store=snapshot_store,
        refresh_interval_seconds=0.01,
    )

    assert shown == [('row-a', 'main:row-a', None)]
