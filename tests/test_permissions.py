"""Unit tests for agent permissions and YOLO mode management."""
import json
from pathlib import Path
import pytest

from autoharness import cli, gateway, permissions
from autoharness.env import Project


def test_check_permissions_defaults_to_strict(tmp_path: Path):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    proj = Project(proj_root, "claude")

    info = permissions.check_agent_permissions(proj)
    assert info["status"] == "ok"
    assert info["agent"] == "claude"
    assert info["project_yolo"] is False


def test_check_permissions_with_env_override(tmp_path: Path, monkeypatch):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    proj = Project(proj_root, "claude")

    monkeypatch.setenv("AUTOHARNESS_YOLO", "1")
    info = permissions.check_agent_permissions(proj)
    assert info["env_yolo"] is True
    assert info["effective_yolo"] is True


def test_enable_and_disable_project_permissions_json(tmp_path: Path):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    proj = Project(proj_root, "claude")

    # 1. Enable project permissions
    res_en = permissions.enable_project_permissions(proj)
    assert res_en["status"] == "ok"
    assert res_en["modified"] is True
    assert res_en["info"]["project_yolo"] is True
    assert res_en["info"]["effective_yolo"] is True

    # Check file contents
    settings_file = proj_root / ".claude" / "settings.json"
    assert settings_file.is_file()
    data = json.loads(settings_file.read_text("utf-8"))
    assert data["autoApproval"] is True
    assert "Bash" in data["permissions"]["allow"]

    # 2. Disable project permissions
    res_dis = permissions.disable_project_permissions(proj)
    assert res_dis["status"] == "ok"
    assert res_dis["modified"] is True
    assert res_dis["info"]["project_yolo"] is False

    # Check file updated
    data_clean = json.loads(settings_file.read_text("utf-8"))
    assert "autoApproval" not in data_clean


def test_enable_project_permissions_toml(tmp_path: Path):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    proj = Project(proj_root, "codex")

    # 1. Enable
    res_en = permissions.enable_project_permissions(proj)
    assert res_en["status"] == "ok"
    assert res_en["info"]["project_yolo"] is True

    config_file = proj_root / ".codex" / "config.toml"
    assert config_file.is_file()
    text = config_file.read_text("utf-8")
    assert 'approval = "never"' in text
    assert "auto_approve = true" in text

    # 2. Disable
    res_dis = permissions.disable_project_permissions(proj)
    assert res_dis["status"] == "ok"
    assert res_dis["info"]["project_yolo"] is False
    text_clean = config_file.read_text("utf-8")
    assert 'approval = "never"' not in text_clean


def test_enable_skipped_when_global_yolo_active(tmp_path: Path, monkeypatch):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    proj = Project(proj_root, "claude")

    # Mock user global settings
    user_settings = tmp_path / "user_claude" / "settings.json"
    user_settings.parent.mkdir()
    user_settings.write_text(json.dumps({"autoApproval": True}), "utf-8")

    def mock_settings_paths(agent, root):
        return root / ".claude" / "settings.json", user_settings

    monkeypatch.setattr(permissions, "_agent_settings_paths", mock_settings_paths)

    res = permissions.enable_project_permissions(proj)
    assert res["status"] == "ok"
    assert res["modified"] is False
    assert "already active" in res["message"]
    assert not (proj_root / ".claude" / "settings.json").exists()


def test_cli_permissions_commands(tmp_path: Path, monkeypatch, capsys):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    (proj_root / ".autoharness").mkdir()
    monkeypatch.chdir(proj_root)

    # 1. status
    code = cli.main(["--project", str(proj_root), "permissions", "status"])
    assert code == 0
    out_status = json.loads(capsys.readouterr().out)
    assert out_status["status"] == "ok"

    # 2. enable
    code = cli.main(["--project", str(proj_root), "permissions", "enable"])
    assert code == 0
    out_en = json.loads(capsys.readouterr().out)
    assert out_en["modified"] is True

    # 3. disable
    code = cli.main(["--project", str(proj_root), "permissions", "disable"])
    assert code == 0
    out_dis = json.loads(capsys.readouterr().out)
    assert out_dis["modified"] is True


def test_gateway_permissions_tools(tmp_path: Path):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    (proj_root / ".autoharness").mkdir()

    written = []
    gw = gateway.Gateway(proj_root, write=written.append, log=lambda s: None)

    # status
    res_st = gw._call({"name": "harness_permissions_status", "arguments": {}})
    assert not res_st.get("isError")
    parsed_st = json.loads(res_st["content"][0]["text"])
    assert parsed_st["status"] == "ok"

    # enable
    res_en = gw._call({"name": "harness_permissions_enable", "arguments": {}})
    assert not res_en.get("isError")
    parsed_en = json.loads(res_en["content"][0]["text"])
    assert parsed_en["modified"] is True
