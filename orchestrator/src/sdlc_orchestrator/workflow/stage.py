"""
The `@stage` decorator: every graph node is wrapped so that, uniformly,

  1. its PRECONDITIONS (contracts.PRE) are checked before it may fire,
  2. an `entry` audit event is written,
  3. its POSTCONDITIONS (contracts.POST) are checked before it may exit,
  4. an `exit` audit event (outcome, reason, duration, actor) is written,
  5. a timeline record is appended to state.

"A node can't fire until preconditions are met; can't exit until
postconditions validate." (document, Day 1). Nodes may steer the logged
outcome by returning `_outcome` / `_reason` / `_detail` keys, which the
wrapper strips before handing the delta to LangGraph.

Nodes that contain a LangGraph `interrupt()` are NOT decorated: on resume
LangGraph re-executes the node from the top, which would double-log entry.
They are split into a decorated *request* node and an undecorated *gate*.
"""
from __future__ import annotations

import functools
import time
from typing import Callable

from sdlc_orchestrator.workflow import contracts
from sdlc_orchestrator.core.events import log_event
from sdlc_orchestrator.core.state import now_iso


class PreconditionError(RuntimeError):
    pass


class PostconditionError(RuntimeError):
    pass


def stage(name: str, actor: str | None = None) -> Callable:
    def deco(fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
        @functools.wraps(fn)
        def wrapper(state: dict) -> dict:
            run_id = state["run_id"]
            who = actor or f"agent:{name}"

            pre = contracts.PRE.get(name, lambda s: [])(state)
            if pre:
                log_event(run_id, name, "precondition_fail", {"failures": pre}, actor="system:contracts",
                          outcome="fail", reason="; ".join(pre)[:500])
                raise PreconditionError(f"[{name}] precondition failed: " + "; ".join(pre))

            started = time.monotonic()
            log_event(run_id, name, "entry", {}, actor=who)
            try:
                delta = fn(state)
            except Exception as exc:
                log_event(run_id, name, "node_error", {"error": type(exc).__name__}, actor=who,
                          outcome="error", reason=f"{type(exc).__name__}: {exc}"[:500])
                raise

            outcome = delta.pop("_outcome", "ok")
            reason = delta.pop("_reason", None)
            detail = delta.pop("_detail", {})

            post = contracts.POST.get(name, lambda s: [])({**state, **delta})
            if post:
                log_event(run_id, name, "postcondition_fail", {"failures": post}, actor="system:contracts",
                          outcome="fail", reason="; ".join(post)[:500])
                raise PostconditionError(f"[{name}] postcondition failed: " + "; ".join(post))

            duration_ms = int((time.monotonic() - started) * 1000)
            log_event(run_id, name, "exit", detail, actor=who, outcome=outcome, reason=reason, duration_ms=duration_ms)
            delta["timeline"] = [{"node": name, "event": "exit", "outcome": outcome, "at": now_iso(), "duration_ms": duration_ms}]
            delta["current_node"] = name
            return delta

        return wrapper

    return deco
