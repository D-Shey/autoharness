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


def test_compact_session_transcript_layers(tmp_path):
    # Construct 18 steps: 6 epoch, 8 distill (with heavy tool outputs), 4 recent
    steps = []
    for i in range(1, 7):
        steps.append({
            "step": i,
            "type": "USER_INPUT" if i == 1 else "PLANNER_RESPONSE",
            "content": f"Step {i}: Initial project discovery and architecture analysis plan.",
            "thinking": f"Deliberating on step {i} requirements and constraints."
        })

    # Middle steps with heavy logs that will be compressed
    large_log = "\n".join([f"line {j}: processing module test_{j}.py ... OK" for j in range(120)])
    for i in range(7, 15):
        steps.append({
            "step": i,
            "type": "PLANNER_RESPONSE",
            "content": f"Step {i}: executing test runner and linters.",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "pytest"}}],
            "tool_output": large_log,
        })

    # Recent steps (keep_last=4)
    for i in range(15, 19):
        steps.append({
            "step": i,
            "type": "PLANNER_RESPONSE",
            "content": f"Step {i}: Finalizing verification and drafting report.",
            "thinking": f"Checking assertions for step {i}."
        })

    transcript_file = tmp_path / "test_session.jsonl"
    transcript_file.write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")

    res = context_pruning.compact_session_transcript(
        transcript_file,
        project_root=tmp_path,
        session_id="test-sess-01",
        keep_last=4,
        epoch_size=6,
        save_disk=True,
    )

    assert res["status"] == "ok"
    assert res["session_id"] == "test-sess-01"
    assert res["original_steps"] == 18
    assert res["compact_steps"] < 18
    assert res["compression_ratio"] > 20.0
    assert res["tiers"]["anchor_steps"] == 1
    assert res["tiers"]["recent_steps"] == 4
    assert res["tiers"]["distill_steps"] == 6
    assert res["tiers"]["epoch_steps"] == 7
    assert res["steps"][0]["tier"] == "anchor"
    assert res["steps"][0]["folded"] is False
    assert res["prompt_cache_status"]["anchor_preserved"] is True

    # Verify disk files
    compact_path = Path(res["compact_file"])
    full_path = Path(res["full_file"])
    assert compact_path.is_file()
    assert full_path.is_file()
    assert compact_path.stat().st_size < full_path.stat().st_size

    # Formatted report check
    report = context_pruning.format_compaction_report(res)
    assert "# Session Compactor Report: `test-sess-01`" in report
    assert "Token Compression Ratio" in report
    assert "**Tier Anchor (Prompt Cache Anchor):** 1 initial step(s) preserved 100% verbatim" in report
    assert "**Tier 0 (Recent Fidelity):** 4 latest steps preserved 100% verbatim" in report
    assert "Anchor Preserved:" in report


def test_session_compaction_status_and_prune(tmp_path):
    # Setup folded sessions
    folded_dir = tmp_path / ".autoharness" / "folded"
    folded_dir.mkdir(parents=True)

    state_file = folded_dir / "folded_sessions.json"
    state_file.write_text(json.dumps({
        "last_updated": "2026-11-15T18:00:00Z",
        "sessions": {
            "sess-1": {
                "session_id": "sess-1",
                "compact_file": str(folded_dir / "sess-1.compact.jsonl"),
                "full_file": str(folded_dir / "sess-1.full.jsonl"),
                "original_steps": 20,
                "compact_steps": 8,
                "estimated_original_tokens": 10000,
                "estimated_compact_tokens": 3000,
                "tokens_saved": 7000,
                "compression_ratio": 70.0,
            },
            "sess-2": {
                "session_id": "sess-2",
                "compact_file": str(folded_dir / "sess-2.compact.jsonl"),
                "full_file": str(folded_dir / "sess-2.full.jsonl"),
                "original_steps": 15,
                "compact_steps": 6,
                "estimated_original_tokens": 8000,
                "estimated_compact_tokens": 2500,
                "tokens_saved": 5500,
                "compression_ratio": 68.7,
            }
        }
    }), encoding="utf-8")

    # Create dummy files
    (folded_dir / "sess-1.compact.jsonl").write_text("compact 1", "utf-8")
    (folded_dir / "sess-1.full.jsonl").write_text("full 1", "utf-8")
    (folded_dir / "sess-2.compact.jsonl").write_text("compact 2", "utf-8")
    (folded_dir / "sess-2.full.jsonl").write_text("full 2", "utf-8")

    # Status overall
    st_all = context_pruning.get_session_compaction_status(tmp_path)
    assert st_all["status"] == "active"
    assert st_all["sessions_count"] == 2
    assert st_all["total_tokens_saved"] == 12500

    # Status specific session
    st_s1 = context_pruning.get_session_compaction_status(tmp_path, session_id="sess-1")
    assert st_s1["status"] == "found"
    assert st_s1["session"]["compression_ratio"] == 70.0

    # Prune keeping latest 1
    prune_res = context_pruning.prune_folded_sessions(tmp_path, keep_latest=1)
    assert prune_res["status"] == "ok"
    assert prune_res["removed_files"] >= 1

    # Verify registry pruned
    st_pruned = context_pruning.get_session_compaction_status(tmp_path)
    assert st_pruned["sessions_count"] == 1


def test_cmd_compact_cli(project, capsys):
    from autoharness.cli import cmd_compact
    import types

    # Prepare sample session transcript
    steps = [
        {"step": i, "content": f"Step {i} execution content with details."}
        for i in range(1, 12)
    ]
    raw_path = project.root / "sample.jsonl"
    raw_path.write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")

    # 1. Run compaction via CLI
    args_run = types.SimpleNamespace(
        action="run",
        target=str(raw_path),
        keep_last=3,
        epoch_size=5,
        json=True,
    )
    code = cmd_compact(args_run, project, None)
    assert code == 0
    captured = capsys.readouterr()
    res = json.loads(captured.out)
    assert res["status"] == "ok"
    assert res["original_steps"] == 11
    assert res["compact_steps"] < 11

    # 2. Check status via CLI
    args_status = types.SimpleNamespace(
        action="status",
        target=None,
        json=True,
    )
    code = cmd_compact(args_status, project, None)
    assert code == 0
    captured = capsys.readouterr()
    st = json.loads(captured.out)
    assert st["sessions_count"] >= 1

    # 3. Prune via CLI
    args_prune = types.SimpleNamespace(
        action="prune",
        keep_last=0,
        json=True,
    )
    code = cmd_compact(args_prune, project, None)
    assert code == 0
    captured = capsys.readouterr()
    pruned = json.loads(captured.out)
    assert pruned["status"] == "ok"


def test_gateway_compact_session_tools(project):
    sent = []
    gw = gateway.Gateway(project.root, sent.append, lambda line: None)
    gw.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    gw.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    steps = [
        {"step": i, "content": f"Step {i} with large logs: " + ("x" * 200)}
        for i in range(1, 15)
    ]

    # Test harness_compact_session
    res = gw.handle({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "harness_compact_session",
            "arguments": {
                "steps": steps,
                "session_id": "gw-compact-test",
                "keep_last": 3,
                "epoch_size": 5,
                "raw_json": True,
            }
        }
    })

    assert not res["result"].get("isError")
    data = json.loads(res["result"]["content"][0]["text"])
    assert data["status"] == "ok"
    assert data["session_id"] == "gw-compact-test"
    assert data["tiers"]["recent_steps"] == 3
    assert data["original_steps"] == 14

    # Test harness_session_status
    st_res = gw.handle({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "harness_session_status",
            "arguments": {
                "session_id": "gw-compact-test",
                "raw_json": True,
            }
        }
    })

    assert not st_res["result"].get("isError")
    st_data = json.loads(st_res["result"]["content"][0]["text"])
    assert st_data["status"] == "found"
    assert st_data["session_id"] == "gw-compact-test"

    # Test harness_check_prompt_cache via gateway
    cache_res = gw.handle({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "harness_check_prompt_cache",
            "arguments": {
                "system_prompt": "You are a helpful assistant. Today is 2026-11-19 and requestId=abc-123.",
                "tools": [{"name": "zebra"}, {"name": "alpha"}],
                "raw_json": True,
            }
        }
    })
    assert not cache_res["result"].get("isError")
    cache_data = json.loads(cache_res["result"]["content"][0]["text"])
    assert cache_data["status"] == "fail"
    rule_ids = [v["rule_id"] for v in cache_data["violations"]]
    assert "AP-1" in rule_ids
    assert "AP-2" in rule_ids


def test_prompt_cache_invariants_comprehensive():
    # 1. Clean valid inputs
    clean_prompt = "You are an expert engineer adhering to strict invariants. Do not execute unverified commands."
    clean_tools = [{"name": "build_project"}, {"name": "run_tests"}, {"name": "verify_diff"}]
    clean_steps = [
        {"step": 0, "type": "system", "content": clean_prompt, "folded": False},
        {"step": 1, "type": "user", "content": "Analyze project architecture", "reasoning_effort": "high"},
        {"step": 2, "type": "assistant", "content": "Result outline", "reasoning_effort": "high"},
    ]
    res_clean = context_pruning.check_prompt_cache_invariants(
        steps=clean_steps,
        system_prompt=clean_prompt,
        tools=clean_tools,
    )
    assert res_clean["status"] == "pass"
    assert res_clean["cache_friendly"] is True
    assert res_clean["total_violations"] == 0

    # 2. Mutated anchor (AP-5)
    bad_steps_anchor = [
        {"step": 0, "type": "EPOCH_MILESTONE", "folded": True, "content": "Folded milestone"},
        {"step": 1, "type": "user", "content": "Next turn"},
    ]
    res_anchor = context_pruning.check_prompt_cache_invariants(steps=bad_steps_anchor)
    assert res_anchor["status"] == "fail"
    assert any(v["rule_id"] == "AP-5" for v in res_anchor["violations"])

    # 3. Effort toggling (AP-15)
    bad_steps_effort = [
        {"step": 0, "type": "system", "content": clean_prompt, "folded": False},
        {"step": 1, "type": "user", "reasoning_effort": "low"},
        {"step": 2, "type": "assistant", "reasoning_effort": "high"},
    ]
    res_effort = context_pruning.check_prompt_cache_invariants(steps=bad_steps_effort)
    assert any(v["rule_id"] == "AP-15" for v in res_effort["violations"])


