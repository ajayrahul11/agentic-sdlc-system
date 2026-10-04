import pytest

from app import dag


def task(i, deps, stage):
    return {"id": i, "description": i, "depends_on": deps, "status": "pending", "owner_stage": stage}


def test_default_plans_are_valid_dags():
    assert dag.validate_dag(dag.default_plan("greenfield"), "greenfield") == []
    assert dag.validate_dag(dag.default_plan("brownfield"), "brownfield") == []


def test_waves_expose_parallel_branches_and_sync_point():
    waves = dag.topo_waves(dag.default_plan("greenfield"))
    assert ["implement-api-layer", "implement-data-layer"] in waves
    flat = [t for w in waves for t in w]
    assert flat.index("run-tests") > max(flat.index("implement-api-layer"), flat.index("implement-data-layer"))


def test_cycle_detected():
    tasks = [task("a", ["b"], "design"), task("b", ["a"], "impl_data")]
    assert "cycle" in " ".join(dag.validate_dag(tasks))


def test_dangling_dependency_detected():
    errs = dag.validate_dag([task("a", ["ghost"], "design")])
    assert any("unknown task 'ghost'" in e for e in errs)


def test_unknown_stage_detected():
    assert any("unknown owner_stage" in e for e in dag.validate_dag([task("a", [], "magic")]))


def test_branches_must_be_parallel():
    plan = dag.default_plan("greenfield")
    for t in plan:
        if t["id"] == "implement-api-layer":
            t["depends_on"].append("implement-data-layer")
    assert any("parallel" in e for e in dag.validate_dag(plan))


def test_testing_must_wait_for_both_branches():
    plan = dag.default_plan("greenfield")
    for t in plan:
        if t["id"] == "run-tests":
            t["depends_on"] = ["implement-data-layer"]
    assert any("sync point" in e for e in dag.validate_dag(plan))


def test_missing_required_stage():
    plan = [t for t in dag.default_plan("greenfield") if t["owner_stage"] != "docs"]
    assert any("docs" in e for e in dag.validate_dag(plan))


def test_downstream():
    plan = dag.default_plan("greenfield")
    assert {"implement-api-layer", "run-tests", "write-docs"} <= dag.downstream(plan, "design-api-contract")
    assert "design-api-contract" not in dag.downstream(plan, "design-api-contract")


def test_topo_waves_raises_on_cycle():
    with pytest.raises(ValueError):
        dag.topo_waves([task("a", ["b"], "design"), task("b", ["a"], "design")])
