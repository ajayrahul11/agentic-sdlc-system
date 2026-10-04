"""
Entry (PRE) and exit (POST) conditions for every graph node. Each entry is
a function state -> list[str] of violated conditions (empty = OK). They
are enforced by app/stage.py, which raises Precondition/PostconditionError
and writes an audit event when one is violated.

These are STRUCTURAL contracts (is the data the next node needs actually
there and well-formed?). Quality judgements (secrets, validation, auth,
OpenAPI match, tests) live in guardrails.py and the testing agent, where
a failure triggers retry/rollback rather than aborting the run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from app import config, dag, gitops
from app.guardrails import REQUIRED_GREENFIELD_ENDPOINTS
from app.java_scan import contract_endpoints

Check = Callable[[dict], list[str]]


def _repo(state: dict) -> Path:
    return Path(state.get("workspace", {}).get("repo_path") or config.target_repo())


def _has_project(repo: Path) -> bool:
    return gitops.is_repo(repo) and (repo / "pom.xml").exists()


# ----------------------------- PRE -----------------------------

def pre_requirements(s: dict) -> list[str]:
    return [] if (s.get("raw_requirement") or "").strip() else ["raw_requirement is empty"]


def pre_workspace(s: dict) -> list[str]:
    repo = config.target_repo()
    scenario = s.get("scenario")
    errs = []
    if scenario == "greenfield":
        if repo.exists() and any(repo.iterdir()):
            errs.append(
                f"greenfield requires an EMPTY/absent target repo but {repo} already contains files. "
                "Greenfield creates the project; use the brownfield scenario to extend it, "
                "or run `python -m app.main reset-target` first."
            )
    elif scenario == "brownfield":
        if not _has_project(repo):
            errs.append(f"brownfield requires an existing generated project (git repo + pom.xml) at {repo}; run the greenfield scenario first")
    if repo.exists() and gitops.is_repo(repo) and not gitops.is_clean(repo):
        errs.append(f"target repo {repo} has uncommitted changes; commit or discard them before a new run")
    return errs


def pre_decomposition(s: dict) -> list[str]:
    errs = []
    if not s.get("requirement_spec"):
        errs.append("requirement_spec missing")
    if s.get("ambiguities") and not s.get("ambiguities_resolved"):
        errs.append("unresolved ambiguities - human clarification required before decomposition")
    if not s.get("workspace", {}).get("base_sha"):
        errs.append("workspace not prepared")
    return errs


def pre_codebase_reasoning(s: dict) -> list[str]:
    return [] if _has_project(_repo(s)) else ["no existing project (pom.xml) to analyse"]


def pre_design(s: dict) -> list[str]:
    errs = []
    if not s.get("requirement_spec"):
        errs.append("requirement_spec missing")
    if not s.get("tasks"):
        errs.append("task DAG missing")
    if s.get("mode") == "brownfield" and not s.get("impacted_modules"):
        errs.append("brownfield design requires codebase-reasoning output (impacted_modules)")
    return errs


def pre_scaffold(s: dict) -> list[str]:
    return [] if (s.get("design_doc") or {}).get("api_contract") else ["design_doc.api_contract missing"]


def _pre_impl(s: dict) -> list[str]:
    errs = []
    if not (s.get("design_doc") or {}).get("component_plan"):
        errs.append("design_doc.component_plan missing")
    if not (_repo(s) / "pom.xml").exists():
        errs.append("pom.xml missing - scaffold must run before implementation")
    return errs


def pre_quality_gate(s: dict) -> list[str]:
    bs = s.get("branch_status") or {}
    return [f"branch {b} has not completed" for b in ("impl_data", "impl_api") if b not in bs]


def pre_testing(s: dict) -> list[str]:
    errs = []
    if s.get("guardrail_failures"):
        errs.append("quality gate has unresolved failures; tests must not run on gated code")
    if not (_repo(s) / "pom.xml").exists():
        errs.append("pom.xml missing")
    return errs


def pre_commit(s: dict) -> list[str]:
    return [] if (s.get("test_results") or {}).get("passed") else ["tests have not passed; nothing is approved to commit"]


pre_docs = pre_commit


def pre_release(s: dict) -> list[str]:
    errs = pre_commit(s)
    if not s.get("docs"):
        errs.append("docs stage has not run")
    return errs


PRE: dict[str, Check] = {
    "requirements": pre_requirements,
    "workspace": pre_workspace,
    "decomposition": pre_decomposition,
    "codebase_reasoning": pre_codebase_reasoning,
    "design": pre_design,
    "scaffold": pre_scaffold,
    "impl_data": _pre_impl,
    "impl_api": _pre_impl,
    "quality_gate": pre_quality_gate,
    "testing": pre_testing,
    "commit": pre_commit,
    "docs": pre_docs,
    "release_readiness": pre_release,
}


# ----------------------------- POST -----------------------------

def post_requirements(s: dict) -> list[str]:
    spec = s.get("requirement_spec") or {}
    errs = [f"requirement_spec.{k} missing" for k in ("problem_statement", "in_scope", "non_functional_requirements") if k not in spec]
    if not isinstance(s.get("ambiguities"), list):
        errs.append("ambiguities must be a list")
    return errs


def post_workspace(s: dict) -> list[str]:
    ws = s.get("workspace") or {}
    return [f"workspace.{k} missing" for k in ("repo_path", "branch", "base_sha") if not ws.get(k)]


def post_decomposition(s: dict) -> list[str]:
    return dag.validate_dag(s.get("tasks") or [], s.get("mode", "greenfield"))


def post_codebase_reasoning(s: dict) -> list[str]:
    return [] if s.get("impacted_modules") else ["impact analysis named no existing files to change"]


def post_design(s: dict) -> list[str]:
    d = s.get("design_doc") or {}
    errs = []
    if not contract_endpoints(d.get("api_contract") or {}):
        errs.append("design_doc.api_contract has no paths")
    if s.get("mode", "greenfield") == "greenfield" and (REQUIRED_GREENFIELD_ENDPOINTS - contract_endpoints(d.get("api_contract") or {})):
        errs.append("design contract misses required greenfield endpoints")
    return errs


def post_scaffold(s: dict) -> list[str]:
    if s.get("mode") == "brownfield":
        return []
    repo = _repo(s)
    errs = [f"{f} missing after scaffold" for f in ("pom.xml", "mvnw") if not (repo / f).exists()]
    if not s.get("workspace", {}).get("scaffold_sha"):
        errs.append("scaffold commit not recorded")
    return errs


def _post_impl(branch: str) -> Check:
    def check(s: dict) -> list[str]:
        owned = [p for p, b in (s.get("artifact_owner") or {}).items() if b == branch]
        return [] if owned else [f"{branch} produced no files"]
    return check


def post_testing(s: dict) -> list[str]:
    tr = s.get("test_results") or {}
    return [] if "passed" in tr and "failure_class" in tr else ["test_results incomplete"]


def post_docs(s: dict) -> list[str]:
    return [] if (s.get("docs") or {}).get("files") else ["docs stage wrote no files"]


POST: dict[str, Check] = {
    "requirements": post_requirements,
    "workspace": post_workspace,
    "decomposition": post_decomposition,
    "codebase_reasoning": post_codebase_reasoning,
    "design": post_design,
    "scaffold": post_scaffold,
    "impl_data": _post_impl("impl_data"),
    "impl_api": _post_impl("impl_api"),
    "testing": post_testing,
    "docs": post_docs,
}
