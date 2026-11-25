"""Consequence-Aware Approval & Blast Radius Simulation (Hugging Face arXiv:2610.40753).

Simulates action consequences before human-in-the-loop approvals or tool execution:
- Computes Blast Radius (affected files, impacted symbols, impacted tests, sensitive paths).
- Classifies Reversibility Tiers: IRREVERSIBLE, HIGH_RISK, REVERSIBLE, READ_ONLY.
- Generates a Consequence Manifest with risk scores and rollback guidance.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from enum import Enum
from pathlib import Path
from typing import Any


class ReversibilityTier(str, Enum):
    IRREVERSIBLE = "IRREVERSIBLE"
    HIGH_RISK = "HIGH_RISK"
    REVERSIBLE = "REVERSIBLE"
    READ_ONLY = "READ_ONLY"


SENSITIVE_PATTERNS = [
    r"gateway\.json",
    r"integrity\.key",
    r"\.env",
    r"secrets?",
    r"credentials?",
    r"id_rsa",
    r"token",
    r"\.git/config",
]

IRREVERSIBLE_COMMAND_PATTERNS = [
    r"\brm\s+-[rf]*",
    r"\bdel\s+/[fqs]*",
    r"\bdrop\s+(table|database|schema)\b",
    r"\btruncate\b",
    r"\bgit\s+clean\s+-[fdx]+",
    r"\bgit\s+reset\s+--hard\b",
    r"\bshred\b",
]

READ_ONLY_COMMANDS = {
    "cat", "type", "ls", "dir", "git status", "git log", "git diff", "grep",
    "findstr", "head", "tail", "pytest", "python -m unittest", "cargo test", "npm test"
}


def is_sensitive_path(path_str: str) -> bool:
    """Check if the path matches sensitive file or credential patterns."""
    normalized = path_str.replace("\\", "/").lower()
    return any(re.search(pat, normalized) for pat in SENSITIVE_PATTERNS)


def is_git_tracked(file_path: Path, project_root: Path | None) -> bool:
    """Check if the given file is tracked in git."""
    if not project_root:
        return False
    try:
        res = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(file_path)],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0
    except OSError:
        return False


def simulate_action(
    action_type: str,
    target: str,
    details: dict[str, Any] | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Simulate consequences and blast radius for a given agent action.

    Args:
        action_type: 'command', 'file_delete', 'file_modify', 'gateway_reconfig', 'query'
        target: file path, command line, or server name
        details: optional additional context
        project_root: optional root path of the project

    Returns:
        Structured simulation dict with reversibility tier, risk score, blast radius, and rollback advice.
    """
    details = details or {}
    action_type = action_type.lower().strip()
    target_clean = target.strip()

    tier = ReversibilityTier.REVERSIBLE
    risk_score = 30
    reasons: list[str] = []
    affected_files: list[str] = []
    impacted_symbols: list[str] = []
    impacted_tests: list[str] = []
    touches_sensitive = is_sensitive_path(target_clean)
    rollback_plan = "Standard rollback supported."

    if action_type == "query":
        tier = ReversibilityTier.READ_ONLY
        risk_score = 5
        reasons.append("Read-only inspection or query operation.")
        rollback_plan = "No state changes to revert."

    elif action_type == "command":
        cmd_lower = target_clean.lower()
        # Check read only commands
        if any(cmd_lower.startswith(ro) for ro in READ_ONLY_COMMANDS):
            tier = ReversibilityTier.READ_ONLY
            risk_score = 10
            reasons.append("Command belongs to safe read-only / test operations set.")
            rollback_plan = "No state changes to revert."
        elif any(re.search(pat, cmd_lower) for pat in IRREVERSIBLE_COMMAND_PATTERNS):
            tier = ReversibilityTier.IRREVERSIBLE
            risk_score = 95
            reasons.append("Command uses destructive flags (rm/del/drop/truncate/git clean/hard reset).")
            rollback_plan = "Cannot be undone automatically; verify backup exists before proceeding."
        else:
            tier = ReversibilityTier.HIGH_RISK if touches_sensitive else ReversibilityTier.REVERSIBLE
            risk_score = 65 if touches_sensitive else 40
            reasons.append("Generic CLI command execution.")
            rollback_plan = "Check repository git status or command logs to reverse modifications."

    elif action_type == "file_delete":
        affected_files.append(target_clean)
        p = Path(target_clean)
        full_path = (project_root / p) if project_root and not p.is_absolute() else p
        tracked = is_git_tracked(full_path, project_root)
        if not tracked:
            tier = ReversibilityTier.IRREVERSIBLE
            risk_score = 90
            reasons.append("Deleting untracked file: file cannot be recovered via git checkout.")
            rollback_plan = "Irreversible data loss unless manual backup is taken beforehand."
        else:
            tier = ReversibilityTier.HIGH_RISK if touches_sensitive else ReversibilityTier.REVERSIBLE
            risk_score = 70 if touches_sensitive else 35
            reasons.append("Deleting git-tracked file: content can be restored from git index.")
            rollback_plan = f"git checkout HEAD -- {target_clean}"

    elif action_type == "file_modify":
        affected_files.append(target_clean)
        p = Path(target_clean)
        full_path = (project_root / p) if project_root and not p.is_absolute() else p
        
        # Analyze blast radius through codemap call graph if Python file
        if project_root and full_path.suffix == ".py":
            try:
                from autoharness import codemap
                cmap = codemap.generate_codemap(project_root)
                call_graph = codemap.build_call_graph(cmap)
                
                # Check symbols defined in this file
                file_rel = str(full_path.relative_to(project_root)).replace("\\", "/")
                target_node = cmap.get("modules", {}).get(file_rel)
                if target_node:
                    sym_names = [f.get("name") for f in target_node.get("functions", []) if f.get("name")]
                    for cls in target_node.get("classes", []):
                        if cls.get("name"):
                            sym_names.append(cls.get("name"))
                        for m in cls.get("methods", []):
                            if m.get("name"):
                                sym_names.append(m.get("name"))

                    for sym_name in sym_names:
                        impact = codemap.analyze_impact(call_graph, sym_name)
                        for caller in impact.get("direct_callers", []):
                            c_name = caller.get("name") if isinstance(caller, dict) else str(caller)
                            if c_name and c_name not in impacted_symbols:
                                impacted_symbols.append(c_name)
                        for test in impact.get("impacted_tests", []):
                            t_name = test.get("file", str(test)) if isinstance(test, dict) else str(test)
                            if t_name and t_name not in impacted_tests:
                                impacted_tests.append(t_name)
            except (OSError, SyntaxError, ValueError, KeyError, AttributeError):
                pass

        if touches_sensitive:
            tier = ReversibilityTier.HIGH_RISK
            risk_score = 80
            reasons.append("Modifying sensitive security or harness configuration path.")
            rollback_plan = f"git checkout HEAD -- {target_clean} or restore from secure backup."
        elif len(impacted_symbols) > 5 or len(impacted_tests) > 3:
            tier = ReversibilityTier.HIGH_RISK
            risk_score = 65
            reasons.append(f"Wide Blast Radius: touches {len(impacted_symbols)} dependent callers and {len(impacted_tests)} test suites.")
            rollback_plan = f"git restore {target_clean}"
        else:
            tier = ReversibilityTier.REVERSIBLE
            risk_score = 25
            reasons.append("Localized file modification.")
            rollback_plan = f"git restore {target_clean}"

    elif action_type == "gateway_reconfig":
        tier = ReversibilityTier.HIGH_RISK
        risk_score = 75
        touches_sensitive = True
        reasons.append("Modifying MCP Gateway configuration (.autoharness/gateway.json).")
        rollback_plan = "Run 'autoharness gateway status' or revert gateway.json using git restore."

    else:
        tier = ReversibilityTier.HIGH_RISK
        risk_score = 50
        reasons.append(f"Unknown action type '{action_type}'. Treating as elevated risk.")
        rollback_plan = "Inspect system logs and environment manually."

    clamped_score = max(0, min(100, risk_score))
    judgment_focus = _build_judgment_focus(
        action_type=action_type,
        target=target_clean,
        tier=tier,
        risk_score=clamped_score,
        affected_files=affected_files,
        touches_sensitive=touches_sensitive,
        rollback_plan=rollback_plan,
        reasons=reasons,
    )

    return {
        "action_type": action_type,
        "target": target_clean,
        "reversibility_tier": tier.value,
        "risk_score": clamped_score,
        "reasons": reasons,
        "blast_radius": {
            "affected_files": affected_files,
            "impacted_symbols": impacted_symbols,
            "impacted_tests": impacted_tests,
            "touches_sensitive_paths": touches_sensitive,
        },
        "rollback_plan": rollback_plan,
        "judgment_focus": judgment_focus,
    }


def _build_judgment_focus(
    action_type: str,
    target: str,
    tier: ReversibilityTier,
    risk_score: int,
    affected_files: list[str],
    touches_sensitive: bool,
    rollback_plan: str,
    reasons: list[str],
) -> dict[str, Any]:
    """Generate Anti-Cognitive-Surrender judgment cues for human oversight."""
    attention_level = "critical" if tier == ReversibilityTier.IRREVERSIBLE or risk_score >= 70 else (
        "elevated" if tier == ReversibilityTier.HIGH_RISK or touches_sensitive else "standard"
    )
    why = reasons[0] if reasons else f"{action_type} on `{target}` carries {tier.value} reversibility."
    counter_q = (
        f"Before approving `{action_type}` on `{target}`, what breaks if rollback ({rollback_plan}) fails or misses downstream dependents?"
        if attention_level != "standard"
        else f"Confirm `{target}` matches intended scope ({len(affected_files)} affected file(s))."
    )
    return {
        "attention_level": attention_level,
        "why_not_routine": why,
        "counter_question": counter_q,
        "human_signoff_required": attention_level in ("critical", "elevated"),
    }


def _strip_dot_slash(path_text: str) -> str:
    norm = path_text.strip().replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return "" if norm in (".", "") else norm


def parse_autonomy_bounds(bounds: dict[str, Any] | str | None) -> dict[str, Any]:
    """Parse bounded-autonomy specification (`allowed_paths`, `max_tokens`, `escalate_on`)."""
    if not bounds:
        return {}
    if isinstance(bounds, dict):
        parsed = dict(bounds)
        for list_key in ("allowed_paths", "escalate_on"):
            if list_key in parsed and parsed[list_key] is not None:
                val = parsed[list_key]
                if isinstance(val, str):
                    items = [x.strip() for x in val.split(",") if x.strip()]
                elif isinstance(val, (list, tuple, set)):
                    items = [str(x).strip() for x in val if str(x).strip()]
                else:
                    raise ValueError(f"invalid {list_key} value: expected list or string")
                if not items:
                    raise ValueError(f"{list_key} cannot be empty")
                parsed[list_key] = [x.replace("\\", "/") for x in items] if list_key == "allowed_paths" else [x.lower() for x in items]
        if "max_tokens" in parsed and parsed["max_tokens"] is not None:
            mt = parsed["max_tokens"]
            if isinstance(mt, bool) or not isinstance(mt, int) or mt <= 0:
                raise ValueError(f"invalid max_tokens value {mt!r}: expected positive integer")
        return parsed
    parsed: dict[str, Any] = {}
    for part in re.split(r"[;\n]+", str(bounds)):
        part = part.strip()
        if not part:
            continue
        if "=" not in part and ":" not in part:
            raise ValueError(f"invalid autonomy bound segment {part!r}: expected key=value")
        sep = "=" if "=" in part else ":"
        k, v = part.split(sep, 1)
        key = k.strip().lower().replace("-", "_")
        val = v.strip()
        if not key:
            raise ValueError(f"empty bound key in {part!r}")
        if key in ("allowed_paths", "paths", "scope"):
            items = [x.strip().replace("\\", "/") for x in val.split(",") if x.strip()]
            if not items:
                raise ValueError("allowed_paths cannot be empty")
            parsed["allowed_paths"] = items
        elif key in ("escalate_on", "escalate"):
            items = [x.strip().lower() for x in val.split(",") if x.strip()]
            if not items:
                raise ValueError("escalate_on cannot be empty")
            parsed["escalate_on"] = items
        elif key == "max_tokens":
            try:
                mt = int(val)
            except ValueError as exc:
                raise ValueError(f"invalid max_tokens value {val!r}: expected positive integer") from exc
            if mt <= 0:
                raise ValueError(f"invalid max_tokens value {val!r}: expected positive integer")
            parsed["max_tokens"] = mt
        else:
            if not val:
                raise ValueError(f"empty value for autonomy bound {key!r}")
            parsed[key] = val
    return parsed


def check_autonomy_bounds(
    project_root: Path,
    target: str | None = None,
    bounds: dict[str, Any] | str | None = None,
    tokens_used: int | None = None,
    action_type: str = "",
) -> dict[str, Any]:
    """Check whether a planned target, action, or token budget stays within declared autonomy bounds."""
    spec = parse_autonomy_bounds(bounds)
    if not spec:
        return {"bounded": False, "within_bounds": True, "violations": [], "bounds": {}}

    violations: list[str] = []
    has_raw_allowed = "allowed_paths" in spec and bool(spec.get("allowed_paths"))
    allowed_paths = [_strip_dot_slash(str(p)) for p in (spec.get("allowed_paths") or []) if str(p).strip()]
    if target and (has_raw_allowed or allowed_paths):
        root_resolved = project_root.resolve()
        target_norm = target.replace("\\", "/")
        raw_path = Path(target_norm)
        candidate = raw_path.resolve() if raw_path.is_absolute() else (root_resolved / raw_path).resolve()
        try:
            rel = candidate.relative_to(root_resolved).as_posix()
            in_repo = True
        except ValueError:
            rel = target_norm
            in_repo = False
        if not in_repo or not any(
            ap == "" or rel == ap.rstrip("/") or rel.startswith(ap.rstrip("/") + "/")
            for ap in allowed_paths
        ):
            violations.append(f"Target `{rel}` is outside allowed_paths {allowed_paths}")

    max_tokens = spec.get("max_tokens")
    if (
        not isinstance(max_tokens, bool)
        and isinstance(max_tokens, int)
        and max_tokens > 0
        and tokens_used is not None
        and tokens_used > max_tokens
    ):
        violations.append(f"Token budget exceeded: {tokens_used} > max_tokens {max_tokens}")

    escalate_on = [e.lower() for e in spec.get("escalate_on", []) if str(e).strip()]
    if action_type and escalate_on and any(tok in action_type.lower() for tok in escalate_on):
        violations.append(f"Action `{action_type}` triggers declared escalation rule {escalate_on}")

    return {
        "bounded": True,
        "within_bounds": len(violations) == 0,
        "violations": violations,
        "bounds": spec,
    }


def format_consequence_manifest(simulation: dict[str, Any]) -> str:
    """Format simulation output as a clean, human-readable Consequence Manifest Markdown."""
    tier = simulation.get("reversibility_tier", "REVERSIBLE")
    score = simulation.get("risk_score", 0)
    action = simulation.get("action_type", "unknown")
    target = simulation.get("target", "unknown")
    blast = simulation.get("blast_radius", {})
    files = blast.get("affected_files", [])
    symbols = blast.get("impacted_symbols", [])
    tests = blast.get("impacted_tests", [])
    sensitive = blast.get("touches_sensitive_paths", False)
    rollback = simulation.get("rollback_plan", "None")
    jfocus = simulation.get("judgment_focus", {})

    lines = [
        f"# Consequence Simulation Manifest (Risk Score: {score}/100 [{tier}])",
        "",
        "## Action Summary",
        f"- **Action Type:** `{action}`",
        f"- **Target:** `{target}`",
        f"- **Reversibility Tier:** `{tier}`",
        f"- **Risk Score:** {score}/100",
        "",
        "## Blast Radius & Impact",
        f"- **Affected Files ({len(files)}):** {', '.join(files) if files else 'None'}",
        f"- **Impacted Dependent Symbols ({len(symbols)}):** {', '.join(symbols[:10]) if symbols else 'None'}{'...' if len(symbols) > 10 else ''}",
        f"- **Impacted Test Suites ({len(tests)}):** {', '.join(tests[:10]) if tests else 'None'}{'...' if len(tests) > 10 else ''}",
        f"- **Touches Sensitive Security/Config Paths:** {'YES (Elevation Warning)' if sensitive else 'No'}",
        "",
        "## Risk Rationale",
    ]
    for r in simulation.get("reasons", []):
        lines.append(f"- {r}")

    if jfocus:
        lines.extend([
            "",
            "## Human Judgment Focus (Anti-Cognitive-Surrender)",
            f"- **Attention Level:** `{jfocus.get('attention_level', 'standard')}`",
            f"- **Why Not Routine:** {jfocus.get('why_not_routine', '')}",
            f"- **Counter-Question:** {jfocus.get('counter_question', '')}",
        ])

    lines.extend([
        "",
        "## Rollback Guidance",
        f"- {rollback}",
    ])

    return "\n".join(lines).strip()
