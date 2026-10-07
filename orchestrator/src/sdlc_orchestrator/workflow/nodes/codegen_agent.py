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

Each branch is generated in small GROUPS (plan_groups): 1-5 related files per model call, in dependency
order (e.g. dto -> service -> security/filters -> web -> tests). Later groups see the files earlier groups
wrote, a group whose reply is missing or truncated files is re-asked for just those files, and a retry
regenerates only the failing files in chunks. A single huge reply is what used to truncate and fail runs.

Output format is a greppable delimiter protocol, not JSON, because large
Java files are miserable to keep valid inside JSON strings.
"""
from __future__ import annotations

import json
import re
import time

import yaml

from sdlc_orchestrator.core import config
from sdlc_orchestrator.core.events import log_event
from sdlc_orchestrator.integrations import deploy
from sdlc_orchestrator.llm.client import invoke_llm, model_for
from sdlc_orchestrator.workflow.nodes.common import repo_path, set_task_status
from sdlc_orchestrator.integrations.repo_io import parse_deletes, parse_file_blocks, read_repo_files, write_files
from sdlc_orchestrator.workflow.stage import stage

OUTPUT_FORMAT = """OUTPUT FORMAT - exactly this, nothing else (no prose, no markdown fences around the whole answer). \
Paths are relative to the REPOSITORY ROOT:

===FILE: src/main/java/com/rahul/urlshortener/web/ExampleController.java===
<complete file content>
===END===

Every file must be COMPLETE (never a diff, never "...rest unchanged..."). Allowed locations: src/**, \
docker-compose.yml, pom.xml (data branch only).
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
csrf disabled, GET redirect/health/info AND the API docs (/v3/api-docs/**, /swagger-ui/**, /swagger-ui.html) permitted, \
everything else .authenticated(), authenticating an X-API-Key \
header against property app.security.api-key (from env SHORTENER_API_KEY - NEVER a literal secret, no default value).
5. No secrets in code or config (${ENV_VAR} placeholders only); no System.out / printStackTrace (SLF4J); no wildcard CORS.
5b. springdoc-openapi (already in the pom) serves the live OpenAPI spec at /v3/api-docs and Swagger UI at \
/swagger-ui/index.html; do NOT write controllers for them and do not exclude them from the rate limiter's allow path logic. \
ContractTest and other tests must not treat them as undeclared operations.
5c. The Flyway migrations in the design are the ONLY database schema. Every table and column name in entities, \
repositories and hand-written SQL (JdbcClient/@Query/native) MUST match them character for character - never invent a table.
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
- docker-compose.yml (services: app, postgres, redis with healthchecks; app built from ./Dockerfile, published on host port \
8080; app configured ONLY via environment variables that application.yml reads - DB_URL, DB_USERNAME, DB_PASSWORD, REDIS_HOST, \
SHORTENER_API_KEY, APP_BASE_URL - with DB_PASSWORD and SHORTENER_API_KEY taken from the host env and no secret literals). It is \
verified by really running `docker compose up --build` and calling the live APIs, so every variable the app needs must be set.
DO NOT write: Dockerfile, .dockerignore or config/OpenApiConfig.java (the orchestrator provides the Swagger X-API-Key scheme), Flyway migration files or docs/openapi.yaml (the orchestrator writes them verbatim), \
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


MAX_GROUP_FILES = 5   # a group never asks for more files than this in one call
REPAIR_PASSES = 2     # re-asks for files a reply left out or cut off, before the group counts as failed
MAIN_JAVA = "src/main/java/"
TEST_JAVA = "src/test/java/com/rahul/urlshortener/"

LAYER_GROUPS = {   # ordered: earlier groups are context for later ones
    "impl_data": [("persistence", ("domain", "entity", "repository")), ("config", ("config",))],
    "impl_api": [("dto-exceptions", ("dto", "exception")), ("services", ("service",)),
                 ("security-filters", ("security", "filter")), ("web", ("web",))],
}
RESOURCE_GROUPS = {
    "impl_data": [
        ("application-yml", "src/main/resources/application.yml",
         "src/main/resources/application.yml: env-var driven datasource/redis/flyway config, spring.jpa.hibernate.ddl-auto: validate, "
         "actuator health/info/metrics exposed and every design.configuration property."),
        ("docker-compose", "docker-compose.yml",
         "docker-compose.yml with services app, postgres, redis (healthchecks), exactly as the branch rules above describe."),
    ],
}
TEST_GROUPS = {
    "impl_api": [
        ("unit-tests", None, "unit tests for the base62 ID generator and the link-expiry logic (plain JUnit, no containers, no Spring context)."),
        ("integration-test", None, "ONE integration test class following the integration-test rules above (RANDOM_PORT + Testcontainers via "
                                   "@DynamicPropertySource, java.net.http.HttpClient). Read the generated sources below for real class and method names."),
        ("contract-test", TEST_JAVA + "ContractTest.java", "ContractTest.java following the contract-test rules above."),
    ],
}


def _chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def plan_groups(design: dict, branch: str, mode: str = "greenfield") -> list[dict]:
    """Ordered generation groups for a branch. Each: name, instruction, components, expected (paths the reply
    must contain), prefix (a directory a reply must write into when no path is known) and a `owns` test."""
    comps = [c for c in _select_components(design, branch) if c.get("layer") != "test"]
    groups: list[dict] = []
    used: set[int] = set()
    for name, layers in LAYER_GROUPS.get(branch, []):
        picked = [c for c in comps if c.get("layer") in layers]
        used.update(id(c) for c in picked)
        for i, part in enumerate(_chunks(picked, MAX_GROUP_FILES), 1):
            groups.append({"name": name if len(picked) <= MAX_GROUP_FILES else f"{name}-{i}",
                           "instruction": f"the {name} classes from YOUR COMPONENTS (layers: {', '.join(layers)}).",
                           "components": part, "expected": [c["path"] for c in part if c.get("path")], "prefix": None})
    rest = [c for c in comps if id(c) not in used]
    for i, part in enumerate(_chunks(rest, MAX_GROUP_FILES), 1):
        groups.append({"name": f"other-{i}", "instruction": "the remaining classes from YOUR COMPONENTS.",
                       "components": part, "expected": [c["path"] for c in part if c.get("path")], "prefix": None})
    if mode == "brownfield":   # the scaffold, config and baseline tests exist: only the planned classes (and planned tests) change
        tests = [c for c in _select_components(design, branch) if c.get("layer") == "test"]
        for i, part in enumerate(_chunks(tests, MAX_GROUP_FILES), 1):
            groups.append({"name": f"tests-{i}", "instruction": "the planned test classes from YOUR COMPONENTS.", "components": part,
                           "expected": [c["path"] for c in part if c.get("path")], "prefix": None, "tests": True})
        return groups
    for name, path, text in RESOURCE_GROUPS.get(branch, []):
        groups.append({"name": name, "instruction": text, "components": [], "expected": [path], "prefix": None})
    for name, path, text in TEST_GROUPS.get(branch, []):
        groups.append({"name": name, "instruction": text, "components": [], "expected": [path] if path else [],
                       "prefix": None if path else "src/test/", "tests": True})
    return groups


def fix_groups(targets: list[str]) -> list[dict]:
    """Retry plan: only the failing files, a few per call."""
    return [{"name": f"fix-{i}", "instruction": "fix the failing files listed below.", "components": [],
             "expected": part, "prefix": None, "fix": True} for i, part in enumerate(_chunks(targets, MAX_GROUP_FILES), 1)]


def _owns(group: dict, path: str) -> bool:
    return path in group["expected"] or bool(group.get("prefix") and path.startswith(group["prefix"])) \
        or any(c.get("path") == path for c in group.get("components", []))


def _missing(group: dict, generated: dict[str, str], problems: list[str], deleted: set[str] = frozenset()) -> list[str]:
    """What the reply still owes: expected paths it did not contain (cut-off files included)."""
    if group.get("optional"):
        return []
    missing = [p for p in group["expected"] if p not in generated and p not in deleted]
    for pr in problems:
        m = re.search(r"incomplete file\(s\): ([^;\n]+)", pr)
        if m:
            missing += [x.strip() for x in m.group(1).split(",") if x.strip() and x.strip() not in generated and x.strip() not in missing and x.strip() not in deleted]
    if group.get("prefix") and not any(p.startswith(group["prefix"]) for p in generated):
        missing.append(f"(at least one file under {group['prefix']})")
    return missing


def _context_block(written: dict[str, str], repo, group: dict, budget: int = 60_000) -> str:
    """Sources earlier groups already wrote, so this group uses their real names and signatures. Test groups
    also get main sources found on disk (the other branch runs in parallel and may have written some)."""
    out, used = [], 0
    sources = dict(written)
    if group.get("tests"):
        for p in sorted((repo / "src/main/java").rglob("*.java")) if (repo / "src/main/java").exists() else []:
            sources.setdefault(p.relative_to(repo).as_posix(), p.read_text())
    for path, content in sources.items():
        if _owns(group, path) or not path.startswith(MAIN_JAVA):
            continue
        if used + len(content) > budget:
            break
        out.append(f"### ALREADY WRITTEN {path}\n{content}")
        used += len(content)
    return "\n\n".join(out)


def _select_components(design: dict, branch: str) -> list[dict]:
    return [c for c in design.get("component_plan", []) if c.get("branch") == branch]


def _write_deterministic(repo, design: dict) -> dict[str, str]:
    """Files that come straight from the design with no model involved."""
    files: dict[str, str] = {}
    for m in design.get("migrations", []):
        files[f"src/main/resources/db/migration/V{int(m['version'])}__{m['name']}.sql"] = m["sql"].rstrip() + "\n"
    files["docs/openapi.yaml"] = yaml.safe_dump(design["api_contract"], sort_keys=False)
    files["Dockerfile"] = deploy.CANONICAL_DOCKERFILE      # build recipe is boilerplate: written by the orchestrator, not the model
    files[".dockerignore"] = deploy.CANONICAL_DOCKERIGNORE
    files[deploy.OPENAPI_CONFIG_PATH] = deploy.CANONICAL_OPENAPI_CONFIG   # Swagger UI "Authorize" (X-API-Key)
    return files


def _prior_files(state: dict, branch: str, budget: int = 90_000, only: list[str] | None = None) -> str:
    owner = state.get("artifact_owner", {})
    out, used = [], 0
    for path, content in (state.get("code_artifacts") or {}).items():
        if owner.get(path) == branch and not path.endswith((".sql", "openapi.yaml")) and (only is None or path in only):
            if used + len(content) > budget:
                break
            out.append(f"### CURRENT {path}\n{content}")
            used += len(content)
    return "\n\n".join(out)


def failing_paths(feedback: str, owned: list[str]) -> list[str]:
    """Owned files the failure output points at: named by path or basename, or reported as
    an incomplete (truncated) file. Empty when the failure is not attributable to a file."""
    hits = [p for p in owned if p in feedback or p.rsplit("/", 1)[-1] in feedback]
    m = re.search(r"incomplete file\(s\): ([^;\n]+)", feedback)
    if m:
        hits += [x.strip() for x in m.group(1).split(",") if x.strip() and x.strip() not in hits]
    return hits


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


def _group_parts(state: dict, branch: str, repo, group: dict, written: dict[str, str]) -> list[str]:
    """Suffix sections for one generation group (the design/system prefix stays identical, so it stays cached)."""
    wanted = group["expected"] or ([f"any files under {group['prefix']}"] if group.get("prefix") else [])
    parts = [f"GROUP: {group['name']}. Write ONLY {group['instruction']}\nGROUP FILES (complete content each, nothing else): "
             + (", ".join(wanted) or "the files this group needs (use the exact paths from YOUR COMPONENTS)")
             + "\nOther groups of this branch are generated in separate steps; classes of other groups are described by YOUR COMPONENTS "
               "and, where already written, shown below. Do not re-output them."]
    if state.get("mode") == "brownfield":
        existing = [f"### EXISTING {r}\n{(repo / r).read_text()}" for r in state.get("impacted_modules", [])
                    if (repo / r).is_file() and _owns(group, r)]
        if existing:
            parts.append("EXISTING FILES TO MODIFY (output the COMPLETE new version of each file you change):\n" + "\n\n".join(existing)
                         + "\nPreserve all existing behaviour and tests.")
    ctx = _context_block(written, repo, group)
    if ctx:
        parts.append("CODE ALREADY WRITTEN FOR THIS PROJECT (use these exact names and signatures):\n" + ctx)
    feedback = state.get("failure_feedback")
    if feedback:
        owner = state.get("artifact_owner", {})
        mine = {p: c for p, c in (state.get("code_artifacts") or {}).items()
                if owner.get(p) == branch and c and not p.endswith((".sql", "openapi.yaml")) and _owns(group, p)}
        if group.get("fix"):
            parts.append("PREVIOUS ATTEMPT FAILED. Regenerate ONLY the files under GROUP FILES (complete content each; files that were cut "
                         f"off or never written must be written in full).\nFAILURE OUTPUT:\n{feedback}")
            parts.append("THEIR CURRENT CONTENT:\n" + "\n\n".join(f"### CURRENT {p}\n{c}" for p, c in mine.items()))
        elif mine:
            parts.append("PREVIOUS ATTEMPT FAILED. Fix the cause ONLY if it is in this group's files; output only the files that need to change "
                         f"(complete content each) and nothing if none do.\nFAILURE OUTPUT:\n{feedback}")
            parts.append("THIS GROUP'S CURRENT FILES:\n" + "\n\n".join(f"### CURRENT {p}\n{c}" for p, c in mine.items()))
        else:
            parts.append("A PREVIOUS ATTEMPT FAILED and the workspace was reset to the clean scaffold. Write this group from scratch, "
                         f"avoiding the cause.\nFAILURE OUTPUT:\n{feedback}")
    return parts


def build_prompt_parts(state: dict, branch: str, repo, group: dict | None = None,
                       written: dict[str, str] | None = None) -> tuple[str, str, str]:
    """(system, stable_prefix, variable_suffix). The prefix (design + this branch's
    components) is identical across retries of a branch, so it is prompt-cached; the
    suffix carries everything that changes per attempt (pom, existing files, failures)."""
    strategy = (state.get("codegen_strategy") or "full")
    design = state["design_doc"]
    system = "\n\n".join([COMMON_RULES, FULL_STRATEGY if strategy == "full" else SIMPLE_STRATEGY, BRANCH_SCOPE[branch], OUTPUT_FORMAT])

    prefix = "\n\n".join([
        f"MODE: {state.get('mode', 'greenfield')}",
        "DESIGN:\n" + json.dumps({k: design.get(k) for k in ("api_contract", "component_plan", "configuration", "adrs", "migrations")}, indent=1),
        f"YOUR COMPONENTS ({branch}):\n" + json.dumps(_select_components(design, branch), indent=1),
    ])
    parts = ["POM.XML (current):\n" + ((repo / "pom.xml").read_text() if (repo / "pom.xml").exists() else "(missing)")]
    if group is not None:
        return system, prefix, "\n\n".join(parts + _group_parts(state, branch, repo, group, written or {}))
    if state.get("mode") == "brownfield":
        parts.append("EXISTING FILES TO MODIFY (output the COMPLETE new version of each file you change):\n" + _existing_context(repo, state))
        parts.append("Preserve all existing behaviour and tests; only add/modify what the requirement needs.")
    feedback = state.get("failure_feedback")
    if feedback:
        owner = state.get("artifact_owner", {})
        owned = [p for p, c in (state.get("code_artifacts") or {}).items()
                 if owner.get(p) == branch and c and not p.endswith((".sql", "openapi.yaml"))]
        targets = failing_paths(feedback, owned)
        prior = _prior_files(state, branch, only=targets or None)
        if targets:
            rest = [p for p in owned if p not in targets]
            parts.append("PREVIOUS ATTEMPT FAILED. Regenerate ONLY the files listed under FILES TO FIX (complete content each); "
                         "every other file is already correct on disk and MUST NOT be output again. Files that were cut off "
                         "or never written must be written in full.\n"
                         f"FILES TO FIX: {', '.join(targets)}\nFAILURE OUTPUT:\n{feedback}")
            parts.append("YOUR FILES FROM THE PREVIOUS ATTEMPT (only those to fix):\n" + prior)
            if rest:
                parts.append("UNCHANGED FILES (do not output; their API is in the design's component_plan):\n" + "\n".join(rest))
        elif prior:
            parts.append("PREVIOUS ATTEMPT FAILED. Fix the cause. Output ONLY files that need to change (complete content each).\n"
                         f"FAILURE OUTPUT:\n{feedback}")
            parts.append("YOUR FILES FROM THE PREVIOUS ATTEMPT:\n" + prior)
        else:
            parts.append("A PREVIOUS ATTEMPT FAILED and the workspace was reset to the clean scaffold. Regenerate ALL files for "
                         f"your branch from scratch, avoiding the cause.\nFAILURE OUTPUT:\n{feedback}")
    return system, prefix, "\n\n".join(parts)


def build_prompts(state: dict, branch: str, repo) -> tuple[str, str]:
    system, prefix, suffix = build_prompt_parts(state, branch, repo)
    return system, f"{prefix}\n\n{suffix}"


def failed_attempts(state: dict) -> int:
    """Failed implementation attempts in the current plan version."""
    pv = state.get("plan_version", 1)
    return sum(1 for r in state.get("retries", []) if r.get("plan_version", 1) == pv)


def _model_for_attempt(state: dict) -> tuple[str, bool]:
    """Cheaper model first; the stronger one once an attempt has failed."""
    escalated = failed_attempts(state) >= config.escalate_after_failures()
    return model_for("codegen", escalated=escalated), escalated


def _not_owned(branch: str, path: str) -> bool:
    """Files a branch must never write (the other branch, or the orchestrator, owns them)."""
    if branch == "impl_data":
        return (path in ("Dockerfile", ".dockerignore", deploy.OPENAPI_CONFIG_PATH) or path.startswith("src/test/")
                or "/web/" in path or path.endswith("/openapi.yaml") or "/db/migration/" in path)
    return path in ("pom.xml", "Dockerfile", "docker-compose.yml") or path.startswith("src/main/resources/")


def _generate_group(state: dict, branch: str, repo, group: dict, written: dict[str, str], model: str) -> tuple[dict[str, str], str, list[str]]:
    """One group = one small model call. If the reply leaves out (or cuts off) files the group owes, ask again for
    just those, escalating the model on the last pass. Returns (files, raw reply text, unresolved problems)."""
    run_id = state["run_id"]
    system, prefix, suffix = build_prompt_parts(state, branch, repo, group, written)
    text = invoke_llm("codegen", system, suffix, run_id=run_id, cache_prefix=prefix, model=model)
    generated, problems = parse_file_blocks(text)
    deletes_text = text
    for n in range(1, REPAIR_PASSES + 1):
        missing = _missing(group, generated, problems, set(parse_deletes(deletes_text)[0]))
        if not missing:
            break
        log_event(run_id, branch, "group_repair", {"group": group["name"], "missing": missing, "pass": n},
                  actor="system:orchestrator", outcome="retrying", reason="reply missing or truncated files; re-asking for just those")
        redo = model_for("codegen", escalated=True) if n == REPAIR_PASSES else model
        more = invoke_llm("codegen", system,
                          suffix + "\n\nYOUR PREVIOUS REPLY WAS INCOMPLETE. Output ONLY these files, each COMPLETE and short enough to "
                                   f"finish: {', '.join(missing)}", run_id=run_id, cache_prefix=prefix, model=redo)
        more_files, more_problems = parse_file_blocks(more)
        generated.update(more_files)
        problems = [x for x in problems if not x.startswith("output truncated")] + more_problems
        deletes_text += "\n" + more
    left = _missing(group, generated, problems, set(parse_deletes(deletes_text)[0]))
    # truncation notes are only a problem if a file is still owed; unsafe-path notes always are
    problems = [x for x in problems if not x.startswith("output truncated")]
    if left:
        problems.append(f"{branch}/{group['name']}: missing or incomplete file(s): {', '.join(left)}"[:400])
    return generated, deletes_text, problems


def _run_branch(state: dict, branch: str) -> dict:
    run_id = state["run_id"]
    repo = repo_path(state)
    design = state["design_doc"]
    errors: list[str] = []
    dropped: list[str] = []

    artifacts: dict[str, str] = {}
    removed: list[str] = []
    if branch == "impl_data":
        artifacts.update(_write_deterministic(repo, design))

    owner = state.get("artifact_owner") or {}
    owned_now = [p for p, c in (state.get("code_artifacts") or {}).items()
                 if owner.get(p) == branch and c and not p.endswith((".sql", "openapi.yaml"))]
    feedback = state.get("failure_feedback")
    targets = failing_paths(feedback, owned_now) if feedback else []
    if targets:
        groups = fix_groups(targets)
    else:
        groups = plan_groups(design, branch, state.get("mode", "greenfield"))
        if feedback and owned_now:   # failure not attributable to a file: each group may change what it owns, or nothing
            groups = [{**g, "optional": True} for g in groups]

    written: dict[str, str] = {}
    if groups:
        model, escalated = _model_for_attempt(state)
        if escalated and model != model_for("codegen"):
            log_event(run_id, branch, "model_escalation", {"model": model, "failed_attempts": failed_attempts(state)},
                      actor="system:orchestrator", outcome="escalated",
                      reason=f"{failed_attempts(state)} failed attempt(s); codegen moved to the stronger model")
    for group in groups:
        started = time.monotonic()
        generated, raw, problems = _generate_group(state, branch, repo, group, written, model)
        errors += problems
        kept: dict[str, str] = {}
        for path, content in generated.items():
            if _not_owned(branch, path):
                dropped.append(path)          # a stray file is dropped, not a failed run
            elif path in written and not _owns(group, path):
                dropped.append(path)          # an earlier group already wrote it; do not let this one overwrite it
            else:
                kept[path] = content
        deletes, del_problems = parse_deletes(raw)
        errors += del_problems
        for rel in deletes:
            if rel in removed:
                continue
            if owner.get(rel) != branch:
                errors.append(f"{branch} tried to delete {rel}, which it did not create in this run")
                continue
            (repo / rel).unlink(missing_ok=True)
            removed.append(rel)
        write_files(repo, kept)               # on disk now, so the next group is generated against real code
        written.update(kept)
        artifacts.update(kept)
        log_event(run_id, branch, "group_generated", {"group": group["name"], "files": sorted(kept), "ok": not problems},
                  actor=f"agent:codegen:{branch}", outcome="ok" if not problems else "partial",
                  duration_ms=int((time.monotonic() - started) * 1000))
    if groups and not artifacts and not removed and not feedback:
        errors.append(f"{branch}: model produced no usable files")

    written_paths = write_files(repo, artifacts)
    log_event(run_id, branch, "files_written", {"files": written_paths, "deleted": removed, "dropped": sorted(set(dropped)),
                                                "groups": [g["name"] for g in groups], "problems": errors[:5]},
              actor=f"agent:codegen:{branch}", outcome="ok" if not errors else "partial")
    return {
        "code_artifacts": {**artifacts, **{p: "" for p in removed}},
        "artifact_owner": {**{p: branch for p in set(artifacts) | {
            p for p, b in (state.get("artifact_owner") or {}).items() if b == branch}},
            **{p: "deleted" for p in removed}},
        "branch_status": {branch: {"ok": not errors, "errors": errors, "files": written_paths}},
        "tasks": set_task_status(state, [branch], "in_progress"),
        "_outcome": "ok" if not errors else "partial",
        "_reason": "; ".join(errors)[:300] or None,
        "_detail": {"files": written_paths, "deleted": removed, "strategy": (state.get("codegen_strategy") or "full"),
                    "groups": [g["name"] for g in groups]},
    }


def _data_impl(state: dict) -> dict:
    return _run_branch(state, "impl_data")


def _api_impl(state: dict) -> dict:
    return _run_branch(state, "impl_api")


impl_data_node = stage("impl_data", actor="agent:codegen:impl_data")(_data_impl)
impl_api_node = stage("impl_api", actor="agent:codegen:impl_api")(_api_impl)
