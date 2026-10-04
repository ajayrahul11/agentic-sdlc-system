"""
Policy / guardrail gates (Core Requirement #6 + document Day 3).

A plain-Python enforceable checklist (OPA is overkill for the time box),
run as a BLOCKING gate: a failing check stops the state from advancing and
feeds the retry / rollback edges in graph.py. Checklist from the document:

  1. no secrets in code/config
  2. input validation present (@Valid on every @RequestBody + constraint
     annotations on the request DTO)
  3. authentication on mutating endpoints
  4. OpenAPI contract matches the implementation (both directions)

plus project NFR checks (rate limiting, expiry/TTL, cache + counter via
Redis for the full strategy, Dockerised, tests present) and brownfield
safety (applied Flyway migrations are immutable).

Stages:
  design           -> check_design_completeness
  quality_gate     -> code checks on the whole repo on disk
  release_readiness-> quality_gate checks + documentation/packaging
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.java_scan import MUTATING, contract_endpoints, extract_endpoints, normalize_path, request_body_types

REQUIRED_GREENFIELD_ENDPOINTS = {
    ("POST", "/api/shorten"),
    ("GET", "/{}"),
    ("GET", "/api/analytics/{}"),
}
REQUIRED_ADR_TOPICS = {
    "id generation": r"id[\s-]*generation|identifier|base62|snowflake",
    "caching": r"cach",
    "consistency": r"consisten|eventual",
    "rate limiting": r"rate[\s-]*limit",
    "expiry/ttl": r"expir|ttl",
}
VALID_BRANCHES = {"impl_data", "impl_api"}


@dataclass
class GuardrailResult:
    passed: bool
    failures: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _main_code(files: dict[str, str]) -> dict[str, str]:
    return {p: c for p, c in files.items() if p.startswith("src/main/") or (not p.startswith("src/") and p.endswith(".java"))}


def _java_main(files: dict[str, str]) -> dict[str, str]:
    return {p: c for p, c in _main_code(files).items() if p.endswith(".java")}


def _all_main_text(files: dict[str, str]) -> str:
    return "\n".join(_main_code(files).values())


# ---------------------------------------------------------------------------
# 1. Secrets
# ---------------------------------------------------------------------------

_JAVA_SECRET = re.compile(r"""(?ix)\b(?:password|passwd|secret|api[_-]?key|apikey|token)\b\s*=\s*"(?P<v>[^"]{4,})\"""")
_YAML_SECRET = re.compile(r"""(?imx)^\s*(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*(?P<v>[^\s#]+)""")
_PEM = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
_KEY_LITERAL = re.compile(r"\b(?:sk-ant-|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})")


def check_no_hardcoded_secrets(files: dict[str, str]) -> list[str]:
    failures = []
    for path, content in _main_code(files).items():
        if _PEM.search(content) or _KEY_LITERAL.search(content):
            failures.append(f"secret: key material / API key literal in {path}")
        if path.endswith(".java"):
            for m in _JAVA_SECRET.finditer(content):
                failures.append(f"secret: possible hardcoded secret in {path} ({m.group(0)[:40]}...)")
        elif path.endswith((".yml", ".yaml", ".properties")):
            for m in _YAML_SECRET.finditer(content):
                v = m.group("v").strip("\"'")
                if not v.startswith("${") and v not in ("", "null", "~"):
                    failures.append(f"secret: literal credential in {path} ({m.group(0).strip()[:40]}...) - use ${{ENV_VAR}}")
    return failures


# ---------------------------------------------------------------------------
# 2. Input validation
# ---------------------------------------------------------------------------

def check_input_validation(files: dict[str, str]) -> list[str]:
    failures = []
    java = _java_main(files)
    for path, content in java.items():
        if "@RestController" not in content and "@Controller" not in content:
            continue
        for m in re.finditer(r"@RequestBody", content):
            start = max(content.rfind("(", 0, m.start()), content.rfind(",", 0, m.start()))
            ends = [i for i in (content.find(",", m.end()), content.find(")", m.end())) if i != -1]
            window = content[start: min(ends) if ends else m.end() + 80]
            if "@Valid" not in window:
                failures.append(f"validation: {path}: @RequestBody parameter without @Valid/@Validated")
    for type_name in request_body_types(java):
        dto = next((c for p, c in java.items() if p.endswith(f"/{type_name}.java")), None)
        if dto is not None and "jakarta.validation.constraints" not in dto:
            failures.append(f"validation: request DTO {type_name} has no jakarta.validation constraint annotations")
    return failures


# ---------------------------------------------------------------------------
# 3. Authentication on mutating endpoints
# ---------------------------------------------------------------------------

def check_auth_on_mutating_endpoints(files: dict[str, str]) -> list[str]:
    java = _java_main(files)
    mutating = [e for e in extract_endpoints(java) if e[0] in MUTATING]
    if not mutating:
        return []
    text = "\n".join(java.values())
    pom = files.get("pom.xml", "")
    failures = []
    if "spring-boot-starter-security" not in pom:
        failures.append("auth: mutating endpoints exist but pom.xml has no spring-boot-starter-security")
    has_chain = "SecurityFilterChain" in text
    enforces = bool(re.search(r"\.authenticated\(\)|hasRole\(|hasAnyRole\(|hasAuthority\(|hasAnyAuthority\(", text)) or "@PreAuthorize" in text or "@Secured" in text
    if not (has_chain and enforces) and not ("@PreAuthorize" in text or "@Secured" in text):
        failures.append("auth: no SecurityFilterChain requiring authentication found for mutating endpoints "
                        f"({', '.join(f'{m} {p}' for m, p in sorted(mutating))})")
    if re.search(r"anyRequest\(\)\s*\.permitAll\(\)", text) and not re.search(r"\.authenticated\(\)|hasRole|hasAuthority", text):
        failures.append("auth: anyRequest().permitAll() leaves mutating endpoints unauthenticated")
    return failures


# ---------------------------------------------------------------------------
# 4. OpenAPI contract <-> implementation
# ---------------------------------------------------------------------------

def check_contract_matches(openapi: dict, files: dict[str, str]) -> list[str]:
    declared = contract_endpoints(openapi)
    implemented = extract_endpoints(_java_main(files))
    failures = []
    for method, path in sorted(declared - implemented):
        failures.append(f"contract: {method} {path} is declared in the OpenAPI contract but not implemented")
    for method, path in sorted(implemented - declared):
        failures.append(f"contract: {method} {path} is implemented but not declared in the OpenAPI contract")
    return failures


# ---------------------------------------------------------------------------
# Hygiene + NFR checks
# ---------------------------------------------------------------------------

def check_code_hygiene(files: dict[str, str]) -> list[str]:
    failures = []
    for path, content in _java_main(files).items():
        if re.search(r'@CrossOrigin\s*(\(\s*(origins\s*=\s*)?"\*"|\(\s*\)|$|\n)', content):
            failures.append(f"hygiene: wildcard @CrossOrigin in {path}")
        if re.search(r"System\.(out|err)\.print|\.printStackTrace\(\)", content):
            failures.append(f"hygiene: System.out/printStackTrace in production code {path} (use SLF4J)")
    return failures


def check_nfr_features(files: dict[str, str], strategy: str = "full") -> list[str]:
    """Document NFRs that must be visible in the code: rate limiting,
    TTL/expiry, cache-aside + atomic counter via Redis (full strategy),
    async analytics, health/metrics endpoints."""
    text = _all_main_text(files)
    failures = []
    if not re.search(r"(?i)rate[\s_-]*limit", text):
        failures.append("nfr: no rate limiting found (document requirement)")
    if not re.search(r"(?i)expires?_?at|\bttl\b", text):
        failures.append("nfr: no TTL/expiry handling found (document requirement)")
    if strategy == "full":
        if not re.search(r"StringRedisTemplate|RedisTemplate|ReactiveRedisTemplate|@Cacheable", text):
            failures.append("nfr: no Redis usage found (cache-aside + counter required by the full strategy)")
        if not re.search(r"\.increment\(|opsForValue\(\)\.increment|INCR\b", text):
            failures.append("nfr: no atomic Redis counter (INCR) found for ID generation / click counting")
        if not re.search(r"@Scheduled|@Async|ExecutorService|CompletableFuture", text):
            failures.append("nfr: no asynchronous write-behind mechanism found for analytics (@Scheduled/@Async)")
    pom = files.get("pom.xml", "")
    if pom and "spring-boot-starter-actuator" not in pom:
        failures.append("nfr: actuator starter missing (health/metrics endpoints)")
    return failures


def check_tests_present(files: dict[str, str]) -> list[str]:
    tests = {p: c for p, c in files.items() if p.startswith("src/test/") and p.endswith(".java")}
    n = sum(c.count("@Test") + c.count("@ParameterizedTest") for c in tests.values())
    failures = []
    if n < 3:
        failures.append(f"tests: only {n} @Test methods found (expected unit + integration + contract tests)")
    if not any(p.endswith("ContractTest.java") for p in tests):
        failures.append("tests: no ContractTest.java (must assert every OpenAPI path+method is served)")
    return failures


def check_packaging(files: dict[str, str]) -> list[str]:
    failures = []
    for required in ("Dockerfile", "docker-compose.yml", "pom.xml", "docs/openapi.yaml"):
        if required not in files:
            failures.append(f"packaging: {required} missing (service must be Dockerised and documented)")
    return failures


def check_release_docs(files: dict[str, str]) -> list[str]:
    return [f"docs: {f} missing" for f in ("README.md", "CHANGELOG.md") if f not in files]


def check_migrations_immutable(changes: list[tuple[str, str]]) -> list[str]:
    """Brownfield: previously committed Flyway migrations must never be
    edited or deleted (checksum drift breaks every deployed database)."""
    return [
        f"migrations: {path} was {'deleted' if st == 'D' else 'modified'} - applied migrations are immutable; add a new V<n+1> file"
        for st, path in changes
        if "db/migration/V" in path and st in ("M", "D")
    ]


# ---------------------------------------------------------------------------
# Design completeness (blocking gate between Design and Implementation)
# ---------------------------------------------------------------------------

def check_design_completeness(design: dict, mode: str, existing_contract: dict | None = None,
                              existing_max_migration: int = 0) -> list[str]:
    failures = []
    contract = design.get("api_contract") or {}
    declared = contract_endpoints(contract)
    if not declared:
        failures.append("design: api_contract.paths is empty or malformed (need OpenAPI paths with HTTP methods)")
    if mode == "greenfield":
        for method, path in sorted(REQUIRED_GREENFIELD_ENDPOINTS - declared):
            failures.append(f"design: required endpoint missing from contract: {method} {path}")
    if mode == "brownfield" and existing_contract:
        for method, path in sorted(contract_endpoints(existing_contract) - declared):
            failures.append(f"design: brownfield contract dropped existing endpoint {method} {path} (breaking change)")

    migrations = design.get("migrations") or []
    if mode == "greenfield" and not any("create table" in (m.get("sql") or "").lower() for m in migrations):
        failures.append("design: no CREATE TABLE migration for the URL mapping store")
    seen_versions = set()
    for m in migrations:
        if not all(m.get(k) for k in ("version", "name", "sql")):
            failures.append(f"design: migration entry incomplete: {m}")
            continue
        try:
            v = int(m["version"])
        except (TypeError, ValueError):
            failures.append(f"design: migration version must be an integer: {m['version']!r}")
            continue
        if v in seen_versions:
            failures.append(f"design: duplicate migration version {v}")
        seen_versions.add(v)
        if v <= existing_max_migration:
            failures.append(f"design: migration V{v} collides with existing migrations (max existing V{existing_max_migration})")
        if not re.fullmatch(r"[a-z0-9_]+", str(m["name"])):
            failures.append(f"design: migration name must be snake_case: {m['name']!r}")

    plan = design.get("component_plan") or []
    if not plan:
        failures.append("design: component_plan is empty (codegen branches need an agreed class list)")
    for c in plan:
        if not c.get("class") or c.get("branch") not in VALID_BRANCHES:
            failures.append(f"design: component needs 'class' and branch in {sorted(VALID_BRANCHES)}: {c}")
    if plan and not {c.get("branch") for c in plan} >= VALID_BRANCHES and mode == "greenfield":
        failures.append("design: component_plan must assign work to BOTH impl_data and impl_api branches")

    adr_text = " ".join(
        f"{a.get('title', '')} {a.get('decision', '')} {a.get('rationale', '')}" for a in design.get("adrs", [])
    ).lower()
    if mode == "greenfield":
        for topic, pattern in REQUIRED_ADR_TOPICS.items():
            if not re.search(pattern, adr_text):
                failures.append(f"design: no ADR covering '{topic}'")
    for a in design.get("adrs", []):
        if not (a.get("decision") and a.get("rationale") and a.get("alternatives_considered")):
            failures.append(f"design: ADR {a.get('title')!r} must state decision, rationale and alternatives_considered")
    return failures


# ---------------------------------------------------------------------------
# Gate runner
# ---------------------------------------------------------------------------

def run_gate(
    stage: str,
    *,
    files: dict[str, str] | None = None,
    design_doc: dict | None = None,
    strategy: str = "full",
    mode: str = "greenfield",
    changes: list[tuple[str, str]] | None = None,
    existing_contract: dict | None = None,
    existing_max_migration: int = 0,
) -> GuardrailResult:
    files = files or {}
    design_doc = design_doc or {}
    failures: list[str] = []

    if stage == "design":
        failures += check_design_completeness(design_doc, mode, existing_contract, existing_max_migration)

    if stage in ("quality_gate", "release_readiness"):
        failures += check_no_hardcoded_secrets(files)
        failures += check_input_validation(files)
        failures += check_auth_on_mutating_endpoints(files)
        failures += check_contract_matches(design_doc.get("api_contract") or {}, files)
        failures += check_code_hygiene(files)
        failures += check_nfr_features(files, strategy)
        failures += check_tests_present(files)
        failures += check_packaging(files)
        failures += check_release_docs(files) if stage == "release_readiness" else []
        if mode == "brownfield":
            failures += check_migrations_immutable(changes or [])

    return GuardrailResult(passed=not failures, failures=failures)
