"""Tests for AutoHarness Active Context Pruning and History Search."""

import json
from pathlib import Path
import pytest
from autoharness import context_pruning, gateway
from autoharness.env import Project


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOHARNESS_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    root.mkdir()
    return Project(root, "claude")


def test_fold_reviews_and_distill():
    reviews = [
        {
            "round": 1,
            "by": "self",
            "worst": "none",
            "top": "Initial block-in implementation with базовые структуры",
            "answer_id": "self-01"
        },
        {
            "round": 2,
            "by": "critic",
            "worst": "major",
            "top": "Async timeout failure and unhandled Promise rejections",
            "answer_id": "ah-critic-01"
        },
        {
            "round": 3,
            "by": "critic",
            "worst": "none",
            "diff": "No differences.",
            "answer_id": "ah-critic-02",
            "coverage": {"B1": "met", "B2": "met"}
        }
    ]

    folded = context_pruning.fold_reviews(reviews)
    assert len(folded) == 3

    # Rounds 1 and 2 should be folded
    assert folded[0]["folded"] is True
    assert folded[0]["round"] == 1
    assert "Initial block-in" in folded[0]["summary"]

    assert folded[1]["folded"] is True
    assert folded[1]["round"] == 2
    assert "Async timeout failure" in folded[1]["summary"]

    # Round 3 should remain active
    assert folded[2]["folded"] is False
    assert folded[2]["round"] == 3
    assert folded[2]["coverage"] == {"B1": "met", "B2": "met"}

    # Format check
    formatted = context_pruning.format_folded_context({"reviews": reviews})
    assert "=== Task History (Folded Context) ===" in formatted
    assert "Round 1 (self): verdict none [self-01]" in formatted
    assert "Round 2 (critic): verdict major [ah-critic-01]" in formatted
    assert "[ACTIVE] Round 3 (critic): verdict none [ah-critic-02]" in formatted
    assert "Coverage: B1:met, B2:met" in formatted


def test_search_task_history(tmp_path):
    task_dir = tmp_path / "task-01"
    task_dir.mkdir()
    runs_dir = task_dir / "runs"
    runs_dir.mkdir()
    answers_dir = task_dir / "answers"
    answers_dir.mkdir()

    # Create mock run log
    run_log = (
        "running pytest tests/test_api.py\n"
        "test_connect ... ok\n"
        "test_timeout ... FAILED\n"
        "AssertionError: timeout waiting for response after 5000ms\n"
        "test_disconnect ... ok\n"
    )
    (runs_dir / "01-uv.log").write_text(run_log, "utf-8")

    # Create mock review answer
    review_ans = (
        "ANSWER-ID: ah-test-123\n"
        "1. [severity: major] Broken connection retry logic on network failure\n"
        "VERDICT: major\n"
    )
    (answers_dir / "critic-01.txt").write_text(review_ans, "utf-8")

    # 1. Search runs for "timeout"
    matches_runs = context_pruning.search_task_history(task_dir, query="timeout", kind="runs")
    assert len(matches_runs) >= 1
    assert matches_runs[0]["category"] == "runs"
    assert "runs/01-uv.log" in matches_runs[0]["source"]
    assert "AssertionError: timeout" in matches_runs[0]["snippet"]

    # 2. Search reviews for "retry logic"
    matches_rev = context_pruning.search_task_history(task_dir, query="retry logic", kind="reviews")
    assert len(matches_rev) == 1
    assert matches_rev[0]["category"] == "reviews"
    assert "Broken connection retry logic" in matches_rev[0]["snippet"]

    # 3. Search all for "major"
    matches_all = context_pruning.search_task_history(task_dir, query="major", kind="all")
    assert any("critic-01.txt" in m["source"] for m in matches_all)

    # 4. Search non-existent
    assert context_pruning.search_task_history(task_dir, query="nonexistent-xyz") == []


def test_gateway_history_search_integration(project):
    # Setup mock task in project
    task_dir = project.root / ".autoharness" / "tasks" / "mock-task"
    task_dir.mkdir(parents=True)
    runs_dir = task_dir / "runs"
    runs_dir.mkdir()

    log_content = (
        "step 1: initialization\n"
        "step 2: processing items\n"
        "critical error: database connection refused on port 5432\n"
        "step 3: cleanup\n"
    )
    (runs_dir / "01-test.log").write_text(log_content, "utf-8")

    sent = []
    gw = gateway.Gateway(project.root, sent.append, lambda line: None)
    gw.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    gw.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    res = gw.handle({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "harness_history_search",
            "arguments": {"query": "database connection refused", "kind": "runs"}
        }
    })

    assert not res["result"].get("isError")
    text = res["result"]["content"][0]["text"]
    assert "[runs] runs/01-test.log" in text
    assert "database connection refused" in text
