"""Follow-ups: work found during a task that lies outside it, kept for the tasks that come after.

They live in .autoharness/followups.json, which git keeps like kept.json: a finding is about the
project, not about one machine.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from autoharness.env import HarnessError, Project, _read_json, _write_json, ensure_state_dir

SHOWN = 10  # open follow-ups init prints; `followup list` shows them all
DEFAULT_IGNORES = {
    ".git", ".autoharness", ".venv", "node_modules", "build", "dist",
    "__pycache__", ".pytest_cache", ".cursor", ".agents"
}


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


def harvest(project: Project, task_id: str | None = None, marker: str = "shortcut") -> dict:
    """Scan the repository for deliberate shortcut comments and register them as followups."""
    rx = re.compile(
        rf"""(?:#|//|/\*|--)\s*(?:{re.escape(marker)}|shortcut|ponytail):\s*([^\r\n]+)""",
        re.IGNORECASE
    )
    items = _load(project)
    existing_prefixes = {
        m.group(1) for i in items if (m := re.match(r"^([^:]+:\d+):", i.get("text", "")))
    }
    found_hits: list[dict] = []
    added_items: list[dict] = []
    no_trigger_count = 0

    root = project.root
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_IGNORES and not d.startswith(".")]
        for fn in filenames:
            ext = Path(fn).suffix.lower()
            if ext in {".py", ".js", ".ts", ".jsx", ".tsx", ".sh", ".ps1", ".go", ".rs", ".java", ".c", ".cpp", ".h"}:
                file_path = Path(dirpath) / fn
                try:
                    if file_path.stat().st_size > 2 * 1024 * 1024:
                        continue
                    rel_path = file_path.relative_to(root).as_posix()
                    lines = file_path.read_text("utf-8", errors="ignore").splitlines()
                except (OSError, UnicodeDecodeError, ValueError):
                    continue
                for line_no, line in enumerate(lines, 1):
                    m = rx.search(line)
                    if m:
                        raw_payload = m.group(1).rstrip("*/").strip()
                        if not raw_payload:
                            continue
                        parts = [p.strip() for p in raw_payload.split(",", 1)]
                        ceiling = parts[0]
                        if not ceiling:
                            continue
                        trigger = parts[1] if len(parts) > 1 and parts[1] else None
                        if not trigger:
                            no_trigger_count += 1
                            tag = " [no-trigger]"
                        else:
                            tag = ""
                        loc_prefix = f"{rel_path}:{line_no}"
                        desc = f"{loc_prefix}: shortcut ceiling '{ceiling}'" + (f", upgrade trigger '{trigger}'" if trigger else "") + tag
                        found_hits.append({"file": rel_path, "line": line_no, "ceiling": ceiling, "trigger": trigger, "text": desc})
                        if loc_prefix not in existing_prefixes:
                            item = {
                                "id": max((i["id"] for i in items), default=0) + 1,
                                "text": desc,
                                "status": "open",
                                "from_task": task_id,
                                "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                            }
                            items.append(item)
                            existing_prefixes.add(loc_prefix)
                            added_items.append(item)
    if added_items:
        _save(project, items)
    return {
        "found": len(found_hits),
        "added": len(added_items),
        "no_trigger": no_trigger_count,
        "hits": found_hits,
        "new_followups": added_items
    }

