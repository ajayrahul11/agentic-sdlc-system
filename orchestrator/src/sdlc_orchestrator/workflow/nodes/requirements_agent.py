"""
Requirement Understanding (Core Requirement #1).

Raw prompt -> structured spec + explicit ambiguity list + logged
assumptions. Blocking ambiguities route the graph to a human clarification
checkpoint (graph.py) instead of the system guessing silently.

Two layers so the ambiguous scenario is reliable rather than prompt-luck:
  1. the model identifies ambiguities/assumptions, and
  2. a deterministic vagueness check ("make the system more reliable")
     adds a blocking ambiguity when a short request uses an
     unmeasurable quality word with no measurable target.
"""
from __future__ import annotations

import re

from sdlc_orchestrator.core.events import log_event
from sdlc_orchestrator.llm.client import invoke_json
from sdlc_orchestrator.workflow.nodes.common import to_bool_list
from sdlc_orchestrator.workflow.stage import stage

# Non-functional requirements every build inherits (from the engineering
# brief). They are merged in deterministically so they cannot be forgotten
# by a model, and they drive Design, Codegen and the guardrail gate.
BASELINE_NFRS = [
    "Redirect path uses cache-aside reads (Redis) so redirects are low-latency; Postgres is the source of truth",
    "Strong consistency for the short-code -> URL mapping; EVENTUAL consistency for click analytics "
    "(Redis counters, write-behind flushed asynchronously) - redirects never block on analytics writes",
    "Collision-free short codes via an atomic Redis counter encoded in base62",
    "Rate limiting per client on all endpoints (HTTP 429 with Retry-After)",
    "Link TTL/expiry (expired links return 410 Gone; cache TTL never exceeds remaining link lifetime)",
    "Input validation on every request (http/https URLs only, max length, alias pattern); errors as RFC 7807 problem details",
    "Authentication on all mutating endpoints (API key from environment; no secrets in code or config)",
    "Observability: Actuator health/metrics endpoints and structured logging",
    "Graceful degradation: redirects still work from Postgres when Redis is unavailable",
    "Dockerised: Dockerfile + docker-compose (app, Postgres, Redis) so the service runs end-to-end with one command",
    "Tested: unit tests, Testcontainers integration tests, and an OpenAPI contract test",
]

VAGUE_TERMS = re.compile(
    r"\b(reliab\w*|robust\w*|scalab\w*|better|faster|quicker|improv\w*|secure\w*|stable|stability|resilien\w*|"
    r"performan\w*|efficien\w*|optimi[sz]\w*|polish\w*|production[- ]ready)\b", re.IGNORECASE)
MEASURABLE = re.compile(r"\d|%|\bms\b|\bslo\b|\bsla\b|\bp\d{2}\b|\bendpoint\b|\balias\b|\bgeo\b|\bshard\w*\b|\bcounter\b|\bredis\b|\bpostgres\b", re.IGNORECASE)

SYSTEM_PROMPT = """You are a senior requirements analyst on a URL-shortener engineering team \
(Java / Spring Boot). Turn the raw request into a structured specification. Return ONLY a JSON \
object with exactly these keys:

- "problem_statement": one paragraph, the normalised engineering problem
- "in_scope": list of concrete capabilities to build or change
- "out_of_scope": list of things explicitly NOT being done
- "acceptance_criteria": list of testable statements
- "non_functional_requirements": list of measurable NFRs implied by the request (may be empty)
- "ambiguities": list of QUESTIONS that block design because a wrong guess would waste the \
whole build (undefined success criteria, unmeasurable goals like "more reliable", conflicting \
requirements). Empty list ONLY if the request is concrete enough to design against.
- "assumptions": list of the assumptions you will make for anything non-blocking, so a human \
can see exactly what was guessed

Be strict: an ambiguity is not a nice-to-have question, it is something you cannot responsibly \
design without. Vague quality words with no target ("reliable", "faster", "scalable") are ambiguities."""


def vagueness_ambiguities(raw: str) -> list[str]:
    words = raw.split()
    if len(words) > 14 or MEASURABLE.search(raw):
        return []
    hits = sorted({m.group(0).lower() for m in VAGUE_TERMS.finditer(raw)})
    return [
        f"'{h}' is not measurable: what concrete target (SLO/latency/availability/failure mode) defines success?"
        for h in hits
    ]


def requirements_node_impl(state: dict) -> dict:
    run_id = state["run_id"]
    scenario = state["scenario"]
    context = {
        "greenfield": "The service does not exist yet; it will be built from scratch.",
        "brownfield": "The service already exists; this is an enhancement to it.",
        "ambiguous": "The request may be underspecified; be strict about what is unclear.",
    }[scenario]
    spec = invoke_json("requirements", SYSTEM_PROMPT, f"CONTEXT: {context}\n\nREQUEST:\n{state['raw_requirement']}", run_id=run_id)

    spec.setdefault("problem_statement", state["raw_requirement"])
    for key in ("in_scope", "out_of_scope", "acceptance_criteria", "non_functional_requirements", "assumptions"):
        spec[key] = to_bool_list(spec.get(key))
    ambiguities = to_bool_list(spec.get("ambiguities"))
    for extra in vagueness_ambiguities(state["raw_requirement"]):
        if extra not in ambiguities:
            ambiguities.append(extra)
    spec["ambiguities"] = ambiguities
    spec["baseline_nfrs"] = BASELINE_NFRS
    assumptions = spec["assumptions"]

    if assumptions:
        log_event(run_id, "requirements", "assumptions_logged", {"assumptions": assumptions}, actor="agent:requirements")
    if ambiguities:
        log_event(run_id, "requirements", "ambiguity_flagged", {"ambiguities": ambiguities}, actor="agent:requirements",
                  reason="blocking ambiguities - human clarification required before design")

    return {
        "requirement_spec": spec,
        "ambiguities": ambiguities,
        "assumptions": assumptions,
        "_outcome": "ambiguous" if ambiguities else "ok",
        "_detail": {"ambiguity_count": len(ambiguities), "assumption_count": len(assumptions)},
    }


requirements_node = stage("requirements")(requirements_node_impl)
