"""
Central, lazily-read configuration. Everything reads os.environ at CALL
time (not import time) so tests and the `--offline` CLI flag can change
behaviour with monkeypatch/os.environ without re-importing modules.

There is deliberately NO switch to disable the release-approval gate:
"nothing auto-approves" (Controlled Autonomy) is not configurable.
"""
from __future__ import annotations

import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def max_retries() -> int:
    """Bounded in-place retries of the implementation stage per plan version."""
    return _int("MAX_RETRIES_PER_NODE", 2)


def fallback_after_failures() -> int:
    """After this many failed attempts, switch codegen to the simpler strategy."""
    return _int("FALLBACK_AFTER_FAILURES", 2)


def max_replans() -> int:
    """Bounded upstream re-plans (Design re-run) per run."""
    return _int("MAX_REPLANS", 1)


def backoff_base_seconds() -> float:
    return _float("RETRY_BACKOFF_BASE_SECONDS", 2.0)


def backoff_cap_seconds() -> float:
    return _float("RETRY_BACKOFF_CAP_SECONDS", 30.0)


def llm_max_attempts() -> int:
    return _int("LLM_MAX_ATTEMPTS", 4)


def recursion_limit() -> int:
    return _int("GRAPH_RECURSION_LIMIT", 120)


def stub_mode() -> bool:
    return os.environ.get("STUB_MODE", "false").lower() == "true"


def target_repo() -> Path:
    return Path(os.environ.get("TARGET_REPO_PATH", "../url-shortener-service")).expanduser().resolve()


def runs_dir() -> Path:
    p = Path(os.environ.get("RUNS_DIR", "runs")).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def java_version() -> str:
    return os.environ.get("JAVA_VERSION", "21")


def min_boot_major() -> int:
    return _int("MIN_SPRING_BOOT_MAJOR", 4)


def initializr_url() -> str:
    return os.environ.get("INITIALIZR_URL", "https://start.spring.io").rstrip("/")


def maven_timeout_seconds() -> int:
    return _int("MAVEN_TIMEOUT_SECONDS", 900)
