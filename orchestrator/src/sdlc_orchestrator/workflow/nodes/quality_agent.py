"""
Quality gate (blocking policy checks on the WHOLE generated repo, run after
the two parallel codegen branches join) and the commit step that records
"one commit per orchestrator-approved task".
"""
from __future__ import annotations

from sdlc_orchestrator.integrations import gitops
from sdlc_orchestrator.core.events import log_event
from sdlc_orchestrator.policy.guardrails import run_gate
from sdlc_orchestrator.workflow.nodes.common import repo_path, set_task_status
from sdlc_orchestrator.integrations.repo_io import read_repo_files
from sdlc_orchestrator.workflow.stage import stage
from sdlc_orchestrator.core.state import CommitRecord, dump


def _quality_impl(state: dict) -> dict:
    run_id = state["run_id"]
    repo = repo_path(state)
    ws = state["workspace"]
    mode = state.get("mode", "greenfield")

    failures: list[str] = []
    for branch, st in (state.get("branch_status") or {}).items():
        failures += [f"{branch}: {e}" for e in st.get("errors", [])]

    changes = gitops.changed_files(repo, ws["base_sha"]) if mode == "brownfield" else []
    gate = run_gate("quality_gate", files=read_repo_files(repo), design_doc=state["design_doc"],
                    strategy=(state.get("codegen_strategy") or "full"), mode=mode, changes=changes)
    failures += gate.failures

    if failures:
        log_event(run_id, "quality_gate", "guardrail_fail", {"failures": failures[:30]}, actor="system:gate",
                  outcome="fail", reason="; ".join(failures)[:400])
        return {
            "guardrail_failures": failures, "failure_class": "guardrail",
            "failure_feedback": "POLICY GATE FAILURES (fix all):\n- " + "\n- ".join(failures),
            "_outcome": "fail", "_reason": f"{len(failures)} guardrail failure(s)", "_detail": {"failures": failures[:30]},
        }
    return {"guardrail_failures": [], "failure_class": "", "failure_feedback": "", "_outcome": "pass"}


quality_gate_node = stage("quality_gate", actor="system:gate")(_quality_impl)


def _commit_impl(state: dict) -> dict:
    """Tests passed -> the implementation tasks are APPROVED: commit one
    commit per task. This is what rollback later reverts to."""
    repo = repo_path(state)
    ws = dict(state["workspace"])
    owner = state.get("artifact_owner", {})
    commits, last = [], ws["last_good_sha"]

    for branch in ("impl_data", "impl_api"):
        task = next((t for t in state.get("tasks", []) if t["owner_stage"] == branch and t["status"] != "superseded"), None)
        tid = task["id"] if task else branch
        paths = [p for p, b in owner.items() if b == branch]
        msg = f"task({tid}): {task['description'] if task else branch} [run {state['run_id']}]"
        sha = gitops.commit_paths(repo, paths, msg)
        if sha != last:
            commits.append(dump(CommitRecord(task_id=tid, sha=sha, message=msg)))
            last = sha
    # anything else the branches touched (e.g. a model-edited pom.xml) rides with the testing task
    sha = gitops.commit_all(repo, f"task(run-tests): all tests green [run {state['run_id']}]")
    if sha != last:
        commits.append(dump(CommitRecord(task_id="run-tests", sha=sha, message="all tests green")))
        last = sha
    ws["last_good_sha"] = last
    return {
        "workspace": ws, "commits": commits,
        "tasks": set_task_status(state, ["impl_data", "impl_api", "testing"], "done"),
        "_detail": {"commits": [c["sha"][:8] for c in commits]},
    }


commit_node = stage("commit", actor="system:git")(_commit_impl)
