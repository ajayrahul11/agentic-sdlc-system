"""
LangSmith wiring (audit-grade LLM tracing). LangSmith auto-instruments
every LangChain/LangGraph call once the env vars are set; this module only
validates configuration and returns run metadata so a trace can be found by
run_id/scenario and correlated with the orchestrator_events log.

Tracing is OPTIONAL: if no API key is present it is switched off with a
warning rather than letting every LLM call fail on trace upload. The
engineering audit trail (events.py) does not depend on LangSmith.
"""
from __future__ import annotations

import os

from sdlc_orchestrator.core import config


def configure_tracing(run_id: str, scenario: str) -> dict[str, str]:
    wants = (os.environ.get("LANGCHAIN_TRACING_V2") or os.environ.get("LANGSMITH_TRACING") or "false").lower() == "true"
    has_key = bool(os.environ.get("LANGCHAIN_API_KEY") or os.environ.get("LANGSMITH_API_KEY"))

    if config.stub_mode() or not wants:
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        os.environ["LANGSMITH_TRACING"] = "false"
    elif not has_key:
        print("[tracing] LangSmith tracing requested but no API key set - tracing disabled for this run.")
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        os.environ["LANGSMITH_TRACING"] = "false"
    else:
        os.environ.setdefault("LANGSMITH_API_KEY", os.environ.get("LANGCHAIN_API_KEY", ""))
        os.environ["LANGSMITH_TRACING"] = "true"

    os.environ.setdefault("LANGCHAIN_PROJECT", "agentic-sdlc-url-shortener")
    os.environ.setdefault("LANGSMITH_PROJECT", os.environ["LANGCHAIN_PROJECT"])
    return {"run_id": run_id, "scenario": scenario}
