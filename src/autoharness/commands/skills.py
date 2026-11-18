"""Skill authoring, testing, installation, and autonomous directive configuration."""
from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

from autoharness import agents, evidence, lessons, library, skilltests
from autoharness.commands.common import EXIT_APPROVAL, out
from autoharness.env import HarnessError, Task, skill_dir


def _find_local_skill(project, name: str) -> tuple[dict, "Task | None"]:
    """The lock entry for `name` from the active task, else from kept.json, and the task it belongs to."""
    active = project.task() if project.active_id() else None
    entry = active.skills_lock().get(name) if active else None
    if entry:
        return entry, active
    entry = project.kept()["skills"].get(name)
    if not entry:
        raise HarnessError(f"skill {name!r} is not in the active task or kept in this project")
    task = Task(project, entry["task"]) if entry.get("task") else None
    return entry, task if task and task.dir.exists() else None


# how a skill that earns its place is written; the agent reads it once, right after `skill new`
SKILL_WRITING = (
    "Fill in every section of SKILL.md now, from the references, docs and findings you have. It is your procedure "
    "for this task: follow it, and change it when you learn something. What makes it work for the next agent too:\n"
    "- Aim it at a failure you saw: what went wrong without it (a critic finding, a wrong first attempt) and what "
    "prevents that. A skill that only restates what any agent already does adds nothing.\n"
    "- The description in the frontmatter decides whether an agent opens the skill at all. Write when to open it "
    "(the situation, the symptoms, the words a request would use), not what the steps are: an agent that finds the "
    "steps in the description tends to follow that summary and skip the body.\n"
    "- Method, not a story of this task: no absolute paths, secrets, people's names or one-off values in the steps. "
    "Values measured here go under \"Facts found here\".\n"
    "- Say why a step matters instead of shouting MUST or NEVER: an agent that knows the reason handles the case "
    "the skill did not foresee.\n"
    "- Keep SKILL.md short (well under 500 lines). Long reference material goes into references/<topic>.md with a "
    "line in SKILL.md saying when to read it; code an agent would otherwise rewrite each time goes into scripts/.\n"
    "Then test it: `autoharness skill test {name} --task \"<a realistic request it is for>\"` (1-3 tasks that "
    "differ) has fresh agents do the work with it and without it, and a blind judge compares. At the end run "
    "`autoharness skill save {name}` and ask the user.")


def _skill_test(args, project) -> int:
    task = project.task()
    if args.answer_file:
        res = skilltests.record(task, args.name, Path(args.answer_file))
        nxt = (f"The skill helped. Save it with `autoharness skill save {args.name}` and ask the user."
               if res["passed"] else
               "The skill did not beat an agent without it. Read the judge's answer and both outputs of each task "
               "it did not win, find where the skill misled the agent or left it on its own, and change the skill "
               "so it would help on any such request, not only these. Then test again: `autoharness skill test "
               f"{args.name}` (same tasks; add a new one with --task to check it did not just fit these).")
        out({"status": "ok", "result": res, "next": nxt})
        return 0
    if args.judge:
        res = skilltests.judge_prompt(task, args.name)
        res["prompt_file"] = evidence.save_prompt(task, "skilltest", res["prompt"])
        out({"status": "ok"} | res | {
            "next": "Give the prompt unchanged to one fresh subagent that did none of the runs, save its answer "
                    f"unedited to a file and record it: `autoharness skill test {args.name} --answer-file <file>`."})
        return 0
    skill_dir = task.agent.skills_path(project.root) / args.name
    res = skilltests.start(task, args.name, skill_dir, args.test_tasks)
    out({"status": "ok"} | res | {
        "next": "Run every prompt in its own fresh subagent, all at once, each unchanged: the runs must not see "
                "each other or this conversation. Which label has the skill stays hidden, also from you. When all "
                f"have finished, `autoharness skill test {args.name} --judge` gives the blind comparison prompt."})
    return 0


def cmd_skill(args, project, catalog):
    if args.action == "list":
        entries, warnings = library.list_skills()
        entries = [e | ({"record": rec} if (rec := lessons.record("skill", e["name"])) else {}) for e in entries]
        out({"skills": entries} | ({"warnings": warnings} if warnings else {}))
        return 0
    if not args.name:
        raise HarnessError(f"usage: autoharness skill {args.action} <name>")
    if args.action == "delete":
        library.delete(args.name)
        out({"status": "ok", "deleted": args.name, "note": "copies already installed in projects are untouched"})
        return 0
    if args.action == "new":
        if project.task().info().get("mode", "full") == "light":
            raise HarnessError("creating skills requires full mode; run `autoharness mode full` first")
        if not args.description:
            raise HarnessError("skill new needs --description: when should an agent use this skill?")
        path = project.task().new_skill(args.name, args.description)
        out({"status": "ok", "created": str(path), "next": SKILL_WRITING.format(name=args.name)})
        return 0
    if args.action == "test":
        return _skill_test(args, project)

    # save
    entry, task = _find_local_skill(project, args.name)
    if not entry["spec"].startswith("local:"):
        raise HarnessError("only skills created with `skill new` or installed from the library (local:) can be saved")
    agent = task.agent if task else project.agent
    src = agent.skills_path(project.root) / args.name
    res = library.check(src)
    if res.status != "ok":
        out({"status": res.status, "skill": args.name, "problems": res.problems, "warnings": res.warnings,
             "next": "Fix the problems and run skill save again." if res.status == "invalid"
                     else "Remove the flagged content; saving is blocked until it is gone."})
        return 1
    if entry.get("source") == "generated":   # a library skill installed here was tested where it was written
        tested = skilltests.status(task, args.name)
        if not tested:
            res.warnings.append(f"never tested: `autoharness skill test {args.name} --task \"...\"` shows whether "
                                "an agent does the work better with it than without")
        elif not tested["passed"]:
            res.warnings.append(f"its last test did not pass ({tested['wins']} won, {tested['losses']} lost, "
                                f"{tested['ties']} tied against an agent without it)")
    if not args.approve:
        out({"status": "approval_required", "skill": args.name, "description": res.description,
             "files": sorted(str(p.relative_to(src)) for p in src.rglob("*") if p.is_file()),
             "warnings": res.warnings, "overwrites": library.exists(args.name),
             "next": "Show the user the name, description and warnings. Re-run with --approve only if they agree."})
        return EXIT_APPROVAL
    dest = library.save(src, {"description": res.description,
                              "capabilities": task.info().get("capabilities", []) if task else [],
                              "task": task.id if task else entry.get("task"), "project": str(project.root)})
    if task:
        task.log(f"saved skill `{args.name}` to the library")
    out({"status": "ok", "saved": str(dest), "use_elsewhere": f"autoharness add skill local:{args.name}"})
    return 0


def render_skill(agent, user: bool = False) -> str:
    """The skill for `agent`; where several agents read the same skills dir, one text names each of them."""
    text = resources.files("autoharness").joinpath("skill/SKILL.md").read_text("utf-8")
    group = agents.sharing_skills_dir(agent, user)
    if len(group) == 1:
        name, mcp_file, reload = agent.name, f"`{agent.mcp_file}`", agent.reload_hint
    else:
        name = "<" + ", ".join(f"{a.name} in {a.title}" for a in group) + ">"
        mcp_file = " or ".join(f"`{a.mcp_file}` ({a.title})" for a in group)
        reload = "do what their agent needs (" + "; ".join(f"{a.title}: {a.reload_hint}" for a in group) + ")"
    return (text.replace("{{AGENT}}", name).replace("`{{MCP_FILE}}`", mcp_file)
            .replace("{{RELOAD}}", reload))


def stale_skills(project) -> list[str]:
    """Installed copies of the autoharness skill that differ from the one this version ships:
    project skills for any agent in the repository, and user skills for the active agent."""
    stale, seen = [], set()
    for agent in agents.AGENTS.values():
        path = agent.skills_path(project.root) / "autoharness" / "SKILL.md"
        if path not in seen:
            seen.add(path)
            want = render_skill(agent, False).replace("\r\n", "\n")
            try:
                if path.is_file() and path.read_text("utf-8").replace("\r\n", "\n") != want:
                    stale.append(f"{path} is out of date with this autoharness: run "
                                 f"`autoharness --agent {agent.name} install-skill`")
            except OSError:
                pass

    active = getattr(project, "agent", None)
    if active:
        user_path = active.user_skills_path() / "autoharness" / "SKILL.md"
        if user_path not in seen:
            seen.add(user_path)
            want = render_skill(active, True).replace("\r\n", "\n")
            try:
                if user_path.is_file() and user_path.read_text("utf-8").replace("\r\n", "\n") != want:
                    stale.append(f"{user_path} is out of date with this autoharness: run "
                                 f"`autoharness --agent {active.name} install-skill --user`")
            except OSError:
                pass
    return stale


def _build_autonomous_directives(title: str, agent_name: str) -> str:
    """Build the standard AutoHarness autonomous directives markdown section for an agent."""
    return (
        f"# {title} AutoHarness Autonomous Directives\n\n"
        "<EXTREMELY_IMPORTANT>\n"
        "## CRITICAL MANDATORY BEHAVIOR: AUTOHARNESS AUTONOMOUS EXECUTION\n"
        "You are paired with the AutoHarness engineering workflow for autonomous agentic software development.\n"
        "You MUST ALWAYS autonomously use AutoHarness on Turn 1 of any non-trivial user task (features, bugfixes, refactoring, systems).\n"
        "Do NOT wait for the user to say \"Use autoharness\". Run it immediately:\n"
        f"`autoharness --agent {agent_name} init \"<user's task verbatim>\" --mode full`\n\n"
        "### Strict Software Engineering & Craftsmanship Invariants:\n"
        "1. **Architectural Decomposition (--components)**:\n"
        "   Every non-trivial software engineering deliverable MUST be decomposed into 2-4 decoupled, independently testable architectural components / modules / layers in `autoharness brief`:\n"
        "   `--components \"<comp1>: <responsibility & interface> - verify <test/assertion>; <comp2>: ...\"`\n"
        "   (e.g., C1: domain models & persistence schema; C2: core business logic / service layer; C3: API endpoints / CLI commands; C4: integration test suite).\n"
        "   NEVER build a monolithic deliverable in a single pass. You MUST build, test, and register each component in isolation\n"
        "   (`autoharness evidence add <file> --kind component --for C1`) before full integration.\n\n"
        "2. **Craftsmanship Contracts (--techniques)**:\n"
        "   You MUST declare explicit professional engineering craftsmanship standards and invariants in `autoharness brief`:\n"
        "   `--techniques \"<tech1>: <principle & enforcement>; <tech2>: ...\"`\n"
        "   (e.g., clean-architecture: domain core has zero external framework dependencies; strict-type-contracts: 100% typed with mypy/tsc strict; boundary-schema-validation: all inputs validated via schemas; zero-mock-integration-tests: tests verify against real state or in-memory fixtures; idempotent-mutations: mutating operations safe to retry; structured-errors: typed domain exceptions).\n\n"
        "3. **Structural Stencils & Invariants (stencil scan)**:\n"
        "   Use `autoharness stencil scan` to extract structural invariants whenever reference code, API schemas, AST structures, or specifications exist:\n"
        "   - Python/TypeScript AST skeletons: `autoharness stencil scan <file.py> --mode ast`\n"
        "   - JSON Schemas / API contracts: `autoharness stencil scan <spec.json> --mode schema`\n"
        "   - Visual / UI wireframes (if media): `autoharness stencil scan <ref.png> --mode contour`\n"
        "   Verify structural conformance with `autoharness stencil check <stencil-id> <target>`.\n\n"
        "4. **Rigorous Verification & Test-Driven Discipline**:\n"
        "   Every check must execute real assertions with pass/fail exit codes (`autoharness verify run \"<test command>\"`). Hollow checks or echo commands without assertions are rejected.\n\n"
        "5. **Independent Critic Review**:\n"
        "   Before declaring any task done, request independent review via `autoharness critic`. Resolve all findings before `autoharness teardown`.\n"
        "</EXTREMELY_IMPORTANT>"
    )


def _write_text_atomic(path: Path, text: str) -> None:
    """Write UTF-8 text atomically via temp file in the same directory + os.replace."""
    from autoharness.transactions import atomic_write
    atomic_write(path, text.encode("utf-8"))


def _upsert_marked_markdown(path: Path, body: str, marker: str = "autoharness:directives") -> None:
    """Insert or update a marker-delimited section in a Markdown file, preserving user content."""
    start = f"<!-- >>> {marker} -->"
    end = f"<!-- <<< {marker} -->"
    block = f"{start}\n{body.rstrip()}\n{end}\n"
    if path.exists():
        existing = path.read_text("utf-8")
        pattern = re.compile(rf"{re.escape(start)}\n.*?{re.escape(end)}\n?", re.S)
        legacy_pattern = re.compile(
            r"# (?:OpenCode|ZCode) AutoHarness Autonomous Directives\s+<EXTREMELY_IMPORTANT>.*?</EXTREMELY_IMPORTANT>\n?",
            re.S,
        )
        if pattern.search(existing):
            updated = pattern.sub(lambda _: block, existing)
        elif legacy_pattern.search(existing):
            updated = legacy_pattern.sub(lambda _: block, existing)
        else:
            sep = "\n\n" if existing.strip() else ""
            updated = existing.rstrip() + sep + block if existing.strip() else block
    else:
        updated = block
    _write_text_atomic(path, updated)


def _configure_opencode_autonomous_directives(user: bool, project_root: Path | None = None) -> list[str]:
    """Ensure OpenCode has ambient instructions to run AutoHarness autonomously for agentic software development."""
    configured = []
    directive = _build_autonomous_directives("OpenCode", "opencode")
    if user:
        cfg_dir = Path.home() / ".config" / "opencode"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        instr_file = cfg_dir / "instructions.md"
        _upsert_marked_markdown(instr_file, directive)
        configured.append(str(instr_file))
        agents_file = cfg_dir / "AGENTS.md"
        _upsert_marked_markdown(agents_file, directive)
        configured.append(str(agents_file))

        opencode_json = cfg_dir / "opencode.json"
        data = {}
        if opencode_json.exists():
            try:
                data = json.loads(opencode_json.read_text("utf-8") or "{}")
                if not isinstance(data, dict):
                    data = {}
            except (OSError, json.JSONDecodeError):
                data = {}
        instructions_list = data.get("instructions")
        if instructions_list is None:
            instructions_list = []
        instr_str = str(instr_file).replace("\\", "/")
        if isinstance(instructions_list, list) and instr_str not in instructions_list:
            instructions_list.append(instr_str)
            data["instructions"] = instructions_list
            _write_text_atomic(opencode_json, json.dumps(data, indent=2) + "\n")
            configured.append(str(opencode_json))
    elif project_root:
        agents_file = project_root / "AGENTS.md"
        _upsert_marked_markdown(agents_file, directive)
        configured.append(str(agents_file))
        opencode_json = project_root / "opencode.json"
        if opencode_json.exists():
            try:
                data = json.loads(opencode_json.read_text("utf-8") or "{}")
                if not isinstance(data, dict):
                    data = {}
            except (OSError, json.JSONDecodeError):
                data = {}
            instructions_list = data.get("instructions")
            if instructions_list is None:
                instructions_list = []
            if isinstance(instructions_list, list) and "AGENTS.md" not in instructions_list:
                instructions_list.append("AGENTS.md")
                data["instructions"] = instructions_list
                _write_text_atomic(opencode_json, json.dumps(data, indent=2) + "\n")
                configured.append(str(opencode_json))
    return configured


def _configure_zcode_autonomous_directives(user: bool, project_root: Path | None = None) -> list[str]:
    """Ensure ZCode has ambient instructions (AGENTS.md) to run AutoHarness autonomously."""
    configured = []
    directive = _build_autonomous_directives("ZCode", "zcode")
    if user:
        cfg_dir = Path.home() / ".zcode"
        agents_file = cfg_dir / "AGENTS.md"
        _upsert_marked_markdown(agents_file, directive)
        configured.append(str(agents_file))
    elif project_root:
        agents_file = project_root / "AGENTS.md"
        _upsert_marked_markdown(agents_file, directive)
        configured.append(str(agents_file))
    return configured


def cmd_install_skill(args, project, catalog):
    names = list(agents.AGENTS) if args.all else [project.agent.name]
    installed = {}
    for name in names:
        agent = agents.AGENTS[name]
        base = agent.user_skills_path() if args.user else agent.skills_path(project.root)
        dest = base / "autoharness" / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_skill(agent, args.user), "utf-8")
        installed[name] = str(dest)
        if name == "opencode":
            _configure_opencode_autonomous_directives(args.user, project.root if project else None)
        if name == "zcode":
            _configure_zcode_autonomous_directives(args.user, project.root if project else None)
    out({"status": "ok", "installed": installed})
    return 0


__all__ = [name for name in globals() if not name.startswith("__")]
