import json
from types import SimpleNamespace

import pytest

from app import llm
from app.llm import AgentOutputError, extract_json, invoke_json, invoke_llm


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
    monkeypatch.setattr(llm, "get_llm", lambda stage: fake)
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


def test_content_blocks_are_flattened():
    resp = SimpleNamespace(content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    assert llm.content_text(resp) == "ab"
