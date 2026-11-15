"""8-Slot Modular Harness Scaffolding based on Google Research Harness² (arXiv:2610.14224).

Defines the canonical 8 editable slots of an agent execution harness:
  1. system_prompt - Core role, character behavior, and primary reasoning format.
  2. guardrails   - Safety boundaries, AST code invariants, and execution constraints.
  3. memory       - Short-term/long-term context structures, rulings, and lessons.
  4. skills       - Reusable procedural functions and modular agent skill libraries.
  5. tool_guidance- Specific instructions and bounds for calling external tools and APIs.
  6. scripts      - Executable snippets, recipes, and automation routines.
  7. subagents    - Topologies and delegation rules for secondary specialized agents.
  8. middleware   - Interceptors, pre-tool hooks, and input/output transformers.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CANONICAL_SLOTS: tuple[str, ...] = (
    "system_prompt",
    "guardrails",
    "memory",
    "skills",
    "tool_guidance",
    "scripts",
    "subagents",
    "middleware",
)

SLOT_DESCRIPTIONS: dict[str, str] = {
    "system_prompt": "Core role, reasoning format, and primary objectives.",
    "guardrails": "Safety boundaries, invariants, and destructive action controls.",
    "memory": "Persistent knowledge, lessons learned, and architectural rulings.",
    "skills": "Modular procedural capabilities and external tool skills.",
    "tool_guidance": "Instructions, rate limits, and schemas for external tools.",
    "scripts": "Executable automation routines, tests, and environment recipes.",
    "subagents": "Specialized subagent roles, models, and delegation topologies.",
    "middleware": "Hooks, secret redaction, and input/output validation filters.",
}


def inspect_harness_slots(
    project_root: Path,
    active_task_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Inspect and extract current live configuration across all 8 canonical harness slots."""
    root = project_root.resolve()
    slots_data: dict[str, Any] = {}

    # 1. system_prompt
    agent_info = (active_task_info or {}).get("agent") or "autoharness"
    agent_name = agent_info if isinstance(agent_info, str) else agent_info.get("name", "autoharness")
    slots_data["system_prompt"] = {
        "agent": agent_name,
        "mode": (active_task_info or {}).get("mode", "standard"),
        "description": "Standard disciplined engineering lifecycle with active credit assignment.",
    }

    # 2. guardrails
    guardrails: list[dict[str, Any]] = [
        {"name": "NoGitPushWithoutConsent", "type": "hard_invariant", "active": True},
        {"name": "DeterministicASTPolicies", "type": "static_analysis", "rules": ["DEAD_PRIVATE_METHOD", "SILENCED_EXCEPTION", "UNBOUNDED_RESOURCE_OPEN"]},
        {"name": "DynamicSecretRedaction", "type": "runtime_shield", "active": True},
    ]
    slots_data["guardrails"] = {
        "total_rules": len(guardrails),
        "rules": guardrails,
    }

    # 3. memory
    mem_db = root / ".autoharness" / "memory.db"
    mem_count = 0
    if mem_db.is_file():
        try:
            import sqlite3
            con = sqlite3.connect(mem_db)
            try:
                cur = con.cursor()
                cur.execute("SELECT count(*) FROM memory_docs")
                row = cur.fetchone()
                mem_count = row[0] if row else 0
            finally:
                con.close()
        except (sqlite3.Error, OSError):
            mem_count = 0
    slots_data["memory"] = {
        "backend": "sqlite_fts5_bm25",
        "db_path": str(mem_db) if mem_db.is_file() else None,
        "indexed_documents": mem_count,
    }

    # 4. skills
    skills_dir = root / ".agents" / "skills"
    installed_skills: list[str] = []
    if skills_dir.is_dir():
        for item in sorted(skills_dir.iterdir()):
            if item.is_dir() and (item / "SKILL.md").is_file():
                installed_skills.append(item.name)
    slots_data["skills"] = {
        "skills_directory": str(skills_dir),
        "total_skills": len(installed_skills),
        "installed": installed_skills,
    }

    # 5. tool_guidance
    slots_data["tool_guidance"] = {
        "strict_powershell": True,
        "no_bash_syntax": True,
        "token_minimization": True,
        "zero_dependency_stdlib": True,
    }

    # 6. scripts
    recipes_dir = root / ".autoharness" / "recipes"
    recipes: list[str] = []
    if recipes_dir.is_dir():
        recipes = [f.name for f in sorted(recipes_dir.iterdir()) if f.is_file()]
    slots_data["scripts"] = {
        "recipes_directory": str(recipes_dir) if recipes_dir.is_dir() else None,
        "available_recipes": recipes,
    }

    # 7. subagents
    slots_data["subagents"] = {
        "default_topology": "coordinator_with_independent_critic",
        "available_roles": ["coordinator", "self", "critic", "research", "swarm_worker"],
    }

    # 8. middleware
    hooks_dir = root / ".autoharness" / "hooks"
    active_hooks: list[str] = []
    if hooks_dir.is_dir():
        active_hooks = [h.name for h in sorted(hooks_dir.iterdir()) if h.is_file()]
    slots_data["middleware"] = {
        "pre_tool_validation": True,
        "secret_masking_interceptor": True,
        "active_hooks": active_hooks,
    }

    return {
        "slots_version": "harness2-8slot-v1",
        "root": str(root).replace("\\", "/"),
        "slots": slots_data,
    }


def export_harness_slots(
    project_root: Path,
    active_task_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Export harness slots configuration into a structured serializable dictionary."""
    return inspect_harness_slots(project_root, active_task_info)


def diff_harness_slots(
    base_slots: dict[str, Any],
    head_slots: dict[str, Any],
) -> dict[str, Any]:
    """Compute structural semantic diff between two harness configurations across 8 slots."""
    b_slots = base_slots.get("slots", {})
    h_slots = head_slots.get("slots", {})

    slot_diffs: dict[str, Any] = {}
    changed_slots: list[str] = []

    for slot in CANONICAL_SLOTS:
        b_val = b_slots.get(slot)
        h_val = h_slots.get(slot)
        if b_val != h_val:
            changed_slots.append(slot)
            slot_diffs[slot] = {
                "base": b_val,
                "head": h_val,
            }

    return {
        "changed_slots_count": len(changed_slots),
        "changed_slots": changed_slots,
        "diffs": slot_diffs,
    }


def format_slots_report(slots_inspection: dict[str, Any]) -> str:
    """Format 8-slot harness scaffolding state into clean, human-readable Markdown."""
    slots = slots_inspection.get("slots", {})
    lines: list[str] = [
        "# 8-Slot Harness Scaffolding (Google Research Harness² Model)",
        "",
        "## Overview",
        f"- Architecture: 8 Canonical Modular Slots (Version: {slots_inspection.get('slots_version', 'v1')})",
        f"- Target Root: `{slots_inspection.get('root', '.')}`",
        "",
        "## Slot Status",
    ]

    for slot_name in CANONICAL_SLOTS:
        data = slots.get(slot_name, {})
        desc = SLOT_DESCRIPTIONS.get(slot_name, "")
        lines.append(f"### {slot_name.upper()} (`{slot_name}`)")
        lines.append(f"*{desc}*")
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list):
                    lines.append(f"- **{k}** ({len(v)}): {', '.join(str(x) for x in v[:6])}{'...' if len(v) > 6 else ''}")
                else:
                    lines.append(f"- **{k}**: `{v}`")
        else:
            lines.append(f"- State: `{data}`")
        lines.append("")

    return "\n".join(lines).strip()
