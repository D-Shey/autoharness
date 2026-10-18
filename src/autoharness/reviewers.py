"""Run an independent reviewer headless and read-only: `critic --run claude|codex|agy`.

The author no longer passes the prompt on or saves the answer: autoharness starts a fresh agent process
with the prompt unchanged, in a mode that cannot edit the project, and keeps its answer as it came.
The reviewer's own sessions get AUTOHARNESS_REVIEWER set to the answer id of their prompt; while that id
waits for its answer, the autoharness hook neither gates nor prompts them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from autoharness.env import HarnessError

AGENTS = ("claude", "codex", "agy")
DEFAULT_TIMEOUT = 1200
ENV_FLAG = "AUTOHARNESS_REVIEWER"
# the only tools that exist for it: allow rules in the user's settings cannot add a shell or an editor back.
# The diff it judges is an evidence file, so it needs no git
CLAUDE_TOOLS = ["Read", "Grep", "Glob"]
# variables of the author's session that would make the reviewer look like it, or refuse to start inside it
INHERITED = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "ANTIGRAVITY_AGENT",
             "ANTIGRAVITY_CONVERSATION_ID", "CODEX_THREAD_ID")


def command(agent: str, root: Path, prompt_file: Path, answer_file: Path) -> tuple[list[str], str | None]:
    """The argv for a read-only, non-interactive run of `agent`, and what to send on stdin."""
    exe = shutil.which(agent)
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
    raise HarnessError(f"reviewer must be one of {', '.join(AGENTS)}")


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
        err = proc.stderr.decode("utf-8", "replace").strip()[-600:]
        raise HarnessError(f"the {agent} reviewer failed (exit {proc.returncode}): {err or stdout[-600:] or 'no output'}")
    answer_file.write_text(stdout + "\n", "utf-8")
    return stdout
