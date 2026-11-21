"""Tests for Telemetry and Swarm DAG metrics (Harnext Agent model)."""
from __future__ import annotations

import json
from pathlib import Path
from autoharness import telemetry


def test_telemetry_record_and_summary(tmp_path: Path):
    task_id = "20261121-000000-abcdef"
    task_dir = tmp_path / ".autoharness" / "tasks" / task_id
    task_dir.mkdir(parents=True)

    telemetry.record(tmp_path, task_id, "verify", 0, 1.25)
    telemetry.record(tmp_path, task_id, "check", 1, 0.75)

    summ = telemetry.summary(tmp_path, task_id)
    assert summ["cli_calls"] == 2
    assert summ["cli_seconds"] == 2.0
    assert summ["cli_errors"] == 1
    assert summ["cli_avg_seconds"] == 1.0
    assert summ["cli_max_seconds"] == 1.25
    assert "swarm" in summ


def test_swarm_telemetry_dag_depth(tmp_path: Path):
    swarm_dir = tmp_path / ".autoharness" / "swarm"
    swarm_dir.mkdir(parents=True)

    # Create a 3-tier DAG: t1 -> t2 -> t3
    plan_data = {
        "id": "test-dag-plan",
        "isolate_worktrees": True,
        "enforce_intent_leases": True,
        "max_concurrency": 2,
        "tasks": [
            {"id": "t1", "dependencies": [], "status": "completed", "receipt": {"signature": "abc"}},
            {"id": "t2", "dependencies": ["t1"], "status": "completed", "receipt": {"signature": "def"}},
            {"id": "t3", "dependencies": ["t2"], "status": "pending"},
        ]
    }
    (swarm_dir / "plan-test-dag-plan.json").write_text(json.dumps(plan_data), "utf-8")

    st = telemetry.swarm_telemetry(tmp_path, "test-dag-plan")
    assert st["total_tasks"] == 3
    assert st["completed_tasks"] == 2
    assert st["failed_tasks"] == 0
    assert st["receipts_issued"] == 2
    assert st["dag_depth"] == 3
    assert st["worktree_isolation"] is True
    assert st["lease_enforcement"] is True
