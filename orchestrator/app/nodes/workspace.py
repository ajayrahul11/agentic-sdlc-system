"""
Workspace preparation (deterministic, no LLM): turns TARGET_REPO_PATH into
a git repository with a per-run branch so every later step can be rolled
back to a REAL commit.

  greenfield  target must be absent/empty -> `git init`, empty root commit
              on main, branch run/<id>.   (The project does not exist yet:
              the orchestrator creates it.)
  brownfield  target must already be a generated project -> branch from
              main's HEAD.
  ambiguous   resolves to brownfield if a project exists, else greenfield.
"""
from __future__ import annotations

from app import config, gitops
from app.stage import stage


def _workspace_impl(state: dict) -> dict:
    repo = config.target_repo()
    scenario = state["scenario"]
    has_project = gitops.is_repo(repo) and (repo / "pom.xml").exists()
    mode = scenario if scenario in ("greenfield", "brownfield") else ("brownfield" if has_project else "greenfield")
    branch = f"run/{state['run_id']}"

    if mode == "greenfield":
        base_sha = gitops.init_repo(repo)
    else:
        gitops.checkout(repo, "main")
        base_sha = gitops.head_sha(repo)
    gitops.create_branch(repo, branch)

    return {
        "mode": mode,
        "workspace": {
            "repo_path": str(repo), "branch": branch, "base_sha": base_sha,
            "rollback_sha": base_sha, "scaffold_sha": None, "last_good_sha": base_sha,
        },
        "_detail": {"mode": mode, "branch": branch, "base_sha": base_sha},
    }


workspace_node = stage("workspace", actor="system:workspace")(_workspace_impl)
