"""
Unit tests for the blocking policy gate (policy/guardrails.py): secrets,
input validation, auth on mutating endpoints, OpenAPI<->code contract,
NFR features, brownfield migration immutability, design completeness.
"""
from sdlc_orchestrator.policy import guardrails as g
from sdlc_orchestrator.policy.guardrails import run_gate

CTRL_OK = """
package a;
import org.springframework.web.bind.annotation.*;
@RestController @RequestMapping("/api")
public class ShortenController {
  @PostMapping("/shorten")
  public Object s(@Valid @RequestBody ShortenRequest req) { return null; }
}
"""
DTO_OK = "package a; import jakarta.validation.constraints.NotBlank; public record ShortenRequest(@NotBlank String originalUrl) {}"
SEC_OK = "@Configuration class S { @Bean SecurityFilterChain c(HttpSecurity h) { return h.authorizeHttpRequests(a -> a.requestMatchers(\"/api/**\").authenticated()).build(); } }"
POM = "<dependency>spring-boot-starter-security</dependency><dependency>spring-boot-starter-actuator</dependency>"


def files(**kw):
    base = {
        "src/main/java/a/ShortenController.java": CTRL_OK,
        "src/main/java/a/ShortenRequest.java": DTO_OK,
        "src/main/java/a/S.java": SEC_OK,
        "pom.xml": POM,
    }
    base.update(kw)
    return base


def test_detects_hardcoded_java_secret():
    f = files(**{"src/main/java/a/C.java": 'class C { String apiKey = "sk-THISISBAD12345"; }'})
    assert any("secret" in m for m in g.check_no_hardcoded_secrets(f))


def test_detects_literal_yaml_password_but_allows_placeholders():
    bad = {"src/main/resources/application.yml": "spring:\n  datasource:\n    password: postgres\n"}
    ok = {"src/main/resources/application.yml": "spring:\n  datasource:\n    password: ${DB_PASSWORD}\n"}
    assert g.check_no_hardcoded_secrets(bad)
    assert g.check_no_hardcoded_secrets(ok) == []


def test_secret_scan_ignores_test_sources():
    assert g.check_no_hardcoded_secrets({"src/test/java/T.java": 'String password = "test-pass";'}) == []


def test_missing_valid_on_request_body():
    bad = CTRL_OK.replace("@Valid ", "")
    assert any("@Valid" in m for m in g.check_input_validation(files(**{"src/main/java/a/ShortenController.java": bad})))
    assert g.check_input_validation(files()) == []


def test_request_dto_needs_constraints():
    f = files(**{"src/main/java/a/ShortenRequest.java": "package a; public record ShortenRequest(String originalUrl) {}"})
    assert any("constraint" in m for m in g.check_input_validation(f))


def test_mutating_endpoint_requires_authentication():
    assert g.check_auth_on_mutating_endpoints(files()) == []
    no_sec = files()
    no_sec.pop("src/main/java/a/S.java")
    assert any("auth" in m for m in g.check_auth_on_mutating_endpoints(no_sec))
    permit_all = files(**{"src/main/java/a/S.java": "class S { SecurityFilterChain c(HttpSecurity h){ h.authorizeHttpRequests(a -> a.anyRequest().permitAll()); } }"})
    assert g.check_auth_on_mutating_endpoints(permit_all)


def test_auth_requires_security_starter_in_pom():
    assert any("starter-security" in m for m in g.check_auth_on_mutating_endpoints(files(**{"pom.xml": "<x/>"})))


def test_contract_must_match_both_directions():
    contract = {"paths": {"/api/shorten": {"post": {}}, "/{shortCode}": {"get": {}}}}
    msgs = g.check_contract_matches(contract, files())
    assert any("declared in the OpenAPI contract but not implemented" in m and "/{}" in m for m in msgs)
    extra = {"paths": {}}
    assert any("implemented but not declared" in m for m in g.check_contract_matches(extra, files()))
    assert g.check_contract_matches({"paths": {"/api/shorten": {"post": {}}}}, files()) == []


def test_hygiene():
    f = files(**{"src/main/java/a/H.java": "@CrossOrigin(\"*\") class H { void x(){ System.out.println(1); } }"})
    msgs = g.check_code_hygiene(f)
    assert len(msgs) == 2


def test_nfr_full_vs_simple_strategy():
    base = {"src/main/java/a/Svc.java": "class Svc { /* rate limit */ Instant expiresAt; }", "pom.xml": POM}
    assert any("Redis" in m for m in g.check_nfr_features(base, "full"))
    assert g.check_nfr_features(base, "simple") == []
    full = {"src/main/java/a/Svc.java": "class Svc { /* rate limit */ Instant expiresAt; StringRedisTemplate r; void n(){ r.opsForValue().increment(\"k\"); } @Scheduled void f(){} }", "pom.xml": POM}
    assert g.check_nfr_features(full, "full") == []


def test_tests_must_exist_including_contract_test():
    assert len(g.check_tests_present({})) == 2
    ok = {"src/test/java/ContractTest.java": "@Test void a(){} @Test void b(){} @Test void c(){}"}
    assert g.check_tests_present(ok) == []


def test_packaging_and_docs_checks():
    assert len(g.check_packaging({})) == 4
    assert len(g.check_release_docs({})) == 2


def test_applied_migrations_are_immutable():
    changes = [("M", "src/main/resources/db/migration/V1__init.sql"), ("A", "src/main/resources/db/migration/V2__x.sql"), ("M", "README.md")]
    msgs = g.check_migrations_immutable(changes)
    assert len(msgs) == 1 and "V1__init.sql" in msgs[0]


def test_gate_is_blocking_and_aggregates():
    r = run_gate("quality_gate", files=files(), design_doc={"api_contract": {"paths": {"/api/shorten": {"post": {}}}}}, strategy="simple")
    assert not r.passed                       # no tests, no rate limiting, etc.
    assert any(m.startswith("tests:") for m in r.failures)


GOOD_DESIGN = {
    "api_contract": {"paths": {"/api/shorten": {"post": {}}, "/{shortCode}": {"get": {}}, "/api/analytics/{shortCode}": {"get": {}}}},
    "migrations": [{"version": "1", "name": "init", "sql": "CREATE TABLE t (id int);"}],
    "component_plan": [{"class": "a.A", "branch": "impl_data"}, {"class": "a.B", "branch": "impl_api"}],
    "adrs": [{"title": t, "decision": d, "rationale": "r", "alternatives_considered": "alt"} for t, d in [
        ("ID generation", "base62 counter"), ("Consistency", "eventual analytics"), ("Caching", "cache-aside"),
        ("Rate limiting", "per ip"), ("Expiry", "ttl")]],
    "design_document": "\n".join(f"## {h}\nx" for h in g.REQUIRED_DESIGN_DOC_SECTIONS),
}


def test_design_gate_accepts_complete_design():
    assert g.check_design_completeness(GOOD_DESIGN, "greenfield") == []


def test_design_gate_catches_gaps():
    bad = {**GOOD_DESIGN, "api_contract": {"paths": {"/api/shorten": {"post": {}}}}, "adrs": GOOD_DESIGN["adrs"][:2], "component_plan": []}
    msgs = g.check_design_completeness(bad, "greenfield")
    assert any("required endpoint missing" in m for m in msgs)
    assert any("no ADR covering 'caching'" in m for m in msgs)
    assert any("component_plan is empty" in m for m in msgs)


def test_design_gate_requires_the_design_document_and_its_sections():
    missing = {k: v for k, v in GOOD_DESIGN.items() if k != "design_document"}
    assert any("design_document" in m and "missing" in m for m in g.check_design_completeness(missing, "greenfield"))
    partial = {**GOOD_DESIGN, "design_document": "## Overview\nonly this"}
    msgs = g.check_design_completeness(partial, "greenfield")
    assert any("'## Failure modes'" in m for m in msgs) and not any("'## Overview'" in m for m in msgs)


def test_brownfield_design_cannot_break_existing_contract_or_reuse_migration_versions():
    existing = {"paths": {"/api/shorten": {"post": {}}, "/{shortCode}": {"get": {}}}}
    design = {**GOOD_DESIGN, "api_contract": {"paths": {"/api/shorten": {"post": {}}}}}
    msgs = g.check_design_completeness(design, "brownfield", existing, existing_max_migration=1)
    assert any("dropped existing endpoint GET /{}" in m for m in msgs)
    assert any("collides" in m for m in msgs)
