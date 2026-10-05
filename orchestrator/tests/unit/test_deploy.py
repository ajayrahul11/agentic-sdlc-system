import subprocess
from types import SimpleNamespace

import pytest

from sdlc_orchestrator.integrations import deploy


def test_canonical_dockerfile_avoids_go_offline_and_retries_transient_maven_errors():
    df = deploy.CANONICAL_DOCKERFILE
    assert "go-offline" not in df
    assert "maven.wagon.http.retryHandler.count" in df and "--mount=type=cache" in df
    assert "USER app" in df and "EXPOSE 8080" in df


def test_missing_compose_file_fails_without_touching_docker(tmp_path):
    res = deploy.verify_deployment(tmp_path, "r1")
    assert not res["passed"] and "docker-compose.yml" in res["output_tail"]


def _fake_run(outputs):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd[4:6])
        out = outputs(cmd)
        return SimpleNamespace(returncode=out[0], stdout=out[1], stderr=out[2])
    return run, calls


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    monkeypatch.setattr(deploy, "_port_free", lambda p: True)
    return tmp_path


def test_transient_maven_central_error_is_retried_then_reported_as_infra_not_a_code_bug(repo, monkeypatch):
    err = "Could not transfer artifact x: status code: 502, reason phrase: Bad Gateway (502)"
    run, calls = _fake_run(lambda cmd: (1, "", err) if "build" in cmd else (0, "", ""))
    monkeypatch.setattr(subprocess, "run", run)
    res = deploy.verify_deployment(repo, "r2", sleep=lambda s: None)
    assert [c for c in calls if c[0] == "build" or "build" in c].__len__() == 3     # retried 3x
    assert res["failure_class"] == "infra" and not res["passed"]


def test_real_compile_error_in_docker_build_is_a_code_failure_not_infra(repo, monkeypatch):
    run, _ = _fake_run(lambda cmd: (1, "", "[ERROR] COMPILATION ERROR : cannot find symbol") if "build" in cmd else (0, "", ""))
    monkeypatch.setattr(subprocess, "run", run)
    res = deploy.verify_deployment(repo, "r3", sleep=lambda s: None)
    assert res["failure_class"] == "compile" and not res["passed"]


def test_stack_is_always_torn_down_even_when_startup_fails(repo, monkeypatch):
    run, calls = _fake_run(lambda cmd: (1, "", "boom") if "up" in cmd else (0, "", ""))
    monkeypatch.setattr(subprocess, "run", run)
    res = deploy.verify_deployment(repo, "r4", sleep=lambda s: None)
    assert not res["passed"]
    assert any("down" in c for c in calls)


def test_busy_port_is_an_infra_problem(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    monkeypatch.setattr(deploy, "_port_free", lambda p: False)
    assert deploy.verify_deployment(tmp_path, "r5")["failure_class"] == "infra"


def test_smoke_fails_when_swagger_ui_has_no_api_key_scheme(monkeypatch):
    """Regression: /v3/api-docs is generated from the controllers, so without OpenApiConfig the Swagger UI has no
    'Authorize' button. The smoke test must catch that instead of only checking the spec is served."""
    spec_without_scheme = '{"openapi":"3.1.0","paths":{"/api/shorten":{}}}'

    def fake_http(method, path, body=None, headers=None, timeout=15.0):
        if method == "POST":
            return (201, {}, '{"shortCode":"abc"}') if (headers or {}).get("X-API-Key") else (401, {}, "{}")
        if path == "/abc":
            return 302, {"location": "https://example.com/verify/x"}, ""
        if path.startswith("/api/analytics"):
            return 200, {}, '{"totalClicks":1}'
        if path == "/v3/api-docs":
            return 200, {}, spec_without_scheme
        return 200, {}, ""

    monkeypatch.setattr(deploy, "_http", fake_http)
    monkeypatch.setattr(deploy.secrets, "token_hex", lambda n: "x")
    steps = []
    err = deploy._smoke(steps, ["/api/shorten"], "k")
    assert err and "X-API-Key scheme" in err
    assert steps[-1]["ok"] is False


def test_smoke_passes_when_spec_declares_the_scheme(monkeypatch):
    spec = '{"paths":{"/api/shorten":{}},"components":{"securitySchemes":{"ApiKeyAuth":{"name":"X-API-Key"}}}}'

    def fake_http(method, path, body=None, headers=None, timeout=15.0):
        if method == "POST":
            return (201, {}, '{"shortCode":"abc"}') if (headers or {}).get("X-API-Key") else (401, {}, "{}")
        if path == "/abc":
            return 302, {"location": "https://example.com/verify/x"}, ""
        if path.startswith("/api/analytics"):
            return 200, {}, '{"totalClicks":1}'
        return (200, {}, spec) if path == "/v3/api-docs" else (200, {}, "")

    monkeypatch.setattr(deploy, "_http", fake_http)
    monkeypatch.setattr(deploy.secrets, "token_hex", lambda n: "x")
    assert deploy._smoke([], ["/api/shorten"], "k") is None


def test_openapi_config_is_canonical_and_declares_the_header_scheme():
    cfg = deploy.CANONICAL_OPENAPI_CONFIG
    assert 'name("X-API-Key")' in cfg and "SecurityScheme.In.HEADER" in cfg and "getPost()" in cfg
    assert deploy.OPENAPI_CONFIG_PATH.endswith("config/OpenApiConfig.java")


def test_generated_readme_has_prerequisites_and_a_mac_and_windows_quick_start():
    from sdlc_orchestrator.workflow.nodes.docs_agent import render_readme

    state = {"run_id": "r", "requirement_spec": {"problem_statement": "p"},
             "design_doc": {"api_contract": {"paths": {"/api/shorten": {"post": {}}}}, "configuration": {}, "adrs": []}}
    readme = render_readme(state)
    for needed in ("## Prerequisites", "## Quick start", "docker compose up --build", "$env:SHORTENER_API_KEY",
                   "export SHORTENER_API_KEY", "swagger-ui/index.html", "Authorize", "mvnw.cmd", "docker compose down", "## Troubleshooting"):
        assert needed in readme, needed
    assert readme.index("## Prerequisites") < readme.index("## Quick start")
