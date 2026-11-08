"""Continuous Autonomy Self-Driving Loop (autoharness drive).

Pure Python standard library implementation (zero runtime dependencies).
Provides:
  - Outer control loop over open backlog items in .autoharness/followups.json.
  - Priority queue scheduling with configurable iteration budgets (max_tasks).
  - Regression sentinel: pre-flight and post-flight test suite verification.
  - Automatic safety rollback (git restore / clean) on test failures or errors.
  - Crash-proof state persistence in .autoharness/drive_state.json.
  - Integration with AutoHarness CLI and Gateway MCP server.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from autoharness.env import HarnessError

DRIVE_STATE_FILE = "drive_state.json"


def get_drive_state_path(project_root: Path) -> Path:
    return project_root / ".autoharness" / DRIVE_STATE_FILE


def load_drive_state(project_root: Path) -> dict[str, Any]:
    state_path = get_drive_state_path(project_root)
    if not state_path.exists():
        return {
            "status": "idle",
            "session_id": None,
            "started_at": None,
            "updated_at": None,
            "max_tasks": 0,
            "current_task": None,
            "completed": [],
            "failed": [],
        }
    try:
        return json.loads(state_path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "status": "idle",
            "session_id": None,
            "started_at": None,
            "updated_at": None,
            "max_tasks": 0,
            "current_task": None,
            "completed": [],
            "failed": [],
        }


def save_drive_state(project_root: Path, state: dict[str, Any]) -> None:
    state_path = get_drive_state_path(project_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = time.time()
    tmp_path = state_path.with_name(f".tmp_{state_path.name}")
    try:
        tmp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", "utf-8")
        tmp_path.replace(state_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def get_drive_queue(project_root: Path) -> list[dict[str, Any]]:
    """Get list of open followups ordered by ID."""
    from autoharness.env import Project
    from autoharness import followups
    proj = Project(project_root)
    items = followups.listing(proj, everything=False)
    # Sort by ID ascending (chronological FIFO backlog)
    return sorted(items, key=lambda x: x.get("id", 0))


def run_regression_sentinel(
    project_root: Path,
    test_command: str = "pytest",
    timeout: int = 60,
) -> tuple[bool, str]:
    """Execute regression test command and return (passed, output)."""
    try:
        res = subprocess.run(
            test_command,
            shell=True,
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        passed = (res.returncode == 0)
        output = (res.stdout or "") + (res.stderr or "")
        return passed, output.strip()
    except subprocess.TimeoutExpired:
        return False, f"Regression test timed out after {timeout}s"
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"Regression test execution error: {e}"


def rollback_changes(project_root: Path) -> tuple[bool, str]:
    """Revert uncommitted modifications and remove untracked files to restore clean working tree."""
    try:
        res_restore = subprocess.run(
            ["git", "restore", "."],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        res_clean = subprocess.run(
            ["git", "clean", "-fd"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if res_restore.returncode != 0:
            return False, res_restore.stderr.strip()
        if res_clean.returncode != 0:
            return False, res_clean.stderr.strip()
        return True, "Rollback successful: git working tree restored and cleaned."
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"Rollback failed: {e}"


def default_backlog_executor(item: dict[str, Any], project_root: Path) -> tuple[bool, str]:
    """Default executor for backlog items: plans and executes multi-agent swarm workflow."""
    from autoharness import swarm
    fid = item.get("id")
    ftext = item.get("text", "")
    plan_title = f"Autonomy Followup #{fid}: {ftext[:60]}"
    tasks = [
        {"id": "research", "name": f"Analyze #{fid}", "role": "researcher"},
        {"id": "implement", "name": f"Implement #{fid}", "role": "coder", "depends_on": ["research"]},
        {"id": "verify", "name": f"Verify #{fid}", "role": "verifier", "depends_on": ["implement"]},
    ]
    plan = swarm.create_sample_swarm_plan(plan_title, tasks=tasks)
    swarm.save_swarm_plan(plan, project_root)
    executed = swarm.execute_swarm_plan(plan, project_root, max_parallel=2)
    if executed.status == "completed":
        return True, f"Swarm plan {executed.id} completed successfully"
    return False, f"Swarm plan {executed.id} failed with status {executed.status}"


def run_drive_loop(
    project_root: Path,
    max_tasks: int = 3,
    dry_run: bool = False,
    followup_id: int | None = None,
    test_command: str | None = None,
    task_executor: Callable[[dict[str, Any]], tuple[bool, str]] | None = None,
) -> dict[str, Any]:
    """Execute the autonomous self-driving backlog resolution loop."""
    queue = get_drive_queue(project_root)
    if followup_id is not None:
        queue = [item for item in queue if item.get("id") == followup_id]

    if not queue:
        return {
            "status": "idle",
            "message": "No open followups to resolve.",
            "processed": 0,
            "completed": [],
            "failed": [],
        }

    # Limit by safety budget
    targets = queue[:max_tasks]

    if dry_run:
        return {
            "status": "dry_run",
            "message": f"Dry-run plan for {len(targets)} followup(s).",
            "max_tasks": max_tasks,
            "planned_tasks": targets,
        }

    # Pre-flight regression sentinel: verify clean baseline before modifying code
    if test_command:
        pre_ok, pre_log = run_regression_sentinel(project_root, test_command)
        if not pre_ok:
            return {
                "status": "error",
                "error": f"Pre-flight regression sentinel failed before drive start: {pre_log}",
                "processed": 0,
                "completed": [],
                "failed": [],
            }

    session_id = f"drive_{int(time.time())}"
    state = load_drive_state(project_root)
    state["session_id"] = session_id
    state["status"] = "running"
    state["started_at"] = time.time()
    state["max_tasks"] = max_tasks
    save_drive_state(project_root, state)

    from autoharness.env import Project
    from autoharness import followups
    proj = Project(project_root)

    processed_count = 0
    completed_items: list[dict[str, Any]] = []
    failed_items: list[dict[str, Any]] = []

    for item in targets:
        fid = item.get("id")
        ftext = item.get("text", "")
        state["current_task"] = item
        save_drive_state(project_root, state)

        # 1. Execute task
        task_passed = False
        task_log = ""
        if task_executor is not None:
            try:
                task_passed, task_log = task_executor(item)
            except Exception as e:
                task_passed = False
                task_log = f"Executor exception: {e}"
        else:
            try:
                task_passed, task_log = default_backlog_executor(item, project_root)
            except Exception as e:
                task_passed = False
                task_log = f"Default executor exception: {e}"

        # 2. Sentinel post-flight regression verification
        regression_passed = True
        regression_log = ""
        if task_passed and test_command:
            regression_passed, regression_log = run_regression_sentinel(project_root, test_command)

        if task_passed and regression_passed:
            # Task succeeded and passed regression checks
            try:
                followups.close(
                    proj,
                    item_id=fid,
                    note=f"Resolved autonomously by drive session {session_id}",
                    task_id=session_id,
                )
            except (HarnessError, KeyError, ValueError) as e:
                rollback_ok, rollback_msg = rollback_changes(project_root)
                record = {
                    "id": fid,
                    "text": ftext,
                    "status": "failed",
                    "reason": f"Failed to close followup: {e}",
                    "rollback": rollback_ok,
                    "rollback_msg": rollback_msg,
                    "failed_at": time.time(),
                }
                failed_items.append(record)
                state["failed"].append(record)
                state["current_task"] = None
                save_drive_state(project_root, state)
                continue

            record = {
                "id": fid,
                "text": ftext,
                "status": "done",
                "log": task_log,
                "resolved_at": time.time(),
            }
            completed_items.append(record)
            state["completed"].append(record)
            processed_count += 1
        else:
            # Failure detected: trigger rollback
            rollback_ok, rollback_msg = rollback_changes(project_root)
            fail_reason = task_log if not task_passed else f"Regression check failed: {regression_log}"
            record = {
                "id": fid,
                "text": ftext,
                "status": "failed",
                "reason": fail_reason,
                "rollback": rollback_ok,
                "rollback_msg": rollback_msg,
                "failed_at": time.time(),
            }
            failed_items.append(record)
            state["failed"].append(record)

        state["current_task"] = None
        save_drive_state(project_root, state)

    state["status"] = "completed"
    save_drive_state(project_root, state)

    return {
        "status": "completed",
        "session_id": session_id,
        "processed": processed_count,
        "completed": completed_items,
        "failed": failed_items,
        "remaining_queue_length": len(get_drive_queue(project_root)),
    }
