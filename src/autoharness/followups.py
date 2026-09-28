"""Follow-ups: work found during a task that lies outside it, kept for the tasks that come after.

They live in .autoharness/followups.json, which git keeps like kept.json: a finding is about the
project, not about one machine.
"""
from __future__ import annotations

import time

from autoharness.env import HarnessError, Project, _read_json, _write_json, ensure_state_dir

SHOWN = 10  # open follow-ups init prints; `followup list` shows them all


def _path(project: Project):
    return project.state / "followups.json"


def _load(project: Project) -> list[dict]:
    return _read_json(_path(project), {"items": []})["items"]


def _save(project: Project, items: list[dict]) -> None:
    ensure_state_dir(project.state)
    _write_json(_path(project), {"items": items})


def add(project: Project, text: str, task_id: str | None) -> dict:
    text = " ".join(text.split())
    if not text:
        raise HarnessError("a follow-up needs text: what is left to do, and where")
    items = _load(project)
    item = {"id": max((i["id"] for i in items), default=0) + 1, "text": text, "status": "open",
            "from_task": task_id, "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _save(project, items + [item])
    return item


def close(project: Project, item_id: int, note: str, task_id: str | None) -> dict:
    items = _load(project)
    item = next((i for i in items if i["id"] == item_id), None)
    if item is None:
        raise HarnessError(f"no follow-up #{item_id}; see `autoharness followup list`")
    if item["status"] == "done":
        raise HarnessError(f"follow-up #{item_id} is already done")
    item |= {"status": "done", "done_task": task_id, "done_note": note,
             "closed": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _save(project, items)
    return item


def listing(project: Project, everything: bool = False) -> list[dict]:
    return [i for i in _load(project) if everything or i["status"] == "open"]


def from_task(project: Project, task_id: str) -> list[dict]:
    return [i for i in _load(project) if i.get("from_task") == task_id]
