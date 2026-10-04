"""
Checkpointer = the "stateful execution" backbone. Every super-step of the
graph is persisted, so a run can pause at a human gate, be resumed days
later from another process (`main.py resume`), replayed for audit, or
recovered after a crash without losing lineage.

CHECKPOINTER=postgres (default, the document's recommendation)
             sqlite   (zero infra, still resumable across processes)
             memory   (tests / single-process only)
"""
from __future__ import annotations

import os
from contextlib import contextmanager

_memory_saver = None


@contextmanager
def get_checkpointer():
    kind = os.environ.get("CHECKPOINTER", "postgres").lower()

    if kind == "memory":
        global _memory_saver
        from langgraph.checkpoint.memory import MemorySaver

        if _memory_saver is None:
            _memory_saver = MemorySaver()
        yield _memory_saver
    elif kind == "sqlite":
        from langgraph.checkpoint.sqlite import SqliteSaver

        from app import config

        with SqliteSaver.from_conn_string(str(config.runs_dir() / "checkpoints.sqlite")) as saver:
            yield saver
    elif kind == "postgres":
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(os.environ["ORCHESTRATOR_DB_URL"]) as saver:
            saver.setup()  # idempotent
            yield saver
    else:
        raise ValueError(f"Unknown CHECKPOINTER {kind!r} (postgres | sqlite | memory)")
