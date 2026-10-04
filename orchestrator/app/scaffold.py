"""
Project scaffolding from Spring Initializr (https://start.spring.io).

Why Initializr instead of asking the model for a pom.xml: "latest starter
jars" must be REAL. Initializr's default bootVersion is the current GA
release and it emits a coherent pom.xml + Maven wrapper (so the old Maven
on a laptop does not matter). Nothing is pinned or hallucinated by us; we
only (a) ask the metadata endpoint which dependency ids are valid, (b)
request the ones the design needs, and (c) assert the resulting Boot
major version meets MIN_SPRING_BOOT_MAJOR (4).
"""
from __future__ import annotations

import io
import json
import re
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from app import config

GROUP_ID = "com.rahul"
ARTIFACT_ID = "url-shortener-service"
PACKAGE = "com.rahul.urlshortener"
APP_NAME = "UrlShortener"
DESCRIPTION = "URL shortener built by the agentic SDLC orchestrator"

# Initializr dependency ids. REQUIRED must exist; OPTIONAL are skipped (and
# logged) if this Initializr version no longer offers them.
REQUIRED_DEPS = ["web", "validation", "data-jpa", "data-redis", "postgresql", "flyway", "security", "actuator"]
OPTIONAL_DEPS = ["testcontainers"]


class ScaffoldError(RuntimeError):
    pass


def _get(url: str, accept: str | None = None, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept} if accept else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https endpoint from config
        return resp.read()


def fetch_metadata() -> dict:
    raw = _get(f"{config.initializr_url()}/metadata/client", accept="application/vnd.initializr.v2.2+json")
    return json.loads(raw)


def valid_dependency_ids(meta: dict) -> set[str]:
    ids: set[str] = set()
    for group in meta.get("dependencies", {}).get("values", []):
        for dep in group.get("values", []):
            ids.add(dep["id"])
    return ids


def resolve_dependencies(valid: set[str]) -> tuple[list[str], list[str]]:
    """Returns (dependency ids to request, optional ids that were skipped).
    Raises ScaffoldError if a REQUIRED id is not offered."""
    missing_required = [d for d in REQUIRED_DEPS if d not in valid]
    if missing_required:
        raise ScaffoldError(f"Spring Initializr does not offer required dependency ids: {missing_required}")
    chosen = list(REQUIRED_DEPS) + [d for d in OPTIONAL_DEPS if d in valid]
    skipped = [d for d in OPTIONAL_DEPS if d not in valid]
    return chosen, skipped


def build_url(deps: list[str], java_version: str, boot_version: str | None = None) -> str:
    params = {
        "type": "maven-project", "language": "java", "groupId": GROUP_ID, "artifactId": ARTIFACT_ID,
        "name": APP_NAME, "description": DESCRIPTION, "packageName": PACKAGE, "packaging": "jar",
        "javaVersion": java_version, "configurationFileFormat": "yaml", "dependencies": ",".join(deps),
    }
    if boot_version:
        params["bootVersion"] = boot_version
    return f"{config.initializr_url()}/starter.zip?" + urllib.parse.urlencode(params)


def extract_zip(data: bytes, dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    names = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise ScaffoldError(f"zip entry escapes destination: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            names.append(info.filename)
    for wrapper in ("mvnw",):
        if (dest / wrapper).exists():
            (dest / wrapper).chmod(0o755)
    return names


def boot_version_from_pom(pom_text: str) -> str | None:
    parent = re.search(r"<parent>.*?</parent>", pom_text, re.DOTALL)
    if not parent:
        return None
    m = re.search(r"<version>([^<]+)</version>", parent.group(0))
    return m.group(1) if m else None


def assert_boot_major(pom_text: str) -> str:
    version = boot_version_from_pom(pom_text)
    if not version:
        raise ScaffoldError("could not read the Spring Boot version from the generated pom.xml")
    major = int(re.match(r"(\d+)", version).group(1))
    if major < config.min_boot_major():
        raise ScaffoldError(
            f"Initializr returned Spring Boot {version} but Spring Boot >= {config.min_boot_major()} is required. "
            "Pin it with SPRING_BOOT_VERSION in .env (e.g. 4.0.0) or check that start.spring.io offers Boot 4.")
    return version


def remove_generated_context_test(repo: Path) -> list[str]:
    """Initializr adds a contextLoads() test that needs a live Postgres +
    Redis and would fail every run. The codegen test branch replaces it
    with Testcontainers-backed tests."""
    removed = []
    for p in (repo / "src/test").rglob("*ApplicationTests.java"):
        p.unlink()
        removed.append(p.relative_to(repo).as_posix())
    return removed


def scaffold_project(repo: Path, boot_version: str | None = None) -> dict:
    """Download + extract the project. Returns facts for the audit log."""
    meta = fetch_metadata()
    chosen, skipped = resolve_dependencies(valid_dependency_ids(meta))
    url = build_url(chosen, config.java_version(), boot_version)
    files = extract_zip(_get(url, timeout=120), repo)
    version = assert_boot_major((repo / "pom.xml").read_text())
    removed = remove_generated_context_test(repo)
    return {"boot_version": version, "dependencies": chosen, "skipped_optional": skipped,
            "files": len(files), "removed": removed, "url": url}


def scaffold_project_stub(repo: Path) -> dict:
    """Offline stand-in used ONLY in STUB_MODE: a placeholder pom (no
    build is ever run on it)."""
    (repo / "src/main/java/com/rahul/urlshortener").mkdir(parents=True, exist_ok=True)
    (repo / "src/test/java/com/rahul/urlshortener").mkdir(parents=True, exist_ok=True)
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project>
  <parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId><version>4.0.0</version></parent>
  <groupId>{GROUP_ID}</groupId><artifactId>{ARTIFACT_ID}</artifactId>
  <dependencies>
    <dependency><artifactId>spring-boot-starter-web</artifactId></dependency>
    <dependency><artifactId>spring-boot-starter-security</artifactId></dependency>
    <dependency><artifactId>spring-boot-starter-actuator</artifactId></dependency>
  </dependencies>
</project>
"""
    (repo / "pom.xml").write_text(pom)
    (repo / "mvnw").write_text("#!/bin/sh\necho stub mvnw\n")
    (repo / "mvnw").chmod(0o755)
    (repo / ".gitignore").write_text("target/\n.env\n")
    return {"boot_version": "4.0.0 (stub)", "dependencies": ["stub"], "files": 3, "removed": []}
