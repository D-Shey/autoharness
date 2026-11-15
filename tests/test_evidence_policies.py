"""Tests for Commit-Before-Critique and Author Self-Report Sanitization in AutoHarness."""
from __future__ import annotations

import pytest
from pathlib import Path
from autoharness import evidence, catalog
from autoharness.catalog import Catalog
from autoharness.env import Project


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOHARNESS_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    root.mkdir()
    return Project(root, "claude")


def test_commit_before_critique_in_prompts(project, tmp_path):
    task = project.init_task("Build robust payment webhook", "full")
    task.record_brief({
        "goal": "Build robust payment webhook",
        "domains": "none",
        "method": "searched: payments",
        "inputs": "none",
        "constraints": "none",
        "expects": "none: ok",
        "verify": "pytest",
        "candidates": "none",
        "gaps": "none: ok"
    })
    dummy_res = tmp_path / "result.txt"
    dummy_res.write_text("def handle_webhook(): pass", encoding="utf-8")
    evidence.add(task, dummy_res, "result")

    # 1. Check critic_prompt contains Commit-Before-Critique and Expected Invariants
    c_prompt = evidence.critic_prompt(Catalog.load(), task)["prompt"]
    assert "Commit-Before-Critique:" in c_prompt
    assert "Expected Invariants" in c_prompt
    assert "DATA_NOT_INSTRUCTIONS" not in c_prompt  # text expanded
    assert "unverified author self-report" in c_prompt

    # 2. Check quality_prompt contains Commit-Before-Critique and Expected Invariants
    q_prompt = evidence.quality_prompt(task)["prompt"]
    assert "Commit-Before-Critique:" in q_prompt
    assert "Expected Invariants" in q_prompt
    assert "unverified author self-report" in q_prompt


def test_sanitize_author_note():
    # Boastful / unverified claims get tagged
    assert "[AUTHOR CLAIM - UNVERIFIED]" in evidence.sanitize_author_note("all tests pass and verified")
    assert "[AUTHOR CLAIM - UNVERIFIED]" in evidence.sanitize_author_note("100% bug free implementation")
    assert "[AUTHOR CLAIM - UNVERIFIED]" in evidence.sanitize_author_note("fully implemented payment webhook")

    # Normal objective labels remain unchanged
    assert evidence.sanitize_author_note("webhook handler implementation") == "webhook handler implementation"
    assert evidence.sanitize_author_note("initial block-in draft") == "initial block-in draft"


def test_action_level_credit_assignment_in_critic_prompt(project, tmp_path):
    task = project.init_task("Build webhook", "full")
    task.record_brief({
        "goal": "1 Parse webhook; 2 Verify signature",
        "domains": "none",
        "method": "searched: payments",
        "inputs": "none",
        "constraints": "none",
        "expects": "none: ok",
        "verify": "pytest",
        "candidates": "none",
        "gaps": "none: ok"
    })
    dummy = tmp_path / "result.txt"
    dummy.write_text("code", encoding="utf-8")
    evidence.add(task, dummy, "result")

    prompt = evidence.critic_prompt(Catalog.load(), task)["prompt"]
    assert "Action-Level Credit Assignment:" in prompt
    assert "Action Attribution" in prompt
    assert "HELPS | HINDERS | NOISE" in prompt


def test_parse_action_attribution():
    text = """
Differences:
1. Some minor formatting

Action Attribution:
B1 [webhook.py]: HELPS - implemented payload parser cleanly
B2 [auth.py]: HINDERS - bypassed signature validation when header is missing
C1 [utils.py]: NOISE - added unneeded helper function

VERDICT: minor
"""
    parsed = evidence.parse_action_attribution(text)
    assert parsed is not None
    assert parsed["helps"] == 1
    assert parsed["hinders"] == 1
    assert parsed["noise"] == 1
    assert len(parsed["items"]) == 3
    assert parsed["items"][0] == {
        "item": "B1",
        "target": "webhook.py",
        "verdict": "helps",
        "evidence": "implemented payload parser cleanly"
    }
    assert parsed["items"][1] == {
        "item": "B2",
        "target": "auth.py",
        "verdict": "hinders",
        "evidence": "bypassed signature validation when header is missing"
    }
    assert parsed["items"][2] == {
        "item": "C1",
        "target": "utils.py",
        "verdict": "noise",
        "evidence": "added unneeded helper function"
    }


def test_review_elevates_severity_on_hinders(project, tmp_path):
    task = project.init_task("Task with credit attribution", "full")
    dummy = tmp_path / "result.txt"
    dummy.write_text("code", encoding="utf-8")
    evidence.add(task, dummy, "result")

    review_text = """
1. Minor style issue: [minor]

Action Attribution:
B1 [core.py]: HINDERS - breaks state consistency on error
"""
    rev = evidence.review(task, diff=review_text, worst="minor", by="critic")
    assert rev["actions"]["hinders"] == 1
    assert rev["worst"] == "major"
    assert rev["fixable"] is True
    assert "hinders_warning" in rev


def test_evidence_diff_detects_anti_shims(project, tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=project.root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project.root, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=project.root, check=True)
    (project.root / "base.py").write_text("# base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project.root, check=True)
    subprocess.run(["git", "commit", "-m", "init", "-q"], cwd=project.root, check=True)

    task = project.init_task("Task with anti-shim detection", "full")
    py_file = project.root / "fake_module.py"
    py_file.write_text("def do_work():\n    pass\n", encoding="utf-8")

    diff_entry = evidence.add_diff(task, project.root)
    assert "anti_shims" in diff_entry
    assert len(diff_entry["anti_shims"]) == 1
    assert diff_entry["anti_shims"][0]["rule"] == "EMPTY_STUB_BODY"
    assert diff_entry["anti_shims"][0]["symbol"] == "do_work"



