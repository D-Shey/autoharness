"""Tests for OpenSpec Compatibility and Workflow Bridge (Fission-AI/OpenSpec)."""
from __future__ import annotations

from pathlib import Path
from autoharness import hooks, openspec
from autoharness.env import Project


def test_is_openspec_command():
    # Slash commands
    assert openspec.is_openspec_command("/opsx:explore") is True
    assert openspec.is_openspec_command("/opsx:propose") is True
    assert openspec.is_openspec_command("/opsx:apply") is True
    assert openspec.is_openspec_command("/opsx:sync") is True
    assert openspec.is_openspec_command("/opsx:archive") is True
    assert openspec.is_openspec_command("/opsx:ff") is True
    assert openspec.is_openspec_command("/opsx:verify") is True

    # CLI invocations
    assert openspec.is_openspec_command("openspec status") is True
    assert openspec.is_openspec_command("openspec.ps1 list") is True
    assert openspec.is_openspec_command(["openspec", "init"]) is True
    assert openspec.is_openspec_command("npx openspec change") is True
    assert openspec.is_openspec_command("npx @fission-ai/openspec validate") is True
    assert openspec.is_openspec_command(["/opsx:propose", "my-change"]) is True

    # Non-openspec commands
    assert openspec.is_openspec_command("git status") is False
    assert openspec.is_openspec_command("pytest") is False


def test_is_openspec_path():
    assert openspec.is_openspec_path("openspec/changes/feat-1/proposal.md") is True
    assert openspec.is_openspec_path("openspec/changes/feat-1/tasks.md") is True
    assert openspec.is_openspec_path(".openspec.yaml") is True
    assert openspec.is_openspec_path(".openspec/specs/auth.md") is True
    assert openspec.is_openspec_path("src/app.py") is False


def test_parse_markdown_tasks():
    content = """
# Tasks for Feature

## Phase 1: Core Implementation
- [x] 1.1 Add database migration
- [ ] 1.2 Implement RPC gateway endpoint

## Phase 2: Testing
- [ ] 2.1 Write integration tests
"""
    tasks = openspec.parse_markdown_tasks(content)
    assert len(tasks) == 3
    assert tasks[0]["completed"] is True
    assert tasks[0]["section"] == "Phase 1: Core Implementation"
    assert tasks[1]["completed"] is False
    assert tasks[2]["completed"] is False


def test_detect_workspace_and_sync(tmp_path: Path):
    proj = Project(tmp_path)
    change_dir = tmp_path / "openspec" / "changes" / "user-auth"
    change_dir.mkdir(parents=True)

    (change_dir / ".openspec.yaml").write_text("name: user-auth\nschema: core\n", "utf-8")
    (change_dir / "proposal.md").write_text("# Proposal: User Auth\n", "utf-8")
    (change_dir / "tasks.md").write_text(
        "## Setup\n- [ ] Create JWT middleware\n- [x] Configure secret\n",
        "utf-8"
    )

    ws = openspec.detect_workspace(tmp_path)
    assert ws["has_openspec"] is True
    assert ws["total_changes"] == 1
    assert ws["active_change"] is not None
    assert ws["active_change"]["name"] == "user-auth"
    assert ws["active_change"]["tasks_total"] == 2
    assert ws["active_change"]["tasks_completed"] == 1

    # Test sync to AutoHarness followups
    sync_res = openspec.sync_tasks_to_followups(proj, "user-auth")
    assert sync_res["status"] == "ok"
    assert sync_res["synced_count"] == 1

    from autoharness import followups
    items = followups.listing(proj, everything=True)
    assert any("Create JWT middleware" in it.get("text", "") for it in items)


def test_hook_permits_openspec_commands():
    # Hook recognition for Skill: opsx:propose
    assert hooks._is_autoharness_call("Skill", {"skill": "opsx:propose"}) is True
    assert hooks._is_autoharness_call("Skill", {"skill": "openspec"}) is True

    # Hook recognition for CLI: openspec status
    assert hooks._is_autoharness_call("PowerShell", {"command": "openspec status"}) is True
    assert hooks._is_autoharness_call("Bash", {"command": "openspec list"}) is True


def test_hook_permits_writing_openspec_artifacts_when_pending():
    assert hooks._is_openspec_file("openspec/changes/my-change/proposal.md") is True
    assert hooks._is_openspec_file(".openspec.yaml") is True
