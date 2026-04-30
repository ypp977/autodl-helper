from __future__ import annotations

from pathlib import Path

from autodl_helper.tracemalloc_profiler import TracemallocConfig, profiler_from_env


def test_tracemalloc_config_from_env_defaults(monkeypatch):
    monkeypatch.delenv('AUTODL_HELPER_TRACEMALLOC', raising=False)
    monkeypatch.delenv('AUTODL_HELPER_TRACEMALLOC_TRACEBACK_LIMIT', raising=False)
    monkeypatch.delenv('AUTODL_HELPER_TRACEMALLOC_TOP_LIMIT', raising=False)
    monkeypatch.delenv('AUTODL_HELPER_TRACEMALLOC_INTERVAL_SECONDS', raising=False)
    monkeypatch.delenv('AUTODL_HELPER_TRACEMALLOC_OUTPUT', raising=False)

    config = TracemallocConfig.from_env()

    assert config.enabled is False
    assert config.traceback_limit == 25
    assert config.top_limit == 20
    assert config.interval_seconds == 30.0
    assert config.output_path == ''


def test_tracemalloc_profiler_writes_snapshot_file(monkeypatch, tmp_path: Path):
    output_path = tmp_path / 'tracemalloc.log'
    monkeypatch.setenv('AUTODL_HELPER_TRACEMALLOC', '1')
    monkeypatch.setenv('AUTODL_HELPER_TRACEMALLOC_TRACEBACK_LIMIT', '5')
    monkeypatch.setenv('AUTODL_HELPER_TRACEMALLOC_TOP_LIMIT', '3')
    monkeypatch.setenv('AUTODL_HELPER_TRACEMALLOC_INTERVAL_SECONDS', '60')
    monkeypatch.setenv('AUTODL_HELPER_TRACEMALLOC_OUTPUT', str(output_path))

    with profiler_from_env():
        payload = [b'x' * 1024 for _ in range(32)]
        assert len(payload) == 32

    text = output_path.read_text(encoding='utf-8')
    assert '[tracemalloc:start]' in text
    assert '[tracemalloc:stop]' in text
    assert 'size=' in text


def test_tracemalloc_profiler_stops_tracing_when_it_started_tracing(monkeypatch, tmp_path: Path):
    import tracemalloc

    output_path = tmp_path / 'tracemalloc.log'
    monkeypatch.setenv('AUTODL_HELPER_TRACEMALLOC', '1')
    monkeypatch.setenv('AUTODL_HELPER_TRACEMALLOC_OUTPUT', str(output_path))

    assert tracemalloc.is_tracing() is False
    with profiler_from_env():
        assert tracemalloc.is_tracing() is True
    assert tracemalloc.is_tracing() is False
