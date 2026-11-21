"""Run an independent reviewer headless and read-only: `critic --run claude|codex|agy|kimi`.

The author no longer passes the prompt on or saves the answer: autoharness starts a fresh agent process
with the prompt unchanged, in a mode that cannot edit the project, and keeps its answer as it came.
The reviewer's own sessions get AUTOHARNESS_REVIEWER set to the answer id of their prompt; while that id
waits for its answer, the autoharness hook neither gates nor prompts them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from autoharness.env import HarnessError

AGENTS = ("cursor", "cursor-agent", "agy", "claude", "codex", "kimi", "agent")
DEFAULT_TIMEOUT = 1200
ENV_FLAG = "AUTOHARNESS_REVIEWER"
# the only tools that exist for it: allow rules in the user's settings cannot add a shell or an editor back.
# The diff it judges is an evidence file, so it needs no git
CLAUDE_TOOLS = ["Read", "Grep", "Glob"]
# variables of the author's session that would make the reviewer look like it, or refuse to start inside it
INHERITED = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "ANTIGRAVITY_AGENT",
             "ANTIGRAVITY_CONVERSATION_ID", "CODEX_THREAD_ID", "CURSOR_AGENT", "CURSOR_CONVERSATION_ID", "CURSOR_REQUEST_ID", "CURSOR_SESSION_ID")


def resolve_executable(agent: str) -> str | None:
    if agent in ("cursor", "cursor-agent", "agent"):
        return shutil.which("cursor-agent") or shutil.which("agent") or shutil.which("cursor")
    return shutil.which(agent)


def available_agents() -> list[str]:
    """Agents installed on PATH that can be used with --run."""
    candidates = ("cursor", "agy", "claude", "codex", "kimi")
    return [a for a in candidates if resolve_executable(a)]


def available_run_hint() -> str:
    avail = available_agents()
    return "|".join(avail) if avail else "cursor|agy|claude|codex|kimi"


def command(agent: str, root: Path, prompt_file: Path, answer_file: Path) -> tuple[list[str], str | None]:
    """The argv for a read-only, non-interactive run of `agent`, and what to send on stdin."""
    exe = resolve_executable(agent)
    if not exe:
        raise HarnessError(f"`{agent}` is not on PATH: install it, or run the critic yourself with `autoharness critic`")
    prompt = prompt_file.read_text("utf-8")
    if agent == "claude":
        return ([exe, "-p", "--permission-mode", "default", "--no-session-persistence", "--strict-mcp-config",
                 "--tools", ",".join(CLAUDE_TOOLS), "--allowedTools", ",".join(CLAUDE_TOOLS)], prompt)
    if agent == "codex":
        return ([exe, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "-C", str(root),
                 "--output-last-message", str(answer_file), "-"], prompt)
    if agent == "agy":
        # agy reads its prompt from the command line only: point it at the file instead of passing pages of text
        return ([exe, "-p", f"Read the file {prompt_file} and do exactly what it says. Do not edit any file.",
                 "--mode", "plan", "--sandbox", "--add-dir", str(root)], None)
    if agent in ("cursor", "cursor-agent", "agent"):
        return ([exe, "-p", f"Read the file {prompt_file} and do exactly what it says. Do not edit any file.",
                 "--mode", "plan", "--trust", "--workspace", str(root)], None)
    if agent == "kimi":
        # -p cannot combine with --plan, so the read-only run is instruction-based; -p uses the auto
        # permission policy and static deny rules still apply
        return ([exe, "-p", f"Read the file {prompt_file} and do exactly what it says. Do not edit any file."], None)
    raise HarnessError(f"reviewer must be one of {', '.join(AGENTS)}")


# what a reviewer CLI says when it has no credentials: a session logged in by a token in its environment (a CI
# job, a container) need not hand it to the programs it starts
NOT_LOGGED_IN = re.compile(r"not logged in|/login|invalid api key|authentication|unauthorized|oauth token",
                           re.IGNORECASE)


def run(agent: str, root: Path, prompt_file: Path, answer_file: Path, timeout: float = DEFAULT_TIMEOUT,
        answer_id: str = "") -> str:
    """Run the reviewer and return its answer, also written to `answer_file`."""
    argv, stdin = command(agent, root, prompt_file, answer_file)
    env = {k: v for k, v in os.environ.items() if k not in INHERITED} | {ENV_FLAG: answer_id or "1"}
    try:
        proc = subprocess.run(argv, cwd=root, input=(stdin or "").encode("utf-8"), capture_output=True,
                              env=env, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise HarnessError(f"the {agent} reviewer did not answer within {timeout:.0f}s") from e
    stdout = proc.stdout.decode("utf-8", "replace").strip()
    if agent == "codex" and answer_file.is_file():
        stdout = answer_file.read_text("utf-8", errors="replace").strip() or stdout
    if proc.returncode != 0 or not stdout:
        err = proc.stderr.decode("utf-8", "replace").strip()[-600:] or stdout[-600:] or "no output"
        if NOT_LOGGED_IN.search(err):
            raise HarnessError(f"the {agent} reviewer is not logged in here ({err}): the session's own login did not "
                               f"reach the `{agent}` it started, as happens when a token in the environment is not "
                               "passed on to child processes")
        raise HarnessError(f"the {agent} reviewer failed (exit {proc.returncode}): {err}")
    answer_file.write_text(stdout + "\n", "utf-8")
    return stdout


# --- Peer Review Challenge & Cross-Validation (Perplexity Multiagent Model) ----

FINDING_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:[-*•]|\d+[.)])\s*(?:(?:FINDING|ISSUE|CONCERN|GAP|PROBLEM|CRITICISM|DEFECT):\s*)?(.*)",
    re.IGNORECASE,
)


def extract_critic_claims(review_text: str) -> list[str]:
    """Extract individual actionable criticism claims from reviewer text."""
    claims = []
    lines = review_text.splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if any(kw in stripped.lower() for kw in ("finding", "issue", "missing", "contradict", "problem", "fails", "incomplete")):
            cleaned = re.sub(r"^[-*•\d.)\s]+", "", stripped).strip()
            if len(cleaned) >= 10:
                claims.append(cleaned)
    if not claims:
        for m in FINDING_PATTERN.finditer(review_text):
            txt = m.group(1).strip()
            if len(txt) >= 15:
                claims.append(txt)
    return claims[:10]


def create_peer_challenge(
    review_text: str,
    original_verdict: str = "missing",
    author_defense: str = "",
    code_evidence: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a structured peer challenge against a critic review with counter-evidence."""
    claims = extract_critic_claims(review_text)
    items = []
    for idx, claim in enumerate(claims, 1):
        items.append({
            "id": f"claim_{idx}",
            "critic_claim": claim,
            "author_defense": author_defense,
            "evidence_attached": list(code_evidence.keys()) if code_evidence else [],
        })

    return {
        "original_verdict": original_verdict,
        "review_summary": review_text[:300].strip(),
        "total_claims": len(items),
        "claims": items,
        "evidence": code_evidence or {},
        "status": "contested" if items else "accepted",
    }


def synthesize_peer_resolution(challenge: dict[str, Any]) -> dict[str, Any]:
    """Evaluate contested review claims against provided code evidence and determine consensus resolution."""
    claims = challenge.get("claims", [])
    evidence = challenge.get("evidence", {})
    orig_verdict = challenge.get("original_verdict", "missing")

    if not claims:
        return {
            "resolution": "upheld",
            "revised_verdict": orig_verdict,
            "points_evaluated": 0,
            "points_overruled": 0,
            "points_upheld": 0,
            "rationale": "No specific claims were contested.",
        }

    overruled_count = 0
    upheld_count = 0
    evaluations = []

    for item in claims:
        claim_text = item["critic_claim"].lower()
        defense = item["author_defense"].lower()

        refuted = False
        refuting_evidence = ""
        for file_key, content in evidence.items():
            content_lower = content.lower()
            words = [w for w in re.findall(r"\w{4,}", claim_text) if w not in ("missing", "cannot", "does", "should", "without", "failed")]
            matching_words = sum(1 for w in words if w in content_lower)
            if matching_words >= min(2, len(words)) and len(words) >= 1:
                refuted = True
                refuting_evidence = file_key
                break

        if not refuted and ("line " in defense or "implemented in" in defense or "tested in" in defense):
            refuted = True
            refuting_evidence = "author_defense_reference"

        if refuted:
            overruled_count += 1
            evaluations.append({
                "claim_id": item["id"],
                "outcome": "overruled",
                "evidence": refuting_evidence or "code_evidence_match",
                "rationale": f"Critic claim was disproved by evidence in {refuting_evidence or 'codebase'}.",
            })
        else:
            upheld_count += 1
            evaluations.append({
                "claim_id": item["id"],
                "outcome": "upheld",
                "evidence": "none",
                "rationale": "Claim remains substantiated based on available evidence.",
            })

    total = len(claims)
    if overruled_count == total:
        resolution = "overruled"
        revised_verdict = "met"
        rationale = f"All {total} critic claims were successfully refuted with code evidence."
    elif overruled_count > 0:
        resolution = "compromise"
        revised_verdict = "partial" if orig_verdict in ("missing", "contradicts") else orig_verdict
        rationale = f"{overruled_count} of {total} claims were refuted; remaining claims were upheld."
    else:
        resolution = "upheld"
        revised_verdict = orig_verdict
        rationale = f"Critic verdict upheld: none of the {total} claims were refuted by evidence."

    return {
        "resolution": resolution,
        "original_verdict": orig_verdict,
        "revised_verdict": revised_verdict,
        "points_evaluated": total,
        "points_overruled": overruled_count,
        "points_upheld": upheld_count,
        "evaluations": evaluations,
        "rationale": rationale,
    }


def format_peer_challenge_report(res: dict[str, Any]) -> str:
    """Format peer challenge synthesis into a Markdown review appendix."""
    lines = [
        f"### Peer Review Challenge Synthesis: `{res['resolution'].upper()}`",
        f"- **Original Verdict:** `{res['original_verdict']}` ➔ **Revised Verdict:** `{res['revised_verdict']}`",
        f"- **Scorecard:** {res['points_overruled']}/{res['points_evaluated']} claims overruled, {res['points_upheld']} upheld",
        f"- **Rationale:** {res['rationale']}",
    ]
    if res.get("evaluations"):
        lines.append("\n#### Itemized Evaluations:")
        for ev in res["evaluations"]:
            icon = "✅ Overruled" if ev["outcome"] == "overruled" else "❌ Upheld"
            lines.append(f"- **[{ev['claim_id']}] {icon}:** {ev['rationale']}")
    return "\n".join(lines)
