"""Tests for Multi-Agent Contract-First Artifact Barriers & Role Isolation in AutoHarness Swarms."""

import json
from pathlib import Path
import pytest
from autoharness import swarm, gateway
from autoharness.env import Project


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOHARNESS_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    root.mkdir()
    return Project(root, "claude")


def test_validate_task_barriers_success(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    src_dir = root / "src"
    src_dir.mkdir()
    target_file = src_dir / "generated.py"
    target_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    config_file = root / "config.json"
    config_file.write_text('{"name": "demo", "version": 1}', encoding="utf-8")

    task = swarm.SwarmTask(
        id="t1",
        name="Generate Code",
        role="coder",
        expected_artifacts=["src/generated.py", "config.json"],
        barrier_assertions=[
            {"type": "file_exists", "target": "src/generated.py"},
            {"type": "file_non_empty", "target": "src/generated.py"},
            {"type": "file_contains", "target": "src/generated.py", "pattern": "def hello()"},
            {"type": "min_lines", "target": "src/generated.py", "count": 2},
            {"type": "json_valid", "target": "config.json"},
            {"type": "exit_code", "code": 0},
        ],
    )

    res = swarm.validate_task_barriers(task, root, execution_result={"returncode": 0, "success": True})
    assert res["status"] == "passed"
    assert len(res["violations"]) == 0
    assert res["passed_count"] == 8
    assert res["total_checks"] == 8


def test_validate_task_barriers_failures(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "empty.txt").write_text("", encoding="utf-8")

    task = swarm.SwarmTask(
        id="t_fail",
        name="Broken Task",
        role="coder",
        expected_artifacts=["non_existent.py", "empty.txt"],
        barrier_assertions=[
            {"type": "file_contains", "target": "empty.txt", "pattern": "expected_pattern"},
            {"type": "min_lines", "target": "empty.txt", "count": 5},
            {"type": "exit_code", "code": 0},
        ],
    )

    res = swarm.validate_task_barriers(task, root, execution_result={"returncode": 1, "success": False})
    assert res["status"] == "failed"
    kinds = {v["kind"] for v in res["violations"]}
    assert "missing_artifact" in kinds
    assert "empty_artifact" in kinds
    assert "assertion_failure" in kinds
    assert len(res["violations"]) >= 4


def test_role_isolation_boundaries(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    src_dir = root / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("print('hello')", encoding="utf-8")

    # Researcher role attempting to mutate implementation source code
    research_task = swarm.SwarmTask(
        id="t_res",
        name="Research Task",
        role="researcher",
        expected_artifacts=["src/app.py"],
    )
    res_r = swarm.validate_task_barriers(research_task, root)
    assert res_r["status"] == "failed"
    assert any(v["kind"] == "role_boundary_breach" for v in res_r["violations"])

    # Task violating allowed_paths restriction
    scoped_task = swarm.SwarmTask(
        id="t_scoped",
        name="Docs Task",
        role="coder",
        allowed_paths=["docs/"],
        expected_artifacts=["src/app.py"],
    )
    res_s = swarm.validate_task_barriers(scoped_task, root)
    assert res_s["status"] == "failed"
    assert any(v["kind"] == "role_violation" for v in res_s["violations"])


def test_execute_swarm_plan_barrier_blocks_downstream(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()

    plan = swarm.SwarmPlan(
        id="plan_barrier_test",
        title="Barrier Test Plan",
        tasks={
            "t1": swarm.SwarmTask(
                id="t1",
                name="Failing Barrier Node",
                role="coder",
                command="python -c \"print('ran')\"",
                expected_artifacts=["missing_output.py"],  # will fail barrier
            ),
            "t2": swarm.SwarmTask(
                id="t2",
                name="Downstream Dependent Node",
                role="verifier",
                depends_on=["t1"],
                command="python -c \"print('should not run')\"",
            ),
        },
    )

    executed = swarm.execute_swarm_plan(plan, root)
    assert executed.status == "failed"
    assert executed.tasks["t1"].status == "failed"
    assert "Barrier contract validation failed" in executed.tasks["t1"].result.get("error", "")
    assert executed.tasks["t2"].status == "blocked"


def test_verify_swarm_plan_contracts_report(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "doc.md").write_text("# Specification\nDetailed requirements\n", encoding="utf-8")

    plan = swarm.SwarmPlan(
        id="plan_verify_report",
        title="Contract Verification Plan",
        tasks={
            "t1": swarm.SwarmTask(
                id="t1",
                name="Specification",
                role="researcher",
                expected_artifacts=["doc.md"],
                barrier_assertions=[{"type": "file_exists", "target": "doc.md"}],
            ),
            "t2": swarm.SwarmTask(
                id="t2",
                name="Invalid Dependency Node",
                role="coder",
                depends_on=["t1"],
                expected_artifacts=["missing.py"],
            ),
        },
    )

    report = swarm.verify_swarm_plan_contracts(plan, root)
    assert report["status"] == "failed"
    assert report["total_violations"] >= 1
    assert report["dag_valid"] is True

    md_report = swarm.format_swarm_verify_report(report)
    assert "# Swarm Plan Contract Verification: `plan_verify_report`" in md_report
    assert "**Overall Status:** `FAILED`" in md_report
    assert "missing_artifact" in md_report


def test_cli_swarm_verify(project, capsys):
    from autoharness.cli import cmd_swarm
    import types

    # Initialize sample swarm plan
    swarm_dir = project.root / ".autoharness" / "swarm"
    swarm_dir.mkdir(parents=True)
    plan = swarm.create_sample_swarm_plan("CLI Test Swarm")
    swarm.save_swarm_plan(plan, project.root)

    args = types.SimpleNamespace(
        action="verify",
        plan_id=plan.id,
        json=True,
    )
    code = cmd_swarm(args, project, None)
    # Default sample plan has no expected_artifacts, so verify should pass
    assert code == 0
    captured = capsys.readouterr()
    res = json.loads(captured.out)
    assert res["status"] == "passed"
    assert res["dag_valid"] is True


def test_gateway_swarm_verify_mcp_tool(project):
    sent = []
    gw = gateway.Gateway(project.root, sent.append, lambda line: None)
    gw.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    gw.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # Setup plan
    plan = swarm.create_sample_swarm_plan("Gateway Verification Test")
    swarm.save_swarm_plan(plan, project.root)

    res = gw.handle({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "harness_swarm_verify",
            "arguments": {
                "plan_id": plan.id,
                "raw_json": True,
            }
        }
    })

    assert not res["result"].get("isError")
    data = json.loads(res["result"]["content"][0]["text"])
    assert data["status"] == "passed"
    assert data["dag_valid"] is True
    assert data["plan_id"] == plan.id
