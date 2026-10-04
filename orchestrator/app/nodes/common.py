from __future__ import annotations

from pathlib import Path

from app import config


def repo_path(state: dict) -> Path:
    return Path(state.get("workspace", {}).get("repo_path") or config.target_repo())


def set_task_status(state: dict, stages: list[str], status: str) -> list[dict]:
    """Return updated copies of active tasks owned by `stages` (merged by
    id through the tasks reducer)."""
    out = []
    for t in state.get("tasks", []):
        if t.get("owner_stage") in stages and t.get("status") != "superseded":
            out.append({**t, "status": status})
    return out


def to_bool_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]
