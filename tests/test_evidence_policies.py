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
