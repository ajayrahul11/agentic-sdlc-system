"""
Task-DAG utilities for the Decomposition Agent (Core Requirement #2).

The DAG is validated deterministically - never trusted from the model:
no cycles, every depends_on id exists, owner stages are known, and the two
implementation branches (data/infra vs API) are INDEPENDENT of each other
(so they can run concurrently) but both feed `testing` (the sync point).
"""
from __future__ import annotations

from collections import defaultdict, deque

VALID_STAGES = {"design", "scaffold", "impl_data", "impl_api", "testing", "docs", "release"}
GREENFIELD_REQUIRED = {"design", "scaffold", "impl_data", "impl_api", "testing", "docs"}
BROWNFIELD_REQUIRED = {"design", "impl_data", "impl_api", "testing", "docs"}


def _active(tasks: list[dict]) -> list[dict]:
    return [t for t in tasks if t.get("status") != "superseded"]


def _deps(tasks: list[dict]) -> dict[str, list[str]]:
    return {t["id"]: list(t.get("depends_on", [])) for t in tasks}


def ancestors(tasks: list[dict], task_id: str) -> set[str]:
    deps = _deps(tasks)
    seen: set[str] = set()
    stack = list(deps.get(task_id, []))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(deps.get(cur, []))
    return seen


def downstream(tasks: list[dict], task_id: str) -> set[str]:
    """All tasks that (transitively) depend on `task_id`."""
    children: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        for d in t.get("depends_on", []):
            children[d].append(t["id"])
    seen: set[str] = set()
    stack = list(children.get(task_id, []))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(children.get(cur, []))
    return seen


def topo_waves(tasks: list[dict]) -> list[list[str]]:
    """Kahn's algorithm grouped into waves; tasks in one wave can run in
    parallel. Raises ValueError on a cycle."""
    tasks = _active(tasks)
    ids = {t["id"] for t in tasks}
    indeg = {t["id"]: len([d for d in t.get("depends_on", []) if d in ids]) for t in tasks}
    children: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        for d in t.get("depends_on", []):
            if d in ids:
                children[d].append(t["id"])
    wave = sorted(i for i, n in indeg.items() if n == 0)
    waves: list[list[str]] = []
    done = 0
    while wave:
        waves.append(wave)
        done += len(wave)
        nxt: list[str] = []
        for tid in wave:
            for c in children[tid]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    nxt.append(c)
        wave = sorted(nxt)
    if done != len(ids):
        raise ValueError("task graph contains a cycle")
    return waves


def validate_dag(tasks: list[dict], mode: str = "greenfield") -> list[str]:
    errors: list[str] = []
    tasks = _active(tasks)
    if not tasks:
        return ["no tasks produced"]

    ids = [t["id"] for t in tasks]
    if len(set(ids)) != len(ids):
        errors.append("duplicate task ids")
    idset = set(ids)

    for t in tasks:
        if t.get("owner_stage") not in VALID_STAGES:
            errors.append(f"task {t['id']!r}: unknown owner_stage {t.get('owner_stage')!r}")
        for d in t.get("depends_on", []):
            if d not in idset:
                errors.append(f"task {t['id']!r} depends on unknown task {d!r}")
            if d == t["id"]:
                errors.append(f"task {t['id']!r} depends on itself")

    if errors:
        return errors

    try:
        topo_waves(tasks)
    except ValueError as exc:
        return [str(exc)]

    required = GREENFIELD_REQUIRED if mode == "greenfield" else BROWNFIELD_REQUIRED
    stages = {t["owner_stage"] for t in tasks}
    for stage in sorted(required - stages):
        errors.append(f"missing a task for stage {stage!r}")
    if errors:
        return errors

    by_stage = defaultdict(list)
    for t in tasks:
        by_stage[t["owner_stage"]].append(t["id"])

    # Parallel branches must be independent of each other...
    for a, b in (("impl_data", "impl_api"), ("impl_api", "impl_data")):
        for ta in by_stage[a]:
            if ancestors(tasks, ta) & set(by_stage[b]):
                errors.append(f"{a} task {ta!r} depends on a {b} task - branches must be able to run in parallel")
    # ...and testing is the sync point that waits for BOTH.
    for tt in by_stage["testing"]:
        anc = ancestors(tasks, tt)
        for branch in ("impl_data", "impl_api"):
            if not (anc & set(by_stage[branch])):
                errors.append(f"testing task {tt!r} does not wait for {branch} (missing sync point)")
    return errors


def default_plan(mode: str = "greenfield", plan_version: int = 1) -> list[dict]:
    """Canonical DAG used as the logged, deterministic fallback when the
    model cannot produce a valid one."""
    t = lambda i, d, deps, st: {  # noqa: E731
        "id": i, "description": d, "depends_on": list(deps), "status": "pending",
        "owner_stage": st, "plan_version": plan_version,
    }
    plan = [t("design-api-contract", "Design OpenAPI contract, Flyway schema, component plan and ADRs", [], "design")]
    impl_dep = ["design-api-contract"]
    if mode == "greenfield":
        plan.append(t("scaffold-project", "Scaffold Spring Boot project from Spring Initializr", ["design-api-contract"], "scaffold"))
        impl_dep = ["scaffold-project"]
    plan += [
        t("implement-data-layer", "Implement schema migrations, entities, repositories, Redis config, Docker packaging", impl_dep, "impl_data"),
        t("implement-api-layer", "Implement controllers, services, validation, security, rate limiting and tests", impl_dep, "impl_api"),
        t("run-tests", "Run the full Maven test suite against the generated code", ["implement-data-layer", "implement-api-layer"], "testing"),
        t("write-docs", "Generate README, CHANGELOG, OpenAPI and ADR documents", ["run-tests"], "docs"),
        t("release-readiness", "Final guardrail pass and human release approval", ["write-docs"], "release"),
    ]
    return plan
