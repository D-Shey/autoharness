"""Small, argument-free record of CLI work for task evaluation."""
from __future__ import annotations

import json
import time
from pathlib import Path


SKIP = {"status", "plan", "agents", "hook", "eval"}


def record(root: Path, task_id: str | None, command: str, exit_code: int, seconds: float) -> None:
    if not task_id or command in SKIP:
        return
    from autoharness import transactions

    path = root / ".autoharness" / "tasks" / task_id / "events.jsonl"
    if not path.parent.exists():
        return
    event = {"time": time.time(), "command": command, "exit_code": exit_code,
             "duration_seconds": round(max(0.0, seconds), 6)}
    with transactions.locked(root), path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def summary(root: Path, task_id: str) -> dict:
    path = root / ".autoharness" / "tasks" / task_id / "events.jsonl"
    if not path.exists():
        return {"cli_calls": 0, "cli_seconds": 0.0, "cli_errors": 0}
    events = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
    return {"cli_calls": len(events),
            "cli_seconds": round(sum(event["duration_seconds"] for event in events), 6),
            "cli_errors": sum(event["exit_code"] != 0 for event in events)}
