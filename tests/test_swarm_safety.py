"""Tests for Swarm ID uniqueness, atomic persistence locking, and Windows concurrency safety."""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import patch

import pytest
from autoharness import swarm


def test_generated_plan_ids_are_distinct_in_same_millisecond():
    # Fix time to the same millisecond
    fixed_time = 1700000000.123456
    with patch("time.time", return_value=fixed_time):
        plan_ids = []
        for i in range(50):
            plan = swarm.create_sample_swarm_plan(title=f"Sample Plan {i}")
            assert plan.id.startswith("swarm_")
            plan_ids.append(plan.id)

    assert len(set(plan_ids)) == 50, f"Expected 50 distinct plan IDs, got {len(set(plan_ids))}"


def test_generated_plans_persist_and_execute_the_requested_workflow(tmp_path: Path):
    fixed_time = 1700000000.123456
    with patch("time.time", return_value=fixed_time):
        plan_first = swarm.SwarmPlan(
            id=f"swarm_first_{fixed_time}",
            title="First Workflow",
            tasks={
                "t1": swarm.SwarmTask(
                    id="t1",
                    name="First Task",
                    command="python -c \"print('WORKFLOW_FIRST_EXECUTED')\"",
                )
            },
        )
        plan_second = swarm.SwarmPlan(
            id=f"swarm_second_{fixed_time}",
            title="Second Workflow",
            tasks={
                "t2": swarm.SwarmTask(
                    id="t2",
                    name="Second Task",
                    command="python -c \"print('WORKFLOW_SECOND_EXECUTED')\"",
                )
            },
        )

        path_first = swarm.save_swarm_plan(plan_first, tmp_path)
        path_second = swarm.save_swarm_plan(plan_second, tmp_path)

    assert path_first != path_second
    assert path_first.is_file()
    assert path_second.is_file()

    # Execute first plan only
    executed_first = swarm.execute_swarm_plan(plan_first, tmp_path)
    assert executed_first.status == "completed"
    assert "WORKFLOW_FIRST_EXECUTED" in executed_first.tasks["t1"].result.get("stdout", "")

    # Reload both from disk
    loaded_first = swarm.load_swarm_plan(plan_first.id, tmp_path)
    loaded_second = swarm.load_swarm_plan(plan_second.id, tmp_path)

    assert loaded_first.status == "completed"
    assert loaded_second.status == "pending"
    assert loaded_second.tasks["t2"].status == "pending"


def test_concurrent_atomic_saves_have_unique_temps(tmp_path: Path):
    plan = swarm.SwarmPlan(
        id="swarm_concurrent_test",
        title="Concurrent Save Plan",
        tasks={
            "t1": swarm.SwarmTask(id="t1", name="Task 1", command="python -c \"print(1)\""),
        },
    )
    swarm_dir = swarm.get_swarm_dir(tmp_path)

    # 10 workers concurrently saving the same plan 10 times each
    def worker(worker_id: int):
        for step in range(10):
            plan.description = f"Updated by worker {worker_id} step {step}"
            swarm.save_swarm_plan(plan, tmp_path)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(worker, i) for i in range(10)]
        for f in as_completed(futures):
            f.result()  # Will raise PermissionError or OSError if race condition exists

    # Verify no dangling temp files remain in swarm directory
    temp_files = list(swarm_dir.glob(".tmp_*"))
    assert len(temp_files) == 0, f"Found dangling temp files: {temp_files}"

    # Verify final plan file is valid and readable
    loaded = swarm.load_swarm_plan(plan.id, tmp_path)
    assert loaded.id == "swarm_concurrent_test"
    assert "Updated by worker" in loaded.description


def test_persistence_calls_wait_for_active_plan_write(tmp_path: Path):
    plan = swarm.SwarmPlan(
        id="swarm_wait_test",
        title="Wait Test Plan",
    )
    swarm.save_swarm_plan(plan, tmp_path)

    lock_hold_duration = 0.20
    started_holding = threading.Event()

    def hold_lock():
        with swarm.swarm_persistence_lock(tmp_path):
            started_holding.set()
            time.sleep(lock_hold_duration)

    t = threading.Thread(target=hold_lock)
    t.start()
    started_holding.wait(timeout=2.0)

    start_time = time.monotonic()
    # Main thread tries to save plan while lock is held
    plan.description = "Saved after lock released"
    swarm.save_swarm_plan(plan, tmp_path)
    elapsed = time.monotonic() - start_time
    t.join()

    # The save call must have waited for the background thread to release lock
    assert elapsed >= lock_hold_duration * 0.7, f"Save returned in {elapsed}s, expected to wait >= {lock_hold_duration * 0.7}s"
    loaded = swarm.load_swarm_plan(plan.id, tmp_path)
    assert loaded.description == "Saved after lock released"
