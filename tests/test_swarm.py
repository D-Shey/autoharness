"""Unit tests for the Swarm DAG multi-agent orchestration engine."""
import json
import sys
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

    # 5. autoharness swarm leases
    code = cli.main(["--project", str(project_dir), "swarm", "leases", plan_id])
    assert code == 0
    out5 = capsys.readouterr().out
    data5 = json.loads(out5)
    assert data5["status"] == "ok"

    # 6. autoharness swarm receipts
    code = cli.main(["--project", str(project_dir), "swarm", "receipts", plan_id])
    assert code == 0
    out6 = capsys.readouterr().out
    data6 = json.loads(out6)
    assert data6["status"] == "ok"


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

    # Test tools/list includes new leases and receipts tools
    tool_names_after = [t["name"] for t in gw._tool_list()]
    assert "harness_swarm_leases" in tool_names_after
    assert "harness_swarm_receipts" in tool_names_after

    # Call harness_swarm_leases
    res_leases = gw._call({"name": "harness_swarm_leases", "arguments": {"plan_id": sample.id}})
    assert not res_leases.get("isError")

    # Call harness_swarm_receipts
    res_receipts = gw._call({"name": "harness_swarm_receipts", "arguments": {"plan_id": sample.id}})
    assert not res_receipts.get("isError")
    receipts_dict = json.loads(res_receipts["content"][0]["text"])
    assert receipts_dict["plan_id"] == sample.id


def test_intent_lease_manager_and_conflict_resolution(tmp_path: Path):
    project_dir = tmp_path / "lease_proj"
    project_dir.mkdir()
    (project_dir / ".autoharness").mkdir()

    mgr = swarm.IntentLeaseManager(project_dir)

    # 1. Acquire lease for task_1
    l1 = mgr.acquire_lease("plan_1", "t1", ["src/core/engine.py"], exclusive=True, ttl=10.0)
    assert l1.status == "active"
    assert l1.task_id == "t1"
    assert len(mgr.list_active_leases()) == 1

    # 2. Collision detection: task_2 tries to acquire exclusive lease on the same path
    with pytest.raises(swarm.IntentLeaseConflictError, match="collision on path 'src/core/engine.py'"):
        mgr.acquire_lease("plan_1", "t2", ["src/core/engine.py"], exclusive=True, ttl=10.0)

    # 3. Non-colliding path succeeds
    l2 = mgr.acquire_lease("plan_1", "t3", ["src/ui/presentation.py"], exclusive=True, ttl=10.0)
    assert l2.status == "active"
    assert len(mgr.list_active_leases()) == 2

    # 4. Release l1
    assert mgr.release_lease(l1.lease_id) is True
    assert len(mgr.list_active_leases()) == 1

    # 5. Now task_2 can acquire lease on src/core/engine.py
    l3 = mgr.acquire_lease("plan_1", "t2", ["src/core/engine.py"], exclusive=True, ttl=10.0)
    assert l3.status == "active"

    # 6. Expiration test
    expired_lease = mgr.acquire_lease("plan_1", "t_exp", ["src/temp.py"], exclusive=True, ttl=0.01)
    import time
    time.sleep(0.03)
    assert expired_lease.is_expired() is True
    # list_active_leases excludes expired
    active_now = [l for l in mgr.list_active_leases() if l["task_id"] == "t_exp"]
    assert len(active_now) == 0


def test_receipt_gated_completion_and_tamper_detection(tmp_path: Path):
    project_dir = tmp_path / "rcpt_proj"
    project_dir.mkdir()
    (project_dir / ".autoharness").mkdir()

    art_file = project_dir / "generated_artifact.json"
    art_file.write_text(json.dumps({"key": "value"}), encoding="utf-8")

    task = swarm.SwarmTask(
        id="t_code",
        name="Generate JSON",
        role="coder",
        expected_artifacts=["generated_artifact.json"],
    )

    barrier_report = {"passed_count": 2, "total_checks": 2}

    # 1. Issue completion receipt
    receipt = swarm.issue_completion_receipt(task, "plan_test", project_dir, barrier_report)
    assert receipt.status == "valid"
    assert "generated_artifact.json" in receipt.artifacts_hashes
    original_hash = receipt.artifacts_hashes["generated_artifact.json"]
    assert len(original_hash) == 64

    # 2. Verify valid receipt
    v1 = swarm.verify_completion_receipt(receipt, project_dir, task)
    assert v1["valid"] is True
    assert v1["status"] == "valid"
    assert len(v1["violations"]) == 0

    # 3. Tamper with artifact on disk
    art_file.write_text(json.dumps({"key": "HACKED"}), encoding="utf-8")
    v2 = swarm.verify_completion_receipt(receipt, project_dir, task)
    assert v2["valid"] is False
    assert v2["status"] == "tampered"
    assert any("tampered after receipt issue" in viol for viol in v2["violations"])

    # 4. Tamper with signature
    receipt.signature = "invalid_fake_signature"
    v3 = swarm.verify_completion_receipt(receipt, project_dir, task)
    assert v3["valid"] is False
    assert any("signature mismatch" in viol for viol in v3["violations"])


def test_swarm_worktree_manager_fallback_and_merging(tmp_path: Path):
    project_dir = tmp_path / "wt_proj"
    project_dir.mkdir()
    (project_dir / ".autoharness").mkdir()

    mgr = swarm.SwarmWorktreeManager(project_dir)
    assert mgr.worktrees_dir.is_dir()

    # Create worktree (fallback mode in non-git directory)
    wt = mgr.create_worktree("t1", "plan_wt")
    assert wt.is_dir()

    # Generate an artifact inside worktree
    (wt / "built_module.py").write_text("print('hello worktree')", encoding="utf-8")

    # Merge artifact back to project root
    merged = mgr.merge_worktree_artifacts(wt, allowed_paths=["built_module.py"])
    assert "built_module.py" in merged
    assert (project_dir / "built_module.py").is_file()
    assert (project_dir / "built_module.py").read_text("utf-8") == "print('hello worktree')"

    # Cleanup worktree
    mgr.cleanup_worktree(wt)
    assert not wt.exists()


def test_swarm_plan_execution_with_worktree_leases_and_receipts(tmp_path: Path):
    project_dir = tmp_path / "e2e_swarm_proj"
    project_dir.mkdir()
    (project_dir / ".autoharness").mkdir()

    plan = swarm.SwarmPlan(
        id="plan_full_features",
        title="E2E Swarm Worktree and Receipt Test",
        isolate_worktrees=True,
        enforce_intent_leases=True,
        receipt_gated=True,
        tasks={
            "t1": swarm.SwarmTask(
                id="t1",
                name="Author Module 1",
                role="coder",
                command=f'"{sys.executable}" -c "from pathlib import Path; Path(\'mod1.txt\').write_text(\'mod1 content\', encoding=\'utf-8\')"',
                allowed_paths=["mod1.txt"],
                expected_artifacts=["mod1.txt"],
                isolate_worktree=True,
            ),
            "t2": swarm.SwarmTask(
                id="t2",
                name="Author Module 2",
                role="coder",
                command=f'"{sys.executable}" -c "from pathlib import Path; Path(\'mod2.txt\').write_text(\'mod2 content\', encoding=\'utf-8\')"',
                allowed_paths=["mod2.txt"],
                expected_artifacts=["mod2.txt"],
                isolate_worktree=True,
            ),
            "t3": swarm.SwarmTask(
                id="t3",
                name="Verify Both Modules",
                role="verifier",
                depends_on=["t1", "t2"],
                command=f'"{sys.executable}" -c "from pathlib import Path; assert Path(\'mod1.txt\').read_text() == \'mod1 content\'; assert Path(\'mod2.txt\').read_text() == \'mod2 content\'"',
                expected_artifacts=[],
            ),
        },
    )

    executed = swarm.execute_swarm_plan(plan, project_dir)
    assert executed.status == "completed", {tid: (t.status, t.result) for tid, t in executed.tasks.items()}
    assert executed.tasks["t1"].status == "completed"
    assert executed.tasks["t2"].status == "completed"
    assert executed.tasks["t3"].status == "completed"

    # Verify artifacts were merged back to project_dir
    assert (project_dir / "mod1.txt").is_file()
    assert (project_dir / "mod2.txt").is_file()

    # Verify receipts were issued and attached
    assert executed.tasks["t1"].receipt is not None
    assert executed.tasks["t1"].receipt["status"] == "valid"
    assert "mod1.txt" in executed.tasks["t1"].receipt["artifacts_hashes"]

    # Verify contracts verification passes
    report = swarm.verify_swarm_plan_contracts(executed, project_dir)
    assert report["status"] == "passed"
    assert report["total_violations"] == 0
