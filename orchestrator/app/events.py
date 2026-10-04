"""
Structured, append-only audit log (Core Requirement #4: observability,
auditability). Every node transition is recorded as who / what / when /
why / outcome:

    actor      who acted        agent:design | human:rahul | system:gate
    node       what stage
    event_type what happened    entry | exit | retry | rollback | replan | ...
    reason     why              free text (failure summary, rationale)
    outcome    result           ok | pass | fail | error | approved | ...
    created_at when             timestamptz
    detail     everything else  JSONB

Sinks (EVENT_SINK): postgres (default, SQL-queryable) | jsonl (zero-infra,
runs/events.jsonl) | memory (tests). Postgres failures degrade to the
JSONL file instead of crashing a run - an audit-log outage must be loud,
not fatal.

Secrets are redacted before anything is written.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import threading
from typing import Any

from app import config

DDL = """
CREATE TABLE IF NOT EXISTS orchestrator_events (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    node TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT,
    reason TEXT,
    outcome TEXT,
    duration_ms INTEGER,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE orchestrator_events ADD COLUMN IF NOT EXISTS actor TEXT;
ALTER TABLE orchestrator_events ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE orchestrator_events ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE orchestrator_events ADD COLUMN IF NOT EXISTS duration_ms INTEGER;
CREATE INDEX IF NOT EXISTS idx_events_run_id ON orchestrator_events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_node ON orchestrator_events(node);
CREATE INDEX IF NOT EXISTS idx_events_type ON orchestrator_events(event_type);
"""

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"lsv2_[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)((?:api[_-]?key|password|secret|token)\s*[=:]\s*)[^\s\"',;]{4,}"),
]


def redact(obj: Any) -> Any:
    if isinstance(obj, str):
        out = obj
        for p in _SECRET_PATTERNS:
            out = p.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", out)
        return out
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------

class MemorySink:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._lock = threading.Lock()

    def write(self, row: dict) -> None:
        with self._lock:
            self.rows.append(row)

    def read(self, run_id: str) -> list[dict]:
        return [r for r in self.rows if r["run_id"] == run_id]

    def all_finished(self) -> list[dict]:
        return [r for r in self.rows if r["event_type"] == "run_finished"]

    def run_ids(self) -> list[str]:
        return list(dict.fromkeys(r["run_id"] for r in self.rows))


class JsonlSink:
    def __init__(self, path=None) -> None:
        self.path = path or (config.runs_dir() / "events.jsonl")
        self._lock = threading.Lock()

    def write(self, row: dict) -> None:
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def _all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def read(self, run_id: str) -> list[dict]:
        return [r for r in self._all() if r["run_id"] == run_id]

    def all_finished(self) -> list[dict]:
        return [r for r in self._all() if r["event_type"] == "run_finished"]

    def run_ids(self) -> list[str]:
        return list(dict.fromkeys(r["run_id"] for r in self._all()))


class PostgresSink:
    def __init__(self, url: str | None = None) -> None:
        self.url = url or os.environ["ORCHESTRATOR_DB_URL"]

    def _conn(self):
        import psycopg

        return psycopg.connect(self.url)

    def init(self) -> None:
        with self._conn() as conn:
            conn.execute(DDL)
            conn.commit()

    def write(self, row: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO orchestrator_events "
                "(run_id,node,event_type,actor,reason,outcome,duration_ms,detail,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (row["run_id"], row["node"], row["event_type"], row.get("actor"), row.get("reason"),
                 row.get("outcome"), row.get("duration_ms"), json.dumps(row.get("detail") or {}), row["created_at"]),
            )
            conn.commit()

    def read(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT run_id,node,event_type,actor,reason,outcome,duration_ms,detail,created_at "
                "FROM orchestrator_events WHERE run_id=%s ORDER BY id ASC", (run_id,),
            ).fetchall()
        return [
            {"run_id": r[0], "node": r[1], "event_type": r[2], "actor": r[3], "reason": r[4],
             "outcome": r[5], "duration_ms": r[6], "detail": r[7], "created_at": r[8].isoformat()}
            for r in rows
        ]

    def all_finished(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT run_id, detail FROM orchestrator_events WHERE event_type='run_finished'"
            ).fetchall()
        return [{"run_id": r[0], "detail": r[1]} for r in rows]

    def run_ids(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT run_id FROM orchestrator_events GROUP BY run_id ORDER BY MIN(id) DESC LIMIT 50"
            ).fetchall()
        return [r[0] for r in rows]


_sink = None


def set_sink(sink) -> None:
    """Tests inject a MemorySink here."""
    global _sink
    _sink = sink


def get_sink():
    global _sink
    if _sink is None:
        kind = os.environ.get("EVENT_SINK", "postgres").lower()
        _sink = {"memory": MemorySink, "jsonl": JsonlSink, "postgres": PostgresSink}[kind]()
    return _sink


def init_db() -> None:
    sink = get_sink()
    if isinstance(sink, PostgresSink):
        sink.init()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_event(
    run_id: str,
    node: str,
    event_type: str,
    detail: dict | None = None,
    *,
    actor: str | None = None,
    reason: str | None = None,
    outcome: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Append one audit event. NEVER raises into the caller's control flow."""
    row = redact({
        "run_id": run_id, "node": node, "event_type": event_type, "actor": actor,
        "reason": reason, "outcome": outcome, "duration_ms": duration_ms,
        "detail": detail or {},
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    })
    try:
        get_sink().write(row)
    except Exception as exc:  # noqa: BLE001 - audit outage must not kill a run
        print(f"[events] WARNING: sink write failed ({event_type}/{node}): {exc}; falling back to JSONL", file=sys.stderr)
        try:
            JsonlSink(config.runs_dir() / "events.fallback.jsonl").write(row)
        except Exception as exc2:  # noqa: BLE001
            print(f"[events] ERROR: fallback write failed too: {exc2}", file=sys.stderr)


def events_for_run(run_id: str) -> list[dict]:
    return get_sink().read(run_id)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        from dotenv import load_dotenv

        load_dotenv()
        init_db()
        print("orchestrator_events table ready")
    else:
        print("usage: python -m app.events init")
