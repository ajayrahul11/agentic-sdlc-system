"""
Release Readiness (Core Requirement #7: Controlled Autonomy).

Runs the FINAL blocking guardrail pass over everything on disk (code +
packaging + docs) and verifies the working tree is fully committed. Only
if it passes does the graph reach the human approval interrupt; nothing
in this module can approve a release.
"""
from __future__ import annotations

from app import gitops
from app.events import log_event
from app.guardrails import run_gate
from app.nodes.common import repo_path
from app.repo_io import read_repo_files
from app.stage import stage


def _impl(state: dict) -> dict:
    run_id = state["run_id"]
    repo = repo_path(state)
    mode = state.get("mode", "greenfield")
    ws = state["workspace"]

    changes = gitops.changed_files(repo, ws["base_sha"]) if mode == "brownfield" else []
    gate = run_gate("release_readiness", files=read_repo_files(repo), design_doc=state["design_doc"],
                    strategy=(state.get("codegen_strategy") or "full"), mode=mode, changes=changes)
    failures = list(gate.failures)
    if not gitops.is_clean(repo):
        failures.append("git: working tree has uncommitted changes - every artifact must belong to an approved task commit")
    if not (state.get("test_results") or {}).get("passed"):
        failures.append("tests: no passing test run recorded")

    if failures:
        log_event(run_id, "release_readiness", "guardrail_fail", {"failures": failures[:30]}, actor="system:gate",
                  outcome="fail", reason="; ".join(failures)[:400])
        return {"guardrail_failures": failures, "status": "failed", "_outcome": "fail",
                "_reason": f"{len(failures)} release guardrail failure(s)", "_detail": {"failures": failures[:30]}}

    log_event(run_id, "release_readiness", "approval_requested", {"gate": "release"}, actor="system:gate",
              outcome="pending", reason="all release guardrails passed; waiting for a human decision")
    return {"guardrail_failures": [], "status": "awaiting_approval"}


release_readiness_node = stage("release_readiness", actor="system:gate")(_impl)
