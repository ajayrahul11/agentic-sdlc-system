"""
Reliability metrics rolled up from the structured event log (document,
Day 3): success rate, retry count, rollback count, MTTR and end-to-end
latency - computed from events, not tracked separately, so the numbers are
auditable back to individual rows.

Definitions
  end_to_end_latency_s  first event -> run_finished, MINUS human wait time
                        (approval_requested -> approval_granted/rejected);
                        wall_clock_s keeps the raw figure.
  MTTR                  mean time from a failure event (test_fail |
                        guardrail_fail) to the next test_pass on the same
                        run - i.e. how long the system takes to get back to
                        green. Failures never recovered are counted
                        separately (unrecovered_failures).
"""
from __future__ import annotations

import sys
from datetime import datetime

from app.events import get_sink

FAILURE_EVENTS = {"test_fail", "guardrail_fail"}


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def compute_metrics(events: list[dict]) -> dict:
    if not events:
        return {"error": "no events found"}
    events = sorted(events, key=lambda e: e["created_at"])
    run_id = events[0]["run_id"]
    start = _ts(events[0]["created_at"])
    finished = [e for e in events if e["event_type"] == "run_finished"]
    end = _ts(finished[-1]["created_at"]) if finished else _ts(events[-1]["created_at"])
    status = finished[-1]["detail"].get("status") if finished else "incomplete"

    human_wait, open_req = 0.0, None
    for e in events:
        if e["event_type"] == "approval_requested":
            open_req = _ts(e["created_at"])
        elif e["event_type"] in ("approval_granted", "approval_rejected") and open_req:
            human_wait += (_ts(e["created_at"]) - open_req).total_seconds()
            open_req = None

    episodes, open_at = [], None
    unrecovered = 0
    for e in events:
        if e["event_type"] in FAILURE_EVENTS and open_at is None:
            open_at = _ts(e["created_at"])
        elif e["event_type"] == "test_pass" and open_at is not None:
            episodes.append((_ts(e["created_at"]) - open_at).total_seconds())
            open_at = None
    if open_at is not None:
        unrecovered = 1

    llm = [e for e in events if e["event_type"] == "llm_call" and e.get("outcome") == "ok"]
    stage_ms: dict[str, int] = {}
    for e in events:
        if e["event_type"] == "exit" and e.get("duration_ms"):
            stage_ms[e["node"]] = stage_ms.get(e["node"], 0) + e["duration_ms"]

    count = lambda t: sum(1 for e in events if e["event_type"] == t)  # noqa: E731
    wall = (end - start).total_seconds()
    return {
        "run_id": run_id,
        "status": status,
        "success": status == "succeeded",
        "end_to_end_latency_s": round(max(wall - human_wait, 0.0), 2),
        "wall_clock_s": round(wall, 2),
        "human_wait_s": round(human_wait, 2),
        "retry_count": count("retry"),
        "rollback_count": count("rollback"),
        "replan_count": count("replan"),
        "fallback_count": count("fallback_strategy"),
        "guardrail_failure_count": count("guardrail_fail"),
        "test_failure_count": count("test_fail"),
        "mttr_s": round(sum(episodes) / len(episodes), 2) if episodes else 0.0,
        "recovered_failure_episodes": len(episodes),
        "unrecovered_failures": unrecovered,
        "llm_calls": len(llm),
        "input_tokens": sum((e["detail"].get("input_tokens") or 0) for e in llm),
        "output_tokens": sum((e["detail"].get("output_tokens") or 0) for e in llm),
        "stage_time_ms": stage_ms,
        "event_count": len(events),
    }


def metrics_for_run(run_id: str) -> dict:
    return compute_metrics(get_sink().read(run_id))


def success_rate_across_runs() -> dict:
    rows = get_sink().all_finished()
    total = len(rows)
    ok = sum(1 for r in rows if r["detail"].get("status") == "succeeded")
    rolled = sum(1 for r in rows if r["detail"].get("status") == "rolled_back")
    return {"total_runs": total, "succeeded": ok, "rolled_back": rolled, "failed": total - ok - rolled,
            "success_rate": round(ok / total, 3) if total else None}


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    if len(sys.argv) > 1 and sys.argv[1] == "--all":
        print(success_rate_across_runs())
    elif len(sys.argv) > 1:
        print(metrics_for_run(sys.argv[1]))
    else:
        print("usage: python -m app.metrics <run_id> | python -m app.metrics --all")
