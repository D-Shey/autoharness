"""OpenSpec Compatibility & Workflow Bridge for AutoHarness.

Pure Python standard library implementation (zero runtime dependencies).
Ensures AutoHarness hooks, gates, and verifiers never conflict with, block, or hinder
OpenSpec (https://github.com/Fission-AI/OpenSpec) specification-driven workflows
(/opsx:explore, /opsx:propose, /opsx:apply, /opsx:update, /opsx:sync, /opsx:archive,
/opsx:new, /opsx:continue, /opsx:ff, /opsx:verify).
Provides bidirectional synchronization between OpenSpec tasks.md, AutoHarness followups,
and Swarm DAG plans.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

# Well-known OpenSpec core and extended command verbs
OPENSPEC_VERBS = {
    "explore", "propose", "apply", "update", "sync", "archive",
    "new", "continue", "ff", "verify", "init", "list", "show",
    "validate", "doctor", "status", "context", "workset", "instructions"
}

# Regex to detect /opsx:<verb> or opsx:<verb> in prompts and tool inputs
OPSX_COMMAND_REGEX = re.compile(
    r"""(?i)(?:^|\s)/(?:opsx|openspec):([a-z]+)\b""",
)

# OpenSpec directory names and metadata files
OPENSPEC_DIRS = {"openspec", ".openspec"}
OPENSPEC_FILES = {".openspec.yaml", ".openspec.yml", "openspec.yaml", "openspec.yml", "openspec.json"}


def is_openspec_installed() -> bool:
    """Return True if openspec or opsx CLI is installed on system PATH."""
    return bool(find_openspec_cli())


def find_openspec_cli() -> str | None:
    """Find the path to the openspec or opsx CLI executable."""
    candidates = ["openspec", "openspec.ps1", "openspec.cmd", "openspec.exe", "opsx", "opsx.cmd", "opsx.exe"]
    for c in candidates:
        found = shutil.which(c)
        if found:
            return found
    return None


def is_openspec_path(path: Path | str) -> bool:
    """Return True if path points to OpenSpec specifications, changes, or metadata."""
    p = Path(path)
    parts_lower = [part.lower() for part in p.parts]
    name_lower = p.name.lower()

    if any(d in parts_lower for d in OPENSPEC_DIRS):
        return True
    if name_lower in OPENSPEC_FILES:
        return True
    if name_lower.startswith(".openspec"):
        return True
    return False


def is_openspec_command(command: str | list[str]) -> bool:
    """Return True if command invocation is an OpenSpec CLI or slash command."""
    if isinstance(command, list):
        if not command:
            return False
        w0 = command[0].lower()
        if w0 in ("openspec", "openspec.exe", "openspec.ps1", "openspec.cmd", "opsx", "opsx.exe", "opsx.cmd"):
            return True
        if w0.startswith("/opsx:") or w0.startswith("opsx:"):
            return True
        if w0 == "npx" and len(command) > 1 and command[1].lower() in ("openspec", "@fission-ai/openspec"):
            return True
        return False

    cmd_str = command.strip()
    if OPSX_COMMAND_REGEX.search(cmd_str):
        return True
    first_token = cmd_str.split()[0].lower() if cmd_str.split() else ""
    first_base = Path(first_token).name.lower()
    if first_base in ("openspec", "openspec.exe", "openspec.ps1", "openspec.cmd", "opsx", "opsx.exe", "opsx.cmd"):
        return True
    if first_base.startswith("/opsx:") or first_base.startswith("opsx:"):
        return True
    if first_base == "npx":
        tokens = cmd_str.split()
        if len(tokens) > 1 and tokens[1].lower() in ("openspec", "@fission-ai/openspec"):
            return True
    return False


def detect_workspace(project_root: Path) -> dict[str, Any]:
    """Detect OpenSpec configuration, changes, and installation in the project."""
    openspec_dir = None
    for candidate in ("openspec", ".openspec"):
        d = project_root / candidate
        if d.is_dir():
            openspec_dir = d
            break

    cli_path = find_openspec_cli()
    changes = list_changes(project_root)
    active_change = None
    for c in changes:
        if c.get("status") == "active":
            active_change = c
            break

    return {
        "installed": bool(cli_path),
        "cli_path": cli_path,
        "workspace_root": str(openspec_dir) if openspec_dir else None,
        "has_openspec": bool(openspec_dir),
        "total_changes": len(changes),
        "active_change": active_change,
        "changes": changes,
    }


def list_changes(project_root: Path) -> list[dict[str, Any]]:
    """List OpenSpec changes across active and archive directories."""
    changes = []
    base_dirs = [project_root / "openspec", project_root / ".openspec"]

    for b in base_dirs:
        if not b.is_dir():
            continue

        # Active changes
        changes_dir = b / "changes"
        if changes_dir.is_dir():
            for child in sorted(changes_dir.iterdir()):
                if child.is_dir():
                    c_info = _read_change_info(child, status="active")
                    changes.append(c_info)

        # Archived changes
        archive_dir = b / "archive"
        if archive_dir.is_dir():
            for child in sorted(archive_dir.iterdir()):
                if child.is_dir():
                    c_info = _read_change_info(child, status="archived")
                    changes.append(c_info)

    return changes


def _read_change_info(change_dir: Path, status: str = "active") -> dict[str, Any]:
    name = change_dir.name
    meta_file = change_dir / ".openspec.yaml"
    tasks_file = change_dir / "tasks.md"
    proposal_file = change_dir / "proposal.md"
    design_file = change_dir / "design.md"

    tasks = parse_markdown_tasks(tasks_file.read_text(encoding="utf-8", errors="replace")) if tasks_file.is_file() else []
    completed_tasks = sum(1 for t in tasks if t["completed"])

    return {
        "name": name,
        "status": status,
        "path": str(change_dir),
        "has_metadata": meta_file.is_file(),
        "has_proposal": proposal_file.is_file(),
        "has_design": design_file.is_file(),
        "has_tasks": tasks_file.is_file(),
        "tasks_total": len(tasks),
        "tasks_completed": completed_tasks,
        "tasks_pending": len(tasks) - completed_tasks,
    }


def parse_markdown_tasks(text: str) -> list[dict[str, Any]]:
    """Parse checklist tasks from OpenSpec tasks.md file."""
    tasks = []
    current_section = "General"
    task_idx = 1

    for line in text.splitlines():
        line_s = line.strip()
        if line_s.startswith("## ") or line_s.startswith("### "):
            current_section = line_s.lstrip("#").strip()
            continue

        # Checkbox match: - [ ] or - [x] or * [ ] or 1. [ ]
        m = re.match(r"^(?:[-*]|\d+\.)\s+\[([ xX])\]\s+(.*)", line_s)
        if m:
            is_done = m.group(1).lower() == "x"
            desc = m.group(2).strip()
            tasks.append({
                "id": task_idx,
                "section": current_section,
                "text": desc,
                "completed": is_done,
            })
            task_idx += 1

    return tasks


def get_change(project_root: Path, change_name: str | None = None) -> dict[str, Any] | None:
    """Get full details of an active or specific OpenSpec change."""
    changes = list_changes(project_root)
    if not changes:
        return None

    target = None
    if change_name:
        for c in changes:
            if c["name"] == change_name:
                target = c
                break
    else:
        for c in changes:
            if c["status"] == "active":
                target = c
                break
        if not target and changes:
            target = changes[0]

    if not target:
        return None

    c_dir = Path(target["path"])
    tasks_file = c_dir / "tasks.md"
    proposal_file = c_dir / "proposal.md"
    design_file = c_dir / "design.md"

    target["tasks"] = parse_markdown_tasks(tasks_file.read_text("utf-8", "replace")) if tasks_file.is_file() else []
    target["proposal"] = proposal_file.read_text("utf-8", "replace") if proposal_file.is_file() else ""
    target["design"] = design_file.read_text("utf-8", "replace") if design_file.is_file() else ""
    return target


def sync_tasks_to_followups(project: Any, change_name: str | None = None) -> dict[str, Any]:
    """Synchronize pending OpenSpec tasks into AutoHarness followups queue."""
    ch = get_change(project.root, change_name)
    if not ch or not ch.get("tasks"):
        return {
            "status": "idle",
            "message": "No active OpenSpec change or tasks found to sync.",
            "synced_count": 0,
        }

    from autoharness import followups
    pending = [t for t in ch["tasks"] if not t["completed"]]
    existing_items = followups.listing(project, everything=True)
    existing_texts = {item.get("text", "") for item in existing_items}

    added_count = 0
    added_ids = []
    task_id = project.active_id() or "openspec-sync"

    for t in pending:
        task_text = f"[OpenSpec:{ch['name']}] {t['section']}: {t['text']}"
        if task_text not in existing_texts:
            rec = followups.add(project, task_text, task_id)
            added_count += 1
            added_ids.append(rec.get("id"))
            existing_texts.add(task_text)

    return {
        "status": "ok",
        "change_name": ch["name"],
        "total_openspec_tasks": len(ch["tasks"]),
        "synced_count": added_count,
        "followup_ids": added_ids,
    }


def format_openspec_status(data: dict[str, Any]) -> str:
    """Format OpenSpec workspace status report in Markdown."""
    installed_str = "Installed" if data.get("installed") else "Not found on PATH"
    ws = data.get("workspace_root") or "None"
    lines = [
        f"## OpenSpec Compatibility Status: `{installed_str}`",
        f"- **CLI Path:** `{data.get('cli_path') or 'N/A'}`",
        f"- **Workspace Directory:** `{ws}`",
        f"- **Total Changes:** {data.get('total_changes', 0)}",
    ]
    ac = data.get("active_change")
    if ac:
        lines.append(f"\n### Active Change: `{ac['name']}`")
        lines.append(f"- **Progress:** {ac['tasks_completed']}/{ac['tasks_total']} tasks completed")
        lines.append(f"- **Artifacts:** proposal: `{ac['has_proposal']}`, design: `{ac['has_design']}`, tasks: `{ac['has_tasks']}`")
    return "\n".join(lines)
