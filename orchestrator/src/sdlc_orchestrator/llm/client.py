"""
Provider-agnostic LLM access with:
  * per-stage model routing  (efficient model routing: cheap models on
    classification-style stages, a strong model on design; override any
    stage with MODEL_<STAGE>=model-id in .env)
  * escalation: codegen starts on the cheaper model and moves to the strong
    one (MODEL_CODEGEN_ESCALATED) once an attempt has failed
  * Anthropic prompt caching on the stable prompt prefix (system prompt and,
    when the caller splits it out, the design), disable with PROMPT_CACHING=false
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
import threading
import time
from typing import Any

from sdlc_orchestrator.core import config
from sdlc_orchestrator.core.events import events_for_run, log_event, redact

DEFAULT_MODELS = {
    "anthropic": {
        "default": "claude-sonnet-5-5",
        "codegen": "claude-sonnet-5-5",     # first attempt; escalates to ESCALATED_MODELS after a failure
        "design": "claude-sonnet-5-5",      # compact JSON contract; Sonnet keeps a run inside a small $ budget
        "design_doc": "claude-sonnet-5-5",  # plain-markdown design document (separate call so JSON never overflows)
        "requirements": "claude-sonnet-5-5",
        "decomposition": "claude-haiku-4-5-20251001",       # DAG is validated deterministically, canonical-plan fallback
        "codebase_reasoning": "claude-haiku-4-5-20251001",  # every named path is checked against disk
        "json_repair": "claude-haiku-4-5-20251001",         # reformats a bad reply, needs no reasoning
    },
    "openai": {"default": "gpt-4.1", "json_repair": "gpt-4.1-mini"},
}
ESCALATED_MODELS = {
    "anthropic": {"codegen": "claude-opus-5-5"},  # code quality is the retry-cost driver
    "openai": {},
}
# Claude 5 models think adaptively and thinking tokens count against max_tokens: at the default effort a
# design call spent ALL 16000 tokens on reasoning and returned an empty reply. Effort is the control; keep it
# low where the output is a structured document, medium for code. Override: LLM_EFFORT_<STAGE>=low|medium|high.
DEFAULT_EFFORT = {"default": "low", "codegen": "medium"}
MAX_REPAIR_CHARS = 20000  # longer replies are re-requested in full, not repaired by a cheap model
MAX_TOKENS = {"codegen": 32000, "design": 16000, "design_doc": 12000}
NON_RETRYABLE = ("ReplayExhaustedError", "BudgetExceededError", "AuthenticationError", "PermissionDeniedError", "BadRequestError", "NotFoundError", "ValueError", "KeyError")


class AgentOutputError(RuntimeError):
    """The model could not produce output satisfying the stage contract."""


class ReplayExhaustedError(AgentOutputError):
    """LLM_REPLAY_RUN is set but the recorded run has no (more) saved replies for this stage."""


class BudgetExceededError(AgentOutputError):
    """The run's cumulative LLM spend reached MAX_RUN_COST_USD; no further calls are made."""


# $ per million tokens (input, output). Cache reads cost 0.1x input, cache writes 1.25x input.
PRICES = {"opus": (4.0, 20.0), "sonnet": (2.0, 10.0), "haiku": (1.0, 5.0)}
_spend: dict[str, float] = {}


def call_cost(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    """USD cost of one call. `input_tokens` is the total prompt size (it includes cached tokens)."""
    tier = next((t for t in PRICES if t in model), "opus")  # unknown model: assume the dearest tier
    p_in, p_out = PRICES[tier]
    fresh = max(0, input_tokens - cache_read - cache_write)
    return (fresh * p_in + cache_read * 0.1 * p_in + cache_write * 1.25 * p_in + output_tokens * p_out) / 1e6


def run_spend(run_id: str) -> float:
    """Cumulative LLM spend of a run. Seeded from the audit log so a resumed run keeps counting."""
    if run_id not in _spend:
        total = 0.0
        try:
            for e in events_for_run(run_id):
                if e.get("event_type") == "llm_call":
                    d = e.get("detail") or {}
                    total += d.get("cost_usd") or 0.0
        except Exception:  # noqa: BLE001 - an unreadable log must not stop the run; the in-memory counter still bounds it
            pass
        _spend[run_id] = total
    return _spend[run_id]


def _check_budget(run_id: str | None, stage: str) -> None:
    if not run_id or config.stub_mode():
        return
    spent, cap = run_spend(run_id), config.max_run_cost_usd()
    if spent >= cap:
        raise BudgetExceededError(f"LLM budget exhausted before stage {stage!r}: ${spent:.2f} spent of ${cap:.2f} cap (MAX_RUN_COST_USD)")


def model_for(stage: str, escalated: bool = False) -> str:
    """Model for a stage. `escalated=True` returns the stronger model used after a failed
    attempt (MODEL_<STAGE>_ESCALATED, else the built-in escalation table), falling back
    to the stage's normal model when none is defined."""
    provider = os.environ.get("MODEL_PROVIDER", "anthropic").lower()
    if escalated:
        override = os.environ.get(f"MODEL_{stage.upper()}_ESCALATED")
        if override:
            return override
        stronger = ESCALATED_MODELS.get(provider, {}).get(stage)
        if stronger:
            return stronger
    override = os.environ.get(f"MODEL_{stage.upper()}")
    if override:
        return override
    table = DEFAULT_MODELS[provider]
    return table.get(stage, table["default"])


def get_llm(stage: str = "default", model: str | None = None):
    if config.stub_mode():
        from sdlc_orchestrator.llm.stubs import StubLLM

        return StubLLM(stage)

    provider = os.environ.get("MODEL_PROVIDER", "anthropic").lower()
    model = model or model_for(stage)
    kwargs: dict[str, Any] = {}
    if os.environ.get("LLM_TEMPERATURE"):
        kwargs["temperature"] = float(os.environ["LLM_TEMPERATURE"])
    max_tokens = MAX_TOKENS.get(stage, 8192)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        effort = os.environ.get(f"LLM_EFFORT_{stage.upper()}") or DEFAULT_EFFORT.get(stage, DEFAULT_EFFORT["default"])
        if "haiku" not in model:   # Haiku 4.5 does not take an effort setting
            kwargs["output_config"] = {"effort": effort}
        return ChatAnthropic(model=model, max_tokens=max_tokens, streaming=max_tokens > 16000,
                             timeout=config.llm_timeout_seconds(), max_retries=1, **kwargs)
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, max_tokens=max_tokens, timeout=config.llm_timeout_seconds(), max_retries=1, **kwargs)
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


def _cache_enabled() -> bool:
    return (not config.stub_mode() and config.prompt_caching()
            and os.environ.get("MODEL_PROVIDER", "anthropic").lower() == "anthropic")


def build_messages(system: str, user: str, cache_prefix: str = "") -> list[dict]:
    """Chat messages. With Anthropic caching on, the system prompt and `cache_prefix` (the
    part of the user message that is identical across calls, e.g. the design) each get a
    cache breakpoint; everything after the prefix is the per-call, uncached part."""
    if not _cache_enabled():
        return [{"role": "system", "content": system},
                {"role": "user", "content": f"{cache_prefix}\n\n{user}" if cache_prefix else user}]
    mark = {"type": "ephemeral"}
    blocks: list[dict] = []
    if cache_prefix:
        blocks.append({"type": "text", "text": cache_prefix, "cache_control": mark})
    blocks.append({"type": "text", "text": user})
    return [{"role": "system", "content": [{"type": "text", "text": system, "cache_control": mark}]},
            {"role": "user", "content": blocks}]


# ---------------------------------------------------------------------------
# Record / replay: every real model reply is saved under runs/<run_id>/llm/ so a failed run can be
# re-run through the rest of the pipeline for $0 (LLM_REPLAY_RUN=<old run id>). Replies are served per
# stage in the order they were recorded; the model is never called while replaying.
# ---------------------------------------------------------------------------
_record_lock = threading.Lock()
_replay_cursor: dict[tuple[str, str], int] = {}


def _record_reply(run_id: str, stage: str, model: str, stop_reason: str | None, text: str) -> None:
    """Best-effort: a recording problem must never fail a run."""
    try:
        d = config.runs_dir() / run_id / "llm"
        with _record_lock:
            d.mkdir(parents=True, exist_ok=True)
            n = len(list(d.glob("*.json"))) + 1
            (d / f"{n:03d}-{stage}.json").write_text(
                json.dumps({"stage": stage, "model": model, "stop_reason": stop_reason, "text": redact(text)}, indent=1),
                encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def replay_source() -> str | None:
    return os.environ.get("LLM_REPLAY_RUN") or None


def _replay_reply(source: str, stage: str, run_id: str | None, attempt: int, started: float) -> dict:
    """Next recorded reply for `stage`. The cursor is the number of replayed calls this run already logged
    for the stage, so it survives `resume` in a new process. The lock covers read + log so the two parallel
    codegen branches never take the same reply."""
    with _record_lock:
        done = (sum(1 for e in events_for_run(run_id)
                    if e["node"] == stage and e["event_type"] == "llm_call" and (e.get("detail") or {}).get("replayed"))
                if run_id else _replay_cursor.get((source, stage), 0))
        d = config.runs_dir() / source / "llm"
        files = sorted(d.glob(f"*-{stage}.json")) if d.is_dir() else []
        if done >= len(files):
            raise ReplayExhaustedError(f"replay of run {source}: no (more) recorded replies for stage {stage!r} "
                                       f"(recordings are in runs/{source}/llm/)")
        rec = json.loads(files[done].read_text(encoding="utf-8"))
        _replay_cursor[(source, stage)] = done + 1
        if run_id:
            log_event(run_id, stage, "llm_call",
                      {"model": f"replay:{source}", "cost_usd": 0.0, "run_cost_usd": round(run_spend(run_id), 4),
                       "stop_reason": rec.get("stop_reason"), "attempt": attempt, "replayed": True},
                      actor=f"agent:{stage}", outcome="ok", duration_ms=int((time.monotonic() - started) * 1000))
        return rec


def invoke_llm(stage: str, system: str, user: str, *, run_id: str | None = None,
               cache_prefix: str = "", model: str | None = None) -> str:
    """One model call with exponential backoff. Returns response text.
    `model` overrides the stage's routed model (used for escalation and JSON repair)."""
    return _invoke(stage, system, user, run_id=run_id, cache_prefix=cache_prefix, model=model)[0]


def _invoke(stage: str, system: str, user: str, *, run_id: str | None, cache_prefix: str,
            model: str | None) -> tuple[str, str | None]:
    """Like invoke_llm but also returns the provider's stop reason (e.g. 'max_tokens')."""
    # prompts are written for "Java 21"; honour the configured JDK (JAVA_VERSION) so a 17-only machine stays consistent
    jv = config.java_version()
    system = system.replace("Java 21", f"Java {jv}").replace("JRE 21", f"JRE {jv}").replace("JDK 21", f"JDK {jv}")
    attempts = config.llm_max_attempts()
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            _check_budget(run_id, stage)
            if (source := replay_source()) and not config.stub_mode():
                rec = _replay_reply(source, stage, run_id, attempt, started)
                return rec["text"], rec.get("stop_reason")
            llm = get_llm(stage, model)
            response = llm.invoke(build_messages(system, user, cache_prefix))
            usage = getattr(response, "usage_metadata", None) or {}
            details = usage.get("input_token_details") or {}
            meta = getattr(response, "response_metadata", None) or {}
            stop_reason = meta.get("stop_reason") or meta.get("finish_reason")
            used_model = (model or model_for(stage)) if not config.stub_mode() else "stub"
            cost = 0.0 if config.stub_mode() else call_cost(
                used_model, usage.get("input_tokens") or 0, usage.get("output_tokens") or 0,
                details.get("cache_read") or 0, details.get("cache_creation") or 0)
            if run_id:
                _spend[run_id] = run_spend(run_id) + cost
                log_event(
                    run_id, stage, "llm_call",
                    {"model": used_model, "cost_usd": round(cost, 4), "run_cost_usd": round(_spend[run_id], 4),
                     "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                     "cache_read_tokens": details.get("cache_read"), "cache_write_tokens": details.get("cache_creation"),
                     "stop_reason": stop_reason, "attempt": attempt},
                    actor=f"agent:{stage}", outcome="ok", duration_ms=int((time.monotonic() - started) * 1000),
                )
            text_out = content_text(response)
            if run_id and not config.stub_mode():
                _record_reply(run_id, stage, used_model, stop_reason, text_out)
            return text_out, stop_reason
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            retryable = type(exc).__name__ not in NON_RETRYABLE
            if isinstance(exc, BudgetExceededError):
                raise
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


def invoke_json(stage: str, system: str, user: str, *, run_id: str | None = None, expect: type = dict,
                cache_prefix: str = "", model: str | None = None) -> Any:
    """Model call that must return JSON of type `expect`. One repair
    attempt, then AgentOutputError (callers decide whether to fall back or fail
    loudly - never silently).

    Repair is cheap when it can be: a short, complete-but-malformed reply goes to the
    `json_repair` model alone (not the original prompt). A long or truncated reply
    (stop_reason 'max_tokens') cannot be repaired by reformatting, so the original
    request is re-sent in full to the original model instead."""
    text, stop = _invoke(stage, system, user, run_id=run_id, cache_prefix=cache_prefix, model=model)
    try:
        parsed = extract_json(text)
        if isinstance(parsed, expect):
            return parsed
        raise ValueError(f"expected {expect.__name__}, got {type(parsed).__name__}")
    except (json.JSONDecodeError, ValueError) as exc:
        truncated = stop in ("max_tokens", "length")
        if truncated or len(text) > MAX_REPAIR_CHARS or not text.strip():
            if run_id:
                log_event(run_id, stage, "json_repair", {"mode": "full_retry", "stop_reason": stop, "reply_chars": len(text)},
                          actor="system:llm", outcome="retrying", reason=f"cannot cheaply repair: {exc}"[:300])
            repair_text = _invoke(stage, system,
                                  f"{user}\n\nYour previous reply was not valid JSON of type {expect.__name__} ({exc}). "
                                  "Reply again with ONLY the JSON, no prose and no markdown fences. Keep it compact.",
                                  run_id=run_id, cache_prefix=cache_prefix, model=model)[0]
        else:
            repair = (
                f"The text below was meant to be a single JSON {expect.__name__} but could not be used ({exc}). "
                f"Reply with ONLY the corrected JSON {expect.__name__}, no prose and no markdown fences. "
                f"Do not add or invent content.\n\n--- TEXT ---\n{text}"
            )
            repair_text = invoke_llm(stage, "You repair malformed JSON.", repair, run_id=run_id, model=model_for("json_repair"))
        try:
            parsed = extract_json(repair_text)
        except json.JSONDecodeError as exc2:
            raise AgentOutputError(f"stage {stage!r}: model returned invalid JSON twice: {exc2}") from exc2
        if not isinstance(parsed, expect):
            raise AgentOutputError(f"stage {stage!r}: expected {expect.__name__}, got {type(parsed).__name__}")
        return parsed
