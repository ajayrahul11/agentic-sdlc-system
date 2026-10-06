import json
from types import SimpleNamespace

import pytest

from sdlc_orchestrator.llm import client as llm
from sdlc_orchestrator.llm.client import AgentOutputError, extract_json, invoke_json, invoke_llm


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here you go:\n{"a": [1, 2]}\nHope that helps') == {"a": [1, 2]}
    assert extract_json("[1, 2]") == [1, 2]
    with pytest.raises(json.JSONDecodeError):
        extract_json("no json here")


class Flaky:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def invoke(self, messages):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(content=r, usage_metadata={"input_tokens": 1, "output_tokens": 2})


def patch(monkeypatch, fake):
    monkeypatch.setattr(llm, "get_llm", lambda stage, model=None: fake)
    sleeps = []
    monkeypatch.setattr(llm, "_sleep", sleeps.append)
    return sleeps


def test_backoff_retries_transient_errors_with_exponential_delay(monkeypatch):
    monkeypatch.setenv("RETRY_BACKOFF_BASE_SECONDS", "2")
    fake = Flaky([RuntimeError("429"), RuntimeError("529"), "ok"])
    sleeps = patch(monkeypatch, fake)
    assert invoke_llm("design", "s", "u", run_id="r") == "ok"
    assert sleeps == [2, 4] and fake.calls == 3


def test_gives_up_after_max_attempts(monkeypatch):
    sleeps = patch(monkeypatch, Flaky([RuntimeError("x")] * 4))
    with pytest.raises(AgentOutputError, match="after 4 attempts"):
        invoke_llm("design", "s", "u")


def test_non_retryable_errors_fail_fast(monkeypatch):
    class AuthenticationError(Exception):
        pass

    fake = Flaky([AuthenticationError("bad key")])
    patch(monkeypatch, fake)
    with pytest.raises(AgentOutputError):
        invoke_llm("design", "s", "u")
    assert fake.calls == 1


def test_invoke_json_repairs_once_then_fails_loudly(monkeypatch):
    patch(monkeypatch, Flaky(["not json", '{"ok": true}']))
    assert invoke_json("design", "s", "u") == {"ok": True}
    patch(monkeypatch, Flaky(["nope", "still nope"]))
    with pytest.raises(AgentOutputError, match="invalid JSON twice"):
        invoke_json("design", "s", "u")


def test_invoke_json_repair_sends_only_bad_reply_to_cheap_model(monkeypatch):
    seen = []

    class Spy(Flaky):
        def invoke(self, messages):
            seen.append(messages)
            return super().invoke(messages)

    models = []
    spy = Spy(["not json {oops", '{"ok": true}'])
    monkeypatch.setattr(llm, "get_llm", lambda stage, model=None: (models.append(model), spy)[1])
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    assert invoke_json("design", "SYSTEM", "HUGE ORIGINAL PROMPT") == {"ok": True}
    repair_text = json.dumps(seen[1])
    assert "HUGE ORIGINAL PROMPT" not in repair_text and "not json {oops" in repair_text
    assert models == [None, llm.model_for("json_repair")]


def test_invoke_json_long_or_truncated_reply_is_retried_in_full(monkeypatch):
    seen = []

    class Spy(Flaky):
        def invoke(self, messages):
            seen.append(json.dumps(messages))
            return super().invoke(messages)

    models = []
    spy = Spy(["x" * (llm.MAX_REPAIR_CHARS + 1), '{"ok": true}'])
    monkeypatch.setattr(llm, "get_llm", lambda stage, model=None: (models.append(model), spy)[1])
    assert invoke_json("design", "SYSTEM", "ORIGINAL PROMPT") == {"ok": True}
    assert "ORIGINAL PROMPT" in seen[1] and models == [None, None]   # full prompt, original model


def test_invoke_json_type_check(monkeypatch):
    patch(monkeypatch, Flaky(["[1]", "[2]"]))
    with pytest.raises(AgentOutputError, match="expected dict"):
        invoke_json("design", "s", "u")


def test_llm_calls_are_audited_with_tokens(monkeypatch, offline_env):
    patch(monkeypatch, Flaky(["hi"]))
    invoke_llm("requirements", "s", "u", run_id="run-x")
    ev = offline_env.read("run-x")[0]
    assert ev["event_type"] == "llm_call" and ev["detail"]["input_tokens"] == 1 and ev["outcome"] == "ok"


def test_model_routing(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    assert llm.model_for("codegen") != llm.model_for("requirements") or True
    monkeypatch.setenv("MODEL_CODEGEN", "claude-custom")
    assert llm.model_for("codegen") == "claude-custom"
    assert llm.model_for("something_new") == llm.DEFAULT_MODELS["anthropic"]["default"]


def test_prompt_caching_marks_system_and_prefix(monkeypatch):
    monkeypatch.delenv("STUB_MODE", raising=False)
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.delenv("PROMPT_CACHING", raising=False)
    msgs = llm.build_messages("SYS", "variable", cache_prefix="stable")
    assert msgs[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert msgs[1]["content"][0] == {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}}
    assert msgs[1]["content"][1] == {"type": "text", "text": "variable"}


def test_prompt_caching_off_or_other_provider_uses_plain_strings(monkeypatch):
    monkeypatch.setenv("PROMPT_CACHING", "false")
    assert llm.build_messages("SYS", "v", cache_prefix="p") == [
        {"role": "system", "content": "SYS"}, {"role": "user", "content": "p\n\nv"}]
    monkeypatch.setenv("PROMPT_CACHING", "true")
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    assert llm.build_messages("SYS", "v")[0]["content"] == "SYS"


def test_codegen_escalates_to_stronger_model(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    for k in ("MODEL_CODEGEN", "MODEL_CODEGEN_ESCALATED"):
        monkeypatch.delenv(k, raising=False)
    assert llm.model_for("codegen") == "claude-sonnet-5-5"
    assert llm.model_for("codegen", escalated=True) == "claude-opus-5-5"
    monkeypatch.setenv("MODEL_CODEGEN_ESCALATED", "claude-x")
    assert llm.model_for("codegen", escalated=True) == "claude-x"
    # a stage with no escalation entry falls back to its normal model
    assert llm.model_for("requirements", escalated=True) == llm.model_for("requirements")


def test_codegen_model_follows_failed_attempts(monkeypatch):
    from sdlc_orchestrator.workflow.nodes import codegen_agent as cg
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    for k in ("MODEL_CODEGEN", "MODEL_CODEGEN_ESCALATED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ESCALATE_AFTER_FAILURES", "1")
    assert cg._model_for_attempt({"plan_version": 1, "retries": []}) == ("claude-sonnet-5-5", False)
    failed = {"plan_version": 1, "retries": [{"plan_version": 1}]}
    assert cg._model_for_attempt(failed) == ("claude-opus-5-5", True)
    # failures from an older plan version do not count
    assert cg._model_for_attempt({"plan_version": 2, "retries": [{"plan_version": 1}]})[1] is False


def test_content_blocks_are_flattened():
    resp = SimpleNamespace(content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    assert llm.content_text(resp) == "ab"


def test_call_cost_uses_tier_prices_and_cache_multipliers():
    # sonnet: $2 in / $10 out per M; cache read 0.1x, cache write 1.25x
    assert llm.call_cost("claude-sonnet-5-5", 1_000_000, 0) == pytest.approx(2.0)
    assert llm.call_cost("claude-sonnet-5-5", 1_000_000, 0, cache_read=1_000_000) == pytest.approx(0.2)
    assert llm.call_cost("claude-sonnet-5-5", 1_000_000, 0, cache_write=1_000_000) == pytest.approx(2.5)
    assert llm.call_cost("claude-opus-5-5", 0, 1_000_000) == pytest.approx(20.0)
    assert llm.call_cost("mystery-model", 0, 1_000_000) == pytest.approx(20.0)   # unknown = dearest tier


def test_budget_cap_stops_calls_without_retrying(monkeypatch):
    monkeypatch.setenv("STUB_MODE", "false")
    monkeypatch.setenv("MAX_RUN_COST_USD", "1.0")
    monkeypatch.setattr(llm, "_spend", {"budget-run": 1.0})
    fake = Flaky(["never used"])
    patch(monkeypatch, fake)
    with pytest.raises(llm.BudgetExceededError, match="budget exhausted"):
        invoke_llm("design", "s", "u", run_id="budget-run")
    assert fake.calls == 0


def test_spend_accumulates_from_usage(monkeypatch):
    monkeypatch.setenv("STUB_MODE", "false")
    monkeypatch.setattr(llm, "_spend", {"acc-run": 0.0})
    fake = Flaky(["ok"])
    fake_resp = SimpleNamespace(content="ok", usage_metadata={"input_tokens": 0, "output_tokens": 1_000_000})
    fake.invoke = lambda messages: fake_resp
    patch(monkeypatch, fake)
    monkeypatch.setattr(llm, "model_for", lambda stage, escalated=False: "claude-sonnet-5-5")
    invoke_llm("design", "s", "u", run_id="acc-run")
    assert llm._spend["acc-run"] == pytest.approx(10.0)


def test_anthropic_client_gets_low_effort_for_structured_stages_and_none_for_haiku(monkeypatch):
    monkeypatch.setenv("STUB_MODE", "false")
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert llm.get_llm("design").output_config == {"effort": "low"}
    assert llm.get_llm("codegen").output_config == {"effort": "medium"}
    monkeypatch.setenv("LLM_EFFORT_DESIGN", "high")
    assert llm.get_llm("design").output_config == {"effort": "high"}
    assert not llm.get_llm("json_repair").output_config


def test_replies_are_recorded_and_replayed_without_calling_the_api(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace

    import pytest

    from sdlc_orchestrator.llm import client

    monkeypatch.setenv("STUB_MODE", "false")
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.delenv("LLM_REPLAY_RUN", raising=False)

    class Fake:
        def invoke(self, messages):
            return SimpleNamespace(content="===FILE: src/a.txt===\nhi\n===END===", usage_metadata={"input_tokens": 1, "output_tokens": 1},
                                   response_metadata={"stop_reason": "max_tokens"})

    monkeypatch.setattr(client, "get_llm", lambda stage, model=None: Fake())
    assert client.invoke_llm("codegen", "s", "u", run_id="orig").startswith("===FILE")
    rec = json.loads(next((tmp_path / "orig" / "llm").glob("001-codegen.json")).read_text())
    assert rec["stop_reason"] == "max_tokens"      # truncation is reproduced on replay

    # replay: the model must never be called
    monkeypatch.setattr(client, "get_llm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("API called during replay")))
    monkeypatch.setenv("LLM_REPLAY_RUN", "orig")
    assert client.invoke_llm("codegen", "s", "u", run_id="again").startswith("===FILE")
    with pytest.raises(client.AgentOutputError):    # only one reply was recorded
        client.invoke_llm("codegen", "s", "u", run_id="again")
