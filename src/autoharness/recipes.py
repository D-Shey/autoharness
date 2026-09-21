"""Recipes: harness setups that worked, saved for reuse on similar tasks.

Stored globally in ~/.autoharness/recipes/<name>.json (override with
AUTOHARNESS_HOME) so they carry across projects.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from autoharness.env import Task
from autoharness.text import stems as _tokens


def home() -> Path:
    return Path(os.environ.get("AUTOHARNESS_HOME", Path.home() / ".autoharness"))


def recipes_dir() -> Path:
    d = home() / "recipes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save(task: Task, name: str, notes: str = "") -> Path:
    info = task.info()
    recipe = {
        "name": name,
        "task_example": info.get("task", ""),
        "capabilities": info.get("capabilities", []),
        "mcp": {k: {"config": v["config"], "source": v["source"]} for k, v in task.mcp_lock().items()},
        "skills": [v["spec"] for v in task.skills_lock().values()],
        "notes": notes,
        "uses": 0,
        "saved": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = recipes_dir() / f"{re.sub(r'[^a-zA-Z0-9_-]', '-', name)}.json"
    if path.exists():
        old = json.loads(path.read_text("utf-8"))
        recipe["uses"] = old.get("uses", 0)
    path.write_text(json.dumps(recipe, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return path


def load_all() -> list[dict]:
    return [json.loads(p.read_text("utf-8")) for p in sorted(recipes_dir().glob("*.json"))]


def match(task_text: str, limit: int = 3) -> list[tuple[float, dict]]:
    """Rank saved recipes by the share of task words found in the recipe's example, capabilities and notes.

    Containment rather than Jaccard, so long notes widen what a recipe matches instead of diluting its score."""
    want = _tokens(task_text)
    scored = []
    for r in load_all():
        have = _tokens(r.get("task_example", "") + " " + " ".join(r.get("capabilities", [])) + " " + r.get("notes", ""))
        if want and have:
            score = len(want & have) / len(want)
            if score > 0:
                scored.append((round(score, 3), r))
    scored.sort(key=lambda s: s[0], reverse=True)
    return scored[:limit]


def mark_used(name: str) -> None:
    for p in recipes_dir().glob("*.json"):
        r = json.loads(p.read_text("utf-8"))
        if r.get("name") == name:
            r["uses"] = r.get("uses", 0) + 1
            p.write_text(json.dumps(r, indent=2, ensure_ascii=False) + "\n", "utf-8")
            return
