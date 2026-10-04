"""
Full-graph tests with the stub LLM: prove wiring, gates, parallel fan-out,
retry/fallback/rollback/replan, approvals and the greenfield->brownfield
hand-off on a REAL git repo in a temp dir. No infra needed.
"""
import pytest
from langgraph.types import Command

from app import config, gitops
from app.checkpoint import get_checkpointer
from app.graph import build_graph
from app.main import pending_interrupt


def run(scenario, requirement, *, approve=None, clarification=None, run_id="r1", graph_holder=None):
    """Drive a run. approve: dict for the release gate; clarification: dict for the clarification gate."""
    cfg = {"configurable": {"thread_id": run_id}, "recursion_limit": 120}
    with get_checkpointer() as cp:
        graph = build_graph(cp)
        graph.invoke({"run_id": run_id, "scenario": scenario, "raw_requirement": requirement, "status": "running"}, config=cfg)
        gates = []
        while (payload := pending_interrupt(graph, cfg)) is not None:
            gates.append(payload["gate"])
            if payload["gate"] == "clarification":
                assert clarification is not None, "unexpected clarification gate"
                graph.invoke(Command(resume=clarification), config=cfg)
            else:
                assert approve is not None, "unexpected release gate"
                graph.invoke(Command(resume=approve), config=cfg)
        return graph.get_state(cfg).values, gates


APPROVE = {"decision": "approve", "approver": "rahul", "rationale": "all green"}
GREEN = "Build a URL shortener with shorten, redirect, and click analytics"


def types(sink, run_id="r1"):
    return [e["event_type"] for e in sink.read(run_id)]


def test_greenfield_creates_the_project_through_the_orchestrator(offline_env):
    repo = config.target_repo()
    assert not repo.exists()                       # nothing pre-exists: the orchestrator must create it
    values, gates = run("greenfield", GREEN, approve=APPROVE)

    assert values["status"] == "succeeded"
    assert gates == ["release"]
    assert (repo / "pom.xml").exists() and (repo / "mvnw").exists()
    assert (repo / "docs/openapi.yaml").exists() and (repo / "README.md").exists()
    assert any(p.name.startswith("V1__") for p in (repo / "src/main/resources/db/migration").iterdir())

    # one commit per approved task, merged to main + tagged on approval
    msgs = gitops.log_oneline(repo)
    assert any("task(scaffold-project)" in m for m in msgs)
    assert any("task(implement-data-layer)" in m for m in msgs)
    assert any("task(implement-api-layer)" in m for m in msgs)
    assert gitops.current_branch(repo) == "main"
    assert gitops.is_clean(repo)

    appr = values["approvals"][-1]
    assert (appr["gate"], appr["decision"], appr["approver"], appr["rationale"]) == ("release", "approve", "rahul", "all green")
    assert appr["timestamp"]


def test_audit_events_carry_who_what_when_why_outcome(offline_env):
    run("greenfield", GREEN, approve=APPROVE)
    events = offline_env.read("r1")
    exits = [e for e in events if e["event_type"] == "exit"]
    assert {"requirements", "workspace", "decomposition", "design", "scaffold", "impl_data", "impl_api",
            "quality_gate", "testing", "commit", "docs", "release_readiness", "finalize"} <= {e["node"] for e in exits}
    assert all(e["actor"] and e["outcome"] and e["created_at"] for e in exits)
    assert any(e["event_type"] == "approval_granted" and e["actor"] == "human:rahul" for e in events)
    finished = [e for e in events if e["event_type"] == "run_finished"]
    assert len(finished) == 1 and finished[0]["detail"]["status"] == "succeeded"


def test_release_rejection_never_succeeds_and_keeps_main_clean(offline_env):
    values, _ = run("greenfield", GREEN, approve={"decision": "reject", "approver": "rahul", "rationale": "not yet"})
    repo = config.target_repo()
    assert values["status"] == "failed"
    assert gitops.current_branch(repo) == "main"
    assert not (repo / "pom.xml").exists()          # main still only has the empty root commit
    assert "approval_rejected" in types(offline_env)


def test_unknown_release_decision_does_not_approve(offline_env):
    values, _ = run("greenfield", GREEN, approve={"decision": "yolo", "approver": "x"})
    assert values["status"] == "failed"


def test_ambiguous_prompt_pauses_for_clarification_before_anything_is_built(offline_env):
    repo = config.target_repo()
    cfg = {"configurable": {"thread_id": "amb"}, "recursion_limit": 120}
    with get_checkpointer() as cp:
        graph = build_graph(cp)
        graph.invoke({"run_id": "amb", "scenario": "ambiguous", "raw_requirement": "make the system more reliable", "status": "running"}, config=cfg)
        payload = pending_interrupt(graph, cfg)
        assert payload["gate"] == "clarification"
        assert payload["ambiguities"] and payload["assumptions"]
        assert not repo.exists()                    # nothing generated while ambiguous
        assert "assumptions_logged" in types(offline_env, "amb")
        graph.invoke(Command(resume={"resolution": "99.9% availability, redirect p99 < 50ms", "approver": "rahul"}), config=cfg)
        assert pending_interrupt(graph, cfg)["gate"] == "release"
        graph.invoke(Command(resume=APPROVE), config=cfg)
        values = graph.get_state(cfg).values
    assert values["status"] == "succeeded"
    assert values["approvals"][0]["gate"] == "clarification" and values["approvals"][0]["approver"] == "rahul"


def test_declined_clarification_aborts(offline_env):
    values, _ = run("ambiguous", "make it better", clarification={"proceed": False, "approver": "rahul"})
    assert values["status"] == "failed"
    assert not config.target_repo().exists()


def test_parallel_branches_and_sync_point_in_plan(offline_env):
    values, _ = run("greenfield", GREEN, approve=APPROVE)
    waves = values["plan_waves"]
    flat = [t for w in waves for t in w]
    impl_wave = next(w for w in waves if "implement-data-layer" in w)
    assert "implement-api-layer" in impl_wave                    # same wave = parallel
    assert flat.index("run-tests") > flat.index("implement-api-layer")   # sync point after both


def test_retry_then_success_logs_retry_and_mttr(offline_env, monkeypatch):
    monkeypatch.setenv("STUB_TEST_FAILURES", "1")
    values, _ = run("greenfield", GREEN, approve=APPROVE)
    assert values["status"] == "succeeded"
    assert len(values["retries"]) == 1 and values["codegen_strategy"] == "full"
    from app.metrics import metrics_for_run
    m = metrics_for_run("r1")
    assert m["retry_count"] == 1 and m["test_failure_count"] == 1 and m["recovered_failure_episodes"] == 1


def test_fallback_strategy_after_two_failures_then_success(offline_env, monkeypatch):
    monkeypatch.setenv("STUB_TEST_FAILURES", "2")
    values, _ = run("greenfield", GREEN, approve=APPROVE)
    assert values["status"] == "succeeded"
    assert values["codegen_strategy"] == "simple"                # degraded path taken
    assert "fallback_strategy" in types(offline_env)
    readme = (config.target_repo() / "README.md").read_text()
    assert "Degraded build" in readme                            # degradation is disclosed, not hidden


def test_rollback_is_a_real_git_revert_after_retries_exhausted(offline_env, monkeypatch):
    monkeypatch.setenv("STUB_TEST_FAILURES", "99")
    values, _ = run("greenfield", GREEN)
    repo = config.target_repo()
    assert values["status"] == "rolled_back"
    assert len(values["retries"]) == 2 and len(values["rollbacks"]) == 1
    rb = values["rollbacks"][0]
    assert rb["reverted_to"] == gitops.head_sha(repo)            # HEAD is exactly the last approved commit
    assert not (repo / "src/main/java/com/rahul/urlshortener/web").exists()   # generated code is gone
    assert (repo / "pom.xml").exists()                           # the approved scaffold remains
    assert gitops.is_clean(repo)
    assert [e for e in offline_env.read("r1") if e["event_type"] == "run_finished"][-1]["detail"]["status"] == "rolled_back"


def test_schema_failure_triggers_replan_not_blind_retry(offline_env, monkeypatch):
    monkeypatch.setenv("STUB_TEST_FAILURES", "1")
    monkeypatch.setenv("STUB_FAILURE_CLASS", "schema")
    values, _ = run("greenfield", GREEN, approve=APPROVE)
    assert values["status"] == "succeeded"
    assert len(values["replans"]) == 1 and values["replans"][0]["cause"] == "schema_mismatch"
    assert len(values["retries"]) == 0                           # replanned, did not retry in place
    assert values["plan_version"] == 2
    statuses = {t["id"]: t["status"] for t in values["tasks"]}
    assert statuses["implement-api-layer"] == "superseded"       # old downstream tasks superseded
    assert any(k.endswith("-v2") and v == "done" for k, v in statuses.items())   # regenerated tasks ran
    design_exits = [e for e in offline_env.read("r1") if e["node"] == "design" and e["event_type"] == "exit"]
    assert len(design_exits) == 2                                # Design re-ran
    assert design_exits[1]["detail"]["replanned"] is True


def test_replan_is_bounded(offline_env, monkeypatch):
    monkeypatch.setenv("STUB_TEST_FAILURES", "5")
    monkeypatch.setenv("STUB_FAILURE_CLASS", "schema")
    monkeypatch.setenv("MAX_REPLANS", "1")
    values, _ = run("greenfield", GREEN)
    assert len(values["replans"]) == 1
    assert values["status"] == "rolled_back"                     # after the replan budget, normal retry -> rollback


def test_infra_failure_aborts_without_burning_retries_or_reverting(offline_env, monkeypatch):
    monkeypatch.setenv("STUB_TEST_FAILURES", "1")
    monkeypatch.setenv("STUB_FAILURE_CLASS", "infra")
    values, _ = run("greenfield", GREEN)
    assert values["status"] == "failed"
    assert not values.get("retries") and not values.get("rollbacks")


def test_human_can_send_release_back_to_design(offline_env):
    cfg = {"configurable": {"thread_id": "rw"}, "recursion_limit": 120}
    with get_checkpointer() as cp:
        graph = build_graph(cp)
        graph.invoke({"run_id": "rw", "scenario": "greenfield", "raw_requirement": GREEN, "status": "running"}, config=cfg)
        assert pending_interrupt(graph, cfg)["gate"] == "release"
        graph.invoke(Command(resume={"decision": "rework_design", "approver": "rahul", "rationale": "ADR on caching is weak"}), config=cfg)
        assert pending_interrupt(graph, cfg)["gate"] == "release"          # went back through Design and is awaiting approval again
        v = graph.get_state(cfg).values
        assert v["replans"][0]["cause"] == "human_rejected_design"
        graph.invoke(Command(resume=APPROVE), config=cfg)
        assert graph.get_state(cfg).values["status"] == "succeeded"


def test_greenfield_refuses_to_run_over_an_existing_project(offline_env):
    run("greenfield", GREEN, approve=APPROVE)
    from app.stage import PreconditionError
    with pytest.raises(PreconditionError, match="EMPTY"):
        run("greenfield", GREEN, approve=APPROVE, run_id="r2")


def test_brownfield_requires_an_existing_project(offline_env):
    from app.stage import PreconditionError
    with pytest.raises(PreconditionError, match="existing generated project"):
        run("brownfield", "Add custom alias support and geo-breakdown analytics", approve=APPROVE)


def test_brownfield_extends_the_greenfield_output(offline_env):
    run("greenfield", GREEN, approve=APPROVE, run_id="g1")
    repo = config.target_repo()
    before = gitops.head_sha(repo)
    v1 = (repo / "src/main/resources/db/migration/V1__init_url_mapping.sql").read_text()

    values, _ = run("brownfield", "Add geo-breakdown analytics", approve=APPROVE, run_id="b1")
    assert values["status"] == "succeeded" and values["mode"] == "brownfield"
    assert values["impacted_modules"], "impact analysis must name real, existing files"
    assert all((repo / p).is_file() for p in values["impacted_modules"])
    scaffold_exit = next(e for e in offline_env.read("b1") if e["node"] == "scaffold" and e["event_type"] == "exit")
    assert scaffold_exit["outcome"] == "skipped"                 # brownfield never regenerates the project
    assert (repo / "src/main/java/com/rahul/urlshortener/web/GeoAnalyticsController.java").exists()
    assert (repo / "src/main/resources/db/migration/V1__init_url_mapping.sql").read_text() == v1   # immutable
    assert (repo / "src/main/resources/db/migration/V2__add_click_geo.sql").exists()
    assert "/api/analytics/{shortCode}/geo" in (repo / "docs/openapi.yaml").read_text()
    assert gitops.head_sha(repo) != before
    assert gitops.is_clean(repo) and gitops.current_branch(repo) == "main"


def test_policy_gate_blocks_a_hardcoded_secret_then_retry_fixes_it(offline_env, monkeypatch):
    monkeypatch.setenv("STUB_INJECT_SECRET", "1")
    values, _ = run("greenfield", GREEN, approve=APPROVE)
    fails = [e for e in offline_env.read("r1") if e["event_type"] == "guardrail_fail"]
    assert fails and any("secret" in f for f in fails[0]["detail"]["failures"])
    assert len(values["retries"]) == 1 and values["status"] == "succeeded"
    # the gate ran BEFORE tests: the stubbed test runner was only invoked once (on the clean attempt)
    assert len([e for e in offline_env.read("r1") if e["event_type"] == "test_pass"]) == 1
    assert not (config.target_repo() / "src/main/java/com/rahul/urlshortener/config/Leaky.java").exists()   # model removed it via DELETE
