import pytest

from sdlc_orchestrator.workflow.nodes import codegen_agent as cg
from sdlc_orchestrator.integrations.repo_io import UnsafePath, check_path, parse_file_blocks, read_repo_files, write_files


def test_path_safety():
    for ok in ("src/main/java/A.java", "pom.xml", "Dockerfile", "docs/x.md", "docker-compose.yml"):
        assert check_path(ok) == ok
    for bad in ("../etc/passwd", "/abs/path", ".git/config", "mvnw", "src/../../x", "random/file.txt", ""):
        with pytest.raises(UnsafePath):
            check_path(bad)


def test_parse_file_blocks_and_fences():
    text = ("===FILE: src/main/java/A.java===\nclass A {}\n===END===\n\n"
            "===FILE: Dockerfile===\n```dockerfile\nFROM x\n```\n===END===")
    files, problems = parse_file_blocks(text)
    assert files == {"src/main/java/A.java": "class A {}\n", "Dockerfile": "FROM x\n"}
    assert problems == []


def test_truncated_output_is_reported():
    text = "===FILE: src/A.java===\nclass A {}\n===END===\n===FILE: src/B.java===\nclass B { // cut off"
    files, problems = parse_file_blocks(text)
    assert list(files) == ["src/A.java"]
    assert any("truncated" in p for p in problems)


def test_unsafe_paths_are_dropped_not_written(tmp_path):
    files, problems = parse_file_blocks("===FILE: ../../evil.sh===\nrm -rf /\n===END===")
    assert files == {} and problems
    with pytest.raises(UnsafePath):
        write_files(tmp_path, {"../evil": "x"})


def test_write_and_read_roundtrip(tmp_path):
    write_files(tmp_path, {"src/main/java/A.java": "class A {}", "pom.xml": "<p/>"})
    (tmp_path / "target").mkdir()
    (tmp_path / "target/ignored.java").write_text("x")
    assert set(read_repo_files(tmp_path)) == {"src/main/java/A.java", "pom.xml"}


def test_deterministic_files_come_straight_from_design(tmp_path):
    design = {"migrations": [{"version": "2", "name": "add_geo", "sql": "CREATE TABLE g (id int)"}],
              "api_contract": {"openapi": "3.0.3", "paths": {"/x": {"get": {}}}}}
    files = cg._write_deterministic(tmp_path, design)
    assert files["src/main/resources/db/migration/V2__add_geo.sql"].startswith("CREATE TABLE g")
    assert "/x:" in files["docs/openapi.yaml"]


def test_branch_prompts_are_scoped_and_strategy_aware(tmp_path):
    (tmp_path / "pom.xml").write_text("<project>boot-4</project>")
    state = {"mode": "greenfield", "codegen_strategy": "full", "design_doc": {
        "api_contract": {"paths": {}}, "configuration": {}, "adrs": [],
        "component_plan": [{"class": "a.Repo", "branch": "impl_data"}, {"class": "a.Ctl", "branch": "impl_api"}]}}
    sys_d, user_d = cg.build_prompts(state, "impl_data", tmp_path)
    sys_a, user_a = cg.build_prompts(state, "impl_api", tmp_path)
    assert "BRANCH: impl_data" in sys_d and "BRANCH: impl_api" in sys_a
    assert "a.Repo" in user_d and "a.Ctl" not in user_d.split("YOUR COMPONENTS")[1]
    assert "STRATEGY: full" in sys_d and "boot-4" in user_d
    assert "STRATEGY: simple" in cg.build_prompts({**state, "codegen_strategy": "simple"}, "impl_api", tmp_path)[0]
    assert "NON-NEGOTIABLE" in sys_a and "@Valid" in sys_a and "X-API-Key" in sys_a


def test_retry_prompt_includes_failure_and_prior_files(tmp_path):
    state = {"mode": "greenfield", "design_doc": {"component_plan": [], "api_contract": {}}, "failure_feedback": "cannot find symbol Foo",
             "code_artifacts": {"src/main/java/A.java": "class A {}"}, "artifact_owner": {"src/main/java/A.java": "impl_api"}}
    _, user = cg.build_prompts(state, "impl_api", tmp_path)
    assert "cannot find symbol Foo" in user and "CURRENT src/main/java/A.java" in user
    _, user_fresh = cg.build_prompts({**state, "code_artifacts": {}, "artifact_owner": {}}, "impl_api", tmp_path)
    assert "Regenerate ALL files" in user_fresh


def test_delete_directives_are_parsed_and_migrations_protected():
    from sdlc_orchestrator.integrations.repo_io import parse_deletes
    paths, problems = parse_deletes("===DELETE: src/main/java/A.java===\n===DELETE: src/main/resources/db/migration/V1__x.sql===\n===DELETE: ../x===")
    assert paths == ["src/main/java/A.java"] and len(problems) == 2
