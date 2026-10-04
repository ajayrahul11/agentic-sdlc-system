import json
import zipfile
import io

import pytest

from sdlc_orchestrator.workflow import contracts
from sdlc_orchestrator.workflow import dag
from sdlc_orchestrator.integrations import scaffold
from sdlc_orchestrator.core.events import MemorySink, log_event, redact, set_sink
from sdlc_orchestrator.core.failures import classify_failure
from sdlc_orchestrator.core.metrics import compute_metrics
from sdlc_orchestrator.workflow.nodes.requirements_agent import BASELINE_NFRS, vagueness_ambiguities
from sdlc_orchestrator.workflow.stage import PostconditionError, PreconditionError, stage


# ---- failure classification (the concrete re-plan trigger) ----
@pytest.mark.parametrize("text,cls", [
    ("Could not find a valid Docker environment", "infra"),
    ("[ERROR] COMPILATION ERROR : cannot find symbol", "compile"),
    ("Schema-validation: missing column [expires_at]", "schema"),
    ("relation \"url_mapping\" does not exist", "schema"),
    ("FlywayException: Validate failed", "schema"),
    ("ContractTest ... GET /x is not served by the application", "contract"),
    ("expected:<302> but was:<500>", "test"),
])
def test_classify_failure(text, cls):
    assert classify_failure(text) == cls


def test_infra_beats_compile_when_jdk_too_old():
    assert classify_failure("error: release version 21 not supported") == "infra"


# ---- requirements: deterministic vagueness backstop ----
def test_vague_prompt_is_flagged():
    amb = vagueness_ambiguities("Make the system more reliable")
    assert amb and "reliable" in amb[0]


def test_concrete_prompts_are_not_flagged():
    assert vagueness_ambiguities("Build a URL shortener with shorten, redirect, and click analytics") == []
    assert vagueness_ambiguities("Add custom alias support and geo-breakdown analytics") == []
    assert vagueness_ambiguities("Make redirects faster: p99 under 50 ms") == []
    assert vagueness_ambiguities("Replace the Redis counter with a sharded ID generator for scale") == []


def test_baseline_nfrs_cover_the_brief():
    text = " ".join(BASELINE_NFRS).lower()
    for needle in ("cache-aside", "eventual", "base62", "rate limit", "expir", "validation", "authentication",
                   "actuator", "docker", "contract test"):
        assert needle in text, needle


# ---- events ----
def test_redaction_masks_secrets():
    out = redact({"k": "key sk-ant-abcdefgh12345678 and password=hunter22", "n": [{"t": "Bearer abcdefghijklmnop"}]})
    blob = json.dumps(out)
    assert "sk-ant" not in blob and "hunter22" not in blob and "abcdefghijklmnop" not in blob
    assert "[REDACTED]" in blob


def test_log_event_never_raises(monkeypatch, tmp_path, capsys):
    class Broken:
        def write(self, row):
            raise RuntimeError("db down")

    set_sink(Broken())
    log_event("r", "n", "t", {"a": 1})              # must not raise
    assert "falling back to JSONL" in capsys.readouterr().err
    assert (tmp_path / "runs" / "events.fallback.jsonl").exists()


# ---- metrics (pure function over events) ----
def ev(t, typ, node="n", outcome=None, detail=None):
    return {"run_id": "r", "node": node, "event_type": typ, "outcome": outcome, "detail": detail or {},
            "created_at": f"2025-01-01T00:00:{t:02d}+00:00"}


def test_metrics_definitions():
    events = [
        ev(0, "run_started"), ev(1, "llm_call", outcome="ok", detail={"input_tokens": 10, "output_tokens": 5}),
        ev(5, "test_fail"), ev(6, "retry"), ev(12, "test_pass"),
        ev(13, "approval_requested"), ev(43, "approval_granted"),
        ev(44, "rollback"), ev(45, "run_finished", detail={"status": "succeeded"}),
    ]
    m = compute_metrics(events)
    assert m["success"] and m["retry_count"] == 1 and m["rollback_count"] == 1
    assert m["mttr_s"] == 7.0                      # test_fail@5 -> test_pass@12
    assert m["wall_clock_s"] == 45 and m["human_wait_s"] == 30 and m["end_to_end_latency_s"] == 15
    assert m["input_tokens"] == 10 and m["output_tokens"] == 5


def test_unrecovered_failure_counted():
    m = compute_metrics([ev(0, "run_started"), ev(1, "test_fail"), ev(2, "run_finished", detail={"status": "rolled_back"})])
    assert m["unrecovered_failures"] == 1 and not m["success"] and m["mttr_s"] == 0.0


# ---- contracts / @stage ----
def test_stage_enforces_preconditions_and_logs(offline_env):
    @stage("design")
    def node(state):
        return {"design_doc": {}}

    with pytest.raises(PreconditionError):
        node({"run_id": "r", "tasks": []})
    assert offline_env.read("r")[-1]["event_type"] == "precondition_fail"


def test_stage_enforces_postconditions(offline_env):
    @stage("decomposition")
    def bad(state):
        return {"tasks": [{"id": "a", "description": "a", "depends_on": ["zzz"], "status": "pending", "owner_stage": "design"}]}

    state = {"run_id": "r", "requirement_spec": {"x": 1}, "workspace": {"base_sha": "abc"}, "mode": "greenfield"}
    with pytest.raises(PostconditionError):
        bad(state)
    types = [e["event_type"] for e in offline_env.read("r")]
    assert types == ["entry", "postcondition_fail"]       # never reached 'exit'


def test_stage_adds_timeline_and_strips_private_keys(offline_env):
    @stage("docs")
    def ok(state):
        return {"docs": {"files": ["README.md"]}, "_outcome": "ok", "_detail": {"x": 1}}

    out = ok({"run_id": "r", "test_results": {"passed": True}, "docs": {}})
    assert "_outcome" not in out and out["timeline"][0]["node"] == "docs" and out["current_node"] == "docs"


def test_precondition_unresolved_ambiguity_blocks_decomposition():
    errs = contracts.pre_decomposition({"requirement_spec": {"a": 1}, "ambiguities": ["?"], "workspace": {"base_sha": "x"}})
    assert any("clarification" in e for e in errs)
    assert contracts.pre_decomposition({"requirement_spec": {"a": 1}, "ambiguities": ["?"], "ambiguities_resolved": True, "workspace": {"base_sha": "x"}}) == []


def test_testing_must_not_run_on_ungated_code():
    assert contracts.pre_testing({"guardrail_failures": ["x"], "workspace": {}})


# ---- scaffold (no network) ----
META = {"dependencies": {"values": [{"values": [{"id": i} for i in
        ("web", "validation", "data-jpa", "data-redis", "postgresql", "flyway", "security", "actuator", "testcontainers")]}]}}


def test_dependency_resolution():
    chosen, skipped = scaffold.resolve_dependencies(scaffold.valid_dependency_ids(META))
    assert "web" in chosen and "testcontainers" in chosen and skipped == []


def test_missing_required_dependency_fails_loudly():
    meta = {"dependencies": {"values": [{"values": [{"id": "web"}]}]}}
    with pytest.raises(scaffold.ScaffoldError, match="required dependency"):
        scaffold.resolve_dependencies(scaffold.valid_dependency_ids(meta))


def test_optional_dependency_skipped_not_fatal():
    meta = {"dependencies": {"values": [{"values": [{"id": i} for i in scaffold.REQUIRED_DEPS]}]}}
    chosen, skipped = scaffold.resolve_dependencies(scaffold.valid_dependency_ids(meta))
    assert skipped == ["testcontainers"] and "testcontainers" not in chosen


def test_initializr_url_uses_latest_boot_by_default(monkeypatch):
    url = scaffold.build_url(["web", "data-jpa"], "21")
    assert "bootVersion" not in url and "javaVersion=21" in url and "dependencies=web%2Cdata-jpa" in url
    assert "bootVersion=4.0.1" in scaffold.build_url(["web"], "21", "4.0.1")


def test_boot_major_assertion():
    pom4 = "<parent><groupId>g</groupId><artifactId>spring-boot-starter-parent</artifactId><version>4.0.0</version></parent>"
    pom3 = pom4.replace("4.0.0", "3.5.7")
    assert scaffold.assert_boot_major(pom4) == "4.0.0"
    with pytest.raises(scaffold.ScaffoldError, match="Spring Boot >= 4"):
        scaffold.assert_boot_major(pom3)


def test_zip_extraction_blocks_traversal(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pom.xml", "<p/>")
        z.writestr("mvnw", "#!/bin/sh")
    names = scaffold.extract_zip(buf.getvalue(), tmp_path / "ok")
    assert set(names) == {"pom.xml", "mvnw"} and (tmp_path / "ok/mvnw").stat().st_mode & 0o111

    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../escape.txt", "x")
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.extract_zip(evil.getvalue(), tmp_path / "bad")


def test_generated_context_test_is_removed(tmp_path):
    t = tmp_path / "src/test/java/x"
    t.mkdir(parents=True)
    (t / "UrlShortenerApplicationTests.java").write_text("x")
    assert scaffold.remove_generated_context_test(tmp_path) == ["src/test/java/x/UrlShortenerApplicationTests.java"]
