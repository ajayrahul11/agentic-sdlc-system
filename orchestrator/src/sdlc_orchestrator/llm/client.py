"""
Provider-agnostic LLM access with:
  * per-stage model routing  (efficient model routing: strongest model on
    codegen, cheaper models on docs-adjacent/low-risk stages; override any
    stage with MODEL_<STAGE>=model-id in .env)
  * bounded exponential-backoff retries on transient API errors
  * structured JSON extraction + one repair attempt
  * an `llm_call` audit event per call (model, tokens, latency) so cost is
    observable from the same event log as everything else
  * STUB_MODE: a deterministic offline LLM (llm/stubs.py) so the whole
    graph can be exercised with no API key and no network
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from sdlc_orchestrator.core import config
from sdlc_orchestrator.core.events import log_event

DEFAULT_MODELS = {
    "anthropic": {
        "default": "claude-sonnet-5-5",
        "codegen": "claude-opus-5-5",       # code quality is the retry-cost driver
        "design": "claude-opus-5-5",        # design errors trigger expensive re-plans
        "requirements": "claude-sonnet-5-5",
        "decomposition": "claude-sonnet-5-5",
        "codebase_reasoning": "claude-sonnet-5-5",
    },
    "openai": {"default": "gpt-4.1"},
}
MAX_TOKENS = {"codegen": 32000, "design": 16000}
NON_RETRYABLE = ("AuthenticationError", "PermissionDeniedError", "BadRequestError", "NotFoundError", "ValueError", "KeyError")


class AgentOutputError(RuntimeError):
    """The model could not produce output satisfying the stage contract."""


def model_for(stage: str) -> str:
    override = os.environ.get(f"MODEL_{stage.upper()}")
    if override:
        return override
    provider = os.environ.get("MODEL_PROVIDER", "anthropic").lower()
    table = DEFAULT_MODELS[provider]
    return table.get(stage, table["default"])


def get_llm(stage: str = "default"):
    if config.stub_mode():
        from sdlc_orchestrator.llm.stubs import StubLLM

        return StubLLM(stage)

    provider = os.environ.get("MODEL_PROVIDER", "anthropic").lower()
    model = model_for(stage)
    kwargs: dict[str, Any] = {}
    if os.environ.get("LLM_TEMPERATURE"):
        kwargs["temperature"] = float(os.environ["LLM_TEMPERATURE"])
    max_tokens = MAX_TOKENS.get(stage, 8192)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model, max_tokens=max_tokens, streaming=max_tokens > 16000,
                             timeout=600, max_retries=1, **kwargs)
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, max_tokens=max_tokens, timeout=600, max_retries=1, **kwargs)
    raise ValueError(f"Unknown MODEL_PROVIDER: {provider!r} (expected 'anthropic' or 'openai')")


def content_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # Anthropic content blocks
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def _sleep(seconds: float) -> None:  # separate function so tests can patch it
    time.sleep(seconds)


def invoke_llm(stage: str, system: str, user: str, *, run_id: str | None = None) -> str:
    """One model call with exponential backoff. Returns response text."""
    # prompts are written for "Java 21"; honour the configured JDK (JAVA_VERSION) so a 17-only machine stays consistent
    jv = config.java_version()
    system = system.replace("Java 21", f"Java {jv}").replace("JRE 21", f"JRE {jv}").replace("JDK 21", f"JDK {jv}")
    attempts = config.llm_max_attempts()
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            llm = get_llm(stage)
            response = llm.invoke([{"role": "system", "content": system}, {"role": "user", "content": user}])
            usage = getattr(response, "usage_metadata", None) or {}
            if run_id:
                log_event(
                    run_id, stage, "llm_call",
                    {"model": model_for(stage) if not config.stub_mode() else "stub",
                     "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                     "attempt": attempt},
                    actor=f"agent:{stage}", outcome="ok", duration_ms=int((time.monotonic() - started) * 1000),
                )
            return content_text(response)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            retryable = type(exc).__name__ not in NON_RETRYABLE
            if run_id:
                log_event(run_id, stage, "llm_call", {"attempt": attempt, "error": type(exc).__name__},
                          actor=f"agent:{stage}", outcome="error", reason=str(exc)[:300])
            if not retryable or attempt == attempts:
                break
            _sleep(min(config.backoff_cap_seconds(), config.backoff_base_seconds() * 2 ** (attempt - 1)))
    raise AgentOutputError(f"LLM call for stage {stage!r} failed after {attempts} attempts: {last_exc}") from last_exc


def extract_json(text: str) -> Any:
    """Parse JSON from model output, tolerating ```json fences and prose
    around the payload."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start: end + 1])
            except json.JSONDecodeError:
                continue
    raise json.JSONDecodeError("no JSON found in model output", text, 0)


def invoke_json(stage: str, system: str, user: str, *, run_id: str | None = None, expect: type = dict) -> Any:
    """Model call that must return JSON of type `expect`. One repair
    attempt, then AgentOutputError (callers decide whether to fail loudly
    or fall back - never silently)."""
    text = invoke_llm(stage, system, user, run_id=run_id)
    try:
        parsed = extract_json(text)
        if isinstance(parsed, expect):
            return parsed
        raise ValueError(f"expected {expect.__name__}, got {type(parsed).__name__}")
    except (json.JSONDecodeError, ValueError) as exc:
        repair = (
            f"{user}\n\nYour previous reply was not valid JSON of type {expect.__name__} ({exc}). "
            "Reply again with ONLY the JSON, no prose and no markdown fences."
        )
        text = invoke_llm(stage, system, repair, run_id=run_id)
        try:
            parsed = extract_json(text)
        except json.JSONDecodeError as exc2:
            raise AgentOutputError(f"stage {stage!r}: model returned invalid JSON twice: {exc2}") from exc2
        if not isinstance(parsed, expect):
            raise AgentOutputError(f"stage {stage!r}: expected {expect.__name__}, got {type(parsed).__name__}")
        return parsed
