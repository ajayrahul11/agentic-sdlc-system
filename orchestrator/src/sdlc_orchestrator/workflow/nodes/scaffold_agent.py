"""
Scaffold stage (greenfield): creates the Maven project from Spring
Initializr and records the first orchestrator-approved commit. This is
also the rollback anchor for the implementation fan-out.

Idempotent: on a re-plan (Design re-runs) the scaffold already exists and
this node is a no-op, so Design changes never regenerate the project.
Brownfield: no-op (the project already exists).
"""
from __future__ import annotations

import os

from sdlc_orchestrator.core import config
from sdlc_orchestrator.integrations import gitops
from sdlc_orchestrator.integrations import scaffold
from sdlc_orchestrator.workflow.nodes.common import repo_path, set_task_status
from sdlc_orchestrator.workflow.stage import stage
from sdlc_orchestrator.core.state import CommitRecord, dump


def _impl(state: dict) -> dict:
    ws = dict(state["workspace"])
    if state.get("mode") == "brownfield" or ws.get("scaffold_sha"):
        return {
            "tasks": set_task_status(state, ["scaffold"], "done"),
            "_outcome": "skipped",
            "_detail": {"reason": "brownfield" if state.get("mode") == "brownfield" else "scaffold already present"},
        }

    repo = repo_path(state)
    facts = (scaffold.scaffold_project_stub(repo) if config.stub_mode()
             else scaffold.scaffold_project(repo, os.environ.get("SPRING_BOOT_VERSION") or None))
    msg = f"task(scaffold-project): spring boot {facts['boot_version']} skeleton [run {state['run_id']}]"
    sha = gitops.commit_all(repo, msg)
    ws.update({"scaffold_sha": sha, "rollback_sha": sha, "last_good_sha": sha})
    return {
        "workspace": ws,
        "commits": [dump(CommitRecord(task_id="scaffold-project", sha=sha, message=msg))],
        "tasks": set_task_status(state, ["scaffold"], "done"),
        "_detail": {**facts, "sha": sha},
    }


scaffold_node = stage("scaffold", actor="system:initializr")(_impl)
