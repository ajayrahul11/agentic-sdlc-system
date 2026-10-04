"""
Pure unit tests for the conditional-edge predicates in workflow/graph.py: no
Postgres, no LLM, no Docker. The fastest feedback loop - run these first.
"""
from sdlc_orchestrator.workflow.graph import (
    route_after_approval, route_after_clarification, route_after_design_review, route_after_decomposition, route_after_quality,
    route_after_release_gate, route_after_requirements, route_after_testing,
)


def retry(pv=1):
    return {"node": "implementation", "attempt": 1, "reason": "x", "plan_version": pv}


def test_requirements_routes_to_clarification_only_with_ambiguities():
    assert route_after_requirements({"ambiguities": []}) == "workspace"
    assert route_after_requirements({"ambiguities": ["what is reliable?"]}) == "clarification_request"


def test_clarification_declined_aborts():
    assert route_after_clarification({"ambiguities_resolved": True}) == "workspace"
    assert route_after_clarification({}) == "abort"


def test_decomposition_routes_on_resolved_mode():
    assert route_after_decomposition({"mode": "greenfield"}) == "design"
    assert route_after_decomposition({"mode": "brownfield"}) == "codebase_reasoning"
    assert route_after_decomposition({"scenario": "brownfield"}) == "codebase_reasoning"
    assert route_after_decomposition({"scenario": "ambiguous", "mode": "greenfield"}) == "design"


def test_quality_gate_routes():
    assert route_after_quality({"guardrail_failures": []}) == "testing"
    assert route_after_quality({"guardrail_failures": ["x"], "retries": []}) == "retry"
    assert route_after_quality({"guardrail_failures": ["x"], "retries": [retry(), retry()]}) == "rollback"


def test_testing_pass_goes_to_commit():
    assert route_after_testing({"test_results": {"passed": True}}) == "commit"


def test_testing_failure_in_place_retry_then_rollback():
    st = {"test_results": {"passed": False, "failure_class": "test"}, "retries": []}
    assert route_after_testing(st) == "retry"
    st["retries"] = [retry(), retry()]
    assert route_after_testing(st) == "rollback"


def test_schema_and_contract_failures_replan_while_budget_remains():
    for cls in ("schema", "contract"):
        assert route_after_testing({"test_results": {"passed": False, "failure_class": cls}, "replans": []}) == "replan"
    spent = {"test_results": {"passed": False, "failure_class": "schema"}, "replans": [{"cause": "x"}], "retries": []}
    assert route_after_testing(spent) == "retry"      # replan budget spent -> ordinary bounded retry


def test_compile_failure_never_replans():
    assert route_after_testing({"test_results": {"passed": False, "failure_class": "compile"}, "retries": []}) == "retry"


def test_infra_failure_aborts():
    assert route_after_testing({"test_results": {"passed": False, "failure_class": "infra"}}) == "abort"


def test_retry_budget_resets_after_a_replan():
    st = {"test_results": {"passed": False, "failure_class": "test"}, "retries": [retry(1), retry(1)], "plan_version": 2}
    assert route_after_testing(st) == "retry"          # old plan's retries don't count against plan v2


def test_max_retries_is_configurable(monkeypatch):
    monkeypatch.setenv("MAX_RETRIES_PER_NODE", "1")
    st = {"test_results": {"passed": False, "failure_class": "test"}, "retries": [retry()]}
    assert route_after_testing(st) == "rollback"


def test_release_gate_and_approval_routes():
    assert route_after_release_gate({"status": "awaiting_approval"}) == "approval"
    assert route_after_release_gate({"status": "failed"}) == "rollback"
    assert route_after_approval({"release_decision": "approve"}) == "finalize"
    assert route_after_approval({"release_decision": "reject"}) == "finalize"
    assert route_after_approval({"release_decision": "rework_design", "replans": []}) == "replan"
    assert route_after_approval({"release_decision": "rework_design", "replans": [{}]}) == "finalize"


def test_design_review_routes():
    assert route_after_design_review({"design_decision": "approve"}) == "scaffold"
    assert route_after_design_review({"design_decision": "revise"}) == "design"
    assert route_after_design_review({"design_decision": "reject"}) == "abort"
    assert route_after_design_review({}) == "abort"                       # no decision never proceeds to codegen
    assert route_after_design_review({"design_decision": "yolo"}) == "abort"
