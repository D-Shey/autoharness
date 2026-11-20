"""`autoharness` command line. Every command prints JSON so an agent can parse it.

Exit codes: 0 ok, 1 error, 3 human approval required (re-run with --approve
only after the user has said yes).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from autoharness import __version__, agents, evidence, reviewers, runs
from autoharness.catalog import Catalog
from autoharness.commands.briefing import *
from autoharness.commands.common import *
from autoharness.commands.discovery import *
from autoharness.commands.lifecycle import *
from autoharness.commands.meta import *
from autoharness.commands.reviews import *
from autoharness.commands.skills import *
from autoharness.commands.subsystems import *
from autoharness.commands.tools import *
from autoharness.env import ContentChanged, HarnessError, Project, SkillRejected, npx_stage_skill
from autoharness.hooks import *


ARGS_FILE = "--args-file"


def expand_args_files(argv: list[str]) -> list[str]:
    """Replace `--args-file <file>` with the options in that JSON object, so long texts (a review, a brief, a
    measurement) need no shell quoting: {"diff": "1 a; 2 b", "worst": "minor", "settled": ["x", "y"]}.
    A list repeats the option, true is a bare flag, false or null leaves it out."""
    out, i = [], 0
    while i < len(argv):
        arg = argv[i]
        if arg != ARGS_FILE and not arg.startswith(ARGS_FILE + "="):
            out.append(arg)
            i += 1
            continue
        if "=" in arg:
            path, i = arg.split("=", 1)[1], i + 1
        elif i + 1 < len(argv):
            path, i = argv[i + 1], i + 2
        else:
            raise HarnessError(f"{ARGS_FILE} needs a file")
        try:
            raw = sys.stdin.buffer.read().decode("utf-8-sig") if path == "-" else Path(path).read_text("utf-8-sig")
            data = json.loads(raw)
        except (OSError, ValueError) as e:
            raise HarnessError(f"{ARGS_FILE} {path}: {e}") from e
        if not isinstance(data, dict):
            raise HarnessError(f"{ARGS_FILE} {path}: expected a JSON object of option names and values")
        for key, value in data.items():
            flag = key if key.startswith("-") else "--" + key.replace("_", "-")
            for v in value if isinstance(value, list) else [value]:
                if v is True:
                    out.append(flag)
                elif v is not None and v is not False:
                    out.append(f"{flag}={v}")   # `=`: a text starting with `-` stays a value
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autoharness", description=__doc__.split("\n")[0],
                                epilog=f"Long texts without shell quoting: {ARGS_FILE} <file.json> anywhere in the "
                                       'command, a JSON object of options, e.g. {"diff": "...", "worst": "minor"}.')
    p.add_argument("--version", action="version", version=f"autoharness {__version__}")
    p.add_argument("--project", default=".", help="project root (default: cwd)")
    p.add_argument("--agent", choices=list(agents.AGENTS),
                   help="target coding agent (default: the task's agent, else $AUTOHARNESS_AGENT, else auto-detect, else claude)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("plan", help="suggest capabilities for a task (no side effects)")
    s.add_argument("task")
    s = sub.add_parser("init", help="start a task-scoped harness")
    s.add_argument("task")
    s.add_argument("--mode", choices=["light", "full", "reviewed", "auto"],
                   help="light (use existing tools), full (default: discovery and brief), reviewed (full and an "
                        "independent critic), auto (full when the task matches a capability, else light)")
    s.add_argument("--profile", choices=["quick", "standard", "reviewed"], help=argparse.SUPPRESS)
    s.add_argument("--light-reason", help="light mode on a task that mentions a reference or a visual check: why "
                                          "nothing real has to be matched")
    s = sub.add_parser("mode", help="raise the active task: light to full, or to reviewed (requires a critic)")
    s.add_argument("value", choices=["full", "reviewed"])
    s = sub.add_parser("eval", help="record and compare measured task runs")
    s.add_argument("action", choices=["record", "compare", "batch"])
    s.add_argument("--case", help="same case name for baseline and harness runs")
    s.add_argument("--variant", choices=["baseline", "harness"])
    s.add_argument("--passed", choices=["yes", "no"])
    s.add_argument("--seconds", type=float, help="wall-clock run duration; inferred for a completed --task-id")
    s.add_argument("--tokens", type=int, help="model tokens reported by the agent host")
    s.add_argument("--cost", type=float, help="model cost reported by the agent host")
    s.add_argument("--tool-calls", type=int, help="tool calls reported by the agent host")
    s.add_argument("--task-id", help="attach a completed task and its CLI measurements")
    s.add_argument("--dataset", help="for batch: JSONL with unique case and input fields")
    s.add_argument("--timeout", type=float, default=300, help="for batch: seconds allowed per case")
    s.add_argument("command", nargs="*", help="for batch: -- <runner> [arguments]")
    s = sub.add_parser("decision", help="record why a candidate was selected or skipped")
    s.add_argument("kind", choices=["mcp", "skill"])
    s.add_argument("name")
    s.add_argument("verdict", choices=["select", "skip"])
    s.add_argument("--reason", required=True)
    s = sub.add_parser("followup", help="keep work found outside the task for later tasks: add, list, done, harvest")
    s.add_argument("action", choices=["add", "list", "done", "harvest"])
    s.add_argument("value", nargs="?", help="add: what is left to do and where; done: its number")
    s.add_argument("--note", help="done: what settled it")
    s.add_argument("--marker", default="shortcut", help="harvest: marker comment keyword to scan (default: shortcut)")
    s.add_argument("--all", action="store_true", help="list: include done items")
    sub.add_parser("journal", help="draft this task's entry for the project's journal (JOURNAL.md or the `journal` "
                                   "setting), in the journal's own form")
    s = sub.add_parser("lesson", help="small conclusions kept across tasks, each with how many tasks confirmed it: "
                                      "add, list, fold (into a skill)")
    s.add_argument("action", choices=["add", "list", "fold"])
    s.add_argument("value", nargs="?", help="add: the conclusion (when X, do Y, because Z); fold: its number")
    s.add_argument("--skill", help="fold: the skill it now lives in")
    s = sub.add_parser("ruling", help="critic points settled for the project, which later critics are told: "
                                      "add (the user rejected one), list, remove")
    s.add_argument("action", choices=["add", "list", "remove"])
    s.add_argument("value", nargs="?", help="add: the critic's point; remove: its number")
    s.add_argument("--reason", help="add: why, in the user's words")
    s = sub.add_parser("audit", help="show structured decisions for a task")
    s.add_argument("task_id", nargs="?")
    s = sub.add_parser("verify", help="run a check and record it by its exit code, or use a verification guide")
    s.add_argument("action", choices=["guide", "record", "run"])
    s.add_argument("kind", metavar="KIND|COMMAND", nargs="*",
                   help=f"guide/record: {', '.join(VERIFY_KINDS)}; run: the command, run in the project root")
    s.add_argument("--command", help="for run: the command, instead of the positional one (fits --args-file, "
                                     "so it needs no shell quoting)")
    s.add_argument("--passed", choices=["yes", "no"])
    s.add_argument("--evidence")
    s.add_argument("--name", help="for run: a short name for the check (default: the command's first word)")
    s.add_argument("--for", dest="purpose", metavar="ITEM",
                   help=f"for run: the brief item it checks (B1, P1, ...) or `{runs.PRESERVE}` for the regression check")
    s.add_argument("--timeout", type=float, default=runs.DEFAULT_TIMEOUT, help="for run: seconds allowed")
    s.add_argument("--no-redact", action="store_true", help="do not redact sensitive tokens and credentials from run logs")
    s = sub.add_parser("need", help="record a capability gap and get curated candidates")
    s.add_argument("capability")
    s.add_argument("--reason")
    s = sub.add_parser("search", help="search the MCP registry or skills.sh")
    s.add_argument("kind", choices=["mcp", "skill"])
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=8)
    s = sub.add_parser("inspect", help="trust report for one MCP server or skill")
    s.add_argument("kind", choices=["mcp", "skill"])
    s.add_argument("name")
    s = sub.add_parser("add", help="add an MCP server or skill to the active task")
    s.add_argument("kind", choices=["mcp", "skill"])
    s.add_argument("name", help="curated name / registry server name, or owner/repo@skill")
    s.add_argument("--approve", action="store_true", help="the user explicitly approved this")
    s.add_argument("--force", action="store_true", help="install a skill despite high-severity scan findings")
    s.add_argument("--as", dest="as_name", help="local name for the MCP server")
    s.add_argument("--direct", action="store_true", help="for mcp: write to the agent's config even if the gateway is installed")
    s = sub.add_parser("vet", help="check a candidate against the brief's constraints by an independent reviewer")
    s.add_argument("kind", choices=["mcp", "skill"])
    s.add_argument("name")
    s.add_argument("--answer-file", help="record the reviewer's answer, saved unedited")
    s.add_argument("--verdict", choices=list(vet.VERDICTS), help="record the reviewer's VERDICT")
    s.add_argument("--text", help="with --verdict: the reviewer's REASON")
    s.add_argument("--run", choices=list(reviewers.AGENTS),
                   help="start that agent headless and read-only as the reviewer, and record its answer")
    s.add_argument("--timeout", type=float, default=reviewers.DEFAULT_TIMEOUT, help="with --run: seconds allowed")
    s = sub.add_parser("remove", help="remove something the active task added or an earlier task kept")
    s.add_argument("kind", choices=["mcp", "skill"])
    s.add_argument("name")
    s.add_argument("--reason", help="why this task no longer needs the tool (saved in the audit log)")
    s.add_argument("--approve", action="store_true", help="the user agreed to uninstall a kept item no task here kept")
    s = sub.add_parser("prune", help="remove everything earlier tasks kept in the project")
    s.add_argument("--approve", action="store_true", help="the user agreed to uninstall kept items no task here kept")
    s = sub.add_parser("status", help="show the active task harness and what the project keeps")
    s.add_argument("task_id", nargs="?")
    s.add_argument("--full", action="store_true", help="every search result and the whole log")
    s = sub.add_parser("teardown", help="end the task: keep its harness in the project, or remove it (setting `teardown`)")
    s.add_argument("task_id", nargs="?")
    s.add_argument("--save-recipe", metavar="NAME")
    s.add_argument("--notes")
    s.add_argument("--query", action="append", metavar="KIND:QUERY",
                   help="with --save-recipe: a registry query to re-run later (skill:<q> or mcp:<q>); repeatable. "
                        "Searches that found what the task installed are saved without it")
    s.add_argument("--force", action="store_true",
                   help="tear down although a planned skill, evidence or a review is missing")
    s.add_argument("--accepted", metavar="USER_WORDS",
                   help="the user accepted the result as it is; their words go into the task log")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--keep", dest="policy", action="store_const", const="keep", help="keep it installed in the project")
    g.add_argument("--remove", dest="policy", action="store_const", const="remove", help="uninstall everything it added")
    s = sub.add_parser("config", help="show or change settings (project, or --global)")
    s.add_argument("action", choices=["get", "set"])
    s.add_argument("key", nargs="?", choices=list(config.SETTINGS))
    s.add_argument("value", nargs="?")
    s.add_argument("--global", dest="glob", action="store_true", help="change ~/.autoharness/config.json")
    s = sub.add_parser("skill", help="write your own skills and keep them in the local library")
    s.add_argument("action", choices=["new", "test", "save", "list", "delete"])
    s.add_argument("name", nargs="?")
    s.add_argument("--description", help="for `new`: when an agent should use the skill")
    s.add_argument("--task", action="append", default=[], dest="test_tasks",
                   help="for `test`: a realistic request the skill is for (1-3, repeat the flag); starts a round")
    s.add_argument("--judge", action="store_true", help="for `test`: the blind comparison prompt for the round")
    s.add_argument("--answer-file", help="for `test`: the judge's answer, saved unedited")
    s.add_argument("--approve", action="store_true", help="for `save`: the user explicitly agreed")
    s = sub.add_parser("gateway", help="one MCP server that hot-adds the servers you add, no agent restarts")
    s.add_argument("action", choices=["install", "uninstall", "status", "serve", "approve"])
    s.add_argument("--user", action="store_true",
                   help="for `install`/`uninstall`: in the agent's own MCP file, for every project")
    s.add_argument("name", nargs="?", help="for `approve`: the server in .autoharness/gateway.json")
    s.add_argument("--approve", action="store_true", help="for `approve`: the user read the config and agreed")
    s.add_argument("--all", action="store_true", help="for `approve`: every server waiting for approval")
    s = sub.add_parser("recipe", help="saved harness recipes")
    s.add_argument("action", choices=["list", "match", "show"])
    s.add_argument("value", nargs="?", default="")
    s = sub.add_parser("install-skill", help="install the autoharness bootstrap skill")
    s.add_argument("--user", action="store_true", help="install to the agent's user skills dir instead of the project")
    s.add_argument("--all", action="store_true", help="install for every supported agent")
    sub.add_parser("hook", help="Claude Code / Antigravity CLI hook: reads the hook JSON on stdin")
    s = sub.add_parser("evidence", help="record a file you checked: a reference, or what a round produced")
    s.add_argument("action", choices=["add", "list", "diff"])
    s.add_argument("file", nargs="?")
    s.add_argument("--kind", choices=list(evidence.KINDS))
    s.add_argument("--base", default="HEAD", help="for `diff`: the revision to compare the working tree with")
    s.add_argument("--note", help="what it shows; reviewers see it as your label, not as a finding")
    s.add_argument("--for", dest="for_item", help="the component or brief item this evidence corresponds to (e.g. C1, C2, B1)")
    s.add_argument("--closeup", "--part", dest="closeup", action="store_true",
                   help="with --kind result: a close-up or another part of the latest whole result (a crop, a "
                        "log beside the diff), not a new version of it")
    s = sub.add_parser("review",help="record one review round of the latest result")
    s.add_argument("--answer-file", help="with --by critic: the critic's answer, saved unedited; autoharness reads "
                                         "its list and VERDICT")
    s.add_argument("--diff", help="the differences found, most important first (your own review)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--worst", choices=list(evidence.SEVERITIES),
                   help="worst severity among the fixable differences (the critic's VERDICT)")
    g.add_argument("--fixable", choices=["yes", "no"], help="short form: yes = major, no = none")
    s.add_argument("--by", choices=["self", "critic"], help="default: critic with --answer-file, else self")
    s.add_argument("--settled", action="append", default=[], metavar="POINT",
                   help="a disputed point this critic settled from measurements in the files, and how; "
                        "later critics see it (repeatable)")
    s.add_argument("--quality-file", help="with --answer-file, when the brief asks for two reviewers: the "
                                          "code-quality reviewer's answer, saved unedited")
    s = sub.add_parser("critic", help="a prompt for an independent reviewer of the latest result, or run one")
    s.add_argument("--dispute", help="a point of the last critic review you believe is wrong; a fresh critic decides")
    s.add_argument("--run", choices=list(reviewers.AGENTS),
                   help="start that agent headless and read-only as the critic, and record its answer")
    s.add_argument("--timeout", type=float, default=reviewers.DEFAULT_TIMEOUT, help="with --run: seconds allowed")
    s = sub.add_parser("approach", help="record a change of approach for a part that keeps failing review")
    s.add_argument("--part", help="the part that loops (default: the one the last review named)")
    s.add_argument("--was", required=True, help="the method used so far")
    s.add_argument("--now", required=True, help="the different method you will use now")
    s = sub.add_parser("arbiter", help="a prompt for an independent arbiter on a part that outlasted its approaches")
    s.add_argument("--part", help="the part to arbitrate (default: the one the last review sent to an arbiter)")
    s = sub.add_parser("arbitrate", help="record an arbiter's verdict: fix (next approach) or limit (stops blocking)")
    s.add_argument("--part", help="the part (default: the one waiting for an arbiter)")
    s.add_argument("--answer-file", help="the arbiter's answer, saved unedited; autoharness reads VERDICT and CHECK")
    s.add_argument("--verdict", choices=["fix", "limit"])
    s.add_argument("--text", help="the arbiter's answer: the technique, or why it is out of reach")
    s.add_argument("--check", help="for a fix: the arbiter's measurable CHECK line")
    s = sub.add_parser("check", help="run the arbiter's check on the new result, or record what it measured")
    s.add_argument("--run", metavar="COMMAND", help="run the check in the project root: the exit code decides")
    s.add_argument("--timeout", type=float, default=runs.DEFAULT_TIMEOUT, help="with --run: seconds allowed")
    s.add_argument("--result", help="what the check measured (when it is not a command)")
    s.add_argument("--pass", dest="passed", choices=["yes", "no"])
    s.add_argument("--deviation", action="append", default=[],
                   help="where you checked differently from the arbiter's check, and why (repeatable)")
    s = sub.add_parser("brief", help="record what a good result is, what is missing, and the verdict on candidates")
    for flag, what in [("--gaps", "`create <skill-name>: <capability>` for each capability no skill covers, "
                                  "or `none: <why>`"),
                       ("--goal", "what a good result looks like (the quality bar)"),
                       ("--domains", "the areas of know-how the task takes, each covered: `<area>: skill|mcp|create "
                                     "<name> (searched: <query>); ...`"),
                       ("--method", "how this kind of work is done well, from sources found and read first: "
                                    "`<technique or steps>: <url>; ...; searched: <queries>`"),
                       ("--inputs", "missing inputs and how you will get them (references, docs, data)"),
                       ("--expects", "the choices the task leaves open and what the person who asked most likely "
                                     "expects for each: `<choice>: <pick> - <why>; ...`, or `none: <why>`"),
                       ("--constraints", "the user's constraints, verbatim"),
                       ("--verify", "how the result will be checked, and against what"),
                       ("--candidates", "each discovered skill/MCP worth considering: take or skip, and why")]:
        s.add_argument(flag, required=flag != "--verify", help=what)
    s.add_argument("--fidelity", help="the level to judge the result at, e.g. `stylised poster, not photographic`")
    s.add_argument("--components", default="",
                   help="decompose into 2-4 primary isolated components: `<name>: <what it does / how to verify alone>; ...` or `none: <why>`")
    s.add_argument("--techniques", default="",
                   help="formal implementation & quality techniques required for this domain (e.g. `[clean-arch, idempotency]` or `technique-name: <rule>; ...`)")
    s.add_argument("--review", choices=list(evidence.REVIEWS),
                   help="require an independent critic for this task even if its capabilities do not; pair: "
                        "each round also a code-quality reviewer, both run again after every fix")
    s.add_argument("--preserve", help="existing behaviour that must keep working; teardown then requires a passing "
                                      f"`verify run ... --for {runs.PRESERVE}` on the final code")
    s.add_argument("--verify-template", choices=["browser", "api", "api-client", "logs", "database"],
                   help="use a built-in verification guide instead of --verify")
    s = sub.add_parser("claim", help="take over the active task from the agent session that started it")
    s.add_argument("session_id", nargs="?", help="this session's id (default: the one the agent sets)")
    s.add_argument("--reason", help="why: the user's words, or that the other session is gone")
    s = sub.add_parser("waive", help="let one agent session skip autoharness (only when the user said so)")
    s.add_argument("session_id")
    s.add_argument("--reason", required=True, help="the user's words")
    s = sub.add_parser("install-hook", help="add the prompt hook to Claude Code settings, agy hooks.json or kimi config.toml (project, or --user)")
    s.add_argument("--user", action="store_true", help="user-level settings (Claude ~/.claude/settings.json, agy ~/.gemini/config/hooks.json, kimi config.toml) instead of project")
    s.add_argument("--settings", help="explicit settings file (Claude: settings.json, agy: hooks.json, kimi: config.toml)")
    s.add_argument("--remove", action="store_true", help="take the hook out again")
    s = sub.add_parser("agents", help="supported agents and where they keep MCP servers and skills")
    s = sub.add_parser("memory", help="in-process SQLite FTS5 persistent memory with BM25 ranking, LEANN vector search, and dynamic drift reconciliation")
    s.add_argument("action", choices=["search", "index", "status", "reconcile"])
    s.add_argument("query", nargs="?", default="", help="search term")
    s.add_argument("--kind", choices=["all", "ruling", "lesson", "review", "followup", "swarm_task"], default="all")
    s.add_argument("--mode", choices=["hybrid", "fts", "vector"], default="hybrid", help="search mode (default: hybrid RRF)")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--prune-orphans", action="store_true", help="permanently prune orphaned and broken memory references")
    s.add_argument("--json", action="store_true", help="output structured JSON")
    s = sub.add_parser("codemap", help="AST Symbol Map and structural code index")
    s.add_argument("path", nargs="?", default=".", help="file or directory path")
    s.add_argument("--query", "-q", help="search for class, method, or function symbols")
    s.add_argument("--json", action="store_true", help="output full structured JSON")
    s.add_argument("--arch", action="store_true", help="analyze architecture dependency graph, cycles, and boundary violations")
    s.add_argument("--fail-on-cycle", action="store_true", help="exit with code 1 if cycles or boundary violations exist")
    s.add_argument("--impact", help="analyze blast radius and callers impact for a symbol")
    s.add_argument("--callers", help="find direct callers for a symbol")
    s.add_argument("--check-shims", action="store_true", help="scan for fake implementation stubs and mock cheats (Anti-Shim Sentinel)")
    s = sub.add_parser("swarm", help="Multi-agent DAG swarm workflow coordinator and runner")
    s.add_argument("action", choices=["init", "run", "status", "list", "verify"], help="swarm action")
    s.add_argument("plan_id", nargs="?", help="plan ID or JSON plan path")
    s.add_argument("--title", help="title for the swarm plan (init)")
    s.add_argument("--file", help="tasks JSON file to initialize from (init)")
    s.add_argument("--parallel", type=int, default=4, help="maximum concurrent worker tasks (run)")
    s.add_argument("--json", action="store_true", help="output structured JSON")
    s = sub.add_parser("drive", help="Continuous autonomy self-driving loop for backlog resolution")
    s.add_argument("action", nargs="?", choices=["run", "status", "queue", "rollback"], default="run", help="drive action")
    s.add_argument("--max-tasks", type=int, default=3, help="maximum followups to process in this run")
    s.add_argument("--followup", type=int, help="specific followup ID to execute")
    s.add_argument("--dry-run", action="store_true", help="plan without executing modifications")
    s.add_argument("--test-cmd", help="custom regression test command to verify before completing")
    s = sub.add_parser("update", help="Check for updates and self-update AutoHarness from GitHub")
    s.add_argument("--check", action="store_true", help="check for updates without installing")
    s.add_argument("--repo", help="target GitHub repository (default: D-Shey/autoharness)")
    s.add_argument("--force", action="store_true", help="force reinstall even if already on latest version")
    s.add_argument("--yes", "-y", action="store_true", help="skip confirmation prompt")
    s = sub.add_parser("permissions", help="Inspect and configure agent YOLO / auto-approval permissions")
    s.add_argument("action", nargs="?", choices=["status", "enable", "disable", "simulate"], default="status", help="action to perform")
    s.add_argument("--action-type", default="command", choices=["command", "file_delete", "file_modify", "gateway_reconfig", "query"], help="action type for simulation")
    s.add_argument("--target", help="target file path, command string, or MCP server name for simulation")
    s.add_argument("--json", action="store_true", help="output structured JSON")
    s = sub.add_parser("yolo", help="Alias for permissions: inspect or enable/disable agent YOLO mode")
    s.add_argument("action", nargs="?", choices=["status", "enable", "disable"], default="status", help="action to perform")
    s = sub.add_parser("simulate", help="Consequence-Aware Approval & Blast Radius Simulation (Hugging Face arXiv:2610.40753)")
    s.add_argument("target", help="target file path, command string, or server name to simulate")
    s.add_argument("--action-type", default="command", choices=["command", "file_delete", "file_modify", "gateway_reconfig", "query"], help="action type")
    s.add_argument("--json", action="store_true", help="output structured JSON")
    s = sub.add_parser("slot", help="8-Slot Modular Harness Scaffolding (Google Research Harness² model)")
    s.add_argument("action", nargs="?", choices=["inspect", "export", "diff"], default="inspect", help="slot action")
    s.add_argument("--json", action="store_true", help="output structured JSON")
    s.add_argument("--target", help="target slots JSON file for diff action")
    s = sub.add_parser("probe", help="Propose-Probe-Compose contrastive harness loop without ground truth")
    s.add_argument("action", nargs="?", choices=["run", "propose", "compose"], default="run", help="probe action")
    s.add_argument("-k", type=int, default=2, help="number of contrastive candidate harness variants to probe (default: 2)")
    s.add_argument("--prompt", help="task prompt or objective for variant generation")
    s.add_argument("--json", action="store_true", help="output structured JSON")
    s = sub.add_parser("skim", help="Intelligent cascading output compression for test, build, and diff logs (dean0x/skim model)")
    s.add_argument("target", help="path to log file or command string to execute and compress")
    s.add_argument("--kind", choices=["auto", "pytest", "diff", "traceback", "compiler", "generic"], default="auto", help="output format kind")
    s.add_argument("--max-lines", type=int, default=60, help="maximum lines for compressed output (default: 60)")
    s.add_argument("--no-save", action="store_true", help="do not persist full uncompressed output to disk")
    s.add_argument("--json", action="store_true", help="output structured JSON")
    s = sub.add_parser("sentinel", help="Self-Supervised Code Refinement & Regression Sentinel with Rollback Guard (Google Research Harness² model)")
    s.add_argument("action", nargs="?", choices=["run", "snapshot", "rollback", "status"], default="status", help="sentinel action")
    s.add_argument("--test-command", "-c", default="pytest", help="test suite command to execute (default: pytest)")
    s.add_argument("--auto-rollback", action="store_true", help="automatically rollback on test or anti-shim verification failure")
    s.add_argument("--pre-flight", action="store_true", help="run baseline pre-flight checks and capture baseline snapshot")
    s.add_argument("--no-shims", action="store_true", help="skip AST anti-shim check during verification")
    s.add_argument("--snapshot-id", help="target snapshot ID for rollback")
    s.add_argument("--label", default="checkpoint", help="label for newly created snapshot")
    s.add_argument("--json", action="store_true", help="output structured JSON")
    s = sub.add_parser("compact", help="Dynamic Context Compactor & Layered Session Folding (Google Research Harness² & ACP model)")
    s.add_argument("action", nargs="?", choices=["run", "status", "prune"], default="run", help="compact action")
    s.add_argument("target", nargs="?", default="", help="path to transcript file or session id")
    s.add_argument("--keep-last", type=int, default=5, help="number of recent steps to keep verbatim (default: 5)")
    s.add_argument("--epoch-size", type=int, default=10, help="step batch size for milestone epoch condensation (default: 10)")
    s.add_argument("--anchor-steps", type=int, default=1, help="number of initial anchor steps preserved verbatim for KV-cache prefix stability (AP-5, default: 1)")
    s.add_argument("--json", action="store_true", help="output structured JSON")
    s = sub.add_parser("stencil", help="Domain-Agnostic Structural Stencil Scanner and Geometric Landmark/Contour Framework")
    s.add_argument("action", nargs="?", choices=["scan", "extract", "list", "show", "check", "add"], default="scan", help="stencil action: scan, list, show, check, add")
    s.add_argument("source", nargs="?", default="", help="source reference file or stencil id")
    s.add_argument("--threshold", type=float, default=0.5, help="edge/contour sensitivity threshold (0.0-1.0)")
    s.add_argument("--max-points", type=int, default=250, help="maximum contour points/polylines to extract")
    s.add_argument("--output", "-o", default="", help="output destination file for extracted stencil")
    s.add_argument("--mode", "-m", choices=["auto", "schema", "ast", "wireframe", "contour", "svg", "outline", "text"], default="auto", help="extraction mode")
    s.add_argument("--target", "-t", default="", help="target file to check against stencil")
    s.add_argument("--note", default="", help="note when registering stencil as task evidence")
    return p


COMMANDS = {"plan": cmd_plan, "init": cmd_init, "mode": cmd_mode, "eval": cmd_eval, "decision": cmd_decision, "followup": cmd_followup, "journal": cmd_journal, "lesson": cmd_lesson, "ruling": cmd_ruling, "audit": cmd_audit, "verify": cmd_verify, "need": cmd_need, "search": cmd_search, "inspect": cmd_inspect, "vet": cmd_vet,
            "add": cmd_add, "remove": cmd_remove, "prune": cmd_prune, "status": cmd_status,
            "teardown": cmd_teardown, "config": cmd_config, "skill": cmd_skill, "gateway": cmd_gateway,
            "recipe": cmd_recipe, "install-skill": cmd_install_skill, "agents": cmd_agents,
            "hook": cmd_hook, "install-hook": cmd_install_hook, "waive": cmd_waive, "brief": cmd_brief,
            "evidence": cmd_evidence, "review": cmd_review, "critic": cmd_critic, "approach": cmd_approach,
            "arbiter": cmd_arbiter, "arbitrate": cmd_arbitrate, "check": cmd_check, "claim": cmd_claim,
            "memory": cmd_memory, "codemap": cmd_codemap, "swarm": cmd_swarm, "drive": cmd_drive, "update": cmd_update,
            "permissions": cmd_permissions, "yolo": cmd_permissions, "slot": cmd_slot, "probe": cmd_probe, "simulate": cmd_simulate, "skim": cmd_skim, "sentinel": cmd_sentinel, "compact": cmd_compact, "stencil": cmd_stencil}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        argv = expand_args_files(sys.argv[1:] if argv is None else list(argv))
    except HarnessError as e:
        out({"status": "error", "error": str(e)})
        return 1
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if extra and args.cmd == "verify" and args.action == "run":
        cleaned_extra = []
        i = 0
        while i < len(extra):
            token = extra[i]
            if token == "--for" and i + 1 < len(extra):
                args.purpose = extra[i + 1]
                i += 2
            elif token.startswith("--for="):
                args.purpose = token.split("=", 1)[1]
                i += 1
            elif token == "--name" and i + 1 < len(extra):
                args.name = extra[i + 1]
                i += 2
            elif token.startswith("--name="):
                args.name = token.split("=", 1)[1]
                i += 1
            elif token == "--timeout" and i + 1 < len(extra):
                args.timeout = float(extra[i + 1])
                i += 2
            elif token.startswith("--timeout="):
                args.timeout = float(token.split("=", 1)[1])
                i += 1
            elif token == "--no-redact":
                args.no_redact = True
                i += 1
            else:
                cleaned_extra.append(token)
                i += 1
        args.kind += cleaned_extra
    elif extra and args.cmd == "eval" and args.action == "batch":
        args.command += extra   # Python 3.11 argparse leaves what follows `--` unparsed here
    elif extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    project = Project(Path(args.project).resolve(), args.agent)
    started = time.monotonic()
    before = project.active_id()
    exit_code = 1
    try:
        # the hook writes only session state in ~/.autoharness, and agy runs it outside the project
        if (args.cmd == "gateway" and args.action == "serve") or (args.cmd == "eval" and args.action == "batch") \
                or args.cmd == "hook":
            exit_code = COMMANDS[args.cmd](args, project, Catalog.load())
            return exit_code
        from autoharness import transactions
        # a check or a reviewer may run for many minutes: those commands lock only while they write
        long = (args.cmd == "verify" and args.action == "run") or (args.cmd in ("critic", "check") and args.run)
        with transactions.locked(project.root):
            transactions.recover(project.root)
            if args.cmd in OWNED:
                check_owner(project, getattr(args, "task_id", None) or project.active_id())
            if not long:
                exit_code = COMMANDS[args.cmd](args, project, Catalog.load())
                return exit_code
        exit_code = COMMANDS[args.cmd](args, project, Catalog.load())
        return exit_code
    except (HarnessError, agents.AgentError) as e:
        out({"status": "error", "error": str(e)})
        return 1
    except OSError as e:
        out({"status": "error", "error": f"network or filesystem error: {e}"})
        return 1
    finally:
        if args.cmd != "gateway" or args.action != "serve":
            from autoharness import telemetry
            try:
                telemetry.record(project.root, before or project.active_id(), args.cmd,
                                 exit_code, time.monotonic() - started)
            except OSError:
                pass  # metrics must not turn a completed task into a failed command
