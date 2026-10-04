"""
Deterministic offline stand-in for the LLM (STUB_MODE=true / `--offline`).

Purpose: prove the GRAPH WIRING, gates, retry/fallback/rollback/replan
logic and approval flow with no API key, no network and no Docker - before
any real prompt is trusted. The canned design and Java files below are
intentionally just rich enough to satisfy the guardrail checks (they are
never compiled); they double as an executable description of what the gate
expects the real agents to produce.

Companion switches (read in app/runners.py):
  STUB_TEST_FAILURES=N     first N stubbed `mvn test` runs fail
  STUB_FAILURE_CLASS=...   test | compile | schema | contract | infra
  STUB_INJECT_SECRET=1     first codegen emits a hardcoded API key -> the secrets gate must block it
"""
from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace

from app import dag

PKG = "com/rahul/urlshortener"
BASE = f"src/main/java/{PKG}"
TEST = f"src/test/java/{PKG}"


def _design(mode: str, existing: dict | None, max_mig: int) -> dict:
    paths = {
        "/api/shorten": {"post": {"summary": "Create a short link", "responses": {"201": {"description": "created"}, "400": {"description": "validation"}, "401": {"description": "unauthorized"}, "429": {"description": "rate limited"}}}},
        "/{shortCode}": {"get": {"summary": "Redirect", "responses": {"302": {"description": "redirect"}, "404": {"description": "unknown"}, "410": {"description": "expired"}}}},
        "/api/analytics/{shortCode}": {"get": {"summary": "Click analytics", "responses": {"200": {"description": "ok"}, "404": {"description": "unknown"}}}},
    }
    migrations = [{"version": "1", "name": "init_url_mapping", "sql":
                   "CREATE TABLE url_mapping (id BIGSERIAL PRIMARY KEY, short_code VARCHAR(32) UNIQUE NOT NULL, "
                   "original_url VARCHAR(2048) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                   "expires_at TIMESTAMPTZ, click_count BIGINT NOT NULL DEFAULT 0);"}]
    plan = [
        {"class": "com.rahul.urlshortener.domain.UrlMapping", "path": f"{BASE}/domain/UrlMapping.java", "layer": "domain", "branch": "impl_data", "change": "new", "responsibility": "JPA entity", "signatures": []},
        {"class": "com.rahul.urlshortener.repository.UrlMappingRepository", "path": f"{BASE}/repository/UrlMappingRepository.java", "layer": "repository", "branch": "impl_data", "change": "new", "responsibility": "repo", "signatures": []},
        {"class": "com.rahul.urlshortener.web.ShortenController", "path": f"{BASE}/web/ShortenController.java", "layer": "web", "branch": "impl_api", "change": "new", "responsibility": "shorten", "signatures": []},
        {"class": "com.rahul.urlshortener.web.RedirectController", "path": f"{BASE}/web/RedirectController.java", "layer": "web", "branch": "impl_api", "change": "new", "responsibility": "redirect", "signatures": []},
        {"class": "com.rahul.urlshortener.web.AnalyticsController", "path": f"{BASE}/web/AnalyticsController.java", "layer": "web", "branch": "impl_api", "change": "new", "responsibility": "analytics", "signatures": []},
    ]
    if mode == "brownfield":
        paths = dict((existing or {}).get("paths") or paths)
        paths["/api/analytics/{shortCode}/geo"] = {"get": {"summary": "Geo breakdown of clicks", "responses": {"200": {"description": "ok"}}}}
        migrations = [{"version": str(max_mig + 1), "name": "add_click_geo", "sql":
                       "CREATE TABLE click_geo (id BIGSERIAL PRIMARY KEY, short_code VARCHAR(32) NOT NULL, country VARCHAR(2) NOT NULL, clicks BIGINT NOT NULL DEFAULT 0);"}]
        plan = [
            {"class": "com.rahul.urlshortener.domain.ClickGeo", "path": f"{BASE}/domain/ClickGeo.java", "layer": "domain", "branch": "impl_data", "change": "new", "responsibility": "geo counters entity", "signatures": []},
            {"class": "com.rahul.urlshortener.web.GeoAnalyticsController", "path": f"{BASE}/web/GeoAnalyticsController.java", "layer": "web", "branch": "impl_api", "change": "new", "responsibility": "geo endpoint", "signatures": []},
        ]
    adrs = [
        {"title": "ID generation strategy", "decision": "Base62-encode an atomic Redis counter", "rationale": "collision-free, simple; hash has collision risk, Snowflake adds clock/worker-id complexity", "alternatives_considered": "hash of URL; Snowflake-style distributed id"},
        {"title": "Consistency model", "decision": "Strong for mapping (Postgres), eventual consistency for click counters (Redis write-behind)", "rationale": "redirect must never block on analytics", "alternatives_considered": "synchronous DB increment"},
        {"title": "Caching", "decision": "Cache-aside on redirect; invalidate on update/delete/alias overwrite", "rationale": "low latency while Postgres stays source of truth", "alternatives_considered": "write-through cache"},
        {"title": "Rate limiting", "decision": "Per-IP fixed window in Redis", "rationale": "shared across instances", "alternatives_considered": "in-memory bucket"},
        {"title": "Link expiry", "decision": "expires_at column; 410 Gone after expiry; cache TTL <= remaining life", "rationale": "explicit semantics", "alternatives_considered": "background deletion"},
    ]
    return {
        "api_contract": {"openapi": "3.0.3", "info": {"title": "URL Shortener", "version": "1.0.0"}, "paths": paths},
        "migrations": migrations, "component_plan": plan, "adrs": adrs,
        "configuration": {"app.security.api-key": "${SHORTENER_API_KEY}", "app.rate-limit.requests-per-minute": "60",
                          "app.link.default-ttl-days": "30", "app.analytics.flush-interval-ms": "5000",
                          "app.cache.redirect-ttl-seconds": "3600"},
    }


def _java_files(branch: str, strategy: str, mode: str, inject_secret: bool = False) -> dict[str, str]:
    files: dict[str, str] = {}
    if branch == "impl_data":
        if mode == "brownfield":
            files[f"{BASE}/domain/ClickGeo.java"] = "package com.rahul.urlshortener.domain;\n@jakarta.persistence.Entity\npublic class ClickGeo { Long id; String country; long clicks; }\n"
            return files
        files[f"{BASE}/domain/UrlMapping.java"] = "package com.rahul.urlshortener.domain;\n@jakarta.persistence.Entity\n@jakarta.persistence.Table(name=\"url_mapping\")\npublic class UrlMapping { Long id; String shortCode; String originalUrl; java.time.Instant expiresAt; long clickCount; }\n"
        files[f"{BASE}/repository/UrlMappingRepository.java"] = "package com.rahul.urlshortener.repository;\npublic interface UrlMappingRepository extends org.springframework.data.jpa.repository.JpaRepository<com.rahul.urlshortener.domain.UrlMapping, Long> {}\n"
        files["src/main/resources/application.yml"] = ("spring:\n  datasource:\n    url: ${DB_URL}\n    username: ${DB_USER}\n    password: ${DB_PASSWORD}\n  jpa:\n    hibernate:\n      ddl-auto: validate\n"
                                                      "management:\n  endpoints:\n    web:\n      exposure:\n        include: health,info,metrics\napp:\n  security:\n    api-key: ${SHORTENER_API_KEY}\n")
        files["Dockerfile"] = "FROM eclipse-temurin:21-jre\nUSER 1000\nCOPY target/*.jar app.jar\nENTRYPOINT [\"java\",\"-jar\",\"/app.jar\"]\n"
        files["docker-compose.yml"] = "services:\n  app:\n    build: .\n    environment:\n      SHORTENER_API_KEY: ${SHORTENER_API_KEY}\n"
        return files

    if mode == "brownfield":
        files[f"{BASE}/web/GeoAnalyticsController.java"] = (
            "package com.rahul.urlshortener.web;\n@org.springframework.web.bind.annotation.RestController\n@org.springframework.web.bind.annotation.RequestMapping(\"/api/analytics\")\n"
            "public class GeoAnalyticsController {\n  @org.springframework.web.bind.annotation.GetMapping(\"/{shortCode}/geo\")\n  public Object geo(@org.springframework.web.bind.annotation.PathVariable String shortCode) { return null; }\n}\n")
        return files

    full = strategy == "full"
    svc_redis = (
        "  private final StringRedisTemplate redis;\n  long nextId() { return redis.opsForValue().increment(\"shortener:id-counter\"); }\n"
        "  @Scheduled(fixedDelayString = \"${app.analytics.flush-interval-ms}\")\n  void flushClicks() { /* write-behind */ }\n"
    ) if full else "  // simple strategy: Postgres identity value encoded in base62; synchronous counter (degraded)\n"
    files[f"{BASE}/service/UrlShortenerService.java"] = (
        "package com.rahul.urlshortener.service;\n"
        + ("import org.springframework.data.redis.core.StringRedisTemplate;\nimport org.springframework.scheduling.annotation.Scheduled;\n" if full else "")
        + "public class UrlShortenerService {\n" + svc_redis
        + "  boolean isExpired(java.time.Instant expiresAt) { return expiresAt != null && expiresAt.isBefore(java.time.Instant.now()); }\n}\n")
    files[f"{BASE}/dto/ShortenRequest.java"] = (
        "package com.rahul.urlshortener.dto;\nimport jakarta.validation.constraints.NotBlank;\nimport jakarta.validation.constraints.Size;\n"
        "public record ShortenRequest(@NotBlank @Size(max = 2048) String originalUrl) {}\n")
    files[f"{BASE}/web/ShortenController.java"] = (
        "package com.rahul.urlshortener.web;\nimport jakarta.validation.Valid;\n@org.springframework.web.bind.annotation.RestController\n@org.springframework.web.bind.annotation.RequestMapping(\"/api\")\n"
        "public class ShortenController {\n  @org.springframework.web.bind.annotation.PostMapping(\"/shorten\")\n"
        "  public Object shorten(@Valid @org.springframework.web.bind.annotation.RequestBody com.rahul.urlshortener.dto.ShortenRequest req) { return null; }\n}\n")
    files[f"{BASE}/web/RedirectController.java"] = (
        "package com.rahul.urlshortener.web;\n@org.springframework.web.bind.annotation.RestController\npublic class RedirectController {\n"
        "  @org.springframework.web.bind.annotation.GetMapping(\"/{shortCode}\")\n  public Object redirect(@org.springframework.web.bind.annotation.PathVariable String shortCode) { return null; }\n}\n")
    files[f"{BASE}/web/AnalyticsController.java"] = (
        "package com.rahul.urlshortener.web;\n@org.springframework.web.bind.annotation.RestController\n@org.springframework.web.bind.annotation.RequestMapping(\"/api/analytics\")\npublic class AnalyticsController {\n"
        "  @org.springframework.web.bind.annotation.GetMapping(\"/{shortCode}\")\n  public Object stats(@org.springframework.web.bind.annotation.PathVariable String shortCode) { return null; }\n}\n")
    files[f"{BASE}/security/SecurityConfig.java"] = (
        "package com.rahul.urlshortener.security;\nimport org.springframework.security.web.SecurityFilterChain;\n@org.springframework.context.annotation.Configuration\npublic class SecurityConfig {\n"
        "  @org.springframework.context.annotation.Bean\n  SecurityFilterChain chain(org.springframework.security.config.annotation.web.builders.HttpSecurity http) throws Exception {\n"
        "    return http.authorizeHttpRequests(a -> a.requestMatchers(\"/api/**\").authenticated().anyRequest().permitAll()).build();\n  }\n}\n")
    if inject_secret:  # STUB_INJECT_SECRET=1: proves the blocking secrets gate + retry-with-feedback loop
        files[f"{BASE}/config/Leaky.java"] = 'package com.rahul.urlshortener.config;\npublic class Leaky { String apiKey = "sk-live-0123456789abcdef"; }\n'
    files[f"{BASE}/filter/RateLimitFilter.java"] = "package com.rahul.urlshortener.filter;\npublic class RateLimitFilter { /* rate limit: per-IP fixed window */ }\n"
    files[f"{TEST}/UrlShortenerIntegrationTest.java"] = ("package com.rahul.urlshortener;\nimport org.junit.jupiter.api.Test;\nclass UrlShortenerIntegrationTest {\n  @Test void shorten() {}\n  @Test void redirect() {}\n  @Test void analytics() {}\n}\n")
    files[f"{TEST}/ContractTest.java"] = "package com.rahul.urlshortener;\nimport org.junit.jupiter.api.Test;\nclass ContractTest {\n  @Test void everyDeclaredOperationIsServed() {}\n}\n"
    return files


def _imports(content: str) -> str:
    if "springframework.web.bind.annotation." not in content:
        return content
    content = content.replace("org.springframework.web.bind.annotation.", "")
    head, _, rest = content.partition("\n")
    return head + "\nimport org.springframework.web.bind.annotation.*;" + "\n" + rest


def _blocks(files: dict[str, str]) -> str:
    files = {p: _imports(c) for p, c in files.items()}
    return "\n\n".join(f"===FILE: {p}===\n{c.rstrip()}\n===END===" for p, c in files.items())


class StubLLM:
    def __init__(self, stage: str) -> None:
        self.stage = stage

    def invoke(self, messages: list[dict]):
        system, user = messages[0]["content"], messages[1]["content"]
        body = self._respond(system, user)
        return SimpleNamespace(content=body, usage_metadata={"input_tokens": len(system + user) // 4, "output_tokens": len(body) // 4})

    def _respond(self, system: str, user: str) -> str:
        if self.stage == "requirements":
            return json.dumps({
                "problem_statement": user.split("REQUEST:", 1)[-1].strip(),
                "in_scope": ["as described in the request"], "out_of_scope": ["multi-region deployment"],
                "acceptance_criteria": ["all declared endpoints behave per contract"],
                "non_functional_requirements": [], "ambiguities": [],
                "assumptions": ["Single-region deployment", "Redirects are public; creation requires an API key"],
            })
        if self.stage == "decomposition":
            mode = re.search(r"MODE: (\w+)", user).group(1)
            return json.dumps(dag.default_plan(mode))
        if self.stage == "codebase_reasoning":
            inv = json.loads(user.split("CODEBASE INVENTORY:\n", 1)[1])
            paths = [c["path"] for c in inv["classes"]]
            pick = [p for p in paths if p.endswith(("AnalyticsController.java", "UrlShortenerService.java"))]
            return json.dumps({"impacted_modules": pick, "new_files": [f"{BASE}/web/GeoAnalyticsController.java"],
                               "impacted_apis": ["GET /api/analytics/{shortCode}/geo"],
                               "data_flow_notes": "analytics reads flow controller -> service -> repository",
                               "schema_impact": "new click_geo table (new Flyway migration)", "risk_notes": ["keep existing analytics response shape"],
                               "regression_tests_to_watch": ["UrlShortenerIntegrationTest"]})
        if self.stage == "design":
            payload = json.loads(user)
            return json.dumps(_design(payload["mode"], payload.get("existing_openapi"), payload.get("existing_max_migration_version", 0)))
        if self.stage == "codegen":
            branch = "impl_data" if "BRANCH: impl_data" in system else "impl_api"
            strategy = "full" if "STRATEGY: full" in system else "simple"
            mode = re.search(r"MODE: (\w+)", user).group(1)
            inject = os.environ.get("STUB_INJECT_SECRET") == "1" and "PREVIOUS ATTEMPT FAILED" not in user
            out = _blocks(_java_files(branch, strategy, mode, inject_secret=inject))
            if os.environ.get("STUB_INJECT_SECRET") == "1" and "PREVIOUS ATTEMPT FAILED" in user and branch == "impl_api":
                out += f"\n\n===DELETE: {BASE}/config/Leaky.java==="
            return out
        raise ValueError(f"no stub for stage {self.stage!r}")
