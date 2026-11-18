from pathlib import Path
import json
import pytest

from autoharness import agents
from autoharness.cli import cmd_install_skill, _configure_opencode_autonomous_directives


def test_opencode_agent_registered():
    assert "opencode" in agents.AGENTS
    agent = agents.AGENTS["opencode"]
    assert agent.title == "OpenCode"
    assert agent.mcp_file == "opencode.json"
    assert agent.skills_dir == ".opencode/skills"
    assert agent.user_skills_dir == ".config/opencode/skills"
    assert agent.mcp_key == "mcp"


def test_to_opencode_local_command():
    cfg = {"command": "python", "args": ["-m", "mcp_server"]}
    native, warnings = agents.to_opencode(cfg)
    assert native == {
        "type": "local",
        "command": ["python", "-m", "mcp_server"],
    }
    assert warnings == []


def test_to_opencode_remote_url():
    cfg = {"url": "https://mcp.example.com/sse", "headers": {"Authorization": "Bearer token"}}
    native, warnings = agents.to_opencode(cfg)
    assert native == {
        "type": "remote",
        "url": "https://mcp.example.com/sse",
        "headers": {"Authorization": "Bearer token"},
    }
    assert warnings == []


def test_opencode_detection(monkeypatch):
    monkeypatch.setenv("OPENCODE", "1")
    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_test123")
    assert agents.detect() == "opencode"
    assert agents.session_id(agent="opencode") == "ses_test123"


def test_configure_opencode_autonomous_directives_project(tmp_path):
    configured = _configure_opencode_autonomous_directives(user=False, project_root=tmp_path)
    agents_file = tmp_path / "AGENTS.md"
    assert agents_file.exists()
    content = agents_file.read_text("utf-8")
    assert "CRITICAL MANDATORY BEHAVIOR: AUTOHARNESS AUTONOMOUS EXECUTION" in content
    assert "--components" in content
    assert "--techniques" in content
    assert "stencil scan" in content


def test_configure_opencode_autonomous_directives_user(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    configured = _configure_opencode_autonomous_directives(user=True)
    cfg_dir = tmp_path / ".config" / "opencode"
    assert (cfg_dir / "instructions.md").exists()
    assert (cfg_dir / "AGENTS.md").exists()
    assert (cfg_dir / "opencode.json").exists()
    data = json.loads((cfg_dir / "opencode.json").read_text("utf-8"))
    assert any("instructions.md" in item for item in data.get("instructions", []))


def test_configure_opencode_handles_null_instructions_and_legacy_agents_md(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text('{"instructions": null}\n', encoding="utf-8")
    (cfg_dir / "AGENTS.md").write_text(
        "# User Notes\n\n# OpenCode AutoHarness Autonomous Directives\n\n<EXTREMELY_IMPORTANT>\nold\n</EXTREMELY_IMPORTANT>\n",
        encoding="utf-8",
    )

    _configure_opencode_autonomous_directives(user=True)
    data = json.loads((cfg_dir / "opencode.json").read_text("utf-8"))
    assert isinstance(data["instructions"], list) and len(data["instructions"]) == 1
    agents_txt = (cfg_dir / "AGENTS.md").read_text("utf-8")
    assert "# User Notes" in agents_txt
    assert agents_txt.count("AutoHarness Autonomous Directives") == 1


def test_configure_opencode_handles_non_object_json(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text('["unexpected"]\n', encoding="utf-8")

    _configure_opencode_autonomous_directives(user=True)
    data = json.loads((cfg_dir / "opencode.json").read_text("utf-8"))
    assert isinstance(data, dict)
    assert isinstance(data["instructions"], list) and len(data["instructions"]) == 1

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    (proj_dir / "opencode.json").write_text("null\n", encoding="utf-8")
    _configure_opencode_autonomous_directives(user=False, project_root=proj_dir)
    proj_data = json.loads((proj_dir / "opencode.json").read_text("utf-8"))
    assert proj_data == {"instructions": ["AGENTS.md"]}
