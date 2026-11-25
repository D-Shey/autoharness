"""Shared CLI output, approval helpers, and monkeypatch-safe symbol lookup."""
from __future__ import annotations

import json
import sys
from typing import Any

EXIT_APPROVAL = 3


def out(data) -> None:
    """Serialize a JSON payload to stdout."""
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def need_approval(what: str, report: dict) -> int:
    """Print an approval-required response with anti-cognitive-surrender framing and return `EXIT_APPROVAL`."""
    risk = report.get("risk_score")
    tier = report.get("reversibility_tier")
    reasons = [str(r) for r in (report.get("reasons") or [])[:3]]
    summary_parts = [f"Action `{what}` requires explicit human judgment."]
    if tier or risk is not None:
        summary_parts.append(f"Profile: tier={tier or 'unknown'}, risk={risk if risk is not None else 'n/a'}.")
    if reasons:
        summary_parts.append("Key signals: " + "; ".join(reasons))
    judgment_focus = {
        "why_not_routine": " ".join(summary_parts),
        "counter_question": "What worst-case side effect or irreversible state change could happen if our assumptions about this target are wrong?",
        "required_check": "Confirm the rollback path and blast radius directly before approving.",
    }
    out({"status": "approval_required", "what": what, "trust": report, "judgment_focus": judgment_focus,
         "next": "Show this to the user with `judgment_focus`. Re-run the same command with --approve only if they explicitly agree."})
    return EXIT_APPROVAL


def cli_ref(name: str, default: Any) -> Any:
    """Return `autoharness.cli.<name>` when loaded so monkeypatches on `cli` reach extracted handlers."""
    cli_mod = sys.modules.get("autoharness.cli")
    if cli_mod is not None and name in cli_mod.__dict__:
        return cli_mod.__dict__[name]
    return default


__all__ = [name for name in globals() if not name.startswith("__")]
