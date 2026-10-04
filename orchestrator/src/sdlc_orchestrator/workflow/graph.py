"""
Workflow Orchestration (Core Requirement #4 - the "critical
differentiator"): an explicit, stateful, gated dependency graph.

                      requirements
                            |
              ambiguities?--+--no--------------------+
                   | yes                             |
        clarification_request                        |
                   |                                 |
          clarification (HUMAN GATE #1)  --declined--> abort
                   |                                 |
                   +-----------------> workspace <---+   (git init / branch)
                                           |
                                      decomposition  (task DAG, waves)
                                           |
                       brownfield? --yes-> codebase_reasoning
                                           |
   +-------------------------------------> design <------------------------+
   |                                       |                               |
   |                          design_review_request                        |
   |                                       |                               |
   |       revise (feedback) <-- design_review (HUMAN GATE #2) --reject--> abort
   |                                       | approve                       |
   |                                    scaffold  (greenfield; idempotent) |
   |                              +--------+--------+   PARALLEL FAN-OUT   |
   |                          impl_data          impl_api                  |
   |                              +--------+--------+   SYNC POINT (join)  |
   |                                  quality_gate  (blocking policy gate)  |
   |                       fail: retry/rollback | pass                      |
   |                                       |                               |
   |                                    testing  (real `mvnw test`)         |
   |   schema/contract failure -> replan --+-- infra -> abort               |
   |        (reset + Design re-runs) ------+--------------------------------+
   |   other failure -> retry_bookkeeping (backoff; fallback strategy after 2) -> fan-out
   |   retries exhausted -> rollback (git reset --hard) -> END
   |                                       | pass
   |                                    commit  (one commit per approved task)
   |                                       |
   |                                     docs
   |                                       |
   |                               release_readiness (final gate) --fail--> rollback
   |                                       |
   |                              approval (HUMAN GATE #3)
   |            rework_design -> replan ---+--- approve/reject
   +---------------------------------------+        |
                                               finalize (merge+tag | back to main) -> END
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from sdlc_orchestrator.core import config
from sdlc_orchestrator.core.failures import REPLAN_CLASSES
from sdlc_orchestrator.workflow.nodes.codebase_reasoning_agent import codebase_reasoning_node
from sdlc_orchestrator.workflow.nodes.codegen_agent import impl_api_node, impl_data_node
from sdlc_orchestrator.workflow.nodes.control import (
    abort_node, approval_node, clarification_node, clarification_request_node, design_review_node,
    design_review_request_node, finalize_node, replan_node, replans_remaining, retries_in_epoch, retry_bookkeeping_node, rollback_node,
)
from sdlc_orchestrator.workflow.nodes.decomposition_agent import decomposition_node
from sdlc_orchestrator.workflow.nodes.design_agent import design_node
from sdlc_orchestrator.workflow.nodes.docs_agent import docs_node
from sdlc_orchestrator.workflow.nodes.quality_agent import commit_node, quality_gate_node
from sdlc_orchestrator.workflow.nodes.release_agent import release_readiness_node
from sdlc_orchestrator.workflow.nodes.requirements_agent import requirements_node
from sdlc_orchestrator.workflow.nodes.scaffold_agent import scaffold_node
from sdlc_orchestrator.workflow.nodes.testing_agent import testing_node
from sdlc_orchestrator.workflow.nodes.workspace import workspace_node
from sdlc_orchestrator.core.state import OrchestratorState


# ---------------------------------------------------------------------------
# Conditional-edge predicates (pure functions of state - unit-testable)
# ---------------------------------------------------------------------------

def route_after_requirements(state: dict) -> str:
    return "clarification_request" if state.get("ambiguities") else "workspace"


def route_after_clarification(state: dict) -> str:
    return "workspace" if state.get("ambiguities_resolved") else "abort"


def route_after_decomposition(state: dict) -> str:
    mode = state.get("mode") or state.get("scenario")
    return "codebase_reasoning" if mode == "brownfield" else "design"


def route_after_design_review(state: dict) -> str:
    d = state.get("design_decision")
    if d == "approve":
        return "scaffold"
    return "design" if d == "revise" else "abort"   # reject (or anything unknown) never proceeds


def _retry_or_rollback(state: dict) -> str:
    return "retry" if retries_in_epoch(state) < config.max_retries() else "rollback"


def route_after_quality(state: dict) -> str:
    if not state.get("guardrail_failures"):
        return "testing"
    return _retry_or_rollback(state)


def route_after_testing(state: dict) -> str:
    tr = state.get("test_results", {})
    if tr.get("passed"):
        return "commit"
    cls = tr.get("failure_class") or state.get("failure_class", "")
    if cls == "infra":
        return "abort"  # environment problem: do not burn retries or revert good code
    if cls in REPLAN_CLASSES and replans_remaining(state):
        return "replan"  # upstream (Design) artifact is wrong
    return _retry_or_rollback(state)


def route_after_release_gate(state: dict) -> str:
    return "approval" if state.get("status") == "awaiting_approval" else "rollback"


def route_after_approval(state: dict) -> str:
    if state.get("release_decision") == "rework_design" and replans_remaining(state):
        return "replan"
    return "finalize"


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def build_graph(checkpointer):
    g = StateGraph(OrchestratorState)

    for name, fn in [
        ("requirements", requirements_node),
        ("clarification_request", clarification_request_node),
        ("clarification", clarification_node),
        ("workspace", workspace_node),
        ("decomposition", decomposition_node),
        ("codebase_reasoning", codebase_reasoning_node),
        ("design", design_node),
        ("design_review_request", design_review_request_node),
        ("design_review", design_review_node),
        ("scaffold", scaffold_node),
        ("impl_data", impl_data_node),
        ("impl_api", impl_api_node),
        ("quality_gate", quality_gate_node),
        ("testing", testing_node),
        ("retry_bookkeeping", retry_bookkeeping_node),
        ("replan", replan_node),
        ("rollback", rollback_node),
        ("commit", commit_node),
        ("docs", docs_node),
        ("release_readiness", release_readiness_node),
        ("approval", approval_node),
        ("finalize", finalize_node),
        ("abort", abort_node),
    ]:
        g.add_node(name, fn)

    g.set_entry_point("requirements")
    g.add_conditional_edges("requirements", route_after_requirements,
                            {"clarification_request": "clarification_request", "workspace": "workspace"})
    g.add_edge("clarification_request", "clarification")
    g.add_conditional_edges("clarification", route_after_clarification, {"workspace": "workspace", "abort": "abort"})
    g.add_edge("workspace", "decomposition")
    g.add_conditional_edges("decomposition", route_after_decomposition,
                            {"codebase_reasoning": "codebase_reasoning", "design": "design"})
    g.add_edge("codebase_reasoning", "design")
    g.add_edge("design", "design_review_request")
    g.add_edge("design_review_request", "design_review")
    g.add_conditional_edges("design_review", route_after_design_review,
                            {"scaffold": "scaffold", "design": "design", "abort": "abort"})

    # Parallel fan-out; LangGraph waits for BOTH before quality_gate (sync point).
    g.add_edge("scaffold", "impl_data")
    g.add_edge("scaffold", "impl_api")
    g.add_edge(["impl_data", "impl_api"], "quality_gate")

    g.add_conditional_edges("quality_gate", route_after_quality,
                            {"testing": "testing", "retry": "retry_bookkeeping", "rollback": "rollback"})
    g.add_conditional_edges("testing", route_after_testing, {
        "commit": "commit", "retry": "retry_bookkeeping", "replan": "replan", "rollback": "rollback", "abort": "abort"})
    g.add_edge("retry_bookkeeping", "impl_data")   # bounded retry loop re-enters the fan-out
    g.add_edge("retry_bookkeeping", "impl_api")
    g.add_edge("replan", "design")                 # Design re-runs, informed by design_feedback

    g.add_edge("commit", "docs")
    g.add_edge("docs", "release_readiness")
    g.add_conditional_edges("release_readiness", route_after_release_gate, {"approval": "approval", "rollback": "rollback"})
    g.add_conditional_edges("approval", route_after_approval, {"replan": "replan", "finalize": "finalize"})

    g.add_edge("finalize", END)
    g.add_edge("rollback", END)
    g.add_edge("abort", END)
    return g.compile(checkpointer=checkpointer)
