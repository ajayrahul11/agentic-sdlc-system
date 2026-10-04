"""
Engineering Output Generation (Core Requirement #5) - the CodeGen agent.

Two PARALLEL branches (fan-out after scaffold, sync at quality_gate):

  impl_data  schema/infra: Flyway files + docs/openapi.yaml (written
             DETERMINISTICALLY from the design, not by the model), JPA
             entities, repositories, config, application.yml, Dockerfile,
             docker-compose.yml
  impl_api   API: DTOs, services, controllers, security, rate limiting,
             analytics write-behind, exception handling and ALL tests

Both read the Design Agent's `component_plan`, so they agree on class
names and signatures without talking to each other.

Strategies (fallback path):
  full    Redis cache-aside + atomic Redis counter + async write-behind
          analytics + Redis rate limit (the brief's design)
  simple  used after the full strategy fails twice: Postgres-only,
          synchronous counters, in-memory rate limiter. Degraded on
          purpose; recorded in state, events and the generated docs.

Output format is a greppable delimiter protocol, not JSON, because large
Java files are miserable to keep valid inside JSON strings.
"""
from __future__ import annotations

import json
import yaml

from app.events import log_event
from app.llm import invoke_llm
from app.nodes.common import repo_path, set_task_status
from app.repo_io import parse_deletes, parse_file_blocks, read_repo_files, write_files
from app.stage import stage

OUTPUT_FORMAT = """OUTPUT FORMAT - exactly this, nothing else (no prose, no markdown fences around the whole answer). \
Paths are relative to the REPOSITORY ROOT:

===FILE: src/main/java/com/rahul/urlshortener/web/ExampleController.java===
<complete file content>
===END===

Every file must be COMPLETE (never a diff, never "...rest unchanged..."). Allowed locations: src/**, \
Dockerfile, docker-compose.yml, .dockerignore, pom.xml (data branch only).
To REMOVE a file that you created in an earlier attempt, emit a line: ===DELETE: path/to/file==="""

COMMON_RULES = """You are a senior Java engineer writing a Spring Boot 4 / Java 21 URL shortener. The Maven project \
skeleton (pom.xml with the CURRENT Spring Boot 4 starters, Maven wrapper) already exists - its pom.xml is provided; \
use only dependencies it already declares unless you must add one (data branch only, output the COMPLETE pom.xml, \
never pin versions that the Spring Boot BOM manages).

Spring Boot 4 notes (verified against start.spring.io): starters are MODULAR - e.g. spring-boot-starter-webmvc \
(not -web), spring-boot-starter-flyway, and matching *-test starters (spring-boot-starter-webmvc-test, \
spring-boot-starter-security-test ...), plus spring-boot-testcontainers, testcontainers-junit-jupiter and \
testcontainers-postgresql. Boot 4 relocated several packages (and uses Jackson 3 under tools.jackson), and \
Testcontainers 2.x moved PostgreSQLContainer to org.testcontainers.postgresql. ALWAYS read the provided pom.xml for the \
exact artifacts. When unsure of a Boot-4-specific import, AVOID the API: write integration tests with plain \
@SpringBootTest(webEnvironment = RANDOM_PORT), the port from @Value("${local.server.port}"), and java.net.http.HttpClient; \
wire Testcontainers with @DynamicPropertySource (spring-test) and GenericContainer<>("redis:7-alpine") for Redis.

NON-NEGOTIABLE requirements (an automated gate blocks the build if any is missing):
1. Package com.rahul.urlshortener; follow the design's component_plan class names, packages and signatures EXACTLY.
2. Implement EXACTLY the operations in the OpenAPI contract - no extra endpoints, none missing.
3. Every @RequestBody parameter is annotated @Valid, and the request DTO uses jakarta.validation constraints \
(URL must be http/https, max 2048 chars; alias pattern ^[A-Za-z0-9_-]{3,32}$).
4. Mutating endpoints (POST/PUT/PATCH/DELETE) require authentication: a SecurityFilterChain with stateless sessions, \
csrf disabled, GET redirect/health/info permitted, everything else .authenticated(), authenticating an X-API-Key \
header against property app.security.api-key (from env SHORTENER_API_KEY - NEVER a literal secret, no default value).
5. No secrets in code or config (${ENV_VAR} placeholders only); no System.out / printStackTrace (SLF4J); no wildcard CORS.
6. Use EXACTLY the property names from design.configuration.
7. Errors are RFC 7807 ProblemDetail: 400 validation, 401, 404 unknown code, 410 expired link, 429 rate limited \
(with Retry-After).
8. Redirect uses HTTP 302 so clicks are observable."""

FULL_STRATEGY = """STRATEGY: full
- ID generation: Redis INCR on key `shortener:id-counter` -> base62 encode (collision-free by construction).
- Redirect: cache-aside - read Redis `shortener:url:{code}` first, fall back to Postgres, populate cache with TTL = \
min(app.cache.redirect-ttl-seconds, remaining link lifetime). INVALIDATE the cache entry on any update/delete/alias overwrite.
- If Redis is down, redirect still works from Postgres (catch and log, never fail the redirect).
- Analytics: eventual consistency. On redirect do a NON-BLOCKING Redis INCR of `shortener:clicks:{code}` (swallow \
errors); a @Scheduled job (every app.analytics.flush-interval-ms; add @EnableScheduling on a config class) flushes \
the counters into Postgres click_count. GET /api/analytics/{code} returns persisted + pending Redis count.
- Rate limiting: OncePerRequestFilter using Redis INCR+EXPIRE per client IP per minute against \
app.rate-limit.requests-per-minute; over the limit -> 429 + Retry-After.
- Expiry: links have expires_at (default app.link.default-ttl-days); expired -> 410."""

SIMPLE_STRATEGY = """STRATEGY: simple (FALLBACK - the full strategy failed twice; prefer a smaller, boring design that compiles and passes)
- ID generation: encode the Postgres sequence/identity value of the row in base62 (no Redis).
- Redirect: straight Postgres lookup, no cache.
- Analytics: synchronous `UPDATE ... SET click_count = click_count + 1` on redirect; no scheduler.
- Rate limiting: in-memory fixed-window per client IP (ConcurrentHashMap), 429 + Retry-After.
- Expiry: expires_at column, expired -> 410.
- Keep the class count small. Do NOT use Redis in code (it stays in the pom/compose, unused).
- State this degradation in a class-level Javadoc comment on the main service."""

BRANCH_SCOPE = {
    "impl_data": """BRANCH: impl_data (schema / infrastructure). You own and must write:
- JPA entities and Spring Data repositories (columns EXACTLY matching the Flyway migrations in the design - Hibernate \
runs with ddl-auto=validate, so any mismatch fails at startup)
- configuration classes the design assigns to this branch (JPA/Redis/properties record @ConfigurationProperties)
- src/main/resources/application.yml: env-var driven datasource/redis/flyway config, spring.jpa.hibernate.ddl-auto: validate, \
actuator health/info/metrics exposed, all design.configuration properties
- Dockerfile (multi-stage Maven build -> slim JRE 21 runtime, non-root user) and docker-compose.yml \
(services: app, postgres, redis with healthchecks; app configured via environment, SHORTENER_API_KEY from env)
- .dockerignore
DO NOT write: Flyway migration files or docs/openapi.yaml (the orchestrator writes them verbatim from the design), \
controllers, services, DTOs, security, filters, or tests.""",
    "impl_api": """BRANCH: impl_api (API layer + tests). You own and must write:
- DTOs (records with validation), services, controllers, GlobalExceptionHandler (ProblemDetail), SecurityFilterChain + \
API-key filter, rate-limit filter, analytics write-behind (full strategy), base62 ID generator
- ALL tests under src/test/java/com/rahul/urlshortener/:
  * unit test for the base62 ID generator and expiry logic (no containers)
  * an integration test class: @SpringBootTest(webEnvironment = RANDOM_PORT) + Testcontainers PostgreSQL and Redis via \
@DynamicPropertySource (also set app.security.api-key=test-key): shorten (with X-API-Key) -> redirect (302, Location) -> \
analytics; 401 without key; 400 on invalid URL; 404 unknown code; 410 expired link; 429 after exceeding the rate limit
  * ContractTest.java: loads docs/openapi.yaml with org.yaml.snakeyaml.Yaml (from the working directory), and for every \
path+method issues a request through the running app; fails with the message "not served by the application" if the \
response is 404/405 for a declared operation
DO NOT write: entities, repositories, application.yml, Dockerfile, docker-compose.yml, migrations.""",
}


def _select_components(design: dict, branch: str) -> list[dict]:
    return [c for c in design.get("component_plan", []) if c.get("branch") == branch]


def _write_deterministic(repo, design: dict) -> dict[str, str]:
    """Files that come straight from the design with no model involved."""
    files: dict[str, str] = {}
    for m in design.get("migrations", []):
        files[f"src/main/resources/db/migration/V{int(m['version'])}__{m['name']}.sql"] = m["sql"].rstrip() + "\n"
    files["docs/openapi.yaml"] = yaml.safe_dump(design["api_contract"], sort_keys=False)
    return files


def _prior_files(state: dict, branch: str, budget: int = 90_000) -> str:
    owner = state.get("artifact_owner", {})
    out, used = [], 0
    for path, content in (state.get("code_artifacts") or {}).items():
        if owner.get(path) == branch and not path.endswith((".sql", "openapi.yaml")):
            if used + len(content) > budget:
                break
            out.append(f"### CURRENT {path}\n{content}")
            used += len(content)
    return "\n\n".join(out)


def _existing_context(repo, state: dict, budget: int = 60_000) -> str:
    """Brownfield: current contents of the files the impact analysis flagged."""
    out, used = [], 0
    for rel in state.get("impacted_modules", []):
        f = repo / rel
        if f.is_file():
            text = f.read_text()
            if used + len(text) > budget:
                break
            out.append(f"### EXISTING {rel}\n{text}")
            used += len(text)
    return "\n\n".join(out)


def build_prompts(state: dict, branch: str, repo) -> tuple[str, str]:
    strategy = (state.get("codegen_strategy") or "full")
    design = state["design_doc"]
    system = "\n\n".join([COMMON_RULES, FULL_STRATEGY if strategy == "full" else SIMPLE_STRATEGY, BRANCH_SCOPE[branch], OUTPUT_FORMAT])

    parts = [
        f"MODE: {state.get('mode', 'greenfield')}",
        "POM.XML (current):\n" + ((repo / "pom.xml").read_text() if (repo / "pom.xml").exists() else "(missing)"),
        "DESIGN:\n" + json.dumps({k: design.get(k) for k in ("api_contract", "component_plan", "configuration", "adrs")}, indent=1),
        f"YOUR COMPONENTS ({branch}):\n" + json.dumps(_select_components(design, branch), indent=1),
    ]
    if state.get("mode") == "brownfield":
        parts.append("EXISTING FILES TO MODIFY (output the COMPLETE new version of each file you change):\n" + _existing_context(repo, state))
        parts.append("Preserve all existing behaviour and tests; only add/modify what the requirement needs.")
    feedback = state.get("failure_feedback")
    if feedback:
        prior = _prior_files(state, branch)
        if prior:
            parts.append("PREVIOUS ATTEMPT FAILED. Fix the cause. Output ONLY files that need to change (complete content each).\n"
                         f"FAILURE OUTPUT:\n{feedback}")
            parts.append("YOUR FILES FROM THE PREVIOUS ATTEMPT:\n" + prior)
        else:
            parts.append("A PREVIOUS ATTEMPT FAILED and the workspace was reset to the clean scaffold. Regenerate ALL files for "
                         f"your branch from scratch, avoiding the cause.\nFAILURE OUTPUT:\n{feedback}")
    return system, "\n\n".join(parts)


def _run_branch(state: dict, branch: str) -> dict:
    run_id = state["run_id"]
    repo = repo_path(state)
    design = state["design_doc"]
    errors: list[str] = []

    artifacts: dict[str, str] = {}
    removed: list[str] = []
    if branch == "impl_data":
        artifacts.update(_write_deterministic(repo, design))

    components = _select_components(design, branch)
    generated: dict[str, str] = {}
    if components or state.get("failure_feedback"):
        system, user = build_prompts(state, branch, repo)
        text = invoke_llm("codegen", system, user, run_id=run_id)
        generated, problems = parse_file_blocks(text)
        errors += problems

        # ownership discipline: the data branch must not emit tests/controllers etc. and vice versa
        for path in list(generated):
            if branch == "impl_data" and (path.startswith("src/test/") or "/web/" in path or path.endswith("/openapi.yaml") or "/db/migration/" in path):
                errors.append(f"{branch} emitted a file it does not own: {path}")
                generated.pop(path)
            if branch == "impl_api" and (path in ("pom.xml", "Dockerfile", "docker-compose.yml") or path.startswith("src/main/resources/")):
                errors.append(f"{branch} emitted a file it does not own: {path}")
                generated.pop(path)
        artifacts.update(generated)
        deletes, del_problems = parse_deletes(text)
        errors += del_problems
        owned = state.get("artifact_owner") or {}
        for rel in deletes:
            if owned.get(rel) != branch:
                errors.append(f"{branch} tried to delete {rel}, which it did not create in this run")
                continue
            (repo / rel).unlink(missing_ok=True)
            removed.append(rel)
        if not generated and not removed and not state.get("failure_feedback"):
            errors.append(f"{branch}: model produced no usable files")

    written = write_files(repo, artifacts)
    log_event(run_id, branch, "files_written", {"files": written, "deleted": removed, "problems": errors[:5]}, actor=f"agent:codegen:{branch}",
              outcome="ok" if not errors else "partial")
    return {
        "code_artifacts": {**artifacts, **{p: "" for p in removed}},
        "artifact_owner": {**{p: branch for p in set(artifacts) | {
            p for p, b in (state.get("artifact_owner") or {}).items() if b == branch}},
            **{p: "deleted" for p in removed}},
        "branch_status": {branch: {"ok": not errors, "errors": errors, "files": written}},
        "tasks": set_task_status(state, [branch], "in_progress"),
        "_outcome": "ok" if not errors else "partial",
        "_reason": "; ".join(errors)[:300] or None,
        "_detail": {"files": written, "deleted": removed, "strategy": (state.get("codegen_strategy") or "full")},
    }


def _data_impl(state: dict) -> dict:
    return _run_branch(state, "impl_data")


def _api_impl(state: dict) -> dict:
    return _run_branch(state, "impl_api")


impl_data_node = stage("impl_data", actor="agent:codegen:impl_data")(_data_impl)
impl_api_node = stage("impl_api", actor="agent:codegen:impl_api")(_api_impl)
