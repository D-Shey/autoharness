"""Session state, agent hooks, tool gating and session claiming."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from autoharness import agents, evidence, lessons, library, recipes, reviewers, text
from autoharness.catalog import Catalog
from autoharness.env import GATEWAY_NAME, HarnessError, Project, Task
from autoharness.transactions import atomic_write

LEVELS = ("light", "full", "reviewed")
ARGS_FILE = "@"


def out(data) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


HOOK_COMMAND = "autoharness hook"
HOOK_MIN_WORDS = 4
# tools that do work; reading and tool discovery stay open so the agent can look around
GATED_TOOLS = re.compile(r"^(Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|mcp__.*)$")
PRE_TOOL_MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|Skill|mcp__.*"
# Antigravity CLI tools that do work, as the Claude Code tool each stands for (shell and MCP are mapped in _agy_event)
AGY_TOOLS = {"write_file": "Write", "edit_file": "Edit", "write_to_file": "Write",
             "replace_file_content": "Edit", "multi_replace_file_content": "Edit",
             "edit_notebook": "NotebookEdit", "run_command": "", "send_command_input": "", "call_mcp_tool": ""}
AGY_PRE_TOOL_MATCHER = "|".join(AGY_TOOLS)
AGY_USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
KIMI_PRE_TOOL_MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|Skill|shell|edit_file|write_file|mcp__.*"
KIMI_TOOLS = {"write_file": "Write", "edit_file": "Edit", "shell": "Bash"}


def session_file(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:80]
    return recipes.home() / "sessions" / f"{safe}.json"


def session_state(session_id: str) -> dict:
    try:
        return json.loads(session_file(session_id).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


PROMPT_KEPT = 20000   # the prompt a session started with: the user's own words, which the brief quotes


def set_session_state(session_id: str, **state) -> None:
    f = session_file(session_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    from autoharness.transactions import atomic_write
    atomic_write(f, json.dumps(state, ensure_ascii=False).encode("utf-8"))


# commands that change the active task: only the agent session that started it (or claimed it) runs them
OWNED = {"mode", "decision", "verify", "need", "add", "remove", "vet", "teardown", "evidence", "review", "critic",
         "approach", "arbiter", "arbitrate", "check", "brief"}
STALE_HOURS = 4   # a task nobody touched for this long is probably abandoned


def idle_hours(task: Task) -> float:
    """Hours since the task last changed (its log or its record)."""
    times = [p.stat().st_mtime for p in (task.dir / "harness.md", task.dir / "task.json") if p.exists()]
    return (time.time() - max(times)) / 3600 if times else 0.0


def handoff(task: Task, session_id: str) -> str:
    """What a session that does not own the active task is told instead of starting its own."""
    info = task.info()
    idle = idle_hours(task)
    log = [line for line in (task.dir / "harness.md").read_text("utf-8").splitlines() if line.startswith("- ")]
    stale = (f" Nothing happened in it for {idle:.0f} hours: it looks abandoned." if idle >= STALE_HOURS else
             f" It last changed {idle * 60:.0f} minutes ago, so the other session may still be working on it.")
    held = ("It does not hold this session's tools: go on with the user's request. This session may still "
            "neither start nor tear down a task while that one is active." if idle >= STALE_HOURS else
            "Tools that do work stay blocked for this session, and it may neither start nor tear down a task while "
            "that one is active.")
    return (f"autoharness: task {task.id} is active in this project and belongs to another agent session "
            f"({info.get('owner')}): \"{info.get('task', '')[:300]}\" (mode {info.get('mode', 'full')}"
            f"{', brief recorded' if info.get('brief') else ', no brief yet'}).{stale} Last steps: "
            + (" | ".join(log[-3:]) or "none") + f". {held} Tell the user. If they want this session "
            f"to carry that task on (or its session is gone), run `autoharness claim {session_id} --reason "
            "\"<their words>\"` and continue from `autoharness status`.")


def check_owner(project: Project, task_id: str | None) -> None:
    """Refuse when the task belongs to another agent session than the one running this command. A command
    outside any agent session (the user's own terminal) or a task without an owner is not refused."""
    if not task_id:
        return
    task = Task(project, task_id)
    owner, me = task.info().get("owner"), my_session(project, task)
    if owner and me and owner != me and task.info().get("status") == "active":
        raise HarnessError(handoff(task, me))


def my_session(project: Project, task: Task | None = None) -> str:
    """The session id of the agent running this command: the one named with --agent, else the task's own agent,
    so a command that leaves out --agent is not taken for the outer agent whose shell started this one."""
    agent = project.agent_name or (task.info().get("agent") if task else None)
    return agents.session_id(agent=agent if isinstance(agent, str) else None)


def write_lock(project: Project):
    """The project lock, for a long command's writes (main holds it for every other command already)."""
    from autoharness import transactions
    return transactions.locked(project.root)


def cmd_claim(args, project, catalog):
    task = project.task()
    me = args.session_id or my_session(project, task)
    if not me:
        raise HarnessError("no agent session id in the environment: pass it, `autoharness claim <session id>`")
    info = task.info()
    previous = info.get("owner")
    mine = session_state(me)
    if os.environ.get(reviewers.ENV_FLAG) or (mine.get("state") == "reviewer"
                                              and mine.get("reviews") in _pending_answers(info)):
        raise HarnessError("this session was given a review prompt of this task: a reviewer answers it and does not "
                           "take the task over. Write the answer the prompt asks for and stop.")
    if previous == me:
        out({"status": "ok", "task_id": task.id, "owner": me, "next": "This session already owns the task."})
        return 0
    task._update_info(owner=me, claims=info.get("claims", []) + [
        {"from": previous, "to": me, "reason": args.reason or "", "time": time.strftime("%Y-%m-%dT%H:%M:%S")}])
    task.log(f"claimed by session {me} from {previous or 'no owner'}" + (f": {args.reason}" if args.reason else ""))
    out({"status": "ok", "task_id": task.id, "owner": me, "previous_owner": previous,
         "next": "This session owns the task now; the other session is refused if it goes on. Read "
                 "`autoharness status` and carry on from where the log stops."})
    return 0


def hook_context(catalog: Catalog, prompt: str, session_id: str = "") -> str | None:
    """The reminder a prompt hook injects, or None for short replies."""
    if len(text.words(prompt)) < HOOK_MIN_WORDS:
        return None
    caps = catalog.classify(prompt)
    listed = ("This task likely needs: " + "; ".join(f"{c.name} ({c.description})" for c, _ in caps) + ". "
              if caps else "")
    gate = (f" Tools that do work (Write, Edit, shell, MCP) stay blocked until `autoharness init` has run and "
            f"`autoharness brief` is recorded. Only if the user explicitly said not to "
            f"use autoharness, run `autoharness waive {session_id} --reason \"<their words>\"`." if session_id else "")
    return ("autoharness: invoke the `autoharness` skill as your first step, before producing anything. "
            + listed + "It works out what a good result looks like, which inputs are missing (references, docs, "
            "data), what the user's constraints are, and how the result will be verified." + gate)


# PowerShell reads these as quotes: a single one ends a '...' string, a double one a "..." string, and inside
# the other kind each is a plain character (`"the hero’s"`), as it always is in Bash
SMART_SINGLE, SMART_DOUBLE = "‘’‚‛", "“”„"
SMART_QUOTES = SMART_SINGLE + SMART_DOUBLE
SHELL_SPECIAL = set(";|&<>#(){}`\r\n")
# stderr into the output the agent reads, and read-only filters of that output: harmless in both shells
# an argument of such a filter: single-quoted, double-quoted with nothing either shell expands, or plain
# spaces and tabs only between the parts: a newline is a new command, never an argument
FILTER_ARG = r"""(?:'[^'\n]*'|"[^"\n$`\\]*"|[\w./\[\]-]+)"""
FILTER_OPTION = r"(?:-[a-zA-Z]+|--[a-z][a-z-]*(?:=" + FILTER_ARG + r")?)"
READ_ONLY_FILTER = (r"(?:(?:head|tail)(?:[ \t]+-[nc][ \t]*\+?\d+|[ \t]+-\d+)?"
                    r"|wc(?:[ \t]+-[lwcm]+)?"
                    r"|Select-Object[ \t]+-(?:First|Last)[ \t]+\d+"
                    r"|Select-Object[ \t]+-ExpandProperty[ \t]+" + FILTER_ARG +
                    r"|Select-String(?:[ \t]+-[a-zA-Z]+)*[ \t]+" + FILTER_ARG +
                    r"|jq(?:[ \t]+" + FILTER_OPTION + r")*(?:[ \t]+" + FILTER_ARG + r")*"
                    r"|grep(?:[ \t]+" + FILTER_OPTION + r")*(?:[ \t]+" + FILTER_ARG + r")+"
                    r"|Out-String|ConvertFrom-Json)")
TRAILING_OUTPUT = re.compile(r"(?:[ \t]+2>(?:&1|/dev/null|\$null)|[ \t]*\|[ \t]*" + READ_ONLY_FILTER + r")*\Z",
                             re.IGNORECASE)
ENV_PREFIX = re.compile(r"^AUTOHARNESS_[A-Z_]+=")   # `AUTOHARNESS_AGENT=codex autoharness ...` in Bash
PLAIN_ESCAPED = re.compile(r"[\w/\\.:-]")   # after an unquoted `\`: a path, not an escape of anything special


def _plain_commands(command: str, bash: bool) -> list[list[str]] | None:
    """The `&&`- or `;`-separated commands of `command` as lists of words, or None if it holds anything that
    Bash or PowerShell could read as more than plain words. Deliberately stricter than both shells (a `#`, `|`
    or redirection outside quotes, `$'`, `@"`, and `$(` or a backtick anywhere but in single quotes, are
    refused outright), so that where the two disagree about quoting nothing slips through either. Only Bash
    gets `\\` line continuations and `\\"` inside double quotes; PowerShell reads a backslash literally."""
    commands, words, word, quote = [], [], None, None
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = None
            elif ch in SMART_SINGLE:
                return None   # ends the string in PowerShell, not in Bash
            else:
                word += ch
        elif quote == '"':
            if ch == '"':
                quote = None
            elif ch in SMART_DOUBLE:
                return None   # ends the string in PowerShell, not in Bash
            elif ch == "\\" and bash and i + 1 < n:
                word += command[i + 1]   # Bash escapes the next character, `\"` and `\$(` included
                i += 1
            elif ch == "\\" and command[i + 1:i + 2] in ('"', "\\"):
                return None   # PowerShell does not escape the quote, so Bash reading would differ
            elif ch == "`" or command.startswith("$(", i):
                return None   # runs a command in Bash; a backtick escapes in PowerShell
            else:
                word += ch
        elif ch in "'\"":
            if command[i - 1:i] in ("$", "@"):
                return None   # $'...' and @"..." quote by other rules
            quote, word = ch, word or ""
        elif ch in " \t":
            if word is not None:
                words.append(word)
            word = None
        elif ch == "\\" and command[i + 1:i + 2] == "\n":
            if not bash:
                return None
            i += 1
            if word is not None:
                words.append(word)
            word = None
        elif ch == "\\" and not PLAIN_ESCAPED.match(command[i + 1:i + 2]):
            return None       # Bash would escape a quote or an operator here
        elif command.startswith("&&", i) or ch == ";":   # `;`: Windows PowerShell 5.1 has no `&&`
            if word is not None:
                words.append(word)
            commands.append(words)
            words, word = [], None
            i += 1 if ch == "&" else 0
        elif ch in SHELL_SPECIAL or ch in SMART_QUOTES:
            return None   # outside quotes PowerShell starts a string at a smart quote; Bash does not
        else:
            word = (word or "") + ch
        i += 1
    if quote:
        return None
    if word is not None:
        words.append(word)
    return [*commands, words] if words or not commands else commands   # a trailing `;` ends nothing new


# data fed to one autoharness command on stdin, never run: a Bash heredoc with a single-quoted delimiter
# (no expansion), or a PowerShell single-quoted here-string piped into it
HEREDOC_START = re.compile(r"[ \t]<<[ \t]*'([A-Za-z_]\w*)'[ \t]*\r?\n")
HERE_STRING_START = re.compile(r"\A@'[ \t]*\r?\n")


def _without_stdin(tool: str, command: str) -> str:
    """The command without the data it is fed on stdin. The data ends where the shell ends it, at the first
    closing line, and nothing may follow that line: what did would run."""
    if tool == "Bash":
        m = HEREDOC_START.search(command)
        if not m:
            return command
        lines = command[m.end():].split("\n")
        close = next((n for n, line in enumerate(lines) if line.rstrip("\r") == m.group(1)), None)
        if close is None or any(line.strip() for line in lines[close + 1:]):
            return command    # unclosed, or more after it: left for the plain-command check to refuse
        return command[:m.start()]
    m = HERE_STRING_START.match(command)
    if not m:
        return command
    end = re.search(r"\r?\n'@", command[m.end() - 1:])
    if not end:
        return command
    rest = command[m.end() - 1 + end.end():]
    pipe = re.match(r"[ \t]*\|[ \t]*(?=autoharness\s)", rest)
    return rest[pipe.end():] if pipe else command


def _is_autoharness_call(tool: str, tool_input: dict) -> bool:
    """The skill, or one plain `autoharness ...` command (after any `cd <dir> &&`). Nothing may run after it
    in the same call, so the gate is not passed by chaining another command on. The gate keeps the agent to
    the procedure; it is not a sandbox (the agent may also `waive` it)."""
    if tool == "Skill":
        return str(tool_input.get("skill", "")).split(":")[-1] == "autoharness"
    if tool not in ("Bash", "PowerShell"):
        return False
    command = TRAILING_OUTPUT.sub("", _without_stdin(tool, str(tool_input.get("command", "")).strip()))
    commands = _plain_commands(command, bash=tool == "Bash")
    if not commands:
        return False
    *prefix, last = commands
    while tool == "Bash" and last and ENV_PREFIX.match(last[0]):
        last = last[1:]
    return (all(len(c) == 2 and c[0] == "cd" for c in prefix)
            and (last[:1] == ["autoharness"] or _looks_up_autoharness(last) or _reads_only(last)))


READERS = {"ls", "dir", "pwd", "tree", "cat", "head", "tail", "type", "get-childitem", "gci", "get-location",
           "get-content", "gc", "get-item", "test-path", "resolve-path"}
GIT_READERS = {"status", "log", "diff", "show", "ls-files", "branch", "remote", "rev-parse"}


def _reads_only(words: list[str]) -> bool:
    """A listing or reading of files (`ls`, `Get-ChildItem`, `cat`, `git status`): reading stays open before the
    brief, and these only read. Redirection and chaining never reach here (`_plain_commands` refuses them)."""
    if not words:
        return False
    head = words[0].lower()
    return head in READERS or (head == "git" and len(words) > 1 and words[1].lower() in GIT_READERS)


def _looks_up_autoharness(words: list[str]) -> bool:
    """`Get-Command autoharness`, `which autoharness` and the like: they only say whether the CLI is there."""
    lookup = [w for w in words if not w.startswith("-") and w.lower() not in ("silentlycontinue", "ignore")]
    return (len(lookup) == 2 and lookup[1] == "autoharness"
            and lookup[0].lower() in ("get-command", "gcm", "which", "where", "where.exe", "type")) \
        or words == ["command", "-v", "autoharness"]


# The task records under .autoharness/ are written by autoharness only (config.json is the user's to edit).
# A write tool aimed there, or a shell command that plainly writes there, is denied; the seal on task.json
# catches what gets past this at teardown. A guard against shortcuts, not a sandbox.
RECORDS = re.compile(r"\.autoharness(?![\w.-])")
KEY_FILE = re.compile(r"integrity\.key", re.I)
_AT_COMMAND = r"(?:^|[;&|({\n]|&&|\|\|)\s*(?:sudo\s+)?"   # where a command word starts
SHELL_WRITES = [
    re.compile(r">>?\s*['\"]?[^\s'\";|&]*\.autoharness(?![\w.-])"),           # > .autoharness/x
    re.compile(_AT_COMMAND + r"tee\b[^|;&\n]*\.autoharness(?![\w.-])"),
    re.compile(_AT_COMMAND + r"sed\b[^|;&\n]*\s-i[^|;&\n]*\.autoharness(?![\w.-])"),
    re.compile(_AT_COMMAND + r"(cp|mv|rm|rmdir|ln|touch|truncate|install|rsync|dd|del|erase|copy|move|ren|rename)\b"
               r"[^|;&\n]*\.autoharness(?![\w.-])", re.I),
    re.compile(_AT_COMMAND + r"(Set-Content|Add-Content|Clear-Content|Out-File|New-Item|Remove-Item|Copy-Item|"
               r"Move-Item|Rename-Item|sc|ac|ni|ri|cpi|mi|rni)\b[^|;\n]*\.autoharness(?![\w.-])", re.I),
    re.compile(r"\|\s*(Set-Content|Add-Content|Out-File)\b[^|;\n]*\.autoharness(?![\w.-])", re.I),
    re.compile(r"open\([^)]*\.autoharness[^)]*['\"][wax]"),
    re.compile(r"(write_text|write_bytes|WriteAllText|WriteAllBytes|unlink|rmtree|os\.remove|json\.dump\b)"
               r"[\s\S]*\.autoharness(?![\w.-])|\.autoharness(?![\w.-])[\s\S]*"
               r"(write_text|write_bytes|WriteAllText|WriteAllBytes|unlink|rmtree|os\.remove|json\.dump\b)"),
]
# text a command only carries, not runs: a commit message, a heredoc fed to something other than an interpreter
_MESSAGE = re.compile(r"""(\s(?:-m|--message)[= ]?\s*)("(?:[^"\\]|\\.)*"|'[^']*')""")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n([\s\S]*?)^\s*\2\s*$", re.M)
_INTERPRETER = re.compile(r"\b(python[\d.]*|py|node|bash|sh|zsh|pwsh|powershell|ruby|perl|php|deno|bun|uv run)\b")


def _shell_code(command: str) -> str:
    """The command without the text it only carries, so a commit message about `.autoharness/` is not a write."""
    command = _MESSAGE.sub(r"\1''", command)

    def body(m: re.Match) -> str:
        line = command[command.rfind("\n", 0, m.start()) + 1:m.start()]
        return m.group(0) if _INTERPRETER.search(line) else m.group(0).replace(m.group(3), "")
    return _HEREDOC.sub(body, command)


RECORDS_DENIED = ("`.autoharness/` holds this task's records, and only autoharness writes them: record through its "
                  "commands (`evidence add`, `review`, `verify run`, `check`, `brief`, ...). Reading them is fine. "
                  "Teardown also checks the records against a seal, so an edit made around autoharness shows there. "
                  "If the user asked you to change a setting, `.autoharness/config.json` is theirs to edit.")
KEY_DENIED = "autoharness's integrity key seals the task records: it is not for tools to read or change."


def _is_args_file(path: Path | str) -> bool:
    """Whether a path is an arguments/brief payload file or in scratch: agents can write these to pass to CLI."""
    p = Path(path)
    name = p.name.lower()
    return (name in ("args.json", "brief.json", "payload.json")
            or name.endswith(".args.json") or name.endswith(".brief.json")
            or "scratch" in p.parts)


def _path_values(tool_input: dict) -> list[str]:
    """The paths a write tool is aimed at: Claude Code's `file_path`/`notebook_path`, agy's `TargetFile` and the
    like (any argument whose name says path or file)."""
    return [v for k, v in tool_input.items()
            if isinstance(v, str) and v and re.search(r"path|file", k, re.I) and "\n" not in v]


def _writes_records(tool: str, tool_input: dict, root: Path) -> str | None:
    """Why this call is denied when it would write the task records or touch the integrity key, else None."""
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        state = (root / ".autoharness").resolve()
        for value in _path_values(tool_input):
            if KEY_FILE.search(value):
                return KEY_DENIED
            target = Path(value) if Path(value).is_absolute() else root / value
            try:
                target = target.resolve()
            except OSError:
                continue
            if (target == state or state in target.parents) and target != state / "config.json":
                if _is_args_file(target):
                    continue
                return RECORDS_DENIED
        return None
    if tool not in ("Bash", "PowerShell"):
        return None
    command = str(tool_input.get("command", ""))
    if KEY_FILE.search(command):
        return KEY_DENIED
    command = re.sub(r"\.autoharness[\\/](?:config\.json|args\.json|brief\.json|payload\.json|[^/\\]+\.(?:args|brief)\.json|scratch[\\/]\S+)", "", _shell_code(command))
    if RECORDS.search(command) and any(w.search(command) for w in SHELL_WRITES):
        return RECORDS_DENIED
    return None


def _autoharness_words(tool: str, tool_input: dict) -> list[str]:
    """The words after `autoharness` in a call that `_is_autoharness_call` let through."""
    if tool not in ("Bash", "PowerShell"):
        return []
    command = TRAILING_OUTPUT.sub("", _without_stdin(tool, str(tool_input.get("command", "")).strip()))
    words = (_plain_commands(command, bash=tool == "Bash") or [[]])[-1]
    return words[words.index("autoharness") + 1:] if "autoharness" in words else []


# Flags that end a task on the user's word: the agent may pass them only after the user said something in the task
def _earlier_level(project, text: str) -> str | None:
    """The highest level this project started the same task text at before, if it did."""
    found = None
    for info_path in (project.state / "tasks").glob("*/task.json"):
        try:
            info = json.loads(info_path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if info.get("task", "").strip() != text.strip():
            continue
        # If an earlier task started as light and was only raised to full by the lightening rule,
        # its base level was light. The lightening rule will re-evaluate on its own at init.
        requested = info.get("requested")
        log = info_path.parent / "harness.md"
        was_lightened = (requested == "light" and (info.get("mode") == "full" or (
            log.is_file() and "light raised to full" in log.read_text("utf-8", errors="replace")))) or (
            requested is None and log.is_file() and "light raised to full" in log.read_text("utf-8", errors="replace"))
        if was_lightened and info.get("profile") != "reviewed":
            level = "light"
        else:
            level = "reviewed" if info.get("profile") == "reviewed" else info.get("mode", "full")
        if level in LEVELS and (found is None or LEVELS.index(level) > LEVELS.index(found)):
            found = level
    return found


def _agent_skills(project, plugins: bool = True) -> list[Path]:
    """The SKILL.md files the agent loads on its own: the project's and the user's skills, and its plugins'."""
    agent = agents.get(project.agent_name)
    found = [p for d in (agent.skills_path(project.root), agent.user_skills_path()) if d.is_dir()
             for p in d.glob("*/SKILL.md")]
    plugin_dir = agent.user_skills_path().parent / "plugins"
    if plugins and plugin_dir.is_dir():
        found += [p for p in plugin_dir.rglob("SKILL.md") if p.parent.parent.name == "skills"]
    return found


def _agent_mcp_servers(project) -> list[str]:
    """All MCP server names known, installed, or built into the agent or project."""
    servers = set()
    try:
        for b in agents.builtin_mcp(project.agent.name):
            servers.add(b.name)
    except Exception:
        pass
    kept_mcp = project.kept().get("mcp", {})
    if isinstance(kept_mcp, dict):
        servers.update(kept_mcp.keys())
    elif isinstance(kept_mcp, (list, set, tuple)):
        servers.update(kept_mcp)
    for store in (project.agent.mcp_store(project.root), project.agent.user_mcp_store()):
        if store is not None and hasattr(store, "names"):
            try:
                servers.update(store.names())
            except Exception:
                pass
    if project.agent.name == "claude":
        claude_json = Path.home() / ".claude.json"
        if claude_json.exists():
            try:
                data = json.loads(claude_json.read_text("utf-8") or "{}")
                if isinstance(data.get("mcpServers"), dict):
                    servers.update(data["mcpServers"].keys())
            except Exception:
                pass
    return sorted(servers)


def _routine(project, text: str) -> str | None:
    """Why a task is a routine the agent already knows how to do, or None: a skill it has is named in the task
    text, or this project ran the same task (times and other numbers aside) in light mode before."""
    # plugins bring generic skills (`pdf`, `debugging`) a task names in passing; the user's own are routines
    for skill_md in _agent_skills(project, plugins=False) + (list(library.root().glob("*/SKILL.md"))
                                                             if library.root().is_dir() else []):
        if re.search(rf"(?<![\w-]){re.escape(skill_md.parent.name)}(?![\w-])", text, re.IGNORECASE):
            return f"it names the skill `{skill_md.parent.name}` the agent has"
    same = re.sub(r"\d+", "#", " ".join(text.split()).lower())
    for info_path in (project.state / "tasks").glob("*/task.json"):
        try:
            info = json.loads(info_path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        # the level the agent asked for, not the one init raised it to: a routine raised once by mistake stays one
        requested = info.get("requested")
        if requested is None:   # recorded before `requested` was: the log says when light was raised
            log = info_path.parent / "harness.md"
            requested = ("light" if info.get("mode") == "light" or (log.is_file() and "light raised to full"
                                                                   in log.read_text("utf-8", errors="replace"))
                         else info.get("mode"))
        if (requested == "light" and info.get("profile") != "reviewed"
                and re.sub(r"\d+", "#", " ".join(str(info.get("task", "")).split()).lower()) == same):
            return "the same task was started light in this project before"
    return None


USER_FLAGS = ("--accepted", "--force")


def _user_flags(tool: str, tool_input: dict) -> list[str]:
    """The flags of USER_FLAGS a `teardown` call carries, `--accepted=...` included."""
    return [flag for flag in USER_FLAGS
            if any(w == flag or w.startswith(flag + "=") for w in _autoharness_words(tool, tool_input))]


def _autoharness_subcommand(tool: str, tool_input: dict) -> str | None:
    """The subcommand of an `autoharness ...` call that `_is_autoharness_call` let through (`init`, `teardown`)."""
    if tool not in ("Bash", "PowerShell"):
        return None
    words = _autoharness_words(tool, tool_input)
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word in ("--agent", "--project", ARGS_FILE):
            skip = True
        elif not word.startswith("-"):
            return word
    return None


def _read_stdin_utf8() -> str:
    """Claude Code sends hook input as UTF-8. On Windows a piped stdin decodes with the ANSI code page
    (cp1251 and the like), which turns Cyrillic into mojibake holding `‚` or `„`: smart quotes that make
    every autoharness command with such an argument look unsafe."""
    buffer = getattr(sys.stdin, "buffer", None)
    return buffer.read().decode("utf-8-sig", "replace") if buffer else sys.stdin.read()


def _agy_prompt(transcript: str | None) -> str:
    """The user's latest request in an Antigravity CLI transcript (JSON lines, the request wrapped in tags)."""
    if not transcript:
        return ""
    p = Path(transcript)
    if p.name == "transcript.jsonl":
        full = p.with_name("transcript_full.jsonl")
        if full.exists():
            p = full
    request = ""
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    step = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if step.get("type") == "USER_INPUT":
                    content = step.get("content", "")
                    m = AGY_USER_REQUEST.search(content)
                    request = m.group(1) if m else content
                elif step.get("type") in ("SYSTEM_MESSAGE", "EPHEMERAL_MESSAGE"):
                    content = step.get("content", "")
                    m = re.search(r"content=(.+)$", content, re.DOTALL)
                    if m:
                        msg = m.group(1).strip()
                        if not request or "prompts" in msg or "critic" in msg or "vet" in msg or "arbiter" in msg:
                            request = msg
    except OSError:
        pass
    return request


def _agy_event(data: dict) -> dict | None:
    """An Antigravity CLI hook payload in the shape Claude Code sends, or None when there is nothing to do.
    agy has no prompt event: the first model call of a turn (PreInvocation, invocationNum 0) stands for it,
    with the prompt read from the transcript. Its shell runs PowerShell on Windows."""
    common = {"session_id": data.get("conversationId", ""), "cwd": (data.get("workspacePaths") or [""])[0]}
    if "toolCall" in data:
        call = data.get("toolCall") or {}
        name = call.get("name", "")
        call_args = call.get("args") or call.get("arguments") or {}
        if not call_args and isinstance(call.get("argumentsJson"), str):
            try:
                call_args = json.loads(call["argumentsJson"])
            except (ValueError, TypeError):
                call_args = {}
        if isinstance(call_args, str):
            try:
                call_args = json.loads(call_args)
            except (ValueError, TypeError):
                call_args = {}
        if not isinstance(call_args, dict):
            call_args = {}
        if name in ("run_command", "send_command_input"):
            tool = "PowerShell" if os.name == "nt" else "Bash"
            tool_input = {"command": call_args.get("CommandLine", "")} if name == "run_command" else {}
        elif name == "call_mcp_tool":
            tool, tool_input = f"mcp__{call_args.get('ServerName', '')}__{call_args.get('ToolName', '')}", {}
        else:
            tool, tool_input = AGY_TOOLS.get(name) or name, call_args
        return {**common, "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}
    if "terminationReason" in data or "executionNum" in data:
        if data.get("terminationReason") and data.get("terminationReason") != "NO_TOOL_CALL":
            return None
        return {**common, "hook_event_name": "Stop"}
    if data.get("invocationNum") != 0:
        return None
    prompt_text = _agy_prompt(data.get("transcriptPath")) or str(data.get("lastUserInput") or "")
    return {**common, "hook_event_name": "UserPromptSubmit", "prompt": prompt_text}


def cmd_hook(args, project, catalog):
    """Claude Code hook for UserPromptSubmit, PreToolUse and Stop: make every real task go through autoharness.
    Antigravity CLI calls it for PreInvocation, PreToolUse and Stop, translated by `_agy_event`.

    The first substantial prompt of a session puts it in `pending`; tools that do work are denied until the
    agent invokes the skill, starts a task, or the user waives it. Writes to the task records are denied
    always (`_writes_records`), and ending a turn with the session's task still open is sent back once
    (`_stop_gate`). Must never break the session, so any
    internal failure lets the call through (for agy too: it reads empty output as no decision, `{}` as deny).
    """
    try:
        data = json.loads(_read_stdin_utf8() or "{}")
        agy = bool("conversationId" in data or "toolCall" in data or "invocationNum" in data
                   or "terminationReason" in data or "executionNum" in data)
        if agy:
            data = _agy_event(data)
            if data is None:
                return 0
            if data["cwd"]:   # agy runs hooks in the folder of hooks.json, not the project
                project = Project(Path(data["cwd"]).resolve(), project.agent_name)
        event = data.get("hook_event_name") or data.get("event") or "UserPromptSubmit"
        kimi = bool(project.agent_name == "kimi" or os.environ.get("AUTOHARNESS_AGENT") == "kimi"
                    or ("event" in data and "hook_event_name" not in data and not agy))

        def _deny(reason: str) -> int:
            if kimi:
                sys.stderr.write(reason + "\n")
                return 2
            out({"decision": "deny", "reason": reason} if agy else
                {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                        "permissionDecisionReason": reason}})
            return 0
        sid = str(data.get("session_id") or "")
        saved_state = session_state(sid) if sid else {}
        state = saved_state.get("state")
        active = project.active_id()
        task_info = Task(project, active).info() if active else {}
        reviewer = task_info.get("answer_ids", {}).get(os.environ.get(reviewers.ENV_FLAG, ""), {})
        if reviewer and not reviewer.get("used") and not reviewer.get("retired"):
            return 0   # the reviewer `critic --run` started for its pending prompt: read-only, no task of its own
        if active and sid and sid != task_info.get("owner"):
            # a subagent the author gave a pending prompt to: it reviews the task, it does not own or take it
            if saved_state.get("state") == "reviewer" and saved_state.get("reviews") in _pending_answers(task_info):
                return 0
            told = str(data.get("prompt", "")) if event == "UserPromptSubmit" else ""
            if not told and data.get("transcriptPath"):
                told = _agy_prompt(data.get("transcriptPath"))
            if told and (reviews := _reviewer_prompt(told, active, task_info)):
                set_session_state(sid, state="reviewer", reviews=reviews, prompt=told[:PROMPT_KEPT])
                return 0
        is_reviewer = bool(active and sid and sid != task_info.get("owner")
                           and saved_state.get("state") == "reviewer"
                           and saved_state.get("reviews") in _pending_answers(task_info))
        if is_reviewer:
            return 0
        owner = task_info.get("owner")
        # the task belongs to another session: this one is told about it and stays gated until it claims it
        foreign = bool(active and sid and owner and owner != sid and task_info.get("status") == "active")
        # a task its owner left alone that long no longer holds other sessions' tools, only its own commands
        abandoned = foreign and idle_hours(Task(project, active)) >= STALE_HOURS
        if active and sid and state != "waived":
            ready = task_info.get("mode") == "light" or bool(task_info.get("brief"))
            wanted = "free" if abandoned else "pending" if foreign or not ready else "done"
            via = ("abandoned" if abandoned else "other session" if foreign else
                   "light" if task_info.get("mode") == "light" else
                   "brief" if ready else "waiting")
            if state != wanted or saved_state.get("task_id") != active or saved_state.get("via", via) != via:
                set_session_state(sid, state=wanted, task_id=active, via=via, told=saved_state.get("told"),
                                  stop_seen=saved_state.get("stop_seen"), prompt=saved_state.get("prompt"))
                state = wanted
        if event == "Stop":
            return _stop_gate(data, sid, state, active, task_info, project=project, agy=agy, kimi=kimi)
        if event == "UserPromptSubmit":
            if foreign and state != "waived" and saved_state.get("told") != active:
                ctx = handoff(Task(project, active), sid)
                set_session_state(sid, state="pending", task_id=active, via="other session", told=active)
                if kimi:
                    sys.stdout.write(ctx + "\n")
                    return 0
                out({"injectSteps": [{"ephemeralMessage": ctx}]} if agy else
                    {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}})
                return 0
            if active and not foreign and state != "waived":
                try:
                    lessons.note_user_message(Task(project, active), str(data.get("prompt", "")),
                                              started_by=saved_state.get("prompt"))
                except OSError:
                    pass   # a lost note must never block the user's message
            if active:
                return 0
            # with no task open, the latest message is the one that sets the next task, also after a task ended or
            # was waived: init reads it, and an older one would raise a chore to full for what that one asked
            if sid:
                set_session_state(sid, **(saved_state | {"prompt": data.get("prompt", "")[:PROMPT_KEPT]}))
            if state in ("done", "waived"):
                return 0
            ctx = hook_context(catalog, data.get("prompt", ""), sid)
            if not ctx:
                return 0
            if sid:
                set_session_state(sid, state="pending", prompt=data.get("prompt", "")[:PROMPT_KEPT])
            if kimi:
                sys.stdout.write(ctx + "\n")
                return 0
            out({"injectSteps": [{"ephemeralMessage": ctx}]} if agy else
                {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}})
            return 0
        if event == "PreToolUse":
            tool, tool_input = data.get("tool_name", ""), data.get("tool_input") or {}
            if kimi and tool in KIMI_TOOLS:
                tool = KIMI_TOOLS[tool]
            if sid and state != "waived" and (times := _repeated(sid, tool, tool_input)) > REPEAT_LIMIT:
                reason = (f"This is the same {tool} call, unchanged, for the {times}th time in a row: repeating it "
                          "will not give a different result. Change what you call (the command, its input, the "
                          "approach); if a tool, input or know-how is missing, `autoharness need <capability> "
                          "--reason \"<what blocks you>\"`; or stop and ask the user.")
                return _deny(reason)
            # the key is never for tools, even in a call that looks like reading autoharness's own files
            if (reason := _writes_records(tool, tool_input, project.root)) == KEY_DENIED:
                return _deny(reason)
            if _is_autoharness_call(tool, tool_input):
                if foreign and state != "waived" and _autoharness_subcommand(tool, tool_input) in ("init", "teardown"):
                    reason = handoff(Task(project, active), sid)
                    return _deny(reason)
                if (active and not foreign and state != "waived"
                        and _autoharness_subcommand(tool, tool_input) == "teardown"
                        and (flags := _user_flags(tool, tool_input))
                        and not lessons.user_messages(Task(project, active))):
                    reason = (f"`teardown {' / '.join(flags)}` ends the task on the user's word, and the user has "
                              "said nothing since this task started: you cannot accept your own result for them. "
                              "Finish what teardown asks for (its `problems`), or stop and ask the user whether "
                              "they accept the result as it is; pass their words only after they answer.")
                    return _deny(reason)
                # a full task ended before its brief is dropped, and starting it again lighter escapes the brief
                elif (active and not foreign and state != "waived"
                        and _autoharness_subcommand(tool, tool_input) == "teardown"
                        and not {"-h", "--help"} & set(_autoharness_words(tool, tool_input))
                        and (info := Task(project, active).info()).get("mode", "full") != "light"
                        and not info.get("brief") and not lessons.user_messages(Task(project, active))):
                    reason = ("This task has no brief yet, and ending it now drops it: only the user decides that. "
                              "If the brief is refused, do what its error says (quote the user's constraints or "
                              "`none`; search for each area and take or create a skill). If you think the task "
                              "needs no harness, stop and ask the user; `autoharness waive` takes their words.")
                    return _deny(reason)
                return 0  # the skill and its CLI always pass; only a recorded brief opens the gate
            if reason := _writes_records(tool, tool_input, project.root):
                return _deny(reason)
            if active and not foreign and (server := MCP_TOOL.match(tool)) and server[1] != GATEWAY_NAME:
                from autoharness.gateway import note_call
                note_call(project.root, server[1])   # the gateway counts its own servers' calls
            from autoharness.cli import _skills_not_installed
            if (state == "done" and active and not foreign and task_info.get("mode", "full") != "light"
                    and task_info.get("brief") and GATED_TOOLS.match(tool) and not MCP_TOOL.match(tool)
                    and (missing := _skills_not_installed(project, Task(project, active), templates=False))):
                reason = ("Install what the brief covers its areas with before doing the work, so the work is done "
                          "with it, not around it: " + "; ".join(missing) + ". Reading files stays open.")
                return _deny(reason)
            if (state == "done" and active and not foreign and EDIT_TOOLS.match(tool)
                    and (reason := _unchecked_edits(sid, active, task_info))):
                return _deny(reason)
            if state == "pending" and GATED_TOOLS.match(tool):
                if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                    paths = [Path(v) for v in _path_values(tool_input)]
                    if paths and all(_is_args_file(p) for p in paths):
                        return 0
                reason = handoff(Task(project, active), sid) if foreign else (
                    "autoharness first: invoke the `autoharness` skill, run `autoharness init`, review the skills and "
                    "MCP servers it discovers, and record `autoharness brief ...` before using " + tool + ". "
                    "An autoharness command passes only on its own: `cd <dir> &&` (or `;`) before it is fine, "
                    "and at the end `2>&1` and read-only filters (`| head`, `| tail`, `| jq`, `| grep`, "
                    "`| Select-Object -First N`, `| Out-String`, `| ConvertFrom-Json`); outside quotes no other "
                    "`;`, `|`, `&&`, `#`, redirection or brackets; `$(...)` and backticks only inside single "
                    "quotes; `\\\"` only in Bash. Long texts go in on stdin: `autoharness brief --args-file - "
                    "<<'EOF'` + JSON + `EOF` in Bash, or `@'` + JSON + `'@ | autoharness brief --args-file -` in "
                    "PowerShell. "
                    "Reading files is still allowed. If the user explicitly said not to use it, run "
                    f"`autoharness waive {sid} --reason \"<their words>\"` and tell the user.")
                return _deny(reason)
        return 0
    except Exception:  # noqa: BLE001 - a hook must not break the session
        return 0


def _reviewer_can_start(agent: str) -> bool:
    """Whether autoharness can start a fresh reviewer of this agent kind here (`--run`)."""
    if agent in ("cursor", "agent"):
        return bool(shutil.which("agent") or shutil.which("cursor"))
    return agent in reviewers.AGENTS and bool(shutil.which(agent))


def _pending_answers(info: dict) -> list[str]:
    """Answer ids of the task's prompts still waiting for their answer, the latest first."""
    ids = info.get("answer_ids", {})
    return sorted((i for i, a in ids.items() if not a.get("used") and not a.get("retired")),
                  key=lambda i: ids[i].get("issued", ""), reverse=True)


def _reviewer_prompt(text: str, task_id: str, info: dict) -> str | None:
    """The pending answer id a session's first prompt hands it (the id itself, or the task's prompt file), or None."""
    pending = _pending_answers(info)
    named = next((i for i in pending if i in text), None)
    if named or not pending:
        return named
    return pending[0] if re.search(rf"tasks[\\/]+{re.escape(task_id)}[\\/]+prompts[\\/]", text) else None


REPEAT_LIMIT = 5   # the same call this many times in a row may pass; one more is a loop, not progress
EDIT_TOOLS = re.compile(r"^(Write|Edit|MultiEdit|NotebookEdit)$")
MCP_TOOL = re.compile(r"^mcp__(.+?)__")
# file edits after a review that found blocking problems, before an independent critic must see the work again:
# an author fixing alone grades its own fixes, and in the end grades them "none"
EDITS_BEFORE_CRITIC = 20


def _unchecked_edits(sid: str, task_id: str, info: dict) -> str | None:
    """Why this edit is denied when the author has edited the work EDITS_BEFORE_CRITIC times without a critic
    seeing it: after the first result, before any critic, or after a review that found blocking problems; None
    otherwise. Counted per session and reset by each critic review."""
    reviews = info.get("reviews", [])
    critics = sum(1 for r in reviews if r["by"] == "critic")
    if any(a["kind"] == "critic" and not a.get("used") and not a.get("retired")
           for a in info.get("answer_ids", {}).values()):
        return None  # a critic prompt is out: its answer has to be saved to a file to be recorded
    first = not critics and any(e["kind"] == "result" for e in info.get("evidence", []))
    if not sid or not (first or reviews and reviews[-1]["worst"] in evidence.BLOCKING):
        return None
    counter = session_file(sid).with_suffix(".edits.json")
    try:
        seen = json.loads(counter.read_text("utf-8"))
    except (OSError, ValueError):
        seen = {}
    if seen.get("task") != task_id or seen.get("critics") != critics:
        seen = {"task": task_id, "critics": critics, "edits": 0}
    seen["edits"] += 1
    try:
        counter.parent.mkdir(parents=True, exist_ok=True)
        counter.write_text(json.dumps(seen), "utf-8")
    except OSError:
        return None
    if seen["edits"] <= EDITS_BEFORE_CRITIC:
        return None
    since = "the first result" if first else "a review found blocking problems"
    return (f"{seen['edits'] - 1} edits since {since}, and no independent critic has seen "
            "the work since: you are grading your own fixes. Record the current result (`autoharness evidence add "
            "<file> --kind result`, or `autoharness evidence diff` for code), run `autoharness critic` and record its "
            "answer; editing opens again with that review.")


def _repeated(sid: str, tool: str, tool_input: dict) -> int:
    """How many times in a row this session has now made exactly this call (this one included)."""
    key = hashlib.sha256(json.dumps([tool, tool_input], sort_keys=True, ensure_ascii=False, default=str)
                         .encode("utf-8")).hexdigest()[:16]
    calls = session_file(sid).with_suffix(".calls.json")
    try:
        last = json.loads(calls.read_text("utf-8"))
    except (OSError, ValueError):
        last = {}
    count = last.get("count", 0) + 1 if last.get("key") == key else 1
    try:
        calls.parent.mkdir(parents=True, exist_ok=True)
        calls.write_text(json.dumps({"key": key, "count": count}), "utf-8")
    except OSError:
        pass
    return count


def _stop_gate(data: dict, sid: str, state: str | None, active: str | None, task_info: dict,
               project: Project | None = None, agy: bool = False, kimi: bool = False) -> int:
    """Claude Code's, Antigravity's and Kimi Code's Stop event: the agent is ending its turn while its own task is still open.
    Once per state of the task it is sent back to finish (verify, critic, teardown) or to say it is waiting on the user;
    a second stop with nothing changed goes through, so a turn that must end (a question, an approval) still can."""
    if data.get("stop_hook_active") or not (active and sid) or state == "waived":
        return 0
    if task_info.get("status") != "active":
        return 0
    owner = task_info.get("owner")
    if owner and owner != sid:
        return 0
    if not owner and project and idle_hours(Task(project, active)) >= STALE_HOURS:
        return 0
    saved = session_state(sid)
    user_prompt = str(saved.get("prompt") or "").strip().lower()
    # Conversational or pause signals from the user: do not block dialogue turns
    if any(w in user_prompt for w in ("стоп", "stop", "пауза", "pause", "подожди", "wait", "?", "что скажешь", "как думаешь", "почему", "зачем", "как")):
        return 0
    # If the task's latest state/review explicitly tells the agent to ask or wait for the user
    reviews = task_info.get("reviews", [])
    if reviews and any(phrase in str(reviews[-1]).lower() for phrase in ("ask the user", "before they answer")):
        return 0
    mark = hashlib.sha256(json.dumps(task_info, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    if saved.get("stop_seen") == mark:
        return 0
    set_session_state(sid, **(saved | {"stop_seen": mark}))
    left = []
    if task_info.get("mode") != "light" and not task_info.get("brief"):
        left.append("record the brief")
    if not task_info.get("runs") and not task_info.get("checks"):
        left.append("check the result (`autoharness verify run \"<command>\"`)")
    left.append("`autoharness teardown`")
    reason = (f"autoharness task {active} is still open: {task_info.get('task', '')[:200]!r}. Before you end "
              f"the turn: {'; '.join(left)} (teardown says what else it owes, such as a critic). If you are "
              "stopping on purpose (a question for the user, an approval, an accepted result), say so in your "
              "reply and end the turn again: this reminder comes once per state of the task.")
    if kimi:
        sys.stderr.write(reason + "\n")
        return 2
    decision = "continue" if agy else "block"
    out({"decision": decision, "reason": reason})
    return 0


def cmd_waive(args, project, catalog):
    set_session_state(args.session_id, state="waived", reason=args.reason)
    log = recipes.home() / "waivers.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": args.session_id,
                            "project": str(project.root), "reason": args.reason}, ensure_ascii=False) + "\n")
    out({"status": "waived", "session": args.session_id,
         "next": "Tell the user autoharness was skipped for this session, and why."})
    return 0


def install_hook(settings: Path, remove: bool = False) -> str:
    """Add or remove the autoharness hooks in a Claude Code settings file, keeping everything else."""
    data = json.loads(settings.read_text("utf-8")) if settings.exists() else {}
    hooks = data.setdefault("hooks", {})
    ours = lambda h: h.get("command") == HOOK_COMMAND  # noqa: E731
    wanted = {"UserPromptSubmit": None, "PreToolUse": PRE_TOOL_MATCHER, "Stop": None}
    changed = False
    for event, matcher in wanted.items():
        entries = hooks.setdefault(event, [])
        mine = [e for e in entries if any(ours(h) for h in e.get("hooks", []))]
        if remove or (mine and mine[0].get("matcher") != matcher):
            for e in mine:
                e["hooks"] = [h for h in e["hooks"] if not ours(h)]
            hooks[event] = entries = [e for e in entries if e.get("hooks")]
            changed = changed or bool(mine)
            mine = []
        if not remove and not mine:
            entry = {"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 10}]}
            if matcher:
                entry = {"matcher": matcher, **entry}
            entries.append(entry)
            changed = True
        if not hooks[event]:
            del hooks[event]
    if not changed:
        return "not_installed" if remove else "already_installed"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return "removed" if remove else "installed"


def install_agy_hook(hooks_file: Path, remove: bool = False) -> str:
    """Add or remove the `autoharness` entry of an Antigravity CLI hooks.json, keeping everything else."""
    data = json.loads(hooks_file.read_text("utf-8-sig")) if hooks_file.exists() else {}
    handler = {"type": "command", "command": HOOK_COMMAND, "timeout": 10}
    wanted = {
        "PreInvocation": [handler],
        "PreToolUse": [{"matcher": AGY_PRE_TOOL_MATCHER, "hooks": [handler]}],
        "Stop": [handler],
    }
    if remove:
        if data.pop("autoharness", None) is None:
            return "not_installed"
    elif data.get("autoharness") == wanted:
        return "already_installed"
    else:
        data["autoharness"] = wanted
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    hooks_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return "removed" if remove else "installed"


KIMI_HOOK_BLOCK = (
    "# >>> autoharness:hooks\n"
    "[[hooks]]\n"
    'event = "UserPromptSubmit"\n'
    f'command = "{HOOK_COMMAND}"\n'
    "\n"
    "[[hooks]]\n"
    'event = "PreToolUse"\n'
    f'matcher = "{KIMI_PRE_TOOL_MATCHER}"\n'
    f'command = "{HOOK_COMMAND}"\n'
    "\n"
    "[[hooks]]\n"
    'event = "Stop"\n'
    f'command = "{HOOK_COMMAND}"\n'
    "# <<< autoharness:hooks\n"
)
KIMI_HOOK_BLOCK_RE = re.compile(r"^# >>> autoharness:hooks\n.*?^# <<< autoharness:hooks\n?", re.M | re.S)


def install_kimi_hook(config_file: Path, remove: bool = False) -> str:
    """Add or remove the autoharness [[hooks]] in a Kimi Code config.toml, keeping everything else."""
    text = config_file.read_text("utf-8") if config_file.exists() else ""
    if text:
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise HarnessError(f"{config_file} is not valid TOML ({e}); not changing it") from e

    has_block = bool(KIMI_HOOK_BLOCK_RE.search(text))
    if remove:
        if not has_block:
            if text and HOOK_COMMAND in text:
                text = re.sub(rf"\[\[hooks\]\][^\[]*?command\s*=\s*\"{re.escape(HOOK_COMMAND)}\"[^\[]*?\n?", "", text, flags=re.S)
                agents._write_text(config_file, text)
                return "removed"
            return "not_installed"
        new_text = KIMI_HOOK_BLOCK_RE.sub("", text)
        agents._write_text(config_file, new_text)
        return "removed"

    if has_block:
        m = KIMI_HOOK_BLOCK_RE.search(text)
        if m and m.group(0).strip() == KIMI_HOOK_BLOCK.strip():
            return "already_installed"
        new_text = KIMI_HOOK_BLOCK_RE.sub(KIMI_HOOK_BLOCK, text)
    else:
        sep = "\n" if text and not text.endswith("\n") else ""
        gap = "\n" if text.strip() else ""
        new_text = text + sep + gap + KIMI_HOOK_BLOCK

    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        raise HarnessError(f"generated TOML is invalid ({e})") from e

    agents._write_text(config_file, new_text)
    return "installed"


def cmd_install_hook(args, project, catalog):
    if project.agent.name == "kimi" or (args.settings and Path(args.settings).suffix == ".toml"):
        if args.settings:
            settings = Path(args.settings)
        else:
            kimi_home = Path(os.environ["KIMI_CODE_HOME"]) if os.environ.get("KIMI_CODE_HOME") else Path.home() / ".kimi-code"
            settings = kimi_home / "config.toml"
        status = install_kimi_hook(settings, remove=args.remove)
        out({"status": status, "settings": str(settings), "command": HOOK_COMMAND,
             "next": "Takes effect in new Kimi Code sessions: restart Kimi Code."})
        return 0
    if project.agent.name == "agy":
        if args.settings:
            settings = Path(args.settings)
        elif args.user:
            settings = Path.home() / ".gemini" / "config" / "hooks.json"
        else:
            settings = project.root / ".agents" / "hooks.json"
        status = install_agy_hook(settings, remove=args.remove)
        out({"status": status, "settings": str(settings), "command": HOOK_COMMAND,
             "next": "Takes effect in new agy sessions: restart agy (a project hooks.json loads once the folder is trusted)."})
        return 0
    if args.settings:
        settings = Path(args.settings)
    elif args.user:
        settings = Path.home() / ".claude" / "settings.json"
    else:
        settings = project.root / ".claude" / "settings.json"
    status = install_hook(settings, remove=args.remove)
    out({"status": status, "settings": str(settings), "command": HOOK_COMMAND,
         "next": "Takes effect in new Claude Code sessions (or open /hooks once in a running one)."})
    return 0


def cmd_agents(args, project, catalog):
    detected = agents.detect()
    out({"detected": detected, "default": agents.DEFAULT_AGENT,
         "agents": {a.name: {"title": a.title, "mcp_config": a.mcp_file, "skills_dir": a.skills_dir,
                             "user_skills_dir": "~/" + a.user_skills_dir} for a in agents.AGENTS.values()}})
    return 0


_read_transcript = None
_first_user_message = None
_task_created_since = None

__all__ = [name for name in globals() if not name.startswith("__")]

