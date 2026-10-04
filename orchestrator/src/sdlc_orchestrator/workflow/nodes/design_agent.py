"""
Design Agent: OpenAPI contract + Flyway schema + component plan + ADRs.

The design is the contract EVERY later stage is checked against:
  * api_contract   -> guardrail "OpenAPI matches implementation", ContractTest
  * migrations     -> written verbatim to Flyway files (no LLM drift)
  * component_plan -> the shared class list that lets the two parallel
                      codegen branches agree on names/signatures
  * adrs           -> decisions the brief says must be stated explicitly

A design that fails the completeness gate (guardrails.check_design_
completeness) is retried once with the failures as feedback, then fails
the run loudly. On a re-plan, `design_feedback` carries the upstream
failure (schema/contract mismatch or human rejection) so Design is
re-run *informed*, not blindly restarted.
"""
from __future__ import annotations

import json

import yaml

from sdlc_orchestrator.core.events import log_event
from sdlc_orchestrator.policy.guardrails import run_gate
from sdlc_orchestrator.llm.client import AgentOutputError, invoke_json
from sdlc_orchestrator.workflow.nodes.common import repo_path, set_task_status
from sdlc_orchestrator.integrations.repo_io import max_migration_version
from sdlc_orchestrator.workflow.stage import stage

SYSTEM_PROMPT = """You are a staff engineer designing a Java 21 / Spring Boot 4 URL shortener. Stack: \
Spring Web, Spring Data JPA (ddl-auto=validate - the Flyway migrations are authoritative), Flyway, \
PostgreSQL (mapping store), Redis (cache + atomic ID counter + click counters), Spring Security (API \
key on mutating endpoints), Actuator. Base package: com.rahul.urlshortener.

Return ONLY a JSON object with these keys:

"api_contract": a complete OpenAPI 3.0.3 document as JSON (openapi, info, paths, components). Each \
operation lists request body schema, and responses incl. 302/200/201, 400 (validation), 401 (missing \
API key), 404, 410 (expired), 429 (rate limited). GREENFIELD must include exactly these core \
operations: POST /api/shorten, GET /{shortCode}, GET /api/analytics/{shortCode}.

"migrations": list of {"version": "1", "name": "snake_case_name", "sql": "<PostgreSQL DDL>"} - Flyway \
files V<version>__<name>.sql. Columns must be what the JPA entities need (id, short_code unique, \
original_url, created_at, expires_at, click_count...).

"component_plan": EVERY Java class to create or change, as \
{"class": "com.rahul.urlshortener.web.ShortenController", "path": "src/main/java/.../ShortenController.java", \
"layer": "domain|repository|config|dto|service|web|security|filter|exception|test", \
"branch": "impl_data|impl_api", "change": "new|modify", "responsibility": "...", "signatures": ["public ..."]}. \
Two PARALLEL teams implement from this list, so signatures of anything the other branch calls MUST be \
exact. impl_data owns: domain entities, repositories, JPA/Redis/properties config, application.yml, \
Dockerfile, docker-compose.yml. impl_api owns: dto, service, web, security, filter (rate limit), exception \
handling, scheduled analytics flush, and ALL tests incl. ContractTest.

"adrs": list of {"title","decision","rationale","alternatives_considered"} covering AT LEAST: \
(1) ID generation - base62 counter vs hash vs Snowflake-style distributed ID, incl. collision risk and \
horizontal scalability trade-off; (2) consistency - strong for mapping, eventual for click analytics \
(Redis write-behind flushed async, redirect never blocks on analytics); (3) caching - cache-aside on \
redirect reads and the invalidation path on update/delete/custom-alias overwrite; (4) rate limiting \
approach; (5) link expiry/TTL semantics.

"configuration": exact Spring property names both teams must use, e.g. \
{"app.security.api-key": "${SHORTENER_API_KEY}", "app.rate-limit.requests-per-minute": "60", \
"app.link.default-ttl-days": "30", "app.analytics.flush-interval-ms": "5000", \
"app.cache.redirect-ttl-seconds": "3600"}.

BROWNFIELD: you receive the existing contract, migrations and codebase analysis. Return the FULL resulting \
contract (existing + additions; never remove or break an existing operation), only NEW migrations with \
version greater than the existing maximum (never edit applied ones), and component_plan entries ONLY for \
new/modified classes. No prose, no markdown fences."""


def _existing_contract(repo) -> dict | None:
    f = repo / "docs/openapi.yaml"
    return (yaml.safe_load(f.read_text()) or None) if f.exists() else None


def _impl(state: dict) -> dict:
    run_id = state["run_id"]
    mode = state.get("mode", "greenfield")
    repo = repo_path(state)
    existing_contract = _existing_contract(repo) if mode == "brownfield" else None
    max_mig = max_migration_version(repo) if mode == "brownfield" else 0

    payload = {
        "mode": mode,
        "requirement_spec": state["requirement_spec"],
        "codebase_analysis": state.get("codebase_analysis"),
        "existing_openapi": existing_contract,
        "existing_max_migration_version": max_mig,
        "design_feedback": state.get("design_feedback") or None,
        "previous_design_that_failed": (
            {k: state.get("design_doc", {}).get(k) for k in ("migrations", "component_plan", "adrs")}
            if state.get("design_feedback") else None),
    }
    user = json.dumps(payload)

    design: dict = {}
    failures: list[str] = []
    for attempt in (1, 2):
        design = invoke_json("design", SYSTEM_PROMPT, user, run_id=run_id)
        gate = run_gate("design", design_doc=design, mode=mode, existing_contract=existing_contract,
                        existing_max_migration=max_mig)
        failures = gate.failures
        if gate.passed:
            break
        log_event(run_id, "design", "guardrail_fail", {"failures": failures, "attempt": attempt},
                  actor="system:gate", outcome="fail", reason="; ".join(failures)[:400])
        user = json.dumps({**payload, "your_previous_design_failed_these_checks": failures})
    if failures:
        raise AgentOutputError("design failed the completeness gate twice: " + "; ".join(failures))

    return {
        "design_doc": design,
        "design_feedback": "",
        "tasks": set_task_status(state, ["design"], "done"),
        "_detail": {
            "endpoints": sorted(design["api_contract"].get("paths", {}).keys()),
            "components": len(design.get("component_plan", [])),
            "adrs": [a.get("title") for a in design.get("adrs", [])],
            "replanned": bool(state.get("design_feedback")),
        },
    }


design_node = stage("design")(_impl)
