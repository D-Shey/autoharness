"""Self-Supervised Code Refinement & Regression Sentinel with Rollback Guard.

Inspired by Google Research Harness² (arXiv:2610.14224) and AutoHarness Drive loop:
- Captures pre-flight working tree snapshots and baseline metrics.
- Verifies post-flight test execution and AST Anti-Shim policies.
- Automatically rolls back destructive regressions or fake stubs.
- Provides unified CLI and Gateway MCP interfaces.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from typing import Any

from autoharness import codemap, evidence
from autoharness.env import HarnessError

SENTINEL_STATE_FILE = "sentinel.json"
SNAPSHOTS_DIR = "snapshots"
SENTINEL_EXCLUDE_DIRS = {
    ".git", ".autoharness", ".venv", "venv", "node_modules",
    "__pycache__", ".pytest_cache", "build", "dist", ".egg-info", ".playwright-mcp"
}
_lock = threading.RLock()


def _get_utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_git(cmd: list[str], cwd: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *cmd],
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            check=False
        )
        return proc.returncode, proc.stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def compute_tree_state(project_root: Path, base_sha: str = "") -> str:
    """Compute fingerprint of working tree state with fallback for non-git trees."""
    if not base_sha:
        _, head = _run_git(["rev-parse", "HEAD"], project_root)
        base_sha = head or "HEAD"
    try:
        return evidence.tree_state(project_root, base_sha)
    except (HarnessError, OSError, subprocess.SubprocessError):
        h = hashlib.sha256()
        for root_dir, dirs, files in os.walk(project_root):
            dirs[:] = [d for d in dirs if d not in SENTINEL_EXCLUDE_DIRS]
            for f in sorted(files):
                f_path = Path(root_dir) / f
                try:
                    rel = f_path.relative_to(project_root).as_posix()
                    st = f_path.stat()
                    if st.st_size < 10_000_000:
                        content_digest = hashlib.sha256(f_path.read_bytes()).hexdigest()
                        h.update(f"{rel}\0{content_digest}\n".encode("utf-8"))
                    else:
                        h.update(f"{rel}\0large:{st.st_size}\n".encode("utf-8"))
                except OSError:
                    pass
        return h.hexdigest()


def capture_snapshot(project_root: Path, label: str = "") -> dict[str, Any]:
    """Capture snapshot of current repository state for safe rollback."""
    with _lock:
        base_dir = project_root / ".autoharness" / SNAPSHOTS_DIR
        base_dir.mkdir(parents=True, exist_ok=True)

        now_str = _get_utc_now()
        ts_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
        snapshot_id = f"snap-{ts_id}"

        # Git HEAD
        _, head_sha = _run_git(["rev-parse", "HEAD"], project_root)
        _, status_out = _run_git(["status", "--porcelain"], project_root)

        modified: list[str] = []
        untracked: list[str] = []
        for line in status_out.splitlines():
            line = line.strip()
            if not line:
                continue
            status_code = line[:2]
            filename = line[3:].strip()
            if status_code.startswith("??"):
                untracked.append(filename)
            else:
                modified.append(filename)

        tree_hash = compute_tree_state(project_root, head_sha)

        all_files: dict[str, str] = {}
        backup_dir = base_dir / f"{snapshot_id}_backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for root_dir, dirs, files in os.walk(project_root):
            dirs[:] = [d for d in dirs if d not in SENTINEL_EXCLUDE_DIRS]
            for f in sorted(files):
                f_path = Path(root_dir) / f
                try:
                    st = f_path.stat()
                    rel = f_path.relative_to(project_root).as_posix()
                    if st.st_size < 10_000_000:
                        content = f_path.read_bytes()
                        all_files[rel] = hashlib.sha256(content).hexdigest()
                        if st.st_size < 2_000_000:
                            b_file = backup_dir / rel
                            b_file.parent.mkdir(parents=True, exist_ok=True)
                            b_file.write_bytes(content)
                    else:
                        all_files[rel] = f"large:{st.st_size}"
                except OSError:
                    pass

        snapshot_data = {
            "id": snapshot_id,
            "label": label or "baseline",
            "timestamp": now_str,
            "head_sha": head_sha,
            "tree_state": tree_hash,
            "modified_files": modified,
            "untracked_files": untracked,
            "all_files": all_files,
        }

        # Atomically write snapshot file
        snap_file = base_dir / f"{snapshot_id}.json"
        temp_fd, temp_path = tempfile.mkstemp(dir=base_dir, prefix=f"{snapshot_id}-", suffix=".tmp")
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(snapshot_data, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, snap_file)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        # Update latest pointer in sentinel.json
        state_file = project_root / ".autoharness" / SENTINEL_STATE_FILE
        state_data = {
            "active_snapshot": snapshot_data,
            "last_updated": now_str,
        }
        state_fd, state_tmp = tempfile.mkstemp(dir=state_file.parent, prefix="sentinel-", suffix=".tmp")
        try:
            with os.fdopen(state_fd, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2, ensure_ascii=False)
            os.replace(state_tmp, state_file)
        finally:
            if os.path.exists(state_tmp):
                try:
                    os.unlink(state_tmp)
                except OSError:
                    pass

        return snapshot_data


def rollback_to_snapshot(project_root: Path, snapshot_id: str | None = None) -> dict[str, Any]:
    """Roll back working directory to specified or latest snapshot."""
    with _lock:
        now_str = _get_utc_now()
        base_dir = project_root / ".autoharness" / SNAPSHOTS_DIR

        target_snap: dict[str, Any] | None = None
        if snapshot_id:
            snap_file = base_dir / f"{snapshot_id}.json"
            if snap_file.is_file():
                try:
                    target_snap = json.loads(snap_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    target_snap = None
        else:
            state_file = project_root / ".autoharness" / SENTINEL_STATE_FILE
            if state_file.is_file():
                try:
                    data = json.loads(state_file.read_text(encoding="utf-8"))
                    target_snap = data.get("active_snapshot")
                except (OSError, json.JSONDecodeError):
                    target_snap = None

        # Fail-safe guard: Never wipe files if no valid snapshot is available
        if not target_snap:
            current_tree = compute_tree_state(project_root)
            return {
                "status": "error",
                "error": f"Snapshot not found: {snapshot_id}" if snapshot_id else "No active baseline snapshot found",
                "target_snapshot": None,
                "timestamp": now_str,
                "restored_files": [],
                "removed_files": [],
                "tree_state_matches_baseline": False,
                "current_tree_state": current_tree,
            }

        restored_files: list[str] = []
        removed_files: list[str] = []

        # 1. Restore tracked modifications via git
        ret, _ = _run_git(["restore", "."], project_root)
        if ret == 0:
            restored_files.append("tracked modifications reverted via git restore")

        # 2. Clean untracked files not in baseline
        snap_all = target_snap.get("all_files", {})
        for root_dir, dirs, files in os.walk(project_root):
            dirs[:] = [d for d in dirs if d not in SENTINEL_EXCLUDE_DIRS]
            for f in files:
                f_path = Path(root_dir) / f
                try:
                    rel = f_path.relative_to(project_root).as_posix()
                    if rel not in snap_all:
                        f_path.unlink()
                        removed_files.append(rel)
                except OSError:
                    pass

        # 3. Restore any modified files from backup
        snap_id = target_snap.get("id")
        b_dir = base_dir / f"{snap_id}_backup"
        if b_dir.is_dir():
            for root_dir, dirs, files in os.walk(b_dir):
                for f in files:
                    b_path = Path(root_dir) / f
                    try:
                        rel = b_path.relative_to(b_dir).as_posix()
                        dest = project_root / rel
                        if b_path.stat().st_size < 2_000_000:
                            content = b_path.read_bytes()
                            if not dest.is_file() or hashlib.sha256(dest.read_bytes()).hexdigest() != snap_all.get(rel):
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                dest.write_bytes(content)
                                if rel not in restored_files:
                                    restored_files.append(rel)
                    except OSError:
                        pass

        current_tree = compute_tree_state(project_root, target_snap.get("head_sha", "HEAD"))
        matched_tree = (current_tree == target_snap.get("tree_state"))

        result = {
            "status": "ok",
            "timestamp": now_str,
            "target_snapshot": target_snap.get("id"),
            "restored_files": restored_files,
            "removed_files": removed_files,
            "tree_state_matches_baseline": matched_tree,
            "current_tree_state": current_tree,
        }

        # Update sentinel state
        state_file = project_root / ".autoharness" / SENTINEL_STATE_FILE
        if state_file.parent.is_dir():
            state_data = {
                "active_snapshot": target_snap,
                "last_rollback": result,
                "last_updated": now_str,
            }
            s_fd, s_tmp = tempfile.mkstemp(dir=state_file.parent, prefix="sentinel-", suffix=".tmp")
            try:
                with os.fdopen(s_fd, "w", encoding="utf-8") as f:
                    json.dump(state_data, f, indent=2, ensure_ascii=False)
                os.replace(s_tmp, state_file)
            finally:
                if os.path.exists(s_tmp):
                    try:
                        os.unlink(s_tmp)
                    except OSError:
                        pass

        return result


def run_sentinel_verification(
    project_root: Path,
    test_command: str = "pytest",
    auto_rollback: bool = False,
    check_shims: bool = True,
    timeout_sec: float = 600.0,
) -> dict[str, Any]:
    """Execute pre/post flight sentinel verification with automated rollback guard."""
    now_str = _get_utc_now()
    issues: list[dict[str, Any]] = []

    # 1. AST Anti-Shim Check
    if check_shims:
        _, status_out = _run_git(["status", "--porcelain"], project_root)
        py_files_to_check: list[Path] = []
        for line in status_out.splitlines():
            line = line.strip()
            if not line:
                continue
            path_str = line[3:].strip()
            if path_str.endswith(".py"):
                parts = Path(path_str).parts
                if any(p in SENTINEL_EXCLUDE_DIRS for p in parts):
                    continue
                full_p = project_root / path_str
                if full_p.is_file():
                    py_files_to_check.append(full_p)

        for p in py_files_to_check:
            try:
                code_text = p.read_text(encoding="utf-8", errors="replace")
                violations = codemap.detect_anti_shims(code_text, filename=p.as_posix())
                issues.extend(violations)
            except OSError:
                pass

    anti_shims_passed = len(issues) == 0

    # 2. Test Command Execution
    proc_output = ""
    proc_code = 0
    try:
        proc = subprocess.run(
            test_command,
            shell=True,
            cwd=project_root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_sec,
            check=False,
        )
        proc_output = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
        proc_code = proc.returncode
    except subprocess.TimeoutExpired:
        proc_code = 124
        proc_output = f"Sentinel execution timed out after {timeout_sec}s"
    except (OSError, subprocess.SubprocessError) as e:
        proc_code = 1
        proc_output = f"Failed to execute sentinel command: {e}"

    tests_passed = (proc_code == 0)
    overall_passed = tests_passed and anti_shims_passed

    rollback_result = None
    if not overall_passed and auto_rollback:
        rollback_result = rollback_to_snapshot(project_root)

    res: dict[str, Any] = {
        "status": "passed" if overall_passed else "failed",
        "timestamp": now_str,
        "test_command": test_command,
        "tests_passed": tests_passed,
        "test_exit_code": proc_code,
        "anti_shims_passed": anti_shims_passed,
        "anti_shim_issues_count": len(issues),
        "anti_shim_issues": issues,
        "auto_rollback_triggered": bool(rollback_result is not None),
        "rollback": rollback_result,
        "output_tail": "\n".join(proc_output.splitlines()[-25:]),
    }

    # Record last run in sentinel state
    with _lock:
        state_file = project_root / ".autoharness" / SENTINEL_STATE_FILE
        if state_file.parent.is_dir():
            try:
                current_state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
            except (OSError, json.JSONDecodeError):
                current_state = {}
            current_state["last_verification"] = res
            current_state["last_updated"] = now_str

            s_fd, s_tmp = tempfile.mkstemp(dir=state_file.parent, prefix="sentinel-", suffix=".tmp")
            try:
                with os.fdopen(s_fd, "w", encoding="utf-8") as f:
                    json.dump(current_state, f, indent=2, ensure_ascii=False)
                os.replace(s_tmp, state_file)
            finally:
                if os.path.exists(s_tmp):
                    try:
                        os.unlink(s_tmp)
                    except OSError:
                        pass

        # Also update drive_state.json if present
        drive_file = project_root / ".autoharness" / "drive_state.json"
        if drive_file.is_file():
            d_tmp = None
            try:
                drive_data = json.loads(drive_file.read_text(encoding="utf-8"))
                drive_data["last_sentinel_run"] = res
                drive_data["last_updated"] = now_str
                d_fd, d_tmp = tempfile.mkstemp(dir=drive_file.parent, prefix="drive-", suffix=".tmp")
                with os.fdopen(d_fd, "w", encoding="utf-8") as f:
                    json.dump(drive_data, f, indent=2, ensure_ascii=False)
                os.replace(d_tmp, drive_file)
            except (OSError, json.JSONDecodeError):
                pass
            finally:
                if d_tmp and os.path.exists(d_tmp):
                    try:
                        os.unlink(d_tmp)
                    except OSError:
                        pass

    return res


def get_sentinel_status(project_root: Path) -> dict[str, Any]:
    """Retrieve current sentinel state and active baseline snapshot."""
    state_file = project_root / ".autoharness" / SENTINEL_STATE_FILE
    if not state_file.is_file():
        return {
            "status": "uninitialized",
            "active_snapshot": None,
            "last_verification": None,
        }
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return {
            "status": "active" if data.get("active_snapshot") else "idle",
            **data,
        }
    except (OSError, json.JSONDecodeError):
        return {
            "status": "error",
            "error": "Failed to parse sentinel state file",
        }


def format_sentinel_report(result: dict[str, Any]) -> str:
    """Format sentinel verification result into Markdown report."""
    status = result.get("status", "unknown").upper()
    cmd = result.get("test_command", "")
    t_pass = "PASSED" if result.get("tests_passed") else "FAILED"
    s_pass = "PASSED" if result.get("anti_shims_passed") else "FAILED"
    rolled_back = result.get("auto_rollback_triggered", False)
    tail = result.get("output_tail", "")

    lines = [
        f"# Regression Sentinel Report: {status}",
        "",
        "## Summary",
        f"- **Test Command:** `{cmd}` ({t_pass}, exit code: {result.get('test_exit_code')})",
        f"- **AST Anti-Shim Sentinel:** {s_pass} ({result.get('anti_shim_issues_count', 0)} issue(s))",
        f"- **Automatic Rollback Triggered:** `{rolled_back}`",
    ]

    if rolled_back and result.get("rollback"):
        rb = result["rollback"]
        lines.extend([
            "",
            "## Rollback Details",
            f"- **Target Snapshot:** `{rb.get('target_snapshot')}`",
            f"- **Restored Files:** {len(rb.get('restored_files', []))} item(s)",
            f"- **Removed Untracked:** {len(rb.get('removed_files', []))} item(s)",
            f"- **Tree State Reverted:** `{rb.get('tree_state_matches_baseline')}`",
        ])

    issues = result.get("anti_shim_issues", [])
    if issues:
        lines.extend(["", "## Detected Anti-Shim Violations"])
        for iss in issues[:10]:
            lines.append(f"- `[{iss.get('severity')}]` {iss.get('file')}:{iss.get('line')} in `{iss.get('symbol')}`: {iss.get('message')}")

    if tail:
        lines.extend([
            "",
            "## Execution Tail",
            "```text",
            tail,
            "```",
        ])

    return "\n".join(lines).strip()
