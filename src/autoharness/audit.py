"""Structured task decisions without command arguments or tool configurations."""
from __future__ import annotations

import json
import time

from autoharness.env import HarnessError, Task


def append(task: Task, action: str, **details) -> dict:
    event = {"time": time.time(), "action": action, **details}
    with (task.dir / "decisions.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def read(task: Task) -> list[dict]:
    path = task.dir / "decisions.jsonl"
    if not path.exists():
        return []
    events = []
    for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise HarnessError(f"invalid audit event at {path}:{number}") from error
        if not isinstance(event, dict) or not isinstance(event.get("action"), str):
            raise HarnessError(f"invalid audit event at {path}:{number}")
        events.append(event)
    return events
