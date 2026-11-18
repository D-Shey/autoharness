"""Tests for Self-Supervised Code Refinement & Regression Sentinel with Rollback Guard."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import pytest

from autoharness import sentinel, cli, gateway


def test_sentinel_capture_snapshot(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".autoharness").mkdir()
    (repo / "file1.txt").write_text("initial content", encoding="utf-8")

    snap = sentinel.capture_snapshot(repo, label="pre-flight")
    assert snap["id"].startswith("snap-")
    assert snap["label"] == "pre-flight"
    assert "timestamp" in snap
    assert "tree_state" in snap

    # Check persistence
    snap_file = repo / ".autoharness" / "snapshots" / f"{snap['id']}.json"
    assert snap_file.is_file()
    state_file = repo / ".autoharness" / "sentinel.json"
    assert state_file.is_file()

    status = sentinel.get_sentinel_status(repo)
    assert status["status"] == "active"
    assert status["active_snapshot"]["id"] == snap["id"]


def test_sentinel_rollback_failsafe_without_snapshot(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".autoharness").mkdir()
    important_file = repo / "vital_code.py"
    important_file.write_text("print('cannot delete me')\n", encoding="utf-8")

    # 1. Rollback when no snapshots exist at all
    res = sentinel.rollback_to_snapshot(repo, snapshot_id=None)
    assert res["status"] == "error"
    assert "No active baseline snapshot found" in res["error"]
    assert important_file.is_file()

    # 2. Rollback with non-existent snapshot ID
    res_bad = sentinel.rollback_to_snapshot(repo, snapshot_id="snap-99999999-999999")
    assert res_bad["status"] == "error"
    assert "Snapshot not found" in res_bad["error"]
    assert important_file.is_file()


def test_sentinel_rollback(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".autoharness").mkdir()
    tracked = repo / "module.py"
    tracked.write_text("def work(): return 42\n", encoding="utf-8")

    # Capture snapshot
    snap = sentinel.capture_snapshot(repo, label="stable")

    # Introduce changes: new untracked file + modify tracked file
    untracked = repo / "bad_artifact.tmp"
    untracked.write_text("junk", encoding="utf-8")
    assert untracked.exists()

    # Trigger rollback
    res = sentinel.rollback_to_snapshot(repo, snapshot_id=snap["id"])
    assert res["status"] == "ok"
    assert res["target_snapshot"] == snap["id"]
    # Untracked junk file should be cleaned up
    assert not untracked.exists()


def test_sentinel_verification_success(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".autoharness").mkdir()

    res = sentinel.run_sentinel_verification(
        repo,
        test_command="python -c \"import sys; sys.exit(0)\"",
        auto_rollback=False,
        check_shims=False
    )
    assert res["status"] == "passed"
    assert res["tests_passed"] is True
    assert res["test_exit_code"] == 0
    assert res["auto_rollback_triggered"] is False

    report = sentinel.format_sentinel_report(res)
    assert "# Regression Sentinel Report: PASSED" in report


def test_sentinel_verification_failure_with_auto_rollback(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".autoharness").mkdir()

    snap = sentinel.capture_snapshot(repo, label="pre-fail")
    junk = repo / "broken.py"
    junk.write_text("raise RuntimeError", encoding="utf-8")

    res = sentinel.run_sentinel_verification(
        repo,
        test_command="python -c \"import sys; sys.exit(1)\"",
        auto_rollback=True,
        check_shims=False
    )
    assert res["status"] == "failed"
    assert res["tests_passed"] is False
    assert res["test_exit_code"] == 1
    assert res["auto_rollback_triggered"] is True
    assert res["rollback"] is not None
    # Junk file was created after snapshot, so auto-rollback cleans it
    assert not junk.exists()


def test_sentinel_anti_shim_rejection_end_to_end(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".autoharness").mkdir()

    # Initialize a git repo to test git-status based anti-shim scanning
    subprocess.run(["git", "init", "."], cwd=repo, capture_output=True, check=False)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, capture_output=True, check=False)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, check=False)

    base_f = repo / "valid.py"
    base_f.write_text("def real():\n    return 1 + 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=False)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=False)

    snap = sentinel.capture_snapshot(repo, label="baseline")

    # Add a fake stub
    shim_f = repo / "fake.py"
    shim_f.write_text("def fake():\n    pass\n", encoding="utf-8")

    res = sentinel.run_sentinel_verification(
        repo,
        test_command="python -c \"import sys; sys.exit(0)\"",
        auto_rollback=True,
        check_shims=True
    )
    assert res["status"] == "failed"
    assert res["anti_shims_passed"] is False
    assert res["anti_shim_issues_count"] >= 1
    assert res["auto_rollback_triggered"] is True
    # Fake file should be rolled back and cleaned up
    assert not shim_f.exists()


def test_cli_sentinel(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".autoharness").mkdir()

    class FakeProject:
        root = repo

    # 1. Snapshot
    class FakeArgsSnapshot:
        action = "snapshot"
        label = "cli-test"
        json = True

    ret = cli.cmd_sentinel(FakeArgsSnapshot(), FakeProject(), None)
    assert ret == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["snapshot"]["label"] == "cli-test"
    snap_id = out["snapshot"]["id"]

    # 2. Status
    class FakeArgsStatus:
        action = "status"
        json = True

    ret = cli.cmd_sentinel(FakeArgsStatus(), FakeProject(), None)
    assert ret == 0
    stat = json.loads(capsys.readouterr().out)
    assert stat["status"] == "active"
    assert stat["active_snapshot"]["label"] == "cli-test"

    # 3. Run with --pre-flight
    class FakeArgsRunPreFlight:
        action = "run"
        pre_flight = True
        test_command = "python -c \"exit(0)\""
        auto_rollback = False
        no_shims = True
        json = True

    ret = cli.cmd_sentinel(FakeArgsRunPreFlight(), FakeProject(), None)
    assert ret == 0
    run_pre = json.loads(capsys.readouterr().out)
    assert run_pre["status"] == "passed"

    # 4. Rollback
    class FakeArgsRollback:
        action = "rollback"
        snapshot_id = snap_id
        json = True

    ret = cli.cmd_sentinel(FakeArgsRollback(), FakeProject(), None)
    assert ret == 0
    rb_out = json.loads(capsys.readouterr().out)
    assert rb_out["status"] == "ok"
    assert rb_out["target_snapshot"] == snap_id


def test_gateway_sentinel_tools(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".autoharness").mkdir()

    gw = gateway.Gateway(repo, write=lambda s: None, log=lambda s: None)

    # 1. Snapshot tool
    res_snap = gw._call({
        "name": "harness_sentinel_snapshot",
        "arguments": {"label": "gw-checkpoint"}
    })
    snap_data = json.loads(res_snap["content"][0]["text"])
    assert snap_data["label"] == "gw-checkpoint"
    snap_id = snap_data["id"]

    # 2. Verify tool
    res_verify = gw._call({
        "name": "harness_sentinel_verify",
        "arguments": {
            "test_command": "python -c \"exit(0)\"",
            "check_shims": False,
            "raw_json": True
        }
    })
    v_data = json.loads(res_verify["content"][0]["text"])
    assert v_data["status"] == "passed"

    # 3. Rollback tool
    res_rb = gw._call({
        "name": "harness_sentinel_rollback",
        "arguments": {"snapshot_id": snap_id}
    })
    rb_data = json.loads(res_rb["content"][0]["text"])
    assert rb_data["status"] == "ok"
    assert rb_data["target_snapshot"] == snap_id


def test_sentinel_bounds_walk_and_ignores_dot_and_os_dirs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".autoharness").mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".cache").mkdir()
    (repo / ".cache" / "junk.bin").write_text("cached", encoding="utf-8")
    (repo / "AppData" / "Local").mkdir(parents=True)
    (repo / "AppData" / "Local" / "state.db").write_text("db", encoding="utf-8")

    # Deep nested directory beyond max_depth=3
    deep = repo / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "too_deep.py").write_text("y = 2\n", encoding="utf-8")

    files = sentinel._iter_project_files(repo, max_files=50, max_depth=3)
    rels = {f.relative_to(repo).as_posix() for f in files}
    assert "src/main.py" in rels
    assert ".cache/junk.bin" not in rels
    assert "AppData/Local/state.db" not in rels
    assert "a/b/c/d/too_deep.py" not in rels

    # Snapshot + rollback should not touch ignored directories
    snap = sentinel.capture_snapshot(repo, label="bounded")
    assert ".cache/junk.bin" not in snap["all_files"]
    assert "AppData/Local/state.db" not in snap["all_files"]
    (repo / ".cache" / "new_cache.bin").write_text("keep", encoding="utf-8")
    sentinel.rollback_to_snapshot(repo, snapshot_id=snap["id"])
    assert (repo / ".cache" / "new_cache.bin").is_file()


def test_sentinel_git_repo_preserves_gitignored_files_on_rollback(tmp_path):
    repo = tmp_path / "git_repo"
    repo.mkdir()
    (repo / ".autoharness").mkdir()
    subprocess.run(["git", "init", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, check=True)

    (repo / ".gitignore").write_text("ignored_dir/\n", encoding="utf-8")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)

    (repo / "ignored_dir").mkdir()
    (repo / "ignored_dir" / "local_secret.txt").write_text("keep me", encoding="utf-8")

    snap = sentinel.capture_snapshot(repo, label="git-baseline")
    assert "ignored_dir/local_secret.txt" not in snap["all_files"]
    (repo / "bad_untracked.py").write_text("bad = 1\n", encoding="utf-8")

    rb = sentinel.rollback_to_snapshot(repo, snapshot_id=snap["id"])
    assert rb["status"] == "ok"
    assert not (repo / "bad_untracked.py").exists()
    assert (repo / "ignored_dir" / "local_secret.txt").is_file()
