"""
Test fixtures: every test runs fully OFFLINE - stub LLM, in-memory event
sink and checkpointer, temp target repo, zero backoff. No Postgres, no
API key, no Docker, no network.
"""
import pytest


@pytest.fixture(autouse=True)
def offline_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "true")
    monkeypatch.setenv("CHECKPOINTER", "memory")
    monkeypatch.setenv("EVENT_SINK", "memory")
    monkeypatch.setenv("RETRY_BACKOFF_BASE_SECONDS", "0")
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("TARGET_REPO_PATH", str(tmp_path / "target" / "url-shortener-service"))
    for var in ("STUB_TEST_FAILURES", "STUB_FAILURE_CLASS", "MAX_RETRIES_PER_NODE", "MAX_REPLANS", "FALLBACK_AFTER_FAILURES"):
        monkeypatch.delenv(var, raising=False)

    from sdlc_orchestrator.core import events
    from sdlc_orchestrator.integrations import runners
    import sdlc_orchestrator.core.checkpoint as checkpoint

    sink = events.MemorySink()
    events.set_sink(sink)
    checkpoint._memory_saver = None
    runners.reset_stub_counters()
    yield sink
    events.set_sink(None)
