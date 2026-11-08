"""Active Context Pruning and History Search for AutoHarness.

Implements tiered context folding (Capture -> Distill -> Condense) inspired by
billion-context / Active Context Pruning (ACP), keeping model context compact during
long-running multi-round tasks while preserving exact log reversibility on disk.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def fold_reviews(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold earlier review rounds into compact distilled summaries.

    The latest review round remains fully detailed (Capture tier), while preceding rounds
    are collapsed into high-density summaries (Distill tier) with links to answer IDs and files.
    """
    if not reviews:
        return []

    folded = []
    latest_idx = len(reviews) - 1

    for idx, r in enumerate(reviews):
        if idx == latest_idx:
            # Latest review keeps full fidelity
            folded.append(dict(r) | {"folded": False})
        else:
            # Earlier reviews are distilled
            summary = r.get("top") or r.get("diff") or "Completed review round"
            if len(summary) > 120:
                summary = summary[:117] + "..."
            entry = {
                "round": r.get("round", idx + 1),
                "by": r.get("by", "unknown"),
                "worst": r.get("worst", "none"),
                "summary": summary,
                "answer_id": r.get("answer_id"),
                "folded": True,
            }
            folded.append(entry)

    return folded


def format_folded_context(task_info: dict[str, Any]) -> str:
    """Format a compact, prefix-cache friendly summary of task progress and reviews."""
    reviews = task_info.get("reviews", [])
    if not reviews:
        return "No reviews recorded yet."

    folded = fold_reviews(reviews)
    lines = ["=== Task History (Folded Context) ==="]

    for item in folded:
        if item.get("folded"):
            aid = f" [{item['answer_id']}]" if item.get("answer_id") else ""
            lines.append(
                f"- Round {item['round']} ({item['by']}): verdict {item['worst']}{aid} -> {item['summary']}"
            )
        else:
            aid = f" [{item.get('answer_id')}]" if item.get("answer_id") else ""
            lines.append(
                f"- [ACTIVE] Round {item['round']} ({item['by']}): verdict {item.get('worst')}{aid}"
            )
            if item.get("coverage"):
                cov = ", ".join(f"{k}:{v}" for k, v in item["coverage"].items())
                lines.append(f"  Coverage: {cov}")
            if item.get("diff") and item["diff"] != "No differences.":
                lines.append(f"  Findings: {item['diff'][:200]}")

    return "\n".join(lines)


def search_task_history(
    task_dir: Path,
    query: str,
    kind: str = "all",
    max_matches: int = 10,
    context_lines: int = 2,
) -> list[dict[str, Any]]:
    """Search across task execution logs (`runs/`) and reviewer answers (`answers/`).

    Args:
        task_dir: Directory of the task (`.autoharness/tasks/<id>/`).
        query: Search term to match case-insensitively.
        kind: Target category ('all', 'runs', 'reviews').
        max_matches: Maximum number of snippet matches to return.
        context_lines: Surrounding lines to include for each match.

    Returns:
        List of match dictionaries containing source, line number, and contextual snippet.
    """
    if not task_dir.is_dir() or not query or not query.strip():
        return []

    q = query.strip().lower()
    targets: list[tuple[str, Path]] = []

    if kind in ("all", "runs"):
        runs_dir = task_dir / "runs"
        if runs_dir.is_dir():
            for p in sorted(runs_dir.glob("*.log")):
                targets.append(("runs", p))

    if kind in ("all", "reviews"):
        answers_dir = task_dir / "answers"
        if answers_dir.is_dir():
            for p in sorted(answers_dir.glob("*.txt")):
                targets.append(("reviews", p))

    matches: list[dict[str, Any]] = []

    for category, file_path in targets:
        if len(matches) >= max_matches:
            break
        try:
            content = file_path.read_text("utf-8", errors="replace")
        except OSError:
            continue

        file_lines = content.splitlines()
        rel_path = f"{file_path.parent.name}/{file_path.name}"

        for idx, line in enumerate(file_lines):
            if q in line.lower():
                start = max(0, idx - context_lines)
                end = min(len(file_lines), idx + context_lines + 1)
                snippet_lines = [
                    f"{' > ' if i == idx else '   '}{i + 1:4d}: {file_lines[i]}"
                    for i in range(start, end)
                ]
                matches.append({
                    "category": category,
                    "source": rel_path,
                    "line": idx + 1,
                    "snippet": "\n".join(snippet_lines),
                })
                if len(matches) >= max_matches:
                    break

    return matches
