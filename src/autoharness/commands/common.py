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
    """Print an approval-required response and return `EXIT_APPROVAL`."""
    out({"status": "approval_required", "what": what, "trust": report,
         "next": "Show this to the user. Re-run the same command with --approve only if they agree."})
    return EXIT_APPROVAL


def cli_ref(name: str, default: Any) -> Any:
    """Return `autoharness.cli.<name>` when loaded so monkeypatches on `cli` reach extracted handlers."""
    cli_mod = sys.modules.get("autoharness.cli")
    if cli_mod is not None and name in cli_mod.__dict__:
        return cli_mod.__dict__[name]
    return default


__all__ = [name for name in globals() if not name.startswith("__")]
