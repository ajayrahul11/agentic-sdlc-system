"""
Process runners (Maven / Docker / JDK preflight). Isolated in one module
so tests can monkeypatch `run_maven_tests` and so STUB_MODE can simulate
test outcomes deterministically (STUB_TEST_FAILURES, STUB_FAILURE_CLASS).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from sdlc_orchestrator.core import config
from sdlc_orchestrator.core.failures import classify_failure

_stub_runs = 0


def reset_stub_counters() -> None:
    global _stub_runs
    _stub_runs = 0


def java_major() -> int | None:
    if not shutil.which("java"):
        return None
    proc = subprocess.run(["java", "-version"], capture_output=True, text=True)
    m = re.search(r'version "(\d+)', proc.stderr + proc.stdout)
    return int(m.group(1)) if m else None


def _java_major_of(java_bin: str) -> int | None:
    try:
        proc = subprocess.run([java_bin, "-version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'version "(\d+)', proc.stderr + proc.stdout)
    return int(m.group(1)) if m else None


def java_home_major() -> int | None:
    """Major version of the JDK in JAVA_HOME (None if unset/broken). The Maven wrapper prefers JAVA_HOME over PATH."""
    home = os.environ.get("JAVA_HOME")
    return _java_major_of(str(Path(home) / "bin" / "java")) if home else None


def maven_env() -> dict:
    """Environment for ./mvnw. A stale JAVA_HOME (e.g. still pointing at JDK 17 while JDK 21 is on PATH) makes
    the wrapper ignore the right JDK and fail with 'release version 21 not supported'. If JAVA_HOME is too old
    but the JDK on PATH is new enough, drop JAVA_HOME for the Maven child process so the PATH JDK is used."""
    env = dict(os.environ)
    want = int(config.java_version())
    home_major = java_home_major()
    if home_major is not None and home_major < want and (java_major() or 0) >= want:
        env.pop("JAVA_HOME", None)
    return env


def effective_java_major() -> int | None:
    """The JDK major version Maven will actually use (see maven_env)."""
    env = maven_env()
    return java_home_major() if env.get("JAVA_HOME") else java_major()


def docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, text=True).returncode == 0


def preflight() -> list[str]:
    """Environment problems that would make test failures meaningless."""
    if config.stub_mode():
        return []
    problems = []
    want = int(config.java_version())
    have = effective_java_major()
    if have is None:
        problems.append("java not found on PATH (need a JDK; set JAVA_HOME)")
    elif have < want:
        problems.append(f"JDK {have} is what Maven would use but the project targets Java {want}: install JDK {want} "
                        f"(and point JAVA_HOME at it or unset JAVA_HOME), or set JAVA_VERSION={have} in .env before the scaffold step")
    if not docker_ok():
        problems.append("Docker daemon not reachable (integration tests use Testcontainers)")
    return problems


def parse_surefire(repo: Path) -> dict:
    tests = failures = errors = skipped = 0
    failed: list[str] = []
    reports = repo / "target/surefire-reports"
    if reports.exists():
        for xml in reports.glob("TEST-*.xml"):
            try:
                root = ET.parse(xml).getroot()
            except ET.ParseError:
                continue
            tests += int(root.get("tests", 0))
            failures += int(root.get("failures", 0))
            errors += int(root.get("errors", 0))
            skipped += int(root.get("skipped", 0))
            for case in root.iter("testcase"):
                if case.find("failure") is not None or case.find("error") is not None:
                    failed.append(f"{case.get('classname')}.{case.get('name')}")
    return {"tests": tests, "failures": failures, "errors": errors, "skipped": skipped, "failed_tests": failed}


def _stub_result() -> dict:
    global _stub_runs
    _stub_runs += 1
    fail_n = int(os.environ.get("STUB_TEST_FAILURES", "0"))
    cls = os.environ.get("STUB_FAILURE_CLASS", "test")
    if _stub_runs <= fail_n:
        out = {
            "test": "[ERROR] Tests run: 5, Failures: 1 ... ShortenIntegrationTest.redirectReturns302 expected:<302> but was:<500>",
            "compile": "[ERROR] COMPILATION ERROR : cannot find symbol  symbol: class ShortCodeGenerator",
            "schema": "Schema-validation: missing column [expires_at] in table [url_mapping]",
            "contract": "ContractTest: GET /api/analytics/{shortCode} is not served by the application",
            "infra": "Could not find a valid Docker environment",
        }[cls]
        return {"passed": False, "returncode": 1, "output_tail": out, "tests": 5, "failures": 1, "errors": 0,
                "skipped": 0, "failed_tests": ["StubTest.fails"], "duration_s": 0.0}
    return {"passed": True, "returncode": 0, "output_tail": "BUILD SUCCESS (stub)", "tests": 12, "failures": 0,
            "errors": 0, "skipped": 0, "failed_tests": [], "duration_s": 0.0}


def run_maven_tests(repo: Path) -> dict:
    """Run the REAL test suite. Codegen output is never trusted on the
    model's word - only on `mvnw test` exiting 0 with tests actually run."""
    if config.stub_mode():
        return _stub_result()

    cmd = ["./mvnw", "-B", "test"] if (repo / "mvnw").exists() else ["mvn", "-B", "test"]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=repo, env=maven_env(), capture_output=True, text=True, timeout=config.maven_timeout_seconds())
        out = (proc.stdout + "\n" + proc.stderr)
        rc = proc.returncode
    except FileNotFoundError:
        out, rc = "mvn/mvnw: command not found", 127
    except subprocess.TimeoutExpired:
        out, rc = f"mvn test timed out after {config.maven_timeout_seconds()}s", 124

    counts = parse_surefire(repo)
    passed = rc == 0 and counts["tests"] > 0 and counts["failures"] == 0 and counts["errors"] == 0
    if rc == 0 and counts["tests"] == 0:
        out += "\n[orchestrator] mvn exited 0 but ZERO tests ran - treated as failure"
    # keep the informative part: surefire summary / [ERROR] lines first, then the tail
    err_lines = "\n".join(l for l in out.splitlines() if "[ERROR]" in l or "FAIL" in l or "Caused by" in l)
    tail = (err_lines[-5000:] + "\n--- tail ---\n" + out[-2500:]) if not passed else out[-1500:]
    return {"passed": passed, "returncode": rc, "output_tail": tail, **counts, "duration_s": round(time.monotonic() - started, 1)}


def classify(result: dict) -> str:
    return "" if result["passed"] else classify_failure(result["output_tail"])
