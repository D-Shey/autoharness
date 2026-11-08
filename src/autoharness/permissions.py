"""Agent permissions and YOLO mode management for AutoHarness.

Detects whether an agent (Claude Code, Gemini/agy, Codex, Kimi, Cursor) is running
in YOLO / auto-approval mode globally or at the project level, and provides safe,
reversible project-level configuration to prevent command execution blocking.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import tomllib

from autoharness.agents import AGENTS, Agent, DEFAULT_AGENT
from autoharness.env import Project


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text("utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {}


def _write_text_atomic(path: Path, text: str) -> None:
    """Atomically write text to path using a temp file in the same directory and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _write_json(path: Path, data: dict[str, Any]) -> None:
    _write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text("utf-8") or "")
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def _agent_settings_paths(agent: Agent, project_root: Path) -> tuple[Path, Path | None]:
    """Return (project_settings_path, user_settings_path) for an agent."""
    name = agent.name
    home = Path.home()

    if name == "claude":
        return project_root / ".claude" / "settings.json", home / ".claude" / "settings.json"
    elif name in ("gemini", "qwen", "gigacode"):
        return project_root / f".{name}" / "settings.json", home / f".{name}" / "settings.json"
    elif name == "agy":
        return project_root / ".agents" / "settings.json", home / ".gemini" / "config" / "settings.json"
    elif name == "codex":
        return project_root / ".codex" / "config.toml", home / ".codex" / "config.toml"
    elif name == "kimi":
        user_root = Path(os.environ["KIMI_CODE_HOME"]) if os.environ.get("KIMI_CODE_HOME") else home / ".kimi-code"
        return project_root / ".kimi-code" / "config.toml", user_root / "config.toml"
    elif name == "cursor":
        return project_root / ".cursor" / "settings.json", None
    else:  # strands or fallback
        return project_root / ".agent" / "settings.json", None


def check_agent_permissions(project: Project) -> dict[str, Any]:
    """Check whether YOLO mode / auto-approval is active globally or in this project."""
    agent = project.agent
    project_path, user_path = _agent_settings_paths(agent, project.root)

    # 1. Environment variable check (global override)
    env_yolo = bool(
        os.environ.get("AUTOHARNESS_YOLO") == "1"
        or os.environ.get("DANGEROUSLY_SKIP_PERMISSIONS") == "1"
        or os.environ.get(f"{agent.name.upper()}_YOLO") == "1"
        or os.environ.get(f"{agent.name.upper()}_AUTO_APPROVE") == "1"
    )

    # 2. Global (user-level) config check
    global_yolo = False
    if user_path and user_path.exists():
        if user_path.suffix == ".toml":
            udata = _read_toml(user_path)
            global_yolo = bool(
                udata.get("approval") == "never"
                or udata.get("auto_approve") is True
                or udata.get("yolo") is True
            )
        else:
            udata = _read_json(user_path)
            global_yolo = bool(
                udata.get("autoApproval") is True
                or udata.get("yoloMode") is True
                or udata.get("approvalMode") == "yolo"
                or udata.get("commandExecution") == "always_allow"
                or udata.get("permissionMode") in ("bypassPermissions", "bypass", "yolo")
                or (isinstance(udata.get("permissions"), dict) and "*" in udata.get("permissions", {}).get("allow", []))
            )

    # 3. Project-level config check
    project_yolo = False
    if project_path and project_path.exists():
        if project_path.suffix == ".toml":
            pdata = _read_toml(project_path)
            project_yolo = bool(
                pdata.get("approval") == "never"
                or pdata.get("auto_approve") is True
                or pdata.get("yolo") is True
            )
        else:
            pdata = _read_json(project_path)
            project_yolo = bool(
                pdata.get("autoApproval") is True
                or pdata.get("yoloMode") is True
                or pdata.get("approvalMode") == "yolo"
                or pdata.get("commandExecution") == "always_allow"
                or pdata.get("permissionMode") in ("bypassPermissions", "bypass", "yolo")
                or (isinstance(pdata.get("permissions"), dict) and "*" in pdata.get("permissions", {}).get("allow", []))
            )

    effective = env_yolo or global_yolo or project_yolo

    return {
        "status": "ok",
        "agent": agent.name,
        "effective_yolo": effective,
        "env_yolo": env_yolo,
        "global_yolo": global_yolo,
        "project_yolo": project_yolo,
        "project_settings_file": str(project_path),
        "user_settings_file": str(user_path) if user_path else None,
        "recommendation": (
            "Permissions are fully automated."
            if effective
            else "Confirmation mode is active. Run `autoharness permissions enable` to prevent command prompt blocking."
        ),
    }


def enable_project_permissions(project: Project) -> dict[str, Any]:
    """Enable project-level YOLO mode/auto-approval for this agent without modifying global configs."""
    info = check_agent_permissions(project)
    if info["global_yolo"] or info["env_yolo"]:
        return {
            "status": "ok",
            "modified": False,
            "message": "Global or environment YOLO mode is already active; project settings left untouched.",
            "info": info,
        }

    agent = project.agent
    project_path, _ = _agent_settings_paths(agent, project.root)

    if project_path.suffix == ".toml":
        # Codex or Kimi TOML
        text = project_path.read_text("utf-8") if project_path.exists() else ""
        if "auto_approve = true" not in text and "approval = \"never\"" not in text:
            block = (
                "\n# >>> autoharness:permissions\n"
                "approval = \"never\"\n"
                "auto_approve = true\n"
                "yolo = true\n"
                "# <<< autoharness:permissions\n"
            )
            project_path.parent.mkdir(parents=True, exist_ok=True)
            _write_text_atomic(project_path, (text + block).lstrip())
    else:
        # JSON configuration (Claude, Gemini, agy, Cursor)
        data = _read_json(project_path)
        data["autoApproval"] = True
        data["yoloMode"] = True
        data["commandExecution"] = "always_allow"
        data.setdefault("permissions", {})
        if isinstance(data["permissions"], dict):
            allowed = set(data["permissions"].get("allow", []))
            allowed.update(["Bash", "PowerShell", "Write", "Edit", "NotebookEdit", "Skill", "mcp__*"])
            data["permissions"]["allow"] = sorted(allowed)
        _write_json(project_path, data)

    new_info = check_agent_permissions(project)
    return {
        "status": "ok",
        "modified": True,
        "message": f"Enabled project-level auto-permissions in {project_path}.",
        "info": new_info,
    }


def disable_project_permissions(project: Project) -> dict[str, Any]:
    """Disable/revert project-level YOLO mode and return to standard confirmation mode."""
    agent = project.agent
    project_path, _ = _agent_settings_paths(agent, project.root)

    if not project_path.exists():
        return {
            "status": "ok",
            "modified": False,
            "message": "Project settings file does not exist; confirmation mode already active.",
            "info": check_agent_permissions(project),
        }

    if project_path.suffix == ".toml":
        text = project_path.read_text("utf-8")
        clean_text = re.sub(
            r"# >>> autoharness:permissions\n.*?# <<< autoharness:permissions\n?",
            "",
            text,
            flags=re.M | re.S,
        )
        _write_text_atomic(project_path, clean_text)
    else:
        data = _read_json(project_path)
        data.pop("autoApproval", None)
        data.pop("yoloMode", None)
        data.pop("commandExecution", None)
        if isinstance(data.get("permissions"), dict):
            data["permissions"].pop("allow", None)
            if not data["permissions"]:
                data.pop("permissions", None)
        _write_json(project_path, data)

    new_info = check_agent_permissions(project)
    return {
        "status": "ok",
        "modified": True,
        "message": f"Reverted project settings in {project_path} to standard confirmation mode.",
        "info": new_info,
    }
