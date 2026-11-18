from pathlib import Path
import json
import pytest

from autoharness import agents
from autoharness.cli import _configure_zcode_autonomous_directives
from autoharness.hooks import install_zcode_hook


def test_zcode_agent_registered():
    assert "zcode" in agents.AGENTS
    agent = agents.AGENTS["zcode"]
    assert agent.title == "ZCode"
    assert agent.mcp_file == ".zcode/config.json"
    assert agent.skills_dir == ".zcode/skills"
    assert agent.user_skills_dir == ".zcode/skills"
    assert agent.mcp_key == "mcp.servers"


def test_to_zcode_local_command():
    cfg = {"command": "uvx", "args": ["mcp-server-fetch"], "env": {"ENV_VAR": "1"}}
    native, warnings = agents.to_zcode(cfg)
    assert native == {
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "env": {"ENV_VAR": "1"}
    }
    assert warnings == []


def test_to_zcode_remote_url():
    cfg = {"url": "https://mcp.example.com/sse", "type": "sse", "headers": {"Authorization": "Bearer token"}}
    native, warnings = agents.to_zcode(cfg)
    assert native == {
        "type": "sse",
        "url": "https://mcp.example.com/sse",
        "headers": {"Authorization": "Bearer token"},
    }
    assert warnings == []


def test_zcode_detection(monkeypatch):
    monkeypatch.setenv("ZCODE", "1")
    monkeypatch.setenv("ZCODE_SESSION_ID", "sess_zcode_42")
    assert agents.detect() == "zcode"
    assert agents.session_id(agent="zcode") == "sess_zcode_42"


def test_zcode_json_mcp_store_nested(tmp_path):
    cfg_file = tmp_path / ".zcode" / "config.json"
    store = agents.JsonMcpStore(cfg_file, key="mcp.servers")
    assert not store.has("fetch")
    assert store.names() == []

    store.add("fetch", {"command": "uvx", "args": ["mcp-server-fetch"]})
    assert store.has("fetch")
    assert store.names() == ["fetch"]

    data = json.loads(cfg_file.read_text("utf-8"))
    assert "mcp" in data
    assert "servers" in data["mcp"]
    assert "fetch" in data["mcp"]["servers"]
    assert data["mcp"]["servers"]["fetch"]["command"] == "uvx"

    store.remove("fetch", created_file=True, created_dir=True)
    assert not store.has("fetch")


def test_configure_zcode_autonomous_directives_project(tmp_path):
    agents_file = tmp_path / "AGENTS.md"
    agents_file.write_text("# My Project Rules\nKeep existing notes.\n", encoding="utf-8")
    configured = _configure_zcode_autonomous_directives(user=False, project_root=tmp_path)
    assert agents_file.exists()
    content = agents_file.read_text("utf-8")
    assert "# My Project Rules\nKeep existing notes." in content
    assert "ZCode AutoHarness Autonomous Directives" in content
    assert "CRITICAL MANDATORY BEHAVIOR: AUTOHARNESS AUTONOMOUS EXECUTION" in content
    assert "--components" in content
    assert "--techniques" in content
    assert "stencil scan" in content
    # Idempotent update does not duplicate block
    _configure_zcode_autonomous_directives(user=False, project_root=tmp_path)
    assert agents_file.read_text("utf-8").count("ZCode AutoHarness Autonomous Directives") == 1


def test_configure_zcode_autonomous_directives_user(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    configured = _configure_zcode_autonomous_directives(user=True)
    cfg_dir = tmp_path / ".zcode"
    assert (cfg_dir / "AGENTS.md").exists()
    content = (cfg_dir / "AGENTS.md").read_text("utf-8")
    assert "ZCode AutoHarness Autonomous Directives" in content


def test_install_zcode_hook_setting_json(tmp_path):
    setting_file = tmp_path / "setting.json"
    setting_file.write_text(json.dumps({
        "hooks": {
            "enabled": False,
            "events": {
                "SessionStart": [],
                "UserPromptSubmit": [],
                "PreToolUse": [],
                "Stop": []
            }
        }
    }), encoding="utf-8")

    status = install_zcode_hook(setting_file, remove=False)
    assert status == "installed"
    data = json.loads(setting_file.read_text("utf-8"))
    assert data["hooks"]["enabled"] is True
    assert len(data["hooks"]["events"]["PreToolUse"]) == 1
    assert data["hooks"]["events"]["PreToolUse"][0]["hooks"][0]["command"] == "autoharness hook"

    # Repeated install -> already_installed
    assert install_zcode_hook(setting_file, remove=False) == "already_installed"

    # Remove
    status_rem = install_zcode_hook(setting_file, remove=True)
    assert status_rem == "removed"
    data_rem = json.loads(setting_file.read_text("utf-8"))
    assert data_rem["hooks"]["enabled"] is False
    assert len(data_rem["hooks"]["events"]["PreToolUse"]) == 0


def test_zcode_json_mcp_store_preserves_siblings_and_recovers_bad_intermediate(tmp_path):
    cfg_file = tmp_path / ".zcode" / "config.json"
    cfg_file.parent.mkdir(parents=True)
    cfg_file.write_text(json.dumps({"mcp": {"enabled": True, "servers": {}}}), encoding="utf-8")
    store = agents.JsonMcpStore(cfg_file, key="mcp.servers")
    store.add("fetch", {"command": "uvx"})
    store.remove("fetch", created_file=True, created_dir=True)
    assert cfg_file.exists()
    assert json.loads(cfg_file.read_text("utf-8")) == {"mcp": {"enabled": True, "servers": {}}}

    cfg_file.write_text(json.dumps({"mcp": "bad"}), encoding="utf-8")
    store.add("fetch", {"command": "uvx"})
    assert json.loads(cfg_file.read_text("utf-8")) == {"mcp": {"servers": {"fetch": {"command": "uvx"}}}}


def test_install_zcode_hook_fixes_enabled_drift_and_keeps_unrelated_hooks(tmp_path):
    setting_file = tmp_path / "setting.json"
    install_zcode_hook(setting_file, remove=False)
    data = json.loads(setting_file.read_text("utf-8"))
    data["hooks"]["enabled"] = False
    data["hooks"]["UserPromptSubmit"].append({"hooks": [{"type": "command", "command": "other-tool"}]})
    setting_file.write_text(json.dumps(data), encoding="utf-8")

    assert install_zcode_hook(setting_file, remove=False) == "installed"
    repaired = json.loads(setting_file.read_text("utf-8"))
    assert repaired["hooks"]["enabled"] is True

    assert install_zcode_hook(setting_file, remove=True) == "removed"
    after_remove = json.loads(setting_file.read_text("utf-8"))
    assert after_remove["hooks"]["enabled"] is True
    assert after_remove["hooks"]["UserPromptSubmit"] == [{"hooks": [{"type": "command", "command": "other-tool"}]}]

    bad_file = tmp_path / "bad.json"
    bad_file.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(Exception):
        install_zcode_hook(bad_file, remove=False)
