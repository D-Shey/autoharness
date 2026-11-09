"""Unit tests for Continuous Autonomy Self-Driving Loop (autoharness drive)."""
import json
import pytest
from pathlib import Path

from autoharness import drive, followups, cli, gateway
from autoharness.env import Project


def test_drive_state_persistence(tmp_path: Path):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()

    # Initial state is idle
    st1 = drive.load_drive_state(proj_root)
    assert st1["status"] == "idle"
    assert st1["completed"] == []

    # Save state
    st1["status"] = "running"
    st1["session_id"] = "test_sess"
    st1["completed"].append({"id": 1, "text": "Fix typo"})
    drive.save_drive_state(proj_root, st1)

    # Reload state
    st2 = drive.load_drive_state(proj_root)
    assert st2["status"] == "running"
    assert st2["session_id"] == "test_sess"
    assert len(st2["completed"]) == 1
    assert st2["completed"][0]["id"] == 1


def test_drive_queue_and_dry_run(tmp_path: Path):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    proj = Project(proj_root)

    # Add followups
    f1 = followups.add(proj, "Update documentation formatting", task_id="t1")
    f2 = followups.add(proj, "Optimize vector normalization", task_id="t2")

    queue = drive.get_drive_queue(proj_root)
    assert len(queue) == 2
    assert queue[0]["id"] == f1["id"]
    assert queue[1]["id"] == f2["id"]

    # Dry-run execution
    res = drive.run_drive_loop(proj_root, max_tasks=1, dry_run=True)
    assert res["status"] == "dry_run"
    assert len(res["planned_tasks"]) == 1
    assert res["planned_tasks"][0]["id"] == f1["id"]

    # Ensure queue items are still open
    assert len(drive.get_drive_queue(proj_root)) == 2


def test_drive_execution_success(tmp_path: Path):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    proj = Project(proj_root)

    f1 = followups.add(proj, "Add missing type hint in module", task_id="t1")
    f2 = followups.add(proj, "Refactor error message formatting", task_id="t2")

    # Custom executor simulating successful resolution
    executed_ids = []

    def mock_executor(item):
        executed_ids.append(item["id"])
        return True, "Mock implementation verified"

    res = drive.run_drive_loop(
        proj_root,
        max_tasks=1,
        dry_run=False,
        task_executor=mock_executor,
        test_command=None,
    )

    assert res["status"] == "completed"
    assert res["processed"] == 1
    assert len(res["completed"]) == 1
    assert res["completed"][0]["id"] == f1["id"]
    assert executed_ids == [f1["id"]]

    # f1 must now be marked done, f2 still open
    remaining = drive.get_drive_queue(proj_root)
    assert len(remaining) == 1
    assert remaining[0]["id"] == f2["id"]


def test_drive_preflight_sentinel_failure(tmp_path: Path, monkeypatch):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    proj = Project(proj_root)

    followups.add(proj, "Task that should not run if baseline is broken", task_id="t1")

    # Mock preflight failing
    monkeypatch.setattr(drive, "run_regression_sentinel", lambda p, cmd: (False, "Baseline test broken"))

    res = drive.run_drive_loop(
        proj_root,
        max_tasks=1,
        dry_run=False,
        test_command="pytest",
    )

    assert res["status"] == "error"
    assert "Pre-flight regression sentinel failed" in res["error"]
    assert res["processed"] == 0
    # Queue item remains untouched
    assert len(drive.get_drive_queue(proj_root)) == 1


def test_drive_regression_failure_and_rollback(tmp_path: Path, monkeypatch):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    proj = Project(proj_root)

    f1 = followups.add(proj, "Introduce risky architectural change", task_id="t1")

    # Mock rollback to check it gets called
    rollback_called = []
    monkeypatch.setattr(drive, "rollback_changes", lambda p: (rollback_called.append(True) or True, "mock rollback ok"))

    def failing_executor(item):
        return False, "Failed to compile generated code"

    res = drive.run_drive_loop(
        proj_root,
        max_tasks=1,
        dry_run=False,
        task_executor=failing_executor,
    )

    assert res["status"] == "completed"
    assert res["processed"] == 0
    assert len(res["failed"]) == 1
    assert res["failed"][0]["id"] == f1["id"]
    assert len(rollback_called) == 1

    # Followup must remain open due to failure
    assert len(drive.get_drive_queue(proj_root)) == 1


def test_default_backlog_executor(tmp_path: Path):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    (proj_root / ".autoharness").mkdir()

    item = {"id": 42, "text": "Implement benchmark baseline"}
    passed, log = drive.default_backlog_executor(item, proj_root)
    assert passed
    assert "Swarm plan" in log


def test_cli_and_gateway_drive_commands(tmp_path: Path, monkeypatch, capsys):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    (proj_root / ".autoharness").mkdir()
    proj = Project(proj_root)

    followups.add(proj, "Backlog item #1", task_id="t1")
    monkeypatch.chdir(proj_root)

    # 1. CLI drive status
    code = cli.main(["--project", str(proj_root), "drive", "status"])
    assert code == 0
    out_status = json.loads(capsys.readouterr().out)
    assert out_status["status"] == "ok"
    assert out_status["open_queue_count"] == 1

    # 2. CLI drive queue
    code = cli.main(["--project", str(proj_root), "drive", "queue"])
    assert code == 0
    out_queue = json.loads(capsys.readouterr().out)
    assert out_queue["total_open"] == 1

    # 3. CLI drive run dry-run
    code = cli.main(["--project", str(proj_root), "drive", "run", "--dry-run", "--max-tasks", "1"])
    assert code == 0
    out_run = json.loads(capsys.readouterr().out)
    assert out_run["status"] == "dry_run"
    assert len(out_run["planned_tasks"]) == 1

    # 4. Gateway harness_drive_status tool
    written = []
    gw = gateway.Gateway(proj_root, write=written.append, log=lambda s: None)
    res_status = gw._call({"name": "harness_drive_status", "arguments": {}})
    assert not res_status.get("isError")
    parsed_gw_status = json.loads(res_status["content"][0]["text"])
    assert parsed_gw_status["open_queue_count"] == 1

    # 5. Gateway harness_drive_run tool (dry-run)
    res_run = gw._call({"name": "harness_drive_run", "arguments": {"dry_run": True, "max_tasks": 1}})
    assert not res_run.get("isError")
    parsed_gw_run = json.loads(res_run["content"][0]["text"])
    assert parsed_gw_run["status"] == "dry_run"


def test_classify_test_failure_harness_vs_assertion():
    # 1. SyntaxError in harness
    out_syntax = "FAILED tests/test_foo.py - SyntaxError: invalid syntax (test_foo.py, line 42)"
    c_syntax = drive.classify_test_failure(out_syntax)
    assert c_syntax["is_harness_fault"] is True
    assert c_syntax["fault_type"] == "harness_syntax"
    assert "SyntaxError" in c_syntax["summary"]

    # 2. Missing fixture
    out_fixture = "ERROR at setup of test_db - fixture 'fake_pool' not found"
    c_fixture = drive.classify_test_failure(out_fixture)
    assert c_fixture["is_harness_fault"] is True
    assert c_fixture["fault_type"] == "harness_fixture"

    # 3. Collection / ModuleNotFoundError
    out_coll = "ERROR collecting tests/test_api.py - ModuleNotFoundError: No module named 'fastapi'"
    c_coll = drive.classify_test_failure(out_coll)
    assert c_coll["is_harness_fault"] is True
    assert c_coll["fault_type"] == "harness_collection"

    # 4. Legitimate assertion failure
    out_assert = "FAILED tests/test_calc.py::test_add - AssertionError: assert 2 + 2 == 5"
    c_assert = drive.classify_test_failure(out_assert)
    assert c_assert["is_harness_fault"] is False
    assert c_assert["fault_type"] == "assertion_failure"


def test_sentinel_result_backward_compatibility():
    res = drive.SentinelResult(False, "SyntaxError: unexpected EOF")
    # Verify tuple unpacking
    passed, output = res
    assert passed is False
    assert output == "SyntaxError: unexpected EOF"
    assert res.passed is False
    assert res.output == "SyntaxError: unexpected EOF"
    assert res.classification["is_harness_fault"] is True
    assert res.classification["fault_type"] == "harness_syntax"


def test_drive_broken_harness_recorded(tmp_path: Path, monkeypatch):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    proj = Project(proj_root)
    f1 = followups.add(proj, "Fix serialization bug", task_id="t1")

    # Mock task succeeds but regression check fails with broken test fixture
    def mock_executor(item):
        return True, "Task code written"

    sentinel_calls = []

    def mock_sentinel(p, cmd):
        sentinel_calls.append(1)
        if len(sentinel_calls) == 1:
            # Pre-flight sentinel passes
            return drive.SentinelResult(True, "1 passed")
        # Post-flight regression check fails with broken fixture
        return drive.SentinelResult(
            False,
            "ERROR at setup of test_core - fixture 'test_db' not found"
        )

    monkeypatch.setattr(drive, "run_regression_sentinel", mock_sentinel)
    monkeypatch.setattr(drive, "rollback_changes", lambda p: (True, "Rollback ok"))

    res = drive.run_drive_loop(
        proj_root,
        max_tasks=1,
        dry_run=False,
        task_executor=mock_executor,
        test_command="pytest",
    )

    assert res["status"] == "completed"
    assert len(res["failed"]) == 1
    fail_rec = res["failed"][0]
    assert fail_rec["is_harness_fault"] is True
    assert fail_rec["fault_type"] == "harness_fixture"
    assert "Broken test harness detected" in fail_rec["reason"]

