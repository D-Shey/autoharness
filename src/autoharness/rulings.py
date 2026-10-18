"""Review points settled for good, so later tasks in the project do not argue them again.

A critic rules on a disputed point from measurements in the files (`review --settled`), or the user
rejects a critic's point (`ruling add`). Fresh critics start from zero in every task and raise the same
points again; these rulings go into the critic prompt of every later task in the project. They live in
.autoharness/rulings.json, which git keeps like followups.json: a ruling is about the project, not about
one machine.
"""
from __future__ import annotations

import time

from autoharness.env import HarnessError, Project, _read_json, _write_json, ensure_state_dir

SHOWN = 12  # rulings a critic prompt carries, newest first; `ruling list` shows them all
BY = ("user", "critic")


def _path(project: Project):
    return project.state / "rulings.json"


def load(project: Project) -> list[dict]:
    return _read_json(_path(project), {"items": []})["items"]


def _save(project: Project, items: list[dict]) -> None:
    ensure_state_dir(project.state)
    _write_json(_path(project), {"items": items})


def add(project: Project, point: str, by: str, task_id: str | None, reason: str = "") -> dict:
    """Record a ruling; the same point ruled again replaces the older ruling."""
    from autoharness.evidence import same_problem  # lazy: evidence imports runs, which imports env
    point, reason = " ".join(point.split()), " ".join(reason.split())
    if not point:
        raise HarnessError("a ruling needs the point it settles")
    if by not in BY:
        raise HarnessError(f"a ruling is made by one of {', '.join(BY)}")
    items = load(project)
    item = {"id": max((i["id"] for i in items), default=0) + 1, "point": point, "by": by, "reason": reason,
            "task": task_id, "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    replaced = [i for i in items if same_problem(i["point"], point)]
    if replaced:
        item["replaces"] = [i["id"] for i in replaced]
    _save(project, [i for i in items if i not in replaced] + [item])
    return item


def remove(project: Project, item_id: int) -> dict:
    items = load(project)
    found = next((i for i in items if i["id"] == item_id), None)
    if not found:
        raise HarnessError(f"no ruling #{item_id}: `autoharness ruling list` shows them")
    _save(project, [i for i in items if i["id"] != item_id])
    return found


def for_critic(project: Project, task_id: str | None) -> list[dict]:
    """What a critic of this task is told: the user's rulings, and critics' rulings from other tasks (this task's
    own critic rulings are already in its prompt)."""
    items = [i for i in load(project) if i["by"] == "user" or i.get("task") != task_id]
    return items[::-1][:SHOWN]


def describe(item: dict) -> str:
    who = "the user rejected this point" if item["by"] == "user" else "a critic settled it from the files"
    return f"{item['point']} ({who}{': ' + item['reason'] if item['reason'] else ''})"
