import pytest

from sdlc_orchestrator.integrations import gitops


def test_commit_per_task_and_hard_rollback(tmp_path):
    repo = tmp_path / "r"
    root = gitops.init_repo(repo)
    gitops.create_branch(repo, "run/1")
    (repo / "a.txt").write_text("approved")
    approved = gitops.commit_all(repo, "task(a)")
    assert approved != root

    (repo / "a.txt").write_text("BROKEN")          # modified tracked file
    (repo / "new.txt").write_text("junk")          # untracked generated file
    assert not gitops.is_clean(repo)

    gitops.hard_reset(repo, approved)
    assert (repo / "a.txt").read_text() == "approved"
    assert not (repo / "new.txt").exists()
    assert gitops.is_clean(repo) and gitops.head_sha(repo) == approved


def test_commit_paths_only_commits_listed_files(tmp_path):
    repo = tmp_path / "r"
    gitops.init_repo(repo)
    (repo / "a").write_text("1")
    (repo / "b").write_text("2")
    gitops.commit_paths(repo, ["a"], "only a")
    assert not gitops.is_clean(repo)               # b still pending
    assert gitops.changed_files(repo, "HEAD") == []  # untracked b not part of diff


def test_no_op_commit_returns_head(tmp_path):
    repo = tmp_path / "r"
    root = gitops.init_repo(repo)
    assert gitops.commit_all(repo, "nothing") == root


def test_merge_ff_and_tag(tmp_path):
    repo = tmp_path / "r"
    gitops.init_repo(repo)
    gitops.create_branch(repo, "run/1")
    (repo / "x").write_text("1")
    sha = gitops.commit_all(repo, "t")
    assert gitops.merge_ff(repo, "run/1") == sha
    assert gitops.current_branch(repo) == "main"
    gitops.tag(repo, "release/1", "msg")


def test_changed_files_detects_modified_and_deleted(tmp_path):
    repo = tmp_path / "r"
    gitops.init_repo(repo)
    (repo / "m.sql").write_text("1")
    (repo / "d.sql").write_text("1")
    base = gitops.commit_all(repo, "base")
    (repo / "m.sql").write_text("2")
    (repo / "d.sql").unlink()
    assert sorted(gitops.changed_files(repo, base)) == [("D", "d.sql"), ("M", "m.sql")]


def test_git_error_is_raised(tmp_path):
    with pytest.raises(gitops.GitError):
        gitops.head_sha(tmp_path)
