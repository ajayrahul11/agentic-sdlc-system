"""
Task Decomposition (Core Requirement #2).

Spec -> explicit task DAG with parallel branches and a sync point:

    design -> scaffold -+-> impl_data (schema, entities, Redis, Docker) -+
                        |                                               +-> testing -> docs -> release
                        +-> impl_api  (controllers, services, tests)  ---+

The DAG is validated deterministically (workflow/dag.py). If the model's DAG is
invalid after one repair attempt, the canonical plan is used and the
fallback is LOGGED (plan_fallback) - never silent.

Also provides `replan_downstream()` used by the re-planning node.
"""
from __future__ import annotations

import json
import re

from sdlc_orchestrator.workflow import dag
from sdlc_orchestrator.core.events import log_event
from sdlc_orchestrator.llm.client import AgentOutputError, invoke_json
from sdlc_orchestrator.workflow.stage import stage
from sdlc_orchestrator.core.state import Task, dump

SYSTEM_PROMPT = """You are a staff engineer decomposing a requirement spec into an executable task DAG \
for a Java 21 / Spring Boot 4 URL-shortener. Return ONLY a JSON array of tasks:

  {"id": "<kebab-case slug>", "description": "<one imperative line>",
   "depends_on": ["<task id>", ...], "owner_stage": "<stage>"}

owner_stage must be one of:
  design      OpenAPI contract + Flyway schema + component plan + ADRs
  scaffold    generate the Maven project from Spring Initializr (GREENFIELD only)
  impl_data   schema/entities/repositories/Redis config/Docker packaging
  impl_api    controllers/services/validation/security/rate limiting/tests
  testing     run the Maven test suite
  docs        README/CHANGELOG/OpenAPI/ADR documents
  release     final guardrails + human approval

Hard rules (a validator rejects violations):
  * impl_data and impl_api tasks MUST NOT depend on each other (they run in parallel) and both depend \
only on design/scaffold work.
  * testing MUST depend on BOTH an impl_data and an impl_api task (this is the sync point).
  * no cycles; every depends_on id must exist; at least one task per required stage.
  * greenfield requires a scaffold task; brownfield must NOT have one."""


def _build(raw: list, plan_version: int) -> list[dict]:
    tasks = []
    for t in raw:
        tid = re.sub(r"[^a-z0-9]+", "-", str(t.get("id", "")).lower()).strip("-")
        tasks.append(dump(Task(
            id=tid, description=str(t.get("description", "")), depends_on=[
                re.sub(r"[^a-z0-9]+", "-", str(d).lower()).strip("-") for d in t.get("depends_on", [])],
            owner_stage=str(t.get("owner_stage", "")), plan_version=plan_version)))
    return tasks


def _decompose_impl(state: dict) -> dict:
    run_id = state["run_id"]
    mode = state.get("mode", "greenfield")
    plan_version = state.get("plan_version", 0) + 1
    spec = json.dumps(state["requirement_spec"])
    user = f"MODE: {mode}\nREQUIREMENT SPEC:\n{spec}"
    if mode == "brownfield":
        user += "\n\n(The project already exists: do NOT include a scaffold task.)"

    tasks: list[dict] = []
    errors: list[str] = []
    for attempt in (1, 2):
        try:
            raw = invoke_json("decomposition", SYSTEM_PROMPT, user, run_id=run_id, expect=list)
            tasks = _build(raw, plan_version)
            errors = dag.validate_dag(tasks, mode)
        except (AgentOutputError, TypeError, AttributeError) as exc:
            errors = [f"unparseable task list: {exc}"]
        if not errors:
            break
        user += "\n\nYour previous DAG was INVALID:\n- " + "\n- ".join(errors) + "\nReturn a corrected JSON array."

    detail = {}
    if errors:
        log_event(run_id, "decomposition", "plan_fallback", {"errors": errors}, actor="system:contracts",
                  outcome="fallback", reason="model DAG invalid twice; using canonical plan")
        tasks = dag.default_plan(mode, plan_version)
        detail["fallback"] = True

    waves = dag.topo_waves(tasks)
    detail.update({"task_count": len(tasks), "waves": waves})
    return {"tasks": tasks, "plan_version": plan_version, "plan_waves": waves, "_detail": detail}


decomposition_node = stage("decomposition")(_decompose_impl)


def replan_downstream(state: dict, cause: str, detail: str) -> list[dict]:
    """Regenerate every task downstream of `design` after an upstream
    change. Returns the tasks to merge: old downstream tasks marked
    SUPERSEDED + fresh replacements (plan_version bumped)."""
    run_id = state["run_id"]
    mode = state.get("mode", "greenfield")
    new_version = state.get("plan_version", 1) + 1
    old = [t for t in state.get("tasks", []) if t.get("status") != "superseded"]
    design_ids = {t["id"] for t in old if t["owner_stage"] == "design"}
    downstream_ids: set[str] = set()
    for d in design_ids:
        downstream_ids |= dag.downstream(old, d)

    superseded = [{**t, "status": "superseded"} for t in old if t["id"] in downstream_ids]
    # Keep the design task (it will re-run) and anything upstream of it as-is but pending for design.
    redo_design = [{**t, "status": "pending", "plan_version": new_version} for t in old if t["id"] in design_ids]

    canonical = dag.default_plan(mode, new_version)
    replacements = [
        {**t, "id": f"{t['id']}-v{new_version}", "depends_on": [
            (f"{d}-v{new_version}" if d in {c['id'] for c in canonical if c['owner_stage'] != 'design'} else d)
            for d in t["depends_on"]]}
        for t in canonical if t["owner_stage"] != "design"
    ]
    # design ids in replacements may differ from the live design task id
    live_design = next(iter(design_ids), "design-api-contract")
    for t in replacements:
        t["depends_on"] = [live_design if d == "design-api-contract" else d for d in t["depends_on"]]

    log_event(run_id, "replan", "tasks_regenerated", {
        "cause": cause, "superseded": [t["id"] for t in superseded], "new": [t["id"] for t in replacements],
        "new_plan_version": new_version, "detail": detail[:300],
    }, actor="agent:decomposition", reason=cause)
    return superseded + redo_design + replacements
