"""
Codebase Reasoning (Core Requirement #3) - brownfield only.

Builds a real inventory of the GENERATED project (classes, endpoints,
entities, migrations, current OpenAPI contract) and has the model reason
about which modules/APIs/data flows an enhancement touches BEFORE design
and codegen run. This is the "codebase-impact reasoning, not just
codegen" the brief asks for.

Anti-hallucination: every path the model names is checked against disk.
Non-existent paths trigger one corrective retry; any that remain are moved
to `new_files` (and logged as `hallucinated_paths`) so impacted_modules
only ever contains real files.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from sdlc_orchestrator.core import config
from sdlc_orchestrator.core.events import log_event
from sdlc_orchestrator.policy.java_scan import extract_endpoints
from sdlc_orchestrator.llm.client import invoke_json
from sdlc_orchestrator.workflow.nodes.common import repo_path, to_bool_list
from sdlc_orchestrator.integrations.repo_io import existing_migrations, read_repo_files
from sdlc_orchestrator.workflow.stage import stage

SYSTEM_PROMPT = """You are a staff engineer doing impact analysis on an EXISTING Java/Spring Boot \
URL-shortener before an enhancement is designed. You receive the enhancement requirement and an \
inventory of the real codebase (file paths, class heads, endpoints, migrations). Return ONLY a JSON \
object:

- "impacted_modules": list of EXISTING file paths (repo-relative, EXACTLY as in the inventory) that \
must be modified
- "new_files": list of repo-relative paths that must be created
- "impacted_apis": list of "METHOD /path" strings affected or added
- "data_flow_notes": one paragraph: how data flows through the impacted modules and where the new \
logic plugs in
- "schema_impact": what database change is needed (new Flyway migration? new columns?) or "none"
- "risk_notes": list of regression risks (what could break for existing clients/data)
- "regression_tests_to_watch": existing test classes most likely to catch regressions

Only name files that appear in the inventory under impacted_modules."""


def build_inventory(repo: Path, max_files: int = 80) -> dict:
    files = read_repo_files(repo, include_tests=False)
    java = {p: c for p, c in files.items() if p.endswith(".java")}
    classes = []
    for path, content in sorted(java.items()):
        head = "\n".join(content.splitlines()[:12])
        classes.append({"path": path, "head": head})
        if len(classes) >= max_files:
            break
    contract = {}
    contract_file = repo / "docs/openapi.yaml"
    if contract_file.exists():
        import yaml

        contract = yaml.safe_load(contract_file.read_text()) or {}
    return {
        "classes": classes,
        "endpoints": sorted(f"{m} {p}" for m, p in extract_endpoints(java)),
        "migrations": existing_migrations(repo),
        "config_files": [p for p in files if p.startswith("src/main/resources/") and not p.endswith(".sql")],
        "openapi_paths": sorted((contract.get("paths") or {}).keys()),
    }


def _normalise(repo: Path, rel: str) -> str:
    rel = rel.strip().lstrip("./")
    if (repo / rel).exists():
        return rel
    for prefix in ("src/main/java/", "src/main/resources/"):
        if (repo / (prefix + rel)).exists():
            return prefix + rel
    return rel


def _impl(state: dict) -> dict:
    run_id = state["run_id"]
    repo = repo_path(state)
    inventory = build_inventory(repo)
    base_user = (
        f"ENHANCEMENT REQUIREMENT:\n{json.dumps(state['requirement_spec'])}\n\n"
        f"CODEBASE INVENTORY:\n{json.dumps(inventory, indent=1)}"
    )

    user = base_user
    analysis: dict = {}
    existing: list[str] = []
    invalid: list[str] = []
    for attempt in (1, 2):
        analysis = invoke_json("codebase_reasoning", SYSTEM_PROMPT, user, run_id=run_id)
        claimed = [_normalise(repo, p) for p in to_bool_list(analysis.get("impacted_modules"))]
        existing = [p for p in dict.fromkeys(claimed) if (repo / p).is_file()]
        invalid = [p for p in dict.fromkeys(claimed) if not (repo / p).is_file()]
        if not invalid and existing:
            break
        user = base_user + (
            "\n\nYour previous answer was rejected: "
            + (f"these impacted_modules do not exist: {invalid}. " if invalid else "")
            + ("You named no existing file to change. " if not existing else "")
            + "Choose ONLY paths from the inventory (put new files under new_files)."
        )

    if invalid:
        log_event(run_id, "codebase_reasoning", "hallucinated_paths", {"paths": invalid}, actor="system:contracts",
                  outcome="corrected", reason="model named files that do not exist; moved to new_files")
    new_files = list(dict.fromkeys(to_bool_list(analysis.get("new_files")) + invalid))
    analysis["impacted_modules"] = existing
    analysis["new_files"] = new_files
    analysis["inventory_summary"] = {"endpoints": inventory["endpoints"], "migrations": inventory["migrations"]}

    return {
        "impacted_modules": existing,
        "codebase_analysis": analysis,
        "_detail": {"impacted": existing, "new_files": new_files, "apis": analysis.get("impacted_apis", [])},
    }


codebase_reasoning_node = stage("codebase_reasoning")(_impl)
