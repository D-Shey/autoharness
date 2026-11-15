"""Intelligent Output Compression & Token Cascading (inspired by dean0x/skim).

Compresses long command, test, build, and diff outputs by 70-95%:
- Auto-detects log formats: pytest, git_diff, traceback, compiler, generic.
- Folds repetitive pass markers, verbose context, and noisy build logs.
- Isolates failure blocks, root exception tracebacks, and summaries.
- Computes compression ratio and token savings.
"""
from __future__ import annotations

import re
from typing import Any


def detect_log_kind(text: str) -> str:
    """Detect format of the log stream."""
    if "test session starts" in text or "short test summary info" in text or ("collected " in text and "passed" in text):
        return "pytest"
    if text.startswith("diff --git") or "\ndiff --git" in text or ("--- a/" in text and "+++ b/" in text):
        return "diff"
    if "Traceback (most recent call last):" in text:
        return "traceback"
    if any(m in text for m in ["error:", "fatal error:", "warning:", "CMakeError", "ninja: error"]):
        return "compiler"
    return "generic"


def compress_pytest(text: str, max_lines: int = 60) -> tuple[str, str]:
    """Compress pytest output: collapse passing tests, isolate failures and summary."""
    lines = text.splitlines()
    summary_line = "pytest run finished"
    for l in reversed(lines):
        if "passed" in l or "failed" in l or "error" in l:
            if "==" in l or "in " in l:
                summary_line = l.strip("= ")
                break

    header_lines: list[str] = []
    failure_lines: list[str] = []
    in_failures = False
    in_summary_info = False

    for l in lines:
        if "test session starts" in l or "rootdir:" in l or "collected " in l:
            header_lines.append(l)
        elif "=== FAILURES ===" in l or "=== ERRORS ===" in l or "___ " in l:
            in_failures = True
            failure_lines.append(l)
        elif "=== short test summary info ===" in l:
            in_failures = False
            in_summary_info = True
            failure_lines.append(l)
        elif in_failures:
            failure_lines.append(l)
        elif in_summary_info:
            if l.startswith("="):
                in_summary_info = False
            else:
                failure_lines.append(l)

    out_lines: list[str] = []
    if header_lines:
        out_lines.extend(header_lines[:5])
    out_lines.append("... [Passing test progress folded] ...")
    if failure_lines:
        if len(failure_lines) <= max_lines:
            out_lines.extend(failure_lines)
        else:
            head_len = max(3, max_lines // 3)
            tail_len = max(3, max_lines - head_len - 1)
            out_lines.extend(failure_lines[:head_len])
            out_lines.append(f"... [{len(failure_lines) - head_len - tail_len} failure lines folded] ...")
            out_lines.extend(failure_lines[-tail_len:])
    out_lines.append(f"Summary: {summary_line}")

    return "\n".join(out_lines).strip(), summary_line


def compress_diff(text: str, max_lines: int = 60) -> tuple[str, str]:
    """Compress git diff: fold unchanged context lines, keep modified headers and hunks."""
    lines = text.splitlines()
    files_changed: list[str] = []
    active_hunks: list[str] = []

    for l in lines:
        if l.startswith("diff --git"):
            parts = l.split(" ")
            if len(parts) >= 4:
                files_changed.append(parts[3].removeprefix("b/"))
            active_hunks.append(l)
        elif l.startswith("--- ") or l.startswith("+++ ") or l.startswith("@@"):
            active_hunks.append(l)
        elif l.startswith("+") or l.startswith("-"):
            if not l.startswith("+++") and not l.startswith("---"):
                active_hunks.append(l)

    summary = f"Diff across {len(files_changed)} file(s): {', '.join(files_changed[:5])}"
    if len(files_changed) > 5:
        summary += f" and {len(files_changed) - 5} more"

    out_lines = [f"# {summary}", "... [Unchanged context folded] ..."]
    if len(active_hunks) <= max_lines:
        out_lines.extend(active_hunks)
    else:
        out_lines.extend(active_hunks[:max_lines])
        out_lines.append(f"... [{len(active_hunks) - max_lines} diff lines across remaining files folded. See full log for details] ...")
    return "\n".join(out_lines).strip(), summary


def compress_traceback(text: str, max_lines: int = 60) -> tuple[str, str]:
    """Compress traceback logs: isolate exception stack and error root cause."""
    lines = text.splitlines()
    tb_lines: list[str] = []
    in_tb = False
    root_cause = "Exception occurred"

    for l in lines:
        if "Traceback (most recent call last):" in l:
            in_tb = True
            tb_lines = [l]
        elif in_tb:
            tb_lines.append(l)
            if l and not l.startswith(" ") and not l.startswith("\t"):
                root_cause = l.strip()
                in_tb = False

    if not tb_lines:
        # Fallback to last non-empty lines
        tb_lines = [l for l in lines if l.strip()]

    summary = f"Traceback root cause: {root_cause}"
    out_lines = ["... [Preceding logs folded] ..."]
    if len(tb_lines) <= max_lines:
        out_lines.extend(tb_lines)
    else:
        head_len = max(3, max_lines // 4)
        tail_len = max(3, max_lines - head_len - 1)
        out_lines.extend(tb_lines[:head_len])
        out_lines.append(f"... [{len(tb_lines) - head_len - tail_len} traceback frames folded] ...")
        out_lines.extend(tb_lines[-tail_len:])
    return "\n".join(out_lines).strip(), summary


def compress_compiler(text: str, max_lines: int = 60) -> tuple[str, str]:
    """Compress compiler/linter logs: keep error and warning diagnostics."""
    lines = text.splitlines()
    diag_lines: list[str] = []
    errors = 0
    warnings = 0

    for l in lines:
        if "error:" in l or "fatal error:" in l or "CMakeError" in l or "FAILED" in l:
            errors += 1
            diag_lines.append(l)
        elif "warning:" in l:
            warnings += 1
            if len(diag_lines) < max_lines:
                diag_lines.append(l)

    summary = f"Build diagnostics: {errors} error(s), {warnings} warning(s)"
    out_lines = [f"# {summary}", "... [Verbose compilation output folded] ..."]
    out_lines.extend(diag_lines[:max_lines])
    return "\n".join(out_lines).strip(), summary


def compress_generic(text: str, max_lines: int = 60) -> tuple[str, str]:
    """Generic log compression: head + tail with folded middle."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text.strip(), f"Total {len(lines)} line(s)"

    head_count = max_lines // 3
    tail_count = max_lines - head_count - 1
    out_lines = lines[:head_count]
    out_lines.append(f"... [Folded {len(lines) - head_count - tail_count} intermediate lines] ...")
    out_lines.extend(lines[-tail_count:])
    summary = f"Folded {len(lines)} lines down to {len(out_lines)}"
    return "\n".join(out_lines).strip(), summary


def save_raw_log(raw_text: str, project_root: str | None = None, prefix: str = "skim") -> str:
    """Save full uncompressed output to disk (.autoharness/logs/) for cascading retrieval."""
    import hashlib
    import time
    from pathlib import Path

    if project_root:
        base_dir = Path(project_root) / ".autoharness" / "logs"
    else:
        base_dir = Path.cwd() / ".autoharness" / "logs"
    base_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    text_hash = hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest()[:8]
    log_path = base_dir / f"{prefix}-{timestamp}-{text_hash}.log"
    log_path.write_text(raw_text, encoding="utf-8", errors="replace")
    return str(log_path)


def compress_output(
    raw_text: str,
    kind: str = "auto",
    max_lines: int = 60,
    save_raw: bool = False,
    save_dir: str | None = None,
) -> dict[str, Any]:
    """Intelligently compress log/output stream and compute token reduction ratio."""
    raw_clean = raw_text.strip()
    detected_kind = detect_log_kind(raw_clean) if kind == "auto" else kind

    if detected_kind == "pytest":
        compressed_text, summary = compress_pytest(raw_clean, max_lines=max_lines)
    elif detected_kind == "diff":
        compressed_text, summary = compress_diff(raw_clean, max_lines=max_lines)
    elif detected_kind == "traceback":
        compressed_text, summary = compress_traceback(raw_clean, max_lines=max_lines)
    elif detected_kind == "compiler":
        compressed_text, summary = compress_compiler(raw_clean, max_lines=max_lines)
    else:
        compressed_text, summary = compress_generic(raw_clean, max_lines=max_lines)

    orig_chars = len(raw_clean)
    orig_lines = len(raw_clean.splitlines())
    comp_chars = len(compressed_text)
    comp_lines = len(compressed_text.splitlines())

    ratio = max(0.0, round(1.0 - (comp_chars / max(1, orig_chars)), 4))

    raw_log_path = None
    if save_raw:
        raw_log_path = save_raw_log(raw_text, project_root=save_dir)

    result: dict[str, Any] = {
        "kind": detected_kind,
        "summary": summary,
        "original_lines": orig_lines,
        "compressed_lines": comp_lines,
        "original_chars": orig_chars,
        "compressed_chars": comp_chars,
        "compression_ratio": ratio,
        "percent_saved": f"{round(ratio * 100, 1)}%",
        "compressed_text": compressed_text,
    }
    if raw_log_path:
        result["raw_log_path"] = raw_log_path

    return result


def format_compressed_report(result: dict[str, Any]) -> str:
    """Format compressed result into human-readable Markdown outline."""
    from pathlib import Path
    kind = result.get("kind", "generic")
    ratio = result.get("percent_saved", "0%")
    summary = result.get("summary", "")
    orig_l = result.get("original_lines", 0)
    comp_l = result.get("compressed_lines", 0)
    orig_c = result.get("original_chars", 0)
    comp_c = result.get("compressed_chars", 0)
    text = result.get("compressed_text", "")
    raw_log = result.get("raw_log_path")

    lines = [
        f"# Output Compression Report ({ratio} token reduction)",
        "",
        "## Summary",
        f"- **Detected Format:** `{kind}`",
        f"- **Key Takeaway:** {summary}",
        f"- **Lines:** {orig_l} -> {comp_l} lines",
        f"- **Characters:** {orig_c} -> {comp_c} bytes ({ratio} saved)",
    ]
    if raw_log:
        raw_p = Path(raw_log)
        lines.append(f"- **Full Uncompressed Log:** [{raw_p.name}](file:///{raw_p.as_posix()})")
    lines.extend([
        "",
        "## Compressed Stream",
        "```text",
        text,
        "```",
    ])
    return "\n".join(lines).strip()
