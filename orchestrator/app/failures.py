"""
Failure classification - this is the CONCRETE re-plan trigger definition
(document: "define concretely what upstream change causes a replan vs
what just retries in place").

  infra     environment problem (no Docker/JDK/Maven). Not the agent's
            fault -> ABORT, never burn retries or roll back code.
  compile   javac error                       -> retry in place
  test      assertion / runtime failure       -> retry in place
  guardrail policy gate failure               -> retry in place
  schema    DB schema disagrees with entities -> REPLAN (Design re-runs)
  contract  implementation disagrees with the
            OpenAPI contract in a test        -> REPLAN (Design re-runs)

Plus a non-test trigger: a human choosing `rework_design` at the release
approval gate (cause: human_rejected_design).
"""
from __future__ import annotations

import re

REPLAN_CLASSES = {"schema", "contract"}

_INFRA = [
    r"Could not find a valid Docker environment",
    r"Cannot connect to the Docker daemon",
    r"docker: command not found",
    r"mvnw?: command not found",
    r"release version \d+ not supported",
    r"invalid target release",
    r"JAVA_HOME .* not (defined|set)",
    r"Unsupported class file major version",
]
_COMPILE = [r"COMPILATION ERROR", r"cannot find symbol", r"package [\w.]+ does not exist", r"error: incompatible types"]
_SCHEMA = [
    r"Schema-validation", r"SchemaManagementException", r"FlywayException", r"Validate failed: Migrations",
    r"relation \"[^\"]+\" does not exist", r"column \"?[\w.]+\"? (of relation \"[^\"]+\" )?does not exist",
    r"Migration .* failed", r"Detected failed migration",
]
_CONTRACT = [r"ContractTest", r"OpenApiContract", r"contract (mismatch|violation)", r"not served by the application"]


def classify_failure(output: str) -> str:
    for cls, patterns in (("infra", _INFRA), ("compile", _COMPILE), ("schema", _SCHEMA), ("contract", _CONTRACT)):
        if any(re.search(p, output or "", re.IGNORECASE) for p in patterns):
            return cls
    return "test"
