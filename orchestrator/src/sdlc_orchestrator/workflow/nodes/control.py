"""
Control-plane nodes: human gates, bounded retry with backoff + fallback
strategy, real git rollback, upstream re-planning, and run finalisation.

Interrupt nodes (clarification, approval) are intentionally small and
undecorated: LangGraph re-executes an interrupted node from the top on
resume, so anything with side effects lives in a separate request node.
"""
from __future__ import annotations

import time

from langgraph.types import interrupt

from sdlc_orchestrator.core import config
from sdlc_orchestrator.integrations import gitops
from sdlc_orchestrator.core.events import log_event
from sdlc_orchestrator.workflow.nodes.common import repo_path, set_task_status
from sdlc_orchestrator.workflow.nodes.decomposition_agent import replan_downstream
from sdlc_orchestrator.workflow.stage import stage
from sdlc_orchestrator.core.state import ApprovalRecord, ReplanRecord, RetryRecord, RollbackRecord, dump
from sdlc_orchestrator.workflow import dag

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def retries_in_epoch(state: dict) -> int:
    """Retries since the last (re)plan: a re-plan gives the new plan a
    fresh retry budget."""
    pv = state.get("plan_version", 1)
    return sum(1 for r in state.get("retries", []) if r.get("plan_version", 1) == pv)


def replans_remaining(state: dict) -> bool:
    return len(state.get("replans", [])) < config.max_replans()


def _backoff(attempt: int) -> float:
    return min(config.backoff_cap_seconds(), config.backoff_base_seconds() * 2 ** (attempt - 1))


def _sleep(seconds: float) -> None:  # patched in tests
    if seconds > 0:
        time.sleep(seconds)


# ---------------------------------------------------------------------------
# Clarification gate (ambiguous requirements)
# ---------------------------------------------------------------------------

def _clarification_request(state: dict) -> dict:
    log_event(state["run_id"], "clarification", "approval_requested", {
        "ambiguities": state.get("ambiguities", []), "assumptions": state.get("assumptions", [])},
        actor="system:requirements", outcome="pending",
        reason="blocking ambiguities - the system will not guess")
    return {"status": "awaiting_approval"}


clarification_request_node = stage("clarification_request", actor="system:requirements")(_clarification_request)


def clarification_node(state: dict) -> dict:
    """Human gate. Resume value:
    {"resolution": str, "approver": str, "proceed": bool=True}
    Empty resolution = 'accept the logged assumptions as stated'."""
    decision = interrupt({
        "gate": "clarification",
        "ambiguities": state.get("ambiguities", []),
        "assumptions": state.get("assumptions", []),
        "prompt": "Answer the ambiguities (or accept the listed assumptions) to proceed.",
    }) or {}
    approver = decision.get("approver") or "unknown"
    proceed = decision.get("proceed", True)
    resolution = (decision.get("resolution") or "").strip() or "ACCEPTED the logged assumptions as stated"

    record = ApprovalRecord(gate="clarification", decision="resolved" if proceed else "reject",
                            approver=approver, rationale=resolution)
    log_event(state["run_id"], "clarification", "approval_granted" if proceed else "approval_rejected",
              {"resolution": resolution}, actor=f"human:{approver}", outcome="resolved" if proceed else "reject",
              reason=resolution[:300])
    if not proceed:
        return {"approvals": [dump(record)], "abort_reason": "clarification declined by human", "status": "failed"}

    spec = dict(state.get("requirement_spec", {}))
    spec["clarification_resolution"] = resolution
    spec["requirements_after_clarification"] = f"{state['raw_requirement']}\n\nCLARIFICATION: {resolution}"
    return {"approvals": [dump(record)], "requirement_spec": spec, "ambiguities_resolved": True, "status": "running"}


# ---------------------------------------------------------------------------
# Design review gate (human approval BEFORE any code is generated)
# ---------------------------------------------------------------------------

def design_revisions(state: dict) -> int:
    return sum(1 for a in state.get("approvals", []) if a.get("gate") == "design" and a.get("decision") == "revise")


def _design_review_request(state: dict) -> dict:
    run_id = state["run_id"]
    design = state.get("design_doc", {})
    review_file = config.runs_dir() / run_id / "DESIGN.md"
    review_file.parent.mkdir(parents=True, exist_ok=True)
    review_file.write_text(design.get("design_document", ""))
    log_event(run_id, "design_review", "approval_requested", {
        "revision": design_revisions(state), "review_file": str(review_file),
        "endpoints": sorted(design.get("api_contract", {}).get("paths", {}).keys())},
        actor="system:design", outcome="pending", reason="design must be approved by a human before code generation")
    return {"status": "awaiting_approval", "_detail": {"review_file": str(review_file)}}


design_review_request_node = stage("design_review_request", actor="system:design")(_design_review_request)


def design_review_node(state: dict) -> dict:
    """Human gate between Design and Scaffold/codegen. Resume value:
    {"decision": "approve"|"reject"|"revise", "approver": str, "feedback": str}
    approve -> scaffold; reject -> abort the run; revise -> Design re-runs
    with the feedback and comes back here. Feedback with no decision means
    revise; an unknown decision never approves."""
    design = state.get("design_doc", {})
    decision = interrupt({
        "gate": "design",
        "prompt": "Review the design. approve / reject / revise (revise needs feedback)",
        "revision": design_revisions(state),
        "design_document": design.get("design_document", ""),
        "endpoints": sorted(design.get("api_contract", {}).get("paths", {}).keys()),
        "migrations": [f"V{m.get('version')}__{m.get('name')}" for m in design.get("migrations", [])],
        "components": [(c.get("class"), c.get("branch")) for c in design.get("component_plan", [])],
        "adrs": [a.get("title") for a in design.get("adrs", [])],
        "review_file": str(config.runs_dir() / state["run_id"] / "DESIGN.md"),
    }) or {}
    feedback = (decision.get("feedback") or decision.get("rationale") or "").strip()
    d = decision.get("decision") or ("revise" if feedback else "")
    if d not in ("approve", "reject", "revise"):
        d = "reject"  # unknown input never approves
    approver = decision.get("approver") or "unknown"

    record = ApprovalRecord(gate="design", decision=d, approver=approver, rationale=feedback)
    log_event(state["run_id"], "design_review", "approval_granted" if d == "approve" else "approval_rejected",
              {"decision": d, "revision": design_revisions(state)}, actor=f"human:{approver}", outcome=d,
              reason=feedback[:300] or None)

    delta: dict = {"approvals": [dump(record)], "design_decision": d, "status": "running"}
    if d == "reject":
        delta.update({"abort_reason": f"design rejected by human ({approver})" + (f": {feedback[:200]}" if feedback else ""),
                      "status": "failed"})
    elif d == "revise":
        delta["design_feedback"] = (
            f"HUMAN DESIGN REVIEW (revision {design_revisions(state) + 1}) - the reviewer asked for these changes. "
            f"Apply them to the previous design and keep everything they did not criticise:\n"
            f"{feedback or 'revise the design (no details given)'}")
    return delta


# ---------------------------------------------------------------------------
# Retry / fallback / rollback
# ---------------------------------------------------------------------------

def _retry_impl(state: dict) -> dict:
    run_id = state["run_id"]
    ws = state["workspace"]
    repo = repo_path(state)
    prior = retries_in_epoch(state)
    attempt = prior + 1
    failed_attempts = attempt  # the failure that brought us here is attempt #attempt
    strategy = (state.get("codegen_strategy") or "full")
    delta: dict = {}

    if failed_attempts >= config.fallback_after_failures() and strategy == "full":
        strategy = "simple"
        gitops.hard_reset(repo, ws["rollback_sha"])  # fresh start from the scaffold/base commit
        delta.update({"code_artifacts": {"__reset__": True}, "artifact_owner": {"__reset__": True}})
        log_event(run_id, "implementation", "fallback_strategy", {"from": "full", "to": "simple", "reset_to": ws["rollback_sha"][:8]},
                  actor="system:orchestrator", outcome="degraded",
                  reason=f"full strategy failed {failed_attempts} times; falling back to the simpler strategy")

    wait = _backoff(attempt)
    record = RetryRecord(node="implementation", attempt=attempt, reason=(state.get("failure_feedback") or "")[-300:],
                         failure_class=state.get("failure_class", ""), strategy=strategy,
                         plan_version=state.get("plan_version", 1))
    log_event(run_id, "implementation", "retry", {"attempt": attempt, "strategy": strategy, "backoff_s": wait,
                                                   "failure_class": state.get("failure_class")},
              actor="system:orchestrator", outcome="retrying", reason=(state.get("failure_feedback") or "")[-200:])
    _sleep(wait)
    delta.update({"retries": [dump(record)], "codegen_strategy": strategy})
    return delta


retry_bookkeeping_node = stage("retry_bookkeeping", actor="system:orchestrator")(_retry_impl)


def _rollback_impl(state: dict) -> dict:
    run_id = state["run_id"]
    ws = dict(state["workspace"])
    repo = repo_path(state)
    sha = ws["rollback_sha"]
    gitops.hard_reset(repo, sha)  # REAL revert of working tree + index + untracked files
    reason = (f"exceeded {config.max_retries()} retries ({state.get('failure_class')})"
              if retries_in_epoch(state) >= config.max_retries() else f"release gate failed: {state.get('guardrail_failures', [''])[:1]}")
    record = RollbackRecord(node="implementation", reverted_to=sha, reason=reason)
    ws["last_good_sha"] = sha
    log_event(run_id, "implementation", "rollback", {"reverted_to": sha, "head_after": gitops.head_sha(repo)},
              actor="system:orchestrator", outcome="reverted", reason=reason)
    log_event(run_id, "run", "run_finished", {"status": "rolled_back", "reason": reason}, actor="system:orchestrator", outcome="rolled_back")
    failed_tasks = [{**t, "status": "failed"} for t in state.get("tasks", []) if t["owner_stage"] in ("impl_data", "impl_api", "testing") and t["status"] != "superseded"]
    return {"rollbacks": [dump(record)], "workspace": ws, "status": "rolled_back", "tasks": failed_tasks,
            "_outcome": "reverted", "_reason": reason}


rollback_node = stage("rollback", actor="system:orchestrator")(_rollback_impl)


# ---------------------------------------------------------------------------
# Re-planning (upstream change -> Design re-runs; not a blind restart)
# ---------------------------------------------------------------------------

def _replan_impl(state: dict) -> dict:
    run_id = state["run_id"]
    repo = repo_path(state)
    ws = dict(state["workspace"])

    if state.get("release_decision") == "rework_design":
        cause = "human_rejected_design"
        approvals = state.get("approvals", [])
        detail = approvals[-1]["rationale"] if approvals else "human requested design rework"
    else:
        cause = f"{state.get('failure_class')}_mismatch"
        detail = state.get("failure_feedback", "")

    from_version = state.get("plan_version", 1)
    new_tasks = replan_downstream(state, cause, detail)
    to_version = from_version + 1
    superseded = [t["id"] for t in new_tasks if t["status"] == "superseded"]

    gitops.hard_reset(repo, ws["rollback_sha"])  # discard implementation built on the faulty design
    ws["last_good_sha"] = ws["rollback_sha"]
    active = [t for t in new_tasks if t["status"] != "superseded"]
    all_tasks = [t for t in state.get("tasks", []) if t["status"] != "superseded"]
    merged_active = {t["id"]: t for t in all_tasks}
    merged_active.update({t["id"]: t for t in new_tasks})
    waves = dag.topo_waves(list(merged_active.values()))

    record = ReplanRecord(cause=cause, from_plan_version=from_version, to_plan_version=to_version,
                          superseded_tasks=superseded, detail=detail[-300:])
    log_event(run_id, "replan", "replan", {"cause": cause, "from": from_version, "to": to_version, "superseded": superseded,
                                          "reset_to": ws["rollback_sha"][:8]},
              actor="system:orchestrator", outcome="replanning", reason=cause)
    return {
        "replans": [dump(record)], "plan_version": to_version, "tasks": new_tasks, "plan_waves": waves, "workspace": ws,
        "design_feedback": f"RE-PLAN ({cause}). The previous design led to this failure - fix the DESIGN so it cannot recur:\n{detail}",
        "codegen_strategy": "full", "code_artifacts": {"__reset__": True}, "artifact_owner": {"__reset__": True},
        "guardrail_failures": [], "failure_feedback": "", "failure_class": "", "test_results": {},
        "release_decision": "", "status": "running",
        "_outcome": "replanning", "_reason": cause, "_detail": {"cause": cause, "to_plan_version": to_version},
    }


replan_node = stage("replan", actor="system:orchestrator")(_replan_impl)


# ---------------------------------------------------------------------------
# Release approval gate
# ---------------------------------------------------------------------------

def approval_node(state: dict) -> dict:
    """Human gate before ANYTHING release-track. Resume value:
    {"decision": "approve"|"reject"|"rework_design", "approver": str, "rationale": str}
    Identity, timestamp and rationale are recorded in state AND the audit log."""
    tr = state.get("test_results", {})
    decision = interrupt({
        "gate": "release",
        "prompt": "Approve release? approve / reject / rework_design",
        "mode": state.get("mode"),
        "strategy": (state.get("codegen_strategy") or "full"),
        "tests": {k: tr.get(k) for k in ("tests", "failures", "errors")},
        "adrs": [a.get("title") for a in state.get("design_doc", {}).get("adrs", [])],
        "endpoints": sorted(state.get("design_doc", {}).get("api_contract", {}).get("paths", {}).keys()),
        "commits": [c["sha"][:8] for c in state.get("commits", [])],
        "retries": len(state.get("retries", [])), "replans": len(state.get("replans", [])),
    }) or {}
    d = decision.get("decision") or ("approve" if decision.get("approved") else "reject")
    if d not in ("approve", "reject", "rework_design"):
        d = "reject"  # unknown input never approves
    approver = decision.get("approver") or "unknown"
    rationale = decision.get("rationale", "")

    record = ApprovalRecord(gate="release", decision=d, approver=approver, rationale=rationale)
    log_event(state["run_id"], "approval", "approval_granted" if d == "approve" else "approval_rejected",
              {"decision": d}, actor=f"human:{approver}", outcome=d, reason=rationale[:300] or None)
    return {"approvals": [dump(record)], "release_decision": d}


# ---------------------------------------------------------------------------
# Terminal nodes
# ---------------------------------------------------------------------------

def _finalize_impl(state: dict) -> dict:
    run_id = state["run_id"]
    repo = repo_path(state)
    ws = dict(state["workspace"])
    if state.get("release_decision") == "approve":
        sha = gitops.merge_ff(repo, ws["branch"], "main")
        gitops.tag(repo, f"release/{run_id}", f"orchestrator-approved release (run {run_id})")
        ws["last_good_sha"] = sha
        log_event(run_id, "run", "run_finished", {"status": "succeeded", "released_sha": sha}, actor="system:release", outcome="succeeded")
        return {"status": "succeeded", "workspace": ws, "tasks": set_task_status(state, ["release"], "done"),
                "_detail": {"released_sha": sha}}

    gitops.checkout(repo, "main")  # working tree returns to the last RELEASE-APPROVED state
    reason = f"release {state.get('release_decision') or 'rejected'}"
    log_event(run_id, "run", "run_finished", {"status": "failed", "reason": reason, "branch_kept": ws["branch"]},
              actor="system:release", outcome="failed", reason=reason)
    return {"status": "failed", "_outcome": "rejected", "_reason": reason}


finalize_node = stage("finalize", actor="system:release")(_finalize_impl)


def _abort_impl(state: dict) -> dict:
    reason = state.get("abort_reason") or state.get("failure_feedback", "")[:300] or "aborted"
    log_event(state["run_id"], "run", "run_finished", {"status": "failed", "reason": reason}, actor="system:orchestrator", outcome="failed", reason=reason)
    return {"status": "failed", "abort_reason": reason, "_outcome": "aborted", "_reason": reason}


abort_node = stage("abort", actor="system:orchestrator")(_abort_impl)
