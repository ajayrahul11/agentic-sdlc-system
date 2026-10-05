"""
Deploy verification: proves the generated service actually BUILDS, STARTS and SERVES its APIs
the way a user would run it (`docker compose up --build`), not just that `mvnw test` is green.

Steps (each recorded, the first failure stops the run and its evidence goes back to codegen):
  1. docker compose build           (retried on transient network errors - Maven Central 5xx etc.)
  2. docker compose up -d           (random DB password + API key, isolated compose project name)
  3. wait for /actuator/health UP
  4. smoke: POST /api/shorten (401 without key, 201 with) -> GET /{code} 302 -> analytics counts the click
            -> OpenAPI JSON at /v3/api-docs lists the contract paths -> Swagger UI is served
  5. docker compose down -v         (always)

The Dockerfile is written by the orchestrator, not the model: a build recipe is boilerplate that must be
reliable, and `mvn dependency:go-offline` (what models tend to write) pulls unrelated plugins and
fails on a single Maven Central 502.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import socket
import subprocess
import time
import urllib.parse
from pathlib import Path

from sdlc_orchestrator.core import config
from sdlc_orchestrator.core.failures import classify_failure

CANONICAL_DOCKERFILE = """# syntax=docker/dockerfile:1
FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /workspace
COPY pom.xml ./
COPY src ./src
# BuildKit cache keeps ~/.m2 between builds; the wagon retry flags ride out transient Maven Central 5xx errors.
RUN --mount=type=cache,target=/root/.m2 \\
    mvn -B -q package -DskipTests \\
        -Dmaven.wagon.http.retryHandler.count=5 \\
        -Dmaven.wagon.httpconnectionManager.ttlSeconds=30

FROM eclipse-temurin:21-jre
RUN groupadd --system app && useradd --system --gid app app
WORKDIR /app
COPY --from=build /workspace/target/*.jar app.jar
USER app
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
"""

CANONICAL_DOCKERIGNORE = "target/\n.git/\n.idea/\n*.iml\n.env\n"

_TRANSIENT = re.compile(r"\b50[0-4]\b|Bad Gateway|Service Unavailable|Gateway Time-?out|Connection (reset|refused)|"
                        r"timed? ?out|Could not transfer artifact|TLS handshake|unexpected EOF|temporary failure", re.IGNORECASE)
OPENAPI_CONFIG_PATH = "src/main/java/com/rahul/urlshortener/config/OpenApiConfig.java"
# springdoc builds /v3/api-docs from the controllers and ignores docs/openapi.yaml, so without this bean the
# Swagger UI has no "Authorize" button and a user cannot call the API-key protected endpoints from it.
CANONICAL_OPENAPI_CONFIG = """package com.rahul.urlshortener.config;

import io.swagger.v3.oas.models.Components;
import io.swagger.v3.oas.models.OpenAPI;
import io.swagger.v3.oas.models.PathItem;
import io.swagger.v3.oas.models.info.Info;
import io.swagger.v3.oas.models.security.SecurityRequirement;
import io.swagger.v3.oas.models.security.SecurityScheme;
import org.springdoc.core.customizers.OpenApiCustomizer;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/** Swagger UI metadata: the X-API-Key scheme ("Authorize" button) applied to every mutating operation. */
@Configuration
public class OpenApiConfig {

    static final String SCHEME = "ApiKeyAuth";

    @Bean
    OpenAPI shortenerOpenApi() {
        return new OpenAPI()
                .info(new Info().title("URL Shortener API").version("1.0.0")
                        .description("Click Authorize and enter the API key (X-API-Key) to call mutating endpoints."))
                .components(new Components().addSecuritySchemes(SCHEME, new SecurityScheme()
                        .type(SecurityScheme.Type.APIKEY).in(SecurityScheme.In.HEADER).name("X-API-Key")));
    }

    @Bean
    OpenApiCustomizer requireApiKeyOnMutatingOperations() {
        return openApi -> openApi.getPaths().values().forEach(item -> {
            for (var op : new io.swagger.v3.oas.models.Operation[]{item.getPost(), item.getPut(), item.getPatch(), item.getDelete()}) {
                if (op != null) {
                    op.addSecurityItem(new SecurityRequirement().addList(SCHEME));
                }
            }
        });
    }
}
"""
PORT = 8080


def _tail(text: str, n: int = 60) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _http(method: str, path: str, body: dict | None = None, headers: dict | None = None, timeout: float = 15.0):
    """(status, headers, text) with NO redirect following, so a 302 is observable."""
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        h = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body)
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=h)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


class _Compose:
    def __init__(self, repo: Path, project: str, env: dict):
        self.repo, self.project, self.env = repo, project, env

    def run(self, *args: str, timeout: int = 900) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", "compose", "-p", self.project, *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True, timeout=timeout)


def _result(steps: list[dict], passed: bool, evidence: str = "", failure_class: str = "") -> dict:
    return {"passed": passed, "steps": steps, "failure_class": failure_class,
            "output_tail": evidence[-6000:] if evidence else "", "ports": {"app": PORT}}


def _smoke(steps: list[dict], contract_paths: list[str], key: str) -> str | None:
    """Run the API smoke test. Returns an error string for the first failing check, else None."""
    def check(name: str, ok: bool, detail: str = "") -> str | None:
        steps.append({"step": name, "ok": ok, "detail": detail[:300]})
        return None if ok else f"smoke check failed: {name}: {detail[:600]}"

    target = "https://example.com/verify/" + secrets.token_hex(4)

    st, _, body = _http("POST", "/api/shorten", {"url": target})
    if err := check("POST /api/shorten without API key -> 401", st == 401, f"got {st}: {body[:200]}"):
        return err
    st, _, body = _http("POST", "/api/shorten", {"url": target}, {"X-API-Key": key})
    if err := check("POST /api/shorten with API key -> 201", st == 201, f"got {st}: {body[:300]}"):
        return err
    try:
        code = json.loads(body)["shortCode"]
    except (ValueError, KeyError):
        return check("shorten response has shortCode", False, body[:300])
    st, hdr, body = _http("GET", f"/{urllib.parse.quote(code)}")
    if err := check("GET /{shortCode} -> 302 to the original URL", st == 302 and hdr.get("location") == target,
                    f"got {st} Location={hdr.get('location')!r}"):
        return err
    clicks, deadline = -1, time.monotonic() + 45   # analytics are eventually consistent (write-behind flush)
    while time.monotonic() < deadline:
        st, _, body = _http("GET", f"/api/analytics/{urllib.parse.quote(code)}")
        if st == 200:
            try:
                clicks = int(json.loads(body).get("totalClicks", -1))
            except (ValueError, TypeError):
                clicks = -1
            if clicks >= 1:
                break
        time.sleep(2)
    if err := check("GET /api/analytics/{shortCode} counts the click", clicks >= 1, f"status {st}, totalClicks={clicks}: {body[:200]}"):
        return err
    st, _, body = _http("GET", "/v3/api-docs")
    missing = [p for p in contract_paths if p not in body]
    if err := check("GET /v3/api-docs serves the OpenAPI spec", st == 200 and not missing, f"got {st}, missing paths {missing}"):
        return err
    if err := check("OpenAPI spec declares the X-API-Key scheme (Swagger UI 'Authorize' button)",
                    "securitySchemes" in body and "X-API-Key" in body, "securitySchemes/X-API-Key missing from /v3/api-docs"):
        return err
    st, _, _ = _http("GET", "/swagger-ui/index.html")
    return check("GET /swagger-ui/index.html serves Swagger UI", st == 200, f"got {st}")


def verify_deployment(repo: Path, run_id: str, contract_paths: list[str] | None = None, sleep=time.sleep) -> dict:
    """Build + start the stack and smoke test it. Always tears the stack down."""
    steps: list[dict] = []
    if not (repo / "docker-compose.yml").exists():
        return _result(steps, False, "docker-compose.yml is missing from the project root", "test")
    if not _port_free(PORT):
        return _result(steps, False, f"host port {PORT} is already in use; free it so the stack can be verified", "infra")

    api_key = secrets.token_urlsafe(16)
    env = {**os.environ, "DB_PASSWORD": secrets.token_urlsafe(12), "SHORTENER_API_KEY": api_key,
           "APP_BASE_URL": f"http://localhost:{PORT}", "DOCKER_BUILDKIT": "1"}
    compose = _Compose(repo, f"sdlc-verify-{run_id}", env)
    paths = contract_paths or ["/api/shorten"]
    try:
        # 1. build (transient registry/Maven Central errors are retried, then reported as infra, not as a code bug)
        build_out = ""
        for attempt in range(1, 4):
            proc = compose.run("build")
            build_out = proc.stdout + "\n" + proc.stderr
            if proc.returncode == 0:
                steps.append({"step": "docker compose build", "ok": True, "detail": f"attempt {attempt}"})
                break
            if not _TRANSIENT.search(build_out) or attempt == 3:
                steps.append({"step": "docker compose build", "ok": False, "detail": f"attempt {attempt}"})
                transient = bool(_TRANSIENT.search(build_out)) and "COMPILATION ERROR" not in build_out
                return _result(steps, False, "docker compose build failed:\n" + _tail(build_out, 80),
                               "infra" if transient else (classify_failure(build_out) or "test"))
            sleep(5 * attempt)

        # 2. start
        proc = compose.run("up", "-d", timeout=300)
        ok = proc.returncode == 0
        steps.append({"step": "docker compose up -d", "ok": ok, "detail": _tail(proc.stderr, 3)})
        if not ok:
            return _result(steps, False, "docker compose up failed:\n" + _tail(proc.stdout + proc.stderr, 60), "test")

        # 3. health
        deadline, status = time.monotonic() + 240, None
        while time.monotonic() < deadline:
            try:
                status, _, body = _http("GET", "/actuator/health", timeout=5)
                if status == 200 and '"UP"' in body:
                    break
            except OSError:
                status = None
            sleep(3)
        else:
            logs = compose.run("logs", "--no-color", "--tail", "80", "app", timeout=60)
            evidence = f"app did not become healthy within 240s (last status {status}). App logs:\n" + _tail(logs.stdout + logs.stderr, 80)
            steps.append({"step": "app healthy", "ok": False, "detail": f"last status {status}"})
            return _result(steps, False, evidence, classify_failure(evidence) if classify_failure(evidence) != "infra" else "test")
        steps.append({"step": "app healthy (/actuator/health UP)", "ok": True, "detail": ""})

        # 4. smoke
        err = _smoke(steps, paths, api_key)
        if err:
            logs = compose.run("logs", "--no-color", "--tail", "60", "app", timeout=60)
            evidence = err + "\nApp logs:\n" + _tail(logs.stdout + logs.stderr, 60)
            cls = classify_failure(evidence)
            return _result(steps, False, evidence, "test" if cls in ("infra", "compile") else cls)
        return _result(steps, True)
    except subprocess.TimeoutExpired as exc:
        return _result(steps, False, f"deploy verification timed out: {exc.cmd}", "infra")
    except FileNotFoundError:
        return _result(steps, False, "docker: command not found", "infra")
    finally:
        try:
            compose.run("down", "-v", "--remove-orphans", timeout=180)
        except Exception:  # noqa: BLE001 - teardown must never mask the real result
            pass


def stub_result() -> dict:
    return {"passed": True, "steps": [{"step": "deploy verification (stub)", "ok": True, "detail": ""}],
            "failure_class": "", "output_tail": "", "ports": {"app": PORT}}


def enabled() -> bool:
    return os.environ.get("DEPLOY_VERIFY", "true").lower() == "true"
