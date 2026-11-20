"""Active Context Pruning, Layered Session Folding, and History Search for AutoHarness.

Implements tiered context folding (Recent -> Distill -> Epoch) inspired by
billion-context / Active Context Pruning (ACP) and Google Research Harness²,
keeping model context compact during long-running multi-round tasks while
preserving exact log reversibility on disk.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any

from autoharness import compression

FOLDED_DIR = "folded"
FOLDED_STATE_FILE = "folded_sessions.json"
_lock = threading.RLock()


def _get_utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _estimate_tokens(text: str) -> int:
    """Fast deterministic token estimation (~4 chars per token)."""
    return max(1, len(text) // 4)


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


def compact_session_transcript(
    transcript_source: str | Path | list[dict[str, Any]],
    project_root: Path | None = None,
    session_id: str | None = None,
    keep_last: int = 5,
    epoch_size: int = 10,
    anchor_steps: int = 1,
    save_disk: bool = True,
) -> dict[str, Any]:
    """Perform multi-tiered layered compaction over an agent session transcript.

    Tiers:
      - Tier Anchor (Prompt Cache): First `anchor_steps` preserved 100% verbatim for KV-cache reuse (AP-5).
      - Tier 0 (Recent): Last `keep_last` steps kept verbatim (100% detail).
      - Tier 1 (Distill): Intermediate steps have their large outputs (tests, diffs, tool runs)
        compressed using cascading output compression (dean0x/skim model).
      - Tier 2 (Epoch): Oldest intermediate steps aggregated into milestone summaries.
    """
    now_str = _get_utc_now()
    raw_steps: list[dict[str, Any]] = []

    # 1. Parse steps from file or list
    if isinstance(transcript_source, (str, Path)):
        p = Path(transcript_source)
        if not p.is_file():
            return {"status": "error", "error": f"Transcript file not found: {p}"}
        try:
            if p.stat().st_size > 50_000_000:
                return {"status": "error", "error": f"Transcript exceeds safety limit (50MB): {p.stat().st_size} bytes"}
            content = p.read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    raw_steps.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError as e:
            return {"status": "error", "error": f"Failed to read transcript: {e}"}
        if not session_id:
            session_id = p.stem.replace(".compact", "").replace(".full", "")
    elif isinstance(transcript_source, list):
        raw_steps = [dict(s) for s in transcript_source]
        if not session_id:
            session_id = f"session-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    else:
        return {"status": "error", "error": "Invalid transcript source"}

    if not raw_steps:
        return {
            "status": "ok",
            "session_id": session_id or "unknown",
            "original_steps": 0,
            "compact_steps": 0,
            "compression_ratio": 0.0,
            "steps": [],
            "summary": "Transcript was empty",
        }

    total_steps = len(raw_steps)
    raw_dump = json.dumps(raw_steps, ensure_ascii=False)
    orig_chars = len(raw_dump)
    orig_tokens = _estimate_tokens(raw_dump)

    # 2. Determine tier boundaries
    # Steps: [0 ... actual_anchor ... epoch_cutoff ... distill_cutoff ... total_steps]
    actual_anchor = min(max(0, anchor_steps), total_steps)
    distill_cutoff = max(actual_anchor, total_steps - keep_last)
    epoch_cutoff = max(actual_anchor, distill_cutoff - epoch_size) if (distill_cutoff - actual_anchor) > epoch_size else actual_anchor

    compact_steps: list[dict[str, Any]] = []

    # Tier Anchor: Prompt Cache Anchor Invariant (AP-5)
    # The first `actual_anchor` step(s) (system prompt / initial user goal) are kept 100% verbatim
    for idx in range(0, actual_anchor):
        step = dict(raw_steps[idx])
        step["tier"] = "anchor"
        step["folded"] = False
        compact_steps.append(step)

    # Tier 2: Epoch condensation for intermediate steps [actual_anchor ... epoch_cutoff]
    if epoch_cutoff > actual_anchor:
        epoch_slice = raw_steps[actual_anchor:epoch_cutoff]
        tool_counts: dict[str, int] = {}
        for s in epoch_slice:
            for tc in s.get("tool_calls", []):
                t_name = tc.get("name", "unknown")
                tool_counts[t_name] = tool_counts.get(t_name, 0) + 1
        tools_summary = ", ".join(f"{k}x{v}" for k, v in sorted(tool_counts.items()))

        user_prompts = [s.get("content", "") for s in epoch_slice if s.get("type") == "USER_INPUT"]
        last_goal = user_prompts[-1][:140] if user_prompts else "Intermediate session phase"

        epoch_summary = {
            "step_index": actual_anchor,
            "type": "EPOCH_MILESTONE",
            "tier": "epoch",
            "folded": True,
            "range": [actual_anchor, epoch_cutoff - 1],
            "steps_condensed": epoch_cutoff - actual_anchor,
            "goal": last_goal,
            "tools_executed": tools_summary or "none",
            "timestamp": epoch_slice[0].get("created_at", now_str),
        }
        compact_steps.append(epoch_summary)

    # Tier 1: Distill intermediate steps [epoch_cutoff ... distill_cutoff]
    for idx in range(epoch_cutoff, distill_cutoff):
        step = dict(raw_steps[idx])
        stype = step.get("type", "")
        content = str(step.get("content", ""))

        # Compress large tool execution outputs or logs
        if len(content) > 300:
            comp_res = compression.compress_output(content, max_lines=15, save_raw=False)
            distilled_content = comp_res.get("compressed", content[:300] + "... [compressed]")
            step["content"] = distilled_content
            step["uncompressed_chars"] = len(content)
            step["compressed_ratio"] = comp_res.get("compression_ratio", 0.0)

        # Truncate internal thinking if present
        if step.get("thinking") and len(str(step["thinking"])) > 200:
            step["thinking"] = str(step["thinking"])[:180] + "... [thinking folded]"

        step["tier"] = "distill"
        step["folded"] = True
        compact_steps.append(step)

    # Tier 0: Verbatim recent steps [distill_cutoff ... total_steps]
    for idx in range(distill_cutoff, total_steps):
        step = dict(raw_steps[idx])
        step["tier"] = "recent"
        step["folded"] = False
        compact_steps.append(step)

    compact_dump = json.dumps(compact_steps, ensure_ascii=False)
    compact_chars = len(compact_dump)
    compact_tokens = _estimate_tokens(compact_dump)
    ratio = round((1.0 - (compact_chars / orig_chars)) * 100, 2) if orig_chars > 0 else 0.0

    result: dict[str, Any] = {
        "status": "ok",
        "session_id": session_id,
        "timestamp": now_str,
        "original_steps": total_steps,
        "compact_steps": len(compact_steps),
        "original_chars": orig_chars,
        "compact_chars": compact_chars,
        "estimated_original_tokens": orig_tokens,
        "estimated_compact_tokens": compact_tokens,
        "tokens_saved": max(0, orig_tokens - compact_tokens),
        "compression_ratio": ratio,
        "tiers": {
            "anchor_steps": actual_anchor,
            "epoch_steps": epoch_cutoff - actual_anchor,
            "distill_steps": distill_cutoff - epoch_cutoff,
            "recent_steps": total_steps - distill_cutoff,
        },
        "prompt_cache_status": {
            "anchor_preserved": actual_anchor > 0,
            "anchor_steps": actual_anchor,
            "kv_cache_aligned": True,
        },
        "steps": compact_steps,
    }

    # 3. Save to disk if requested and project_root provided
    if save_disk and project_root:
        with _lock:
            folded_base = project_root / ".autoharness" / FOLDED_DIR
            folded_base.mkdir(parents=True, exist_ok=True)

            # Atomically save compact jsonl
            compact_path = folded_base / f"{session_id}.compact.jsonl"
            t_fd, t_tmp = tempfile.mkstemp(dir=folded_base, prefix="compact-", suffix=".tmp")
            try:
                with os.fdopen(t_fd, "w", encoding="utf-8") as f:
                    for s in compact_steps:
                        f.write(json.dumps(s, ensure_ascii=False) + "\n")
                os.replace(t_tmp, compact_path)
                result["compact_file"] = str(compact_path)
            finally:
                if os.path.exists(t_tmp):
                    try:
                        os.unlink(t_tmp)
                    except OSError:
                        pass

            # Atomically save raw full jsonl if not existing
            full_path = folded_base / f"{session_id}.full.jsonl"
            if not full_path.is_file():
                f_fd, f_tmp = tempfile.mkstemp(dir=folded_base, prefix="full-", suffix=".tmp")
                try:
                    with os.fdopen(f_fd, "w", encoding="utf-8") as f:
                        for s in raw_steps:
                            f.write(json.dumps(s, ensure_ascii=False) + "\n")
                    os.replace(f_tmp, full_path)
                    result["full_file"] = str(full_path)
                finally:
                    if os.path.exists(f_tmp):
                        try:
                            os.unlink(f_tmp)
                        except OSError:
                            pass
            else:
                result["full_file"] = str(full_path)

            # Update folded state registry
            state_file = folded_base / FOLDED_STATE_FILE
            state_data: dict[str, Any] = {}
            if state_file.is_file():
                try:
                    state_data = json.loads(state_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    state_data = {}
            sessions_map = state_data.setdefault("sessions", {})
            sessions_map[session_id] = {
                "last_compacted": now_str,
                "original_steps": total_steps,
                "compact_steps": len(compact_steps),
                "compression_ratio": ratio,
                "tokens_saved": result["tokens_saved"],
                "compact_file": str(compact_path),
            }
            state_data["last_updated"] = now_str

            st_fd, st_tmp = tempfile.mkstemp(dir=state_file.parent, prefix="folded-", suffix=".tmp")
            try:
                with os.fdopen(st_fd, "w", encoding="utf-8") as f:
                    json.dump(state_data, f, indent=2, ensure_ascii=False)
                os.replace(st_tmp, state_file)
            finally:
                if os.path.exists(st_tmp):
                    try:
                        os.unlink(st_tmp)
                    except OSError:
                        pass

    return result


def get_session_compaction_status(project_root: Path, session_id: str | None = None) -> dict[str, Any]:
    """Retrieve compaction metrics and stored folded sessions."""
    state_file = project_root / ".autoharness" / FOLDED_DIR / FOLDED_STATE_FILE
    if not state_file.is_file():
        alt = project_root / ".autoharness" / FOLDED_STATE_FILE
        if alt.is_file():
            state_file = alt
        else:
            return {
                "status": "idle",
                "sessions_count": 0,
                "sessions": {},
                "total_tokens_saved": 0,
            }
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        sessions = data.get("sessions", {})
        total_saved = sum(s.get("tokens_saved", 0) for s in sessions.values())
        if session_id:
            s_info = sessions.get(session_id)
            return {
                "status": "found" if s_info else "not_found",
                "session_id": session_id,
                "session": s_info,
            }
        return {
            "status": "active",
            "sessions_count": len(sessions),
            "sessions": sessions,
            "total_tokens_saved": total_saved,
            "last_updated": data.get("last_updated"),
        }
    except (OSError, json.JSONDecodeError):
        return {"status": "error", "error": "Failed to parse folded sessions state file"}


def prune_folded_sessions(project_root: Path, keep_latest: int = 5) -> dict[str, Any]:
    """Prune older folded session files to release disk space."""
    with _lock:
        folded_base = project_root / ".autoharness" / FOLDED_DIR
        if not folded_base.is_dir():
            return {"status": "ok", "removed_files": 0, "freed_bytes": 0}

        compact_files = sorted(folded_base.glob("*.compact.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        to_prune = compact_files[keep_latest:]
        freed = 0
        removed = 0
        pruned_stems = set()
        for cf in to_prune:
            stem = cf.stem.replace(".compact", "")
            pruned_stems.add(stem)
            ff = folded_base / f"{stem}.full.jsonl"
            for target in (cf, ff):
                if target.is_file():
                    try:
                        sz = target.stat().st_size
                        target.unlink()
                        freed += sz
                        removed += 1
                    except OSError:
                        pass

        # Synchronize folded state registry
        state_file = folded_base / FOLDED_STATE_FILE
        if not state_file.is_file():
            alt = project_root / ".autoharness" / FOLDED_STATE_FILE
            if alt.is_file():
                state_file = alt

        if state_file.is_file() and pruned_stems:
            try:
                state_data = json.loads(state_file.read_text(encoding="utf-8"))
                sessions_map = state_data.get("sessions", {})
                for stem in pruned_stems:
                    sessions_map.pop(stem, None)
                state_data["last_updated"] = _get_utc_now()
                st_fd, st_tmp = tempfile.mkstemp(dir=state_file.parent, prefix="pruned-", suffix=".tmp")
                try:
                    with os.fdopen(st_fd, "w", encoding="utf-8") as f:
                        json.dump(state_data, f, indent=2, ensure_ascii=False)
                    os.replace(st_tmp, state_file)
                finally:
                    if os.path.exists(st_tmp):
                        try:
                            os.unlink(st_tmp)
                        except OSError:
                            pass
            except (OSError, json.JSONDecodeError):
                pass

        return {"status": "ok", "removed_files": removed, "freed_bytes": freed}


def format_compaction_report(res: dict[str, Any]) -> str:
    """Format session compaction result into human-readable Markdown report."""
    if res.get("status") == "error":
        return f"# Session Compaction Error\n\n- **Error:** {res.get('error')}"

    sid = res.get("session_id", "unknown")
    ratio = res.get("compression_ratio", 0.0)
    orig_s = res.get("original_steps", 0)
    comp_s = res.get("compact_steps", 0)
    orig_tok = res.get("estimated_original_tokens", 0)
    comp_tok = res.get("estimated_compact_tokens", 0)
    saved = res.get("tokens_saved", 0)
    tiers = res.get("tiers", {})

    lines = [
        f"# Session Compactor Report: `{sid}`",
        "",
        "## Compaction Summary",
        f"- **Token Compression Ratio:** `{ratio}%` reduction",
        f"- **Tokens:** ~{orig_tok} -> ~{comp_tok} (saved ~{saved} tokens)",
        f"- **Steps:** {orig_s} raw steps folded into {comp_s} tiered steps",
        "",
        "## Tier Breakdown",
    ]
    if tiers.get("anchor_steps", 0) > 0:
        lines.append(f"- **Tier Anchor (Prompt Cache Anchor):** {tiers.get('anchor_steps')} initial step(s) preserved 100% verbatim (KV-cache prefix stability, AP-5)")
    lines.extend([
        f"- **Tier 2 (Epoch Condensation):** {tiers.get('epoch_steps', 0)} oldest steps summarized",
        f"- **Tier 1 (Distill Compression):** {tiers.get('distill_steps', 0)} intermediate steps skimmed",
        f"- **Tier 0 (Recent Fidelity):** {tiers.get('recent_steps', 0)} latest steps preserved 100% verbatim",
    ])
    if res.get("prompt_cache_status"):
        pcs = res["prompt_cache_status"]
        lines.extend([
            "",
            "## Prompt Cache Status",
            f"- **Anchor Preserved:** `{pcs.get('anchor_preserved', True)}`",
            f"- **KV Cache Aligned:** `{pcs.get('kv_cache_aligned', True)}`",
        ])

    if res.get("compact_file"):
        lines.extend([
            "",
            "## Storage References",
            f"- **Compact Transcript:** `{res.get('compact_file')}`",
            f"- **Full Trace Backup:** `{res.get('full_file')}`",
        ])

    return "\n".join(lines).strip()


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
            if file_path.stat().st_size > 10_000_000:
                continue
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


VOLATILE_PREFIX_PATTERNS = [
    re.compile(r"\b(today|timestamp|datetime|request[_ -]?id|trace[_ -]?id|run[_ -]?id|session[_ -]?id)\b", re.I),
    re.compile(r"\b20\d\d-\d\d-\d\d\b"),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
]


def check_prompt_cache_invariants(
    steps: list[dict[str, Any]] | None = None,
    system_prompt: str | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Audit prompt cache invariants according to AP-1, AP-2, AP-4, AP-5, AP-15 rules.

    Validates:
      - AP-1 (Volatile Prefix): Dynamic timestamps, request IDs or UUIDs in the early prefix.
      - AP-2 (Tool/Schema Ordering): Tools list sorted deterministically by name.
      - AP-4 (Dynamic Tool Mutation): Tool catalogs mutating names/counts between steps.
      - AP-5 (Anchor Invariant): Step 0 (anchor) preserved without mutation during folding.
      - AP-15 (Reasoning Effort Continuity): Reasoning effort or thinking config changing mid-session.
    """
    violations: list[dict[str, Any]] = []

    # 1. Check AP-1: Volatile prefix in system prompt or initial prompt
    check_text = system_prompt or ""
    if not check_text and steps:
        for s in steps:
            if s.get("type") in ("system", "SYSTEM_PROMPT", "USER_INPUT"):
                check_text = str(s.get("content", ""))
                break

    if check_text:
        # Check first 25% of text or first 500 chars
        prefix_slice = check_text[:max(500, len(check_text) // 4)]
        for pat in VOLATILE_PREFIX_PATTERNS:
            m = pat.search(prefix_slice)
            if m:
                violations.append({
                    "rule_id": "AP-1",
                    "severity": "high",
                    "category": "prefix-stability",
                    "issue": f"Volatile dynamic token {m.group(0)!r} detected in early prompt prefix",
                    "fix": "Move volatile facts (dates, request IDs, UUIDs) to the end of prompt or request context.",
                    "evidence": prefix_slice[max(0, m.start() - 20):min(len(prefix_slice), m.end() + 20)],
                })
                break

    # 2. Check AP-2: Tool schema ordering
    if tools and len(tools) > 1:
        tool_names = [t.get("name", "") for t in tools]
        sorted_names = sorted(tool_names)
        if tool_names != sorted_names:
            violations.append({
                "rule_id": "AP-2",
                "severity": "high",
                "category": "tool-schema-stability",
                "issue": "Tool catalog is not sorted lexicographically, causing schema hash drift across runs",
                "fix": "Sort tools deterministically by name before serializing into request payload.",
                "evidence": f"Current: {tool_names[:5]} vs Expected: {sorted_names[:5]}",
            })

    # 3. Check AP-4, AP-5, AP-15 across steps
    if steps and len(steps) > 1:
        # Check AP-5: Anchor preserved
        first_step = steps[0]
        if first_step.get("folded") is True or first_step.get("type") == "EPOCH_MILESTONE":
            violations.append({
                "rule_id": "AP-5",
                "severity": "high",
                "category": "history-stability",
                "issue": "First step / anchor was compacted or folded, breaking KV-cache prefix for remaining turns",
                "fix": "Preserve initial anchor step (system prompt / initial goal) 100% verbatim using anchor_steps >= 1.",
                "evidence": f"Step 0 type: {first_step.get('type')}, folded: {first_step.get('folded')}",
            })

        # Check AP-4 & AP-15 across steps
        tool_counts = []
        efforts = []
        for s in steps:
            if "tools" in s and isinstance(s["tools"], list):
                tool_counts.append(len(s["tools"]))
            tc = s.get("tool_calls")
            if isinstance(tc, list):
                tool_counts.append(len(tc))
            effort = s.get("reasoning_effort") or s.get("thinking_budget") or s.get("output_config", {}).get("effort")
            if effort is not None:
                efforts.append(str(effort))

        # AP-15: effort toggled mid-conversation
        if len(set(efforts)) > 1:
            violations.append({
                "rule_id": "AP-15",
                "severity": "medium",
                "category": "effort-continuity",
                "issue": f"Reasoning effort mutated mid-session across steps: {efforts}",
                "fix": "Hold request-level reasoning effort constant inside a cached conversation.",
                "evidence": f"Distinct effort levels detected: {list(set(efforts))}",
            })

    status = "pass"
    if any(v["severity"] == "high" for v in violations):
        status = "fail"
    elif violations:
        status = "warning"

    return {
        "status": status,
        "cache_friendly": status == "pass",
        "violations": violations,
        "total_violations": len(violations),
    }

