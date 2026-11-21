"""Telemetry: execution tracking, session performance, and Swarm DAG metrics (inspired by Harnext Agent)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

SKIP = {"status", "plan", "agents", "hook", "eval"}


def record(root: Path, task_id: str | None, command: str, exit_code: int, seconds: float) -> None:
    if not task_id or command in SKIP:
        return
    from autoharness import transactions

    path = root / ".autoharness" / "tasks" / task_id / "events.jsonl"
    if not path.parent.exists():
        return
    event = {
        "time": time.time(),
        "command": command,
        "exit_code": exit_code,
        "duration_seconds": round(max(0.0, seconds), 6),
    }
    with transactions.locked(root), path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def swarm_telemetry(root: Path, plan_id: str | None = None) -> dict[str, Any]:
    """Compute Swarm DAG topological execution metrics and receipt stats."""
    swarm_dir = root / ".autoharness" / "swarm"
    if not swarm_dir.is_dir():
        return {"plans_count": 0, "total_tasks": 0, "dag_depth": 0}

    plan_files = list(swarm_dir.glob("plan-*.json"))
    if not plan_files:
        return {"plans_count": 0, "total_tasks": 0, "dag_depth": 0}

    target_file = None
    if plan_id:
        p = swarm_dir / f"plan-{plan_id}.json"
        if p.is_file():
            target_file = p
    if target_file is None:
        target_file = sorted(plan_files, key=lambda f: f.stat().st_mtime, reverse=True)[0]

    try:
        data = json.loads(target_file.read_text("utf-8"))
    except (OSError, ValueError):
        return {"plans_count": len(plan_files), "total_tasks": 0, "dag_depth": 0}

    tasks = data.get("tasks", [])
    completed = sum(1 for t in tasks if t.get("status") == "completed")
    failed = sum(1 for t in tasks if t.get("status") == "failed")
    with_receipt = sum(1 for t in tasks if t.get("receipt") is not None)

    # Compute topological depth of DAG
    task_map = {t["id"]: t for t in tasks if "id" in t}
    depth_cache: dict[str, int] = {}

    def get_depth(tid: str) -> int:
        if tid in depth_cache:
            return depth_cache[tid]
        t = task_map.get(tid)
        if not t or not t.get("dependencies"):
            depth_cache[tid] = 1
            return 1
        d = 1 + max(get_depth(dep) for dep in t["dependencies"] if dep in task_map)
        depth_cache[tid] = d
        return d

    max_depth = max((get_depth(tid) for tid in task_map), default=0)

    return {
        "plans_count": len(plan_files),
        "active_plan_id": data.get("id"),
        "total_tasks": len(tasks),
        "completed_tasks": completed,
        "failed_tasks": failed,
        "receipts_issued": with_receipt,
        "dag_depth": max_depth,
        "concurrency_limit": data.get("max_concurrency", 4),
        "worktree_isolation": bool(data.get("isolate_worktrees")),
        "lease_enforcement": bool(data.get("enforce_intent_leases")),
    }


def summary(root: Path, task_id: str) -> dict[str, Any]:
    """Summary of CLI events, duration, error rates, and Harnext session telemetry."""
    path = root / ".autoharness" / "tasks" / task_id / "events.jsonl"
    if not path.exists():
        base: dict[str, Any] = {"cli_calls": 0, "cli_seconds": 0.0, "cli_errors": 0}
    else:
        events = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
        durations = [event["duration_seconds"] for event in events]
        total_sec = round(sum(durations), 6)
        base = {
            "cli_calls": len(events),
            "cli_seconds": total_sec,
            "cli_errors": sum(event["exit_code"] != 0 for event in events),
            "cli_avg_seconds": round(total_sec / len(events), 4) if events else 0.0,
            "cli_max_seconds": round(max(durations), 4) if durations else 0.0,
        }

    # Add Swarm DAG telemetry
    base["swarm"] = swarm_telemetry(root)
    return base
