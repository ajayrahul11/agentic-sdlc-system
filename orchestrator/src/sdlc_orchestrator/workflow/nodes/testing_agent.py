"""
Validation and Risk Control (Core Requirement #6) - the Test Agent.

Runs the REAL Maven suite against the generated code, classifies any
failure (core/failures.py) and records it so the graph can decide between
in-place retry, upstream RE-PLAN (schema/contract failures), or abort
(environment problems). Never trusts codegen on its own word.
"""
from __future__ import annotations

from sdlc_orchestrator.core import config
from sdlc_orchestrator.integrations import deploy, runners
from sdlc_orchestrator.policy.java_scan import contract_endpoints
from sdlc_orchestrator.core.events import log_event
from sdlc_orchestrator.workflow.nodes.common import repo_path
from sdlc_orchestrator.workflow.stage import stage


def _impl(state: dict) -> dict:
    run_id = state["run_id"]
    repo = repo_path(state)

    problems = runners.preflight()
    if problems:
        result = {"passed": False, "returncode": -1, "output_tail": "Could not find a valid environment: " + "; ".join(problems),
                  "tests": 0, "failures": 0, "errors": 0, "skipped": 0, "failed_tests": [], "duration_s": 0.0,
                  "failure_class": "infra"}
        log_event(run_id, "testing", "test_fail", {"failure_class": "infra", "problems": problems}, actor="agent:testing",
                  outcome="infra", reason="; ".join(problems)[:300])
        return {"test_results": result, "failure_class": "infra", "failure_feedback": result["output_tail"],
                "_outcome": "infra", "_reason": result["output_tail"][:300]}

    result = runners.run_maven_tests(repo)
    result["failure_class"] = runners.classify(result)

    # Green unit/integration tests are not enough: the service must also build as a container, start, and
    # serve its APIs exactly the way a user would run it (docker compose up --build).
    if result["passed"] and deploy.enabled():
        if config.stub_mode():
            dep = deploy.stub_result()
        else:
            paths = sorted({p for _, p in contract_endpoints(state["design_doc"]["api_contract"])}) if state.get("design_doc") else None
            dep = deploy.verify_deployment(repo, run_id, paths)
        result["deploy"] = dep
        log_event(run_id, "testing", "deploy_verified" if dep["passed"] else "deploy_failed",
                  {"steps": dep["steps"], "failure_class": dep["failure_class"]}, actor="agent:testing",
                  outcome="pass" if dep["passed"] else "fail", reason=None if dep["passed"] else dep["output_tail"][-300:])
        if not dep["passed"]:
            failed = [s for s in dep["steps"] if not s["ok"]]
            result.update({"passed": False, "failure_class": dep["failure_class"] or "test",
                           "output_tail": "DEPLOY VERIFICATION FAILED (the unit tests passed, but the service does not build/run/serve "
                                          "correctly as a container).\nFailed step: "
                                          f"{failed[-1]['step'] if failed else 'n/a'}\n{dep['output_tail']}"})

    if result["passed"]:
        log_event(run_id, "testing", "test_pass", {"tests": result["tests"], "duration_s": result["duration_s"]},
                  actor="agent:testing", outcome="pass")
    else:
        log_event(run_id, "testing", "test_fail", {
            "failure_class": result["failure_class"], "failed_tests": result["failed_tests"][:10],
            "output_tail": result["output_tail"][-800:]}, actor="agent:testing", outcome="fail",
            reason=f"{result['failure_class']}: {result['output_tail'][-200:]}")

    return {
        "test_results": result,
        "failure_class": result["failure_class"],
        "failure_feedback": "" if result["passed"] else result["output_tail"],
        "_outcome": "pass" if result["passed"] else "fail",
        "_reason": None if result["passed"] else result["failure_class"],
        "_detail": {k: result[k] for k in ("tests", "failures", "errors", "duration_s")},
    }


testing_node = stage("testing")(_impl)
