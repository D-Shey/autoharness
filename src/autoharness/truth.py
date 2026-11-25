"""Dormant Data Truth Engine & Delayed Assumption Postmortem.

Implements Kai-Fu Lee's Boss AI / Truth Engine & Delayed Postmortem (AI Native, Ch. 6-7):
- Awakens "dormant data" across git churn logs, task records, review loops, and followups
  to detect thrashing files, recurring bugs after `done`, bypassed rigor, and silent drift.
- Records explicit task assumptions at brief/teardown time and runs delayed postmortems
  against subsequent repository reality (`validated`, `challenged`, `falsified`).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from autoharness import memory, transactions
from autoharness.env import HarnessError

_STOPWORDS = {
    "this", "that", "with", "from", "into", "when", "then", "also", "have", "will",
    "should", "must", "need", "needs", "work", "works", "file", "files", "code",
    "test", "tests", "task", "tasks", "make", "made", "keep", "keeps", "only",
    "none", "true", "false", "after", "before", "under", "over", "same", "more",
}


def assumptions_path(project_root: Path) -> Path:
    """Return path to `.autoharness/assumptions.json`."""
    return project_root.resolve() / ".autoharness" / "assumptions.json"


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zA-Z0-9_\u0400-\u04FF]{4,}", text.lower()) if w not in _STOPWORDS}


def _read_json_strict(path: Path, expected_type: type, label: str) -> Any:
    if not path.is_file():
        return expected_type()
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise HarnessError(f"corrupt or unreadable {label} file {path}: {exc}") from exc
    if not isinstance(data, expected_type):
        raise HarnessError(f"corrupt {label} file {path}: expected {expected_type.__name__}")
    return data


def _load_followups_list(root: Path) -> list[dict[str, Any]]:
    path = root / ".autoharness" / "followups.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise HarnessError(f"corrupt or unreadable followups file {path}: {exc}") from exc
    raw_items = data.get("items", []) if isinstance(data, dict) else (data if isinstance(data, list) else None)
    if raw_items is None:
        raise HarnessError(f"corrupt followups file {path}: expected list or dict with 'items'")
    return [i for i in raw_items if isinstance(i, dict)]


def _parse_iso_epoch(ts: Any) -> float | None:
    if not isinstance(ts, str) or not ts.strip():
        return None
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # Naive strings (e.g. `followups.add` uses local `time.strftime`) intentionally stay local for `.timestamp()`.
    return dt.timestamp()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_git_log_entries(
    project_root: Path,
    max_commits: int = 50,
    timeout: float = 10.0,
) -> tuple[bool, list[tuple[int, list[tuple[int, int, str]]]]]:
    """Run `git log` once with explicit timeout and return commit timestamp + file numstats."""
    if max_commits <= 0:
        raise ValueError(f"max_commits must be positive, got {max_commits}")
    cmd = ["git", "log", f"-n{min(max_commits, 500)}", "--numstat", "--format=COMMIT:%H:%ct"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, []
    if proc.returncode != 0:
        return False, []

    commits: list[tuple[int, list[tuple[int, int, str]]]] = []
    current_ts: int | None = None
    current_files: list[tuple[int, int, str]] = []
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("COMMIT:"):
            if current_ts is not None:
                commits.append((current_ts, current_files))
            parts = line.split(":")
            current_ts = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
            current_files = []
            continue
        if current_ts is None:
            continue
        cols = raw_line.split("\t")
        if len(cols) < 3:
            continue
        add_s, del_s, rel_path = cols[0].strip(), cols[1].strip(), cols[2].strip().replace("\\", "/")
        if not rel_path or add_s == "-" or del_s == "-":
            continue
        try:
            current_files.append((int(add_s), int(del_s), rel_path))
        except ValueError:
            continue
    if current_ts is not None:
        commits.append((current_ts, current_files))
    return True, commits


def _churn_from_commits(
    available: bool,
    commits: list[tuple[int, list[tuple[int, int, str]]]],
    since_epoch: float | None = None,
) -> dict[str, Any]:
    if not available:
        return {"available": False, "commits_scanned": 0, "files": {}, "thrashing_files": []}
    files: dict[str, dict[str, int]] = {}
    commits_scanned = 0
    for commit_ts, entries in commits:
        if since_epoch is not None and commit_ts < since_epoch:
            continue
        commits_scanned += 1
        for added, deleted, rel_path in entries:
            entry = files.setdefault(rel_path, {"touches": 0, "added": 0, "deleted": 0, "churn": 0})
            entry["touches"] += 1
            entry["added"] += added
            entry["deleted"] += deleted
            entry["churn"] += added + deleted

    thrashing: list[dict[str, Any]] = []
    for path_str, st in files.items():
        if st["touches"] >= 4 or (st["touches"] >= 3 and st["deleted"] >= 0.4 * max(1, st["added"])):
            thrashing.append({"path": path_str, **st})
    thrashing.sort(key=lambda x: (-x["touches"], -x["churn"], x["path"]))
    return {
        "available": True,
        "commits_scanned": commits_scanned,
        "files": files,
        "thrashing_files": thrashing,
    }


def git_churn_stats(
    project_root: Path,
    max_commits: int = 50,
    since_epoch: float | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Compute per-file git edit frequency and churn without failing outside git repos."""
    available, commits = _read_git_log_entries(project_root, max_commits=max_commits, timeout=timeout)
    return _churn_from_commits(available, commits, since_epoch=since_epoch)


def _audit_task_history(project_root: Path) -> dict[str, Any]:
    """Inspect `.autoharness/tasks/*/task.json` for hidden friction, bypassed rigor, and rework."""
    tasks_dir = project_root / ".autoharness" / "tasks"
    if not tasks_dir.is_dir():
        return {
            "total_tasks": 0,
            "completed_tasks": 0,
            "missing_brief_tasks": 0,
            "retold_reviews": 0,
            "hollow_artifacts": 0,
            "went_backwards_rounds": 0,
            "approach_pivots": 0,
            "accepted_with_gaps": 0,
            "tasks": [],
        }

    summary = {
        "total_tasks": 0,
        "completed_tasks": 0,
        "missing_brief_tasks": 0,
        "retold_reviews": 0,
        "hollow_artifacts": 0,
        "went_backwards_rounds": 0,
        "approach_pivots": 0,
        "accepted_with_gaps": 0,
        "tasks": [],
    }
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        tfile = task_dir / "task.json"
        if not tfile.is_file():
            continue
        info = _read_json_strict(tfile, dict, "task.json")
        summary["total_tasks"] += 1
        if info.get("status") == "done" or info.get("finished_epoch") is not None:
            summary["completed_tasks"] += 1
        if info.get("mode", "full") != "light" and not info.get("brief"):
            summary["missing_brief_tasks"] += 1
        if info.get("accepted_by_user"):
            summary["accepted_with_gaps"] += 1
        hollow = sum(1 for e in info.get("evidence", []) if e.get("hollow"))
        summary["hollow_artifacts"] += hollow
        reviews = info.get("reviews", [])
        retold = sum(1 for r in reviews if r.get("by") == "critic" and "answer_id" not in r)
        backwards = sum(1 for r in reviews if (r.get("compared") or {}).get("closer") == "earlier")
        pivots = len(info.get("approaches", []))
        summary["retold_reviews"] += retold
        summary["went_backwards_rounds"] += backwards
        summary["approach_pivots"] += pivots
        summary["tasks"].append({
            "id": info.get("id", task_dir.name),
            "status": info.get("status", "unknown"),
            "mode": info.get("mode", "full"),
            "rounds": len(reviews),
            "retold_reviews": retold,
            "hollow_artifacts": hollow,
            "approach_pivots": pivots,
            "started_epoch": info.get("started_epoch") or 0,
        })
    return summary


def _followup_recurrence(project_root: Path) -> dict[str, Any]:
    """Detect recurring problems across followups (issues returning after being marked done)."""
    items = _load_followups_list(project_root)
    open_items = [i for i in items if i.get("status", "open") != "done"]
    done_items = [i for i in items if i.get("status") == "done"]

    recurring: list[dict[str, Any]] = []
    for d_item in done_items:
        d_text = str(d_item.get("text", ""))
        d_paths, _ = memory.extract_code_references(d_text)
        d_words = _keywords(d_text)
        d_id = d_item.get("id")
        for later in items:
            if not isinstance(later, dict) or later.get("id") == d_id:
                continue
            l_id = later.get("id")
            if isinstance(d_id, int) and isinstance(l_id, int) and l_id <= d_id:
                continue
            l_text = str(later.get("text", ""))
            l_paths, _ = memory.extract_code_references(l_text)
            l_words = _keywords(l_text)
            shared_paths = sorted(d_paths & l_paths)
            shared_words = sorted(d_words & l_words)
            if shared_paths or len(shared_words) >= 2:
                recurring.append({
                    "closed_id": d_id,
                    "returned_id": l_id,
                    "returned_status": later.get("status", "open"),
                    "shared_paths": shared_paths,
                    "shared_keywords": shared_words[:5],
                    "closed_text": d_text,
                    "returned_text": l_text,
                })
                break

    return {
        "total": len(items),
        "open_count": len(open_items),
        "done_count": len(done_items),
        "recurring_count": len(recurring),
        "recurring": recurring,
    }


def analyze_dormant_signals(project_root: Path, max_commits: int = 50) -> dict[str, Any]:
    """Aggregate unfiltered repository & task signals into a Truth Engine diagnostic report."""
    root = project_root.resolve()
    churn = git_churn_stats(root, max_commits=max_commits)
    tasks = _audit_task_history(root)
    fups = _followup_recurrence(root)

    findings: list[dict[str, Any]] = []
    for tf in churn["thrashing_files"]:
        findings.append({
            "severity": "warning",
            "category": "thrashing",
            "target": tf["path"],
            "message": f"`{tf['path']}` modified {tf['touches']} times ({tf['added']}+/{tf['deleted']}-) in last {churn['commits_scanned']} commits",
        })
    for rec in fups["recurring"]:
        findings.append({
            "severity": "major",
            "category": "recurring_issue",
            "target": f"followup #{rec['returned_id']}",
            "message": f"Followup #{rec['returned_id']} repeats closed #{rec['closed_id']} ({', '.join(rec['shared_paths'] or rec['shared_keywords'])})",
        })
    if tasks["retold_reviews"] > 0:
        findings.append({
            "severity": "major",
            "category": "unconfirmed_review",
            "target": "reviews",
            "message": f"{tasks['retold_reviews']} critic review(s) recorded from retelling instead of raw answer file",
        })
    if tasks["hollow_artifacts"] > 0:
        findings.append({
            "severity": "warning",
            "category": "hollow_artifact",
            "target": "evidence",
            "message": f"{tasks['hollow_artifacts']} hollow artifact(s) flagged across task history",
        })
    if tasks["went_backwards_rounds"] > 0:
        findings.append({
            "severity": "major",
            "category": "regression_loop",
            "target": "reviews",
            "message": f"{tasks['went_backwards_rounds']} review round(s) regressed compared to an earlier result",
        })
    if tasks["approach_pivots"] > 0:
        findings.append({
            "severity": "warning",
            "category": "rework_churn",
            "target": "approaches",
            "message": f"{tasks['approach_pivots']} mid-task approach pivot(s) recorded across task history",
        })
    if tasks["missing_brief_tasks"] > 0:
        findings.append({
            "severity": "warning",
            "category": "unplanned_execution",
            "target": "brief",
            "message": f"{tasks['missing_brief_tasks']} full-mode task(s) ran without a recorded brief",
        })

    has_major = any(f["severity"] == "major" for f in findings)
    status = "warning" if has_major else ("attention" if findings else "healthy")
    return {
        "status": status,
        "findings_count": len(findings),
        "findings": findings,
        "git_churn": {
            "available": churn["available"],
            "commits_scanned": churn["commits_scanned"],
            "thrashing_files": churn["thrashing_files"],
        },
        "task_audit": {k: v for k, v in tasks.items() if k != "tasks"},
        "followup_recurrence": fups,
    }


def load_assumptions(root: Path) -> list[dict[str, Any]]:
    return _read_json_strict(assumptions_path(root), list, "assumptions")


def record_assumptions(
    project_root: Path,
    task_id: str,
    assumptions: list[str],
    touched_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Persist explicit task assumptions for delayed postmortem verification."""
    root = project_root.resolve()
    clean_items = [a.strip() for a in assumptions if a and a.strip()]
    if not clean_items:
        return []
    files = sorted({f.replace("\\", "/").strip() for f in (touched_files or []) if f and f.strip()})
    path = assumptions_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    added: list[dict[str, Any]] = []

    with transactions.locked(root):
        existing = load_assumptions(root)
        seen = {(e.get("task_id"), str(e.get("text", "")).strip()) for e in existing}
        now = time.time()
        next_id = max((int(e.get("id", 0)) for e in existing if isinstance(e.get("id"), int)), default=0) + 1
        for text_val in clean_items:
            if (task_id, text_val) in seen:
                continue
            ref_paths, _ = memory.extract_code_references(text_val)
            own_files = {p.replace("\\", "/") for p in ref_paths}
            merged_files = sorted(own_files if own_files else set(files))
            record = {
                "id": next_id,
                "task_id": task_id,
                "text": text_val,
                "files": merged_files,
                "recorded_epoch": now,
                "recorded_at": _utc_now_iso(),
                "status": "pending",
                "postmortem": None,
            }
            existing.append(record)
            added.append(record)
            seen.add((task_id, text_val))
            next_id += 1
        transactions.atomic_write(path, (json.dumps(existing, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return added


def run_postmortem(project_root: Path, min_subsequent_tasks: int = 1, max_commits: int = 200) -> dict[str, Any]:
    """Re-evaluate recorded assumptions against subsequent git churn, followups, and tasks."""
    if min_subsequent_tasks <= 0:
        raise ValueError(f"min_subsequent_tasks must be positive, got {min_subsequent_tasks}")
    root = project_root.resolve()
    path = assumptions_path(root)
    # Scan git history once outside the lock so external subprocess I/O stays lock-free.
    git_available, git_commits = _read_git_log_entries(root, max_commits=max_commits)
    with transactions.locked(root):
        items = load_assumptions(root)
        if not items:
            return {"status": "ok", "total": 0, "pending": 0, "validated": 0, "challenged": 0, "falsified": 0, "items": []}

        tasks = _audit_task_history(root)["tasks"]
        fups = _load_followups_list(root)

        for item in items:
            rec_epoch = float(item.get("recorded_epoch") or 0.0)
            a_files = set(item.get("files") or [])
            a_words = _keywords(str(item.get("text", "")))
            subsequent_tasks = [t for t in tasks if t.get("id") != item.get("task_id") and float(t.get("started_epoch") or 0.0) >= rec_epoch]

            prior_signals = [str(s) for s in ((item.get("postmortem") or {}).get("signals") or []) if str(s)]
            new_signals: list[str] = []
            # 1. Subsequent followups from *later* tasks mentioning the same files or >=2 keywords
            for f in fups:
                f_task = f.get("from_task") or f.get("task")
                if f_task == item.get("task_id"):
                    continue
                created_ts = _parse_iso_epoch(f.get("created"))
                if created_ts is not None and created_ts < int(rec_epoch):
                    continue
                f_text = str(f.get("text", ""))
                f_paths, _ = memory.extract_code_references(f_text)
                if (a_files & f_paths) or len(a_words & _keywords(f_text)) >= 2:
                    new_signals.append(f"Subsequent followup #{f.get('id')}: {f_text[:80]}")

            # 2. Post-recording git thrashing on referenced files
            if a_files:
                post_churn = _churn_from_commits(git_available, git_commits, since_epoch=rec_epoch)
                for tf in post_churn["thrashing_files"]:
                    if tf["path"] in a_files:
                        new_signals.append(f"Post-assumption thrashing on `{tf['path']}` ({tf['touches']} edits)")

            signals = list(dict.fromkeys([*prior_signals, *new_signals]))
            if len(signals) >= 2:
                status = "falsified"
            elif len(signals) == 1:
                status = "challenged"
            elif len(subsequent_tasks) >= min_subsequent_tasks:
                status = "validated"
            else:
                status = "pending"

            item["status"] = status
            item["postmortem"] = {
                "evaluated_at": _utc_now_iso(),
                "subsequent_tasks": len(subsequent_tasks),
                "signals": signals,
            }

        path.parent.mkdir(parents=True, exist_ok=True)
        transactions.atomic_write(path, (json.dumps(items, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

    counts = {s: sum(1 for i in items if i.get("status") == s) for s in ("pending", "validated", "challenged", "falsified")}
    return {"status": "ok", "total": len(items), **counts, "items": items}


def relevant_assumptions(project_root: Path, task_text: str, limit: int = 5) -> list[dict[str, Any]]:
    """Return challenged, falsified, or topic-overlapping assumptions relevant to a new task."""
    clamped = max(0, int(limit))
    if clamped == 0:
        return []
    items = load_assumptions(project_root.resolve())
    if not items:
        return []
    t_words = _keywords(task_text)
    t_paths, _ = memory.extract_code_references(task_text)
    matched: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        status = item.get("status", "pending")
        overlap = len(t_words & _keywords(str(item.get("text", "")))) + len(t_paths & set(item.get("files") or []))
        if status in ("falsified", "challenged") or overlap > 0:
            priority = (4 if status == "falsified" else 2 if status == "challenged" else 0) + overlap
            matched.append((priority, item))
    matched.sort(key=lambda pair: (-pair[0], -int(pair[1].get("id") or 0)))
    return [m[1] for m in matched[:clamped]]


def format_truth_report(report: dict[str, Any]) -> str:
    """Format Truth Engine or Delayed Postmortem results as readable Markdown."""
    if "findings" in report:
        lines = [
            "# Dormant Data Truth Engine Audit",
            "",
            f"- **Status:** `{report['status'].upper()}`",
            f"- **Findings:** {report.get('findings_count', 0)}",
            f"- **Git Commits Scanned:** {report.get('git_churn', {}).get('commits_scanned', 0)}",
            f"- **Recurring Followups:** {report.get('followup_recurrence', {}).get('recurring_count', 0)}",
            "",
        ]
        if not report["findings"]:
            lines.append("No dormant anomalies, thrashing files, or recurring issues detected.")
            return "\n".join(lines)
        lines.append("## Signals")
        for f in report["findings"]:
            lines.append(f"- [{f['severity'].upper()}] ({f['category']}) {f['message']}")
        return "\n".join(lines)

    lines = [
        "# Delayed Assumption Postmortem",
        "",
        f"- **Total Assumptions:** {report.get('total', 0)}",
        f"- **Validated:** {report.get('validated', 0)} | **Challenged:** {report.get('challenged', 0)} | **Falsified:** {report.get('falsified', 0)} | **Pending:** {report.get('pending', 0)}",
        "",
    ]
    for item in report.get("items", []):
        sigs = (item.get("postmortem") or {}).get("signals") or []
        sig_note = f" — {'; '.join(sigs)}" if sigs else ""
        lines.append(f"- #{item.get('id')} [{str(item.get('status', 'pending')).upper()}] {item.get('text')}{sig_note}")
    return "\n".join(lines)
