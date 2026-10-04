"""
Shared state that flows through every node in the orchestration graph.

This IS the audit trail (Core Requirement #4). Every node reads state and
returns a *delta*; LangGraph merges deltas with the reducers below, which
is what makes the two parallel implementation branches safe (they write
the same keys).

Design rules:
  * Everything stored in state is a plain JSON-serialisable dict/list/str.
    Pydantic models below are used to CONSTRUCT and VALIDATE records, then
    `.model_dump(mode="json")` is stored. This keeps the Postgres/SQLite
    checkpoint serialisation boring and version-proof.
  * Append-only lists (approvals, retries, rollbacks, replans, commits,
    timeline) use operator.add - history is never rewritten.
  * "Current" values (guardrail_failures, test_results...) are
    last-write-wins; their history lives in the orchestrator_events log.
"""
from __future__ import annotations

import datetime as dt
import operator
from enum import Enum
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Record models (validated on construction, stored as dicts)
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SUPERSEDED = "superseded"  # replaced by a re-plan


class Task(BaseModel):
    """One node of the decomposition DAG - a unit of engineering work, NOT
    a LangGraph node. `owner_stage` says which graph stage executes it."""

    id: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    owner_stage: str = ""
    plan_version: int = 1


class ApprovalRecord(BaseModel):
    gate: str                      # clarification | design | release
    decision: str                  # approve | reject | revise | rework_design | resolved
    approver: str                  # identity
    rationale: str = ""
    timestamp: str = Field(default_factory=now_iso)


class RetryRecord(BaseModel):
    node: str
    attempt: int
    reason: str
    failure_class: str = ""
    strategy: str = "full"
    plan_version: int = 1
    timestamp: str = Field(default_factory=now_iso)


class RollbackRecord(BaseModel):
    node: str
    reverted_to: str               # real git sha
    reason: str
    timestamp: str = Field(default_factory=now_iso)


class ReplanRecord(BaseModel):
    cause: str                     # schema_mismatch | contract_mismatch | human_rejected_design
    from_plan_version: int
    to_plan_version: int
    superseded_tasks: list[str] = Field(default_factory=list)
    detail: str = ""
    timestamp: str = Field(default_factory=now_iso)


class CommitRecord(BaseModel):
    task_id: str
    sha: str
    message: str
    timestamp: str = Field(default_factory=now_iso)


def dump(model: BaseModel) -> dict:
    return model.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------

def replace(_current: Any, new: Any) -> Any:
    return new


def merge_dict(current: dict | None, new: dict | None) -> dict:
    return {**(current or {}), **(new or {})}


def merge_dict_resettable(current: dict | None, new: dict | None) -> dict:
    """Like merge_dict, but a "__reset__" key in the update discards the
    previous contents first (used when a fallback restarts codegen from a
    clean scaffold, so stale files do not linger in state)."""
    new = dict(new or {})
    if new.pop("__reset__", False):
        return new
    return {**(current or {}), **new}


def merge_tasks(current: list[dict] | None, new: list[dict] | None) -> list[dict]:
    """Merge by task id: newer wins, first-seen order preserved. Lets two
    parallel branches update different tasks, and lets a re-plan mark old
    tasks SUPERSEDED while appending the regenerated ones."""
    merged: dict[str, dict] = {}
    for t in list(current or []) + list(new or []):
        merged[t["id"]] = t
    return list(merged.values())


class OrchestratorState(TypedDict, total=False):
    run_id: str
    scenario: Literal["greenfield", "brownfield", "ambiguous"]
    mode: Literal["greenfield", "brownfield"]  # resolved by the workspace node

    # --- Requirement understanding ---
    raw_requirement: str
    requirement_spec: Annotated[dict, replace]
    ambiguities: Annotated[list[str], replace]
    assumptions: Annotated[list[str], replace]
    ambiguities_resolved: bool

    # --- Workspace (git-backed target repo) ---
    workspace: Annotated[dict, merge_dict]  # repo_path, branch, base_sha, rollback_sha, ...

    # --- Task decomposition ---
    tasks: Annotated[list[dict], merge_tasks]
    plan_version: int
    plan_waves: Annotated[list[list[str]], replace]

    # --- Codebase reasoning (brownfield) ---
    impacted_modules: Annotated[list[str], replace]
    codebase_analysis: Annotated[dict, replace]

    # --- Design ---
    design_doc: Annotated[dict, replace]
    design_feedback: Annotated[str, replace]
    design_decision: Annotated[str, replace]    # "" | approve | reject | revise  (human design-review gate)

    # --- Implementation (two parallel branches write these) ---
    codegen_strategy: Annotated[str, replace]          # full | simple (fallback)
    code_artifacts: Annotated[dict[str, str], merge_dict_resettable]   # path -> content
    artifact_owner: Annotated[dict[str, str], merge_dict_resettable]   # path -> impl_data|impl_api
    branch_status: Annotated[dict[str, dict], merge_dict]   # branch -> {ok, errors}

    # --- Quality / testing ---
    guardrail_failures: Annotated[list[str], replace]
    failure_feedback: Annotated[str, replace]
    failure_class: Annotated[str, replace]
    test_results: Annotated[dict, replace]

    # --- Docs ---
    docs: Annotated[dict, replace]

    # --- Governance / control plane (append-only history) ---
    approvals: Annotated[list[dict], operator.add]
    retries: Annotated[list[dict], operator.add]
    rollbacks: Annotated[list[dict], operator.add]
    replans: Annotated[list[dict], operator.add]
    commits: Annotated[list[dict], operator.add]
    timeline: Annotated[list[dict], operator.add]  # node entry/exit timestamps

    release_decision: Annotated[str, replace]   # approve | reject | rework_design
    abort_reason: Annotated[str, replace]

    current_node: Annotated[str, replace]
    status: Annotated[str, replace]  # running|awaiting_approval|succeeded|failed|rolled_back
