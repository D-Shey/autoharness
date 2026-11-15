"""Tests for Dynamic Memory Drift Detection & Knowledge Reconciliation (Google Research Harness² model)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from autoharness import gateway, memory
from autoharness.cli import main


def test_extract_code_references():
    content = """
    We fixed the bug in `src/autoharness/cli.py` at line 140.
    Referenced implementation in `tests/test_swarm.py:45` for class `SwarmTask`.
    Also see https://example.com/docs.py and function `verify_contract`.
    The method was removed and function has passed tests.
    """
    meta = {"file": "src/autoharness/swarm.py", "symbol": "validate_task_barriers"}
    paths, symbols = memory.extract_code_references(content, meta)

    assert "src/autoharness/cli.py" in paths
    assert "tests/test_swarm.py" in paths
    assert "src/autoharness/swarm.py" in paths
    assert "https://example.com/docs.py" not in paths

    assert "SwarmTask" in symbols
    assert "verify_contract" in symbols
    assert "validate_task_barriers" in symbols
    assert "was" not in symbols
    assert "has" not in symbols


def test_collect_file_ast_symbols(tmp_path):
    py_file = tmp_path / "module.py"
    py_file.write_text(
        """
GLOBAL_VAR = 123

class Engine:
    def start(self):
        pass

def helper_func():
    return True
""",
        encoding="utf-8",
    )

    symbols = memory.collect_file_ast_symbols(py_file)
    assert "GLOBAL_VAR" in symbols
    assert "Engine" in symbols
    assert "start" in symbols
    assert "helper_func" in symbols


def test_detect_statement_polarity():
    pos = "Always verify subprocess exit codes before asserting completion"
    neg = "Never bypass sandbox boundaries under any circumstances"
    neu = "The quick brown fox jumps over the lazy dog"

    assert memory.detect_statement_polarity(pos) == "positive"
    assert memory.detect_statement_polarity(neg) == "negative"
    assert memory.detect_statement_polarity(neu) == "neutral"

    # Russian phrasings
    assert memory.detect_statement_polarity("Всегда подтверждай ввод команд") == "positive"
    assert memory.detect_statement_polarity("Никогда не делай git push без разрешения") == "negative"


def test_reconcile_memory_drift_healthy(tmp_path):
    # Setup valid workspace file
    src_file = tmp_path / "src" / "worker.py"
    src_file.parent.mkdir(parents=True)
    src_file.write_text("class Worker:\n    def execute(self):\n        return True\n", encoding="utf-8")

    con = memory.get_memory_db(tmp_path)
    memory.reconcile_and_store(
        con,
        kind="lesson",
        source="src/worker.py",
        content="Use `src/worker.py` function `execute` to run tasks safely",
        title="Worker Execution",
        metadata={"file": "src/worker.py", "symbol": "execute"},
        doc_id="lesson_worker",
    )
    con.close()

    res = memory.reconcile_memory_drift(tmp_path)
    assert res["status"] == "ok"
    assert res["scanned_docs"] == 1
    assert res["valid_count"] == 1
    assert res["drifted_count"] == 0
    assert res["orphaned_count"] == 0
    assert res["drift_rate"] == 0.0
    assert len(res["drifted_items"]) == 0

    report = memory.format_memory_reconcile_report(res)
    assert "`HEALTHY`" in report
    assert "No knowledge drift detected" in report


def test_reconcile_memory_drift_detects_missing_file_and_symbol(tmp_path):
    # Setup real file with different symbol
    py_file = tmp_path / "src" / "core.py"
    py_file.parent.mkdir(parents=True)
    py_file.write_text("def modern_symbol(): pass\n", encoding="utf-8")

    con = memory.get_memory_db(tmp_path)
    # 1. Stale symbol
    memory.reconcile_and_store(
        con,
        kind="lesson",
        source="src/core.py",
        content="Deprecated class `OldEngine` in `src/core.py`",
        title="Stale Symbol Lesson",
        metadata={"file": "src/core.py", "symbol": "OldEngine"},
        doc_id="lesson_stale_sym",
    )
    # 2. Missing file (orphaned)
    memory.reconcile_and_store(
        con,
        kind="ruling",
        source="rulings.json",
        content="Check obsolete `src/deleted_module.py` for legacy configs",
        title="Legacy Module Ruling",
        metadata={"file": "src/deleted_module.py"},
        doc_id="ruling_missing_file",
    )
    con.close()

    res = memory.reconcile_memory_drift(tmp_path)
    assert res["scanned_docs"] == 2
    assert res["drifted_count"] >= 1
    assert res["orphaned_count"] >= 1
    assert res["drift_rate"] > 0.0

    ids = {item["id"] for item in res["drifted_items"]}
    assert "lesson_stale_sym" in ids
    assert "ruling_missing_file" in ids

    report = memory.format_memory_reconcile_report(res)
    assert "`DRIFT_DETECTED`" in report
    assert "ruling_missing_file" in report


def test_reconcile_memory_drift_detects_contradictory_rulings(tmp_path):
    con = memory.get_memory_db(tmp_path)
    # Opposing directives on sandbox policy
    memory.reconcile_and_store(
        con,
        kind="ruling",
        source="rulings.json",
        content="Always require sandbox boundary isolation for external execution",
        title="Sandbox Strict Rule",
        doc_id="ruling_sandbox_require",
    )
    memory.reconcile_and_store(
        con,
        kind="ruling",
        source="rulings.json",
        content="Never require sandbox boundary isolation for external execution",
        title="Sandbox Permissive Rule",
        doc_id="ruling_sandbox_permit",
    )
    con.close()

    res = memory.reconcile_memory_drift(tmp_path)
    assert res["scanned_docs"] == 2
    items = {it["id"]: it for it in res["drifted_items"]}
    assert "ruling_sandbox_require" in items or "ruling_sandbox_permit" in items


def test_reconcile_memory_drift_prune_orphans(tmp_path):
    con = memory.get_memory_db(tmp_path)
    # 1. Valid doc
    memory.reconcile_and_store(
        con,
        kind="lesson",
        source="memory",
        content="Pure conceptual lesson without code paths",
        title="Conceptual Lesson",
        doc_id="doc_valid",
    )
    # 2. Orphaned doc
    memory.reconcile_and_store(
        con,
        kind="ruling",
        source="deleted.py",
        content="References completely non-existent file `src/vanished_file.py`",
        title="Vanished Ruling",
        metadata={"file": "src/vanished_file.py"},
        doc_id="doc_orphaned",
    )
    con.close()

    # Reconcile without prune
    dry_run = memory.reconcile_memory_drift(tmp_path, prune_orphans=False)
    assert dry_run["orphaned_count"] == 1
    assert dry_run["pruned_count"] == 0

    # Reconcile with prune
    pruned = memory.reconcile_memory_drift(tmp_path, prune_orphans=True)
    assert pruned["orphaned_count"] == 1
    assert pruned["pruned_count"] == 1

    # Verify database state
    con2 = memory.get_memory_db(tmp_path)
    rows = con2.execute("SELECT id FROM memory_docs").fetchall()
    remaining_ids = [r["id"] for r in rows]
    con2.close()

    assert "doc_valid" in remaining_ids
    assert "doc_orphaned" not in remaining_ids


def test_memory_reconcile_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AUTOHARNESS_PROJECT", str(tmp_path))

    con = memory.get_memory_db(tmp_path)
    memory.reconcile_and_store(
        con,
        kind="lesson",
        source="memory",
        content="CLI integration test lesson",
        title="CLI Lesson",
        doc_id="lesson_cli",
    )
    con.close()

    # 1. Markdown output
    ret = main(["--project", str(tmp_path), "memory", "reconcile"])
    assert ret == 0
    captured = capsys.readouterr().out
    assert "AutoHarness Knowledge Reconciliation" in captured

    # 2. JSON output
    ret_json = main(["--project", str(tmp_path), "memory", "reconcile", "--json"])
    assert ret_json == 0
    json_out = capsys.readouterr().out
    data = json.loads(json_out)
    assert data["status"] == "ok"
    assert data["scanned_docs"] == 1


def test_gateway_memory_reconcile_tool(tmp_path):
    con = memory.get_memory_db(tmp_path)
    memory.reconcile_and_store(
        con,
        kind="lesson",
        source="memory",
        content="Gateway integration test lesson",
        title="Gateway Lesson",
        doc_id="lesson_gw",
    )
    con.close()

    gw = gateway.Gateway(tmp_path, write=lambda msg: None, log=lambda msg: None)
    # Test Markdown report
    reply = gw._call({"name": "harness_memory_reconcile", "arguments": {"prune_orphans": False}})
    assert reply["content"][0]["type"] == "text"
    assert "Knowledge Reconciliation" in reply["content"][0]["text"]

    # Test raw JSON
    reply_json = gw._call({"name": "harness_memory_reconcile", "arguments": {"raw_json": True}})
    data = json.loads(reply_json["content"][0]["text"])
    assert data["status"] == "ok"
    assert data["scanned_docs"] == 1
