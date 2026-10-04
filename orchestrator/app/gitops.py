"""
Git-backed rollback mechanics (document: "one commit per orchestrator-
approved task, rollback = revert to last approved commit, not a vague
'undo'").

Layout of the generated repo:
  main                  last RELEASE-APPROVED state (only moved by finalize)
  run/<run_id>          this run's work; one commit per approved task
  rollback_sha          commit before the implementation fan-out

All functions shell out to git so behaviour is identical to what a human
would see with `git log`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

AUTHOR = ["-c", "user.name=agentic-sdlc-orchestrator", "-c", "user.email=orchestrator@localhost"]


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *AUTHOR, *args], cwd=repo, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def is_repo(repo: Path) -> bool:
    return (repo / ".git").exists()


def head_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def init_repo(repo: Path) -> str:
    """Create the repo with an empty root commit on `main`. Returns sha."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "commit", "--allow-empty", "-m", "chore: initialise repository (agentic-sdlc-orchestrator)")
    return head_sha(repo)


def create_branch(repo: Path, name: str) -> None:
    _git(repo, "checkout", "-b", name)


def current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def is_clean(repo: Path) -> bool:
    return _git(repo, "status", "--porcelain") == ""


def commit_all(repo: Path, message: str) -> str:
    """Stage everything and commit. Returns the new sha (or current HEAD if
    there was nothing to commit)."""
    _git(repo, "add", "-A")
    if _git(repo, "status", "--porcelain") == "":
        return head_sha(repo)
    _git(repo, "commit", "-m", message)
    return head_sha(repo)


def commit_paths(repo: Path, paths: list[str], message: str) -> str:
    existing = [p for p in paths if (repo / p).exists()]
    if existing:
        _git(repo, "add", "--", *existing)
    if _git(repo, "diff", "--cached", "--name-only") == "":
        return head_sha(repo)
    _git(repo, "commit", "-m", message)
    return head_sha(repo)


def hard_reset(repo: Path, sha: str) -> None:
    """The actual rollback: working tree + index + untracked files return
    to exactly `sha`."""
    _git(repo, "reset", "--hard", sha)
    _git(repo, "clean", "-fd")


def checkout(repo: Path, ref: str) -> None:
    _git(repo, "checkout", ref)


def merge_ff(repo: Path, branch: str, into: str = "main") -> str:
    _git(repo, "checkout", into)
    _git(repo, "merge", "--ff-only", branch)
    return head_sha(repo)


def tag(repo: Path, name: str, message: str) -> None:
    _git(repo, "tag", "-a", name, "-m", message)


def changed_files(repo: Path, since_sha: str) -> list[tuple[str, str]]:
    """[(status, path)] between `since_sha` and the working tree.
    Status letters: A added, M modified, D deleted."""
    out = _git(repo, "diff", "--name-status", since_sha)
    rows = []
    for line in out.splitlines():
        status, _, path = line.partition("\t")
        rows.append((status.strip()[:1], path.strip()))
    return rows


def log_oneline(repo: Path, n: int = 20) -> list[str]:
    return _git(repo, "log", f"-{n}", "--oneline").splitlines()
