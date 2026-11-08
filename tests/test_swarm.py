"""Unit tests for the Swarm DAG multi-agent orchestration engine."""
import json
import pytest
from pathlib import Path

from autoharness import swarm, memory, cli, gateway


def test_swarm_dag_validation_and_cycle_detection():
    # Valid diamond DAG:
    #      t1
    #     /  \
    #    t2  t3
    #     \  /
    #      t4
    plan = swarm.SwarmPlan(
        id="plan_diamond",
        title="Diamond Test",
        tasks={
            "t1": swarm.SwarmTask(id="t1", name="Task 1", depends_on=[]),
            "t2": swarm.SwarmTask(id="t2", name="Task 2", depends_on=["t1"]),
            "t3": swarm.SwarmTask(id="t3", name="Task 3", depends_on=["t1"]),
            "t4": swarm.SwarmTask(id="t4", name="Task 4", depends_on=["t2", "t3"]),
        },
    )

    order = swarm.validate_dag(plan)
    assert order[0] == "t1"
    assert order[-1] == "t4"
    assert set(order[1:3]) == {"t2", "t3"}

    tiers = swarm.compute_execution_tiers(plan)
    assert len(tiers) == 3
    assert tiers[0] == ["t1"]
    assert set(tiers[1]) == {"t2", "t3"}
    assert tiers[2] == ["t4"]

    # Cyclic DAG (t1 -> t2 -> t3 -> t1)
    cyclic_plan = swarm.SwarmPlan(
        id="plan_cyclic",
        title="Cyclic Test",
        tasks={
            "t1": swarm.SwarmTask(id="t1", name="Task 1", depends_on=["t3"]),
            "t2": swarm.SwarmTask(id="t2", name="Task 2", depends_on=["t1"]),
            "t3": swarm.SwarmTask(id="t3", name="Task 3", depends_on=["t2"]),
        },
    )
    with pytest.raises(ValueError, match="Cycle detected"):
        swarm.validate_dag(cyclic_plan)


def test_swarm_execution_with_concurrency_and_gated_failure(tmp_path: Path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    # Plan where t2 fails and blocks t4; t3 succeeds
    plan = swarm.SwarmPlan(
        id="plan_run",
        title="Execution Run Test",
        max_parallel=2,
        tasks={
            "t1": swarm.SwarmTask(id="t1", name="Init", command='python -c "print(\'init ok\')"'),
            "t2": swarm.SwarmTask(id="t2", name="Fail Task", depends_on=["t1"], command='python -c "import sys; sys.exit(1)"'),
            "t3": swarm.SwarmTask(id="t3", name="Success Task", depends_on=["t1"], command='python -c "print(\'t3 ok\')"'),
            "t4": swarm.SwarmTask(id="t4", name="Blocked Task", depends_on=["t2"], command='python -c "print(\'should not run\')"'),
        },
    )

    executed = swarm.execute_swarm_plan(plan, project_root)
    assert executed.status == "failed"
    assert executed.tasks["t1"].status == "completed"
    assert executed.tasks["t2"].status == "failed"
    assert executed.tasks["t3"].status == "completed"
    assert executed.tasks["t4"].status == "blocked"

    # Persistence verification
    saved_plan = swarm.load_swarm_plan("plan_run", project_root)
    assert saved_plan.id == "plan_run"
    assert saved_plan.tasks["t4"].status == "blocked"

    # Verify memory integration: completed tasks are indexed into memory.db
    con = memory.get_memory_db(project_root)
    try:
        results = memory.search_memory(con, "init", kind="swarm_task")
        assert len(results) >= 1
        assert "Init" in results[0]["title"]
    finally:
        con.close()


def test_swarm_sample_plan_generation_and_listing(tmp_path: Path):
    project_root = tmp_path / "project2"
    project_root.mkdir()

    sample = swarm.create_sample_swarm_plan("Multi-Agent Feature")
    assert len(sample.tasks) == 4
    assert "t1_research" in sample.tasks
    assert "t4_verify" in sample.tasks

    saved_path = swarm.save_swarm_plan(sample, project_root)
    assert saved_path.exists()

    plans = swarm.list_swarm_plans(project_root)
    assert len(plans) == 1
    assert plans[0]["id"] == sample.id
    assert plans[0]["total_tasks"] == 4


def test_swarm_cli_lifecycle(tmp_path: Path, monkeypatch, capsys):
    project_dir = tmp_path / "cli_project"
    project_dir.mkdir()
    (project_dir / ".autoharness").mkdir()

    monkeypatch.chdir(project_dir)

    # 1. autoharness swarm init --title "Test Epic"
    code = cli.main(["--project", str(project_dir), "swarm", "init", "--title", "Test Epic"])
    assert code == 0
    out1 = capsys.readouterr().out
    data1 = json.loads(out1)
    assert data1["status"] == "ok"
    plan_id = data1["plan_id"]
    assert data1["tasks_count"] == 4

    # 2. autoharness swarm list
    code = cli.main(["--project", str(project_dir), "swarm", "list"])
    assert code == 0
    out2 = capsys.readouterr().out
    data2 = json.loads(out2)
    assert data2["total_plans"] == 1

    # 3. autoharness swarm status
    code = cli.main(["--project", str(project_dir), "swarm", "status", plan_id])
    assert code == 0
    out3 = capsys.readouterr().out
    data3 = json.loads(out3)
    assert data3["plan"]["title"] == "Test Epic"

    # 4. autoharness swarm run
    code = cli.main(["--project", str(project_dir), "swarm", "run", plan_id])
    assert code == 0
    out4 = capsys.readouterr().out
    data4 = json.loads(out4)
    assert data4["plan_status"] == "completed"


def test_gateway_swarm_tools(tmp_path: Path):
    project_dir = tmp_path / "gw_project"
    project_dir.mkdir()
    (project_dir / ".autoharness").mkdir()

    # Pre-create a successful swarm plan
    sample = swarm.create_sample_swarm_plan("Gateway Swarm Test")
    swarm.save_swarm_plan(sample, project_dir)

    written = []
    gw = gateway.Gateway(project_dir, write=written.append, log=lambda s: None)

    # Test tools/list includes swarm tools
    tool_names = [t["name"] for t in gw._tool_list()]
    assert "harness_swarm_status" in tool_names
    assert "harness_swarm_run" in tool_names

    # Call harness_swarm_status
    res_status = gw._call({"name": "harness_swarm_status", "arguments": {"plan_id": sample.id}})
    assert not res_status.get("isError")
    plan_dict = json.loads(res_status["content"][0]["text"])
    assert plan_dict["id"] == sample.id

    # Call harness_swarm_run
    res_run = gw._call({"name": "harness_swarm_run", "arguments": {"plan_id": sample.id, "parallel": 2}})
    assert not res_run.get("isError")
    run_dict = json.loads(res_run["content"][0]["text"])
    assert run_dict["status"] == "completed"
