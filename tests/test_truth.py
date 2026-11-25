"""Unit and integration tests for Dormant Data Truth Engine & Delayed Postmortem."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from autoharness import followups, truth
from autoharness.env import Project


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_dormant_signals_detects_churn_and_recurring_followups(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Tester")

    target = repo / "src" / "flaky.py"
    target.parent.mkdir(parents=True)
    for i in range(4):
        target.write_text(f"def step():\n    return {i}\n" + "\n".join(f"x{k} = {k}" for k in range(5)), encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", f"churn {i}")

    proj = Project(repo, "claude")
    f1 = followups.add(proj, "Fix timeout retry in `src/flaky.py` webhook handler", task_id="t1")
    followups.close(proj, f1["id"], "patched", task_id="t1")
    f2 = followups.add(proj, "Recurring timeout retry failure in `src/flaky.py` webhook handler", task_id="t2")

    report = truth.analyze_dormant_signals(repo, max_commits=20)
    assert report["git_churn"]["available"] is True
    assert any(tf["path"] == "src/flaky.py" for tf in report["git_churn"]["thrashing_files"])
    assert report["followup_recurrence"]["recurring_count"] == 1
    assert report["followup_recurrence"]["recurring"][0]["returned_id"] == f2["id"]
    assert report["status"] == "warning"

    md = truth.format_truth_report(report)
    assert "Dormant Data Truth Engine Audit" in md
    assert "src/flaky.py" in md


def test_delayed_assumption_postmortem_lifecycle(tmp_path: Path):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    proj = Project(proj_root, "claude")

    # Task 1 records two assumptions
    t1 = proj.init_task("Build parser and cache", mode="light")
    recorded = truth.record_assumptions(
        proj_root,
        t1.id,
        [
            "Parser handles all UTF-8 inputs in `src/parser.py`",
            "Cache invalidation never needs locks in `src/cache.py`",
        ],
        touched_files=["src/parser.py", "src/cache.py"],
    )
    assert len(recorded) == 2
    t1.teardown("keep")

    # Wait minimally so subsequent task timestamp is >= recorded_epoch
    time.sleep(0.02)
    t2 = proj.init_task("Add CLI flag", mode="light")
    t2.teardown("keep")

    # Followup arrives challenging/falsifying the cache assumption twice
    followups.add(proj, "Race condition in `src/cache.py` invalidation during concurrent writes", task_id=t2.id)
    followups.add(proj, "Corrupt cache entry in `src/cache.py` under load", task_id=t2.id)

    pm = truth.run_postmortem(proj_root, min_subsequent_tasks=1)
    assert pm["total"] == 2
    by_text = {i["text"]: i for i in pm["items"]}
    assert by_text["Parser handles all UTF-8 inputs in `src/parser.py`"]["status"] == "validated"
    assert by_text["Cache invalidation never needs locks in `src/cache.py`"]["status"] == "falsified"

    rel = truth.relevant_assumptions(proj_root, "modify src/cache.py")
    assert any("cache.py" in r["text"] and r["status"] == "falsified" for r in rel)


def test_postmortem_ignores_same_task_and_preexisting_followups_and_surfaces_corrupt_json(tmp_path: Path):
    import json
    import pytest
    from autoharness import gateway
    from autoharness.env import HarnessError

    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    proj = Project(proj_root, "claude")

    # Pre-existing followup created 2 hours BEFORE the assumption (local time naive format like followups.add)
    old_t = proj.init_task("Old task", mode="light")
    old_t.teardown("keep")
    old_fup = followups.add(proj, "Old bug in `src/parser.py`", task_id=old_t.id)
    fups_path = proj_root / ".autoharness" / "followups.json"
    fups_raw = json.loads(fups_path.read_text("utf-8"))
    fups_raw["items"][0]["created"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 7200))
    fups_path.write_text(json.dumps(fups_raw, indent=2), encoding="utf-8")

    # Task 1 records assumption AND adds a followup inside the SAME task -> both must be excluded
    t1 = proj.init_task("Build parser", mode="light")
    truth.record_assumptions(proj_root, t1.id, ["Parser stays lossless in `src/parser.py`"])
    followups.add(proj, "Same-task note about `src/parser.py`", task_id=t1.id)
    assert fups_raw["items"][0]["id"] == old_fup["id"]
    t1.teardown("keep")

    time.sleep(0.02)
    t2 = proj.init_task("Next task", mode="light")
    t2._update_info(approaches=[{"round": 1, "note": "pivot"}], reviews=[{"by": "critic", "compared": {"closer": "earlier"}}])
    t2.teardown("keep")

    pm = truth.run_postmortem(proj_root, min_subsequent_tasks=1)
    assert pm["items"][0]["status"] == "validated"
    assert truth.relevant_assumptions(proj_root, "parser", limit=-1) == []

    # analyze_dormant_signals surfaces regression_loop and rework_churn
    audit = truth.analyze_dormant_signals(proj_root, max_commits=5)
    cats = {f["category"] for f in audit["findings"]}
    assert "regression_loop" in cats and "rework_churn" in cats

    # Gateway validation on unknown action and boolean arg
    gw = gateway.Gateway(proj_root, lambda _m: None, lambda _l: None)
    assert gw._call({"name": "harness_truth_audit", "arguments": {"action": "unknown"}})["isError"] is True
    assert gw._call({"name": "harness_truth_audit", "arguments": {"action": "audit", "max_commits": True}})["isError"] is True

    # Preserves settled falsified/challenged signals even if git log window no longer sees old thrashing
    assump_path = truth.assumptions_path(proj_root)
    saved = json.loads(assump_path.read_text("utf-8"))
    saved[0]["status"] = "falsified"
    saved[0]["postmortem"] = {"evaluated_at": "2026-01-01T00:00:00Z", "subsequent_tasks": 1, "signals": ["Historical thrashing", "Historical bug"]}
    assump_path.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    pm_keep = truth.run_postmortem(proj_root, min_subsequent_tasks=1, max_commits=1)
    assert pm_keep["items"][0]["status"] == "falsified"
    assert len(pm_keep["items"][0]["postmortem"]["signals"]) == 2

    with pytest.raises(ValueError):
        truth.git_churn_stats(proj_root, max_commits=0)
    with pytest.raises(ValueError):
        truth.run_postmortem(proj_root, min_subsequent_tasks=-1)

    # Corrupt JSON in followups.json or assumptions.json must raise HarnessError instead of silent pass/overwrite
    fups_path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(HarnessError):
        truth.run_postmortem(proj_root)
    fups_path.write_text("[]", encoding="utf-8")
    truth.assumptions_path(proj_root).write_text("{corrupt", encoding="utf-8")
    with pytest.raises(HarnessError):
        truth.record_assumptions(proj_root, t2.id, ["Should fail cleanly"])
