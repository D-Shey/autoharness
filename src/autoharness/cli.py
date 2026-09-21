"""`autoharness` command line. Every command prints JSON so an agent can parse it.

Exit codes: 0 ok, 1 error, 3 human approval required (re-run with --approve
only after the user has said yes).
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from pathlib import Path

from autoharness import recipes, registry
from autoharness.catalog import Catalog
from autoharness.env import HarnessError, Project, SkillRejected, parse_skill_spec
from autoharness.trust import curated_report, score_registry_entry, score_skill

EXIT_APPROVAL = 3


def out(data) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def need_approval(what: str, report: dict) -> int:
    out({"status": "approval_required", "what": what, "trust": report,
         "next": "Show this to the user. Re-run the same command with --approve only if they agree."})
    return EXIT_APPROVAL


# --- resolution helpers -----------------------------------------------------

def resolve_mcp(catalog: Catalog, name: str) -> tuple[dict, str]:
    """Curated short names win; otherwise the name must be an exact MCP registry server name."""
    if name in catalog.mcp_servers:
        return curated_report(name, catalog.mcp_servers[name]).to_dict(), "catalog"
    entry = registry.get_mcp(name)
    if not entry:
        raise HarnessError(f"{name!r} is neither a curated server ({', '.join(catalog.mcp_servers)}) nor in the MCP registry")
    return score_registry_entry(entry, catalog.trusted_mcp_publishers).to_dict(), "registry.modelcontextprotocol.io"


def local_mcp_name(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def resolve_skill(catalog: Catalog, spec: str) -> dict:
    curated = {s for c in catalog.capabilities.values() for s in c.skills}
    if spec in curated:
        report = score_skill(spec, 10_000, catalog.trusted_skill_owners).to_dict()
        report["reasons"].insert(0, "curated catalog entry")
        return report
    repo, skill = parse_skill_spec(spec)
    installs = 0
    try:
        for r in registry.search_skills(skill, limit=25):
            if r["spec"] == spec:
                installs = r["installs"]
                break
    except OSError:
        pass
    return score_skill(spec, installs, catalog.trusted_skill_owners).to_dict()


def suggestions(catalog: Catalog, caps) -> list[dict]:
    return [{"capability": c.name, "description": c.description, "matched": m,
             "mcp": c.mcp, "skills": c.skills} for c, m in caps]


# --- commands ---------------------------------------------------------------

def cmd_plan(args, project, catalog):
    out({"task": args.task, "suggested": suggestions(catalog, catalog.classify(args.task)),
         "recipes": [{"score": s, "name": r["name"], "capabilities": r["capabilities"]} for s, r in recipes.match(args.task)],
         "all_capabilities": {n: c.description for n, c in catalog.capabilities.items()}})
    return 0


def cmd_init(args, project, catalog):
    task = project.init_task(args.task)
    out({"status": "ok", "task_id": task.id, "dir": str(task.dir),
         "suggested": suggestions(catalog, catalog.classify(args.task)),
         "recipes": [{"score": s, "name": r["name"], "capabilities": r["capabilities"],
                      "mcp": list(r["mcp"]), "skills": r["skills"]} for s, r in recipes.match(args.task)]})
    return 0


def cmd_need(args, project, catalog):
    task = project.task()
    task.note_capability(args.capability, args.reason or "")
    cap = catalog.capabilities.get(args.capability)
    if cap:
        out({"status": "ok", "capability": cap.name, "curated": {"mcp": cap.mcp, "skills": cap.skills}})
    else:
        out({"status": "ok", "capability": args.capability, "curated": None,
             "next": f"No curated entry. Try `autoharness search mcp \"{args.capability}\"` and `autoharness search skill \"{args.capability}\"`."})
    return 0


def cmd_search(args, project, catalog):
    if args.kind == "mcp":
        results = [score_registry_entry(e, catalog.trusted_mcp_publishers).to_dict() | {"description": e["server"].get("description", "")}
                   for e in registry.search_mcp(args.query, limit=args.limit)]
        curated = [curated_report(n, e).to_dict() for n, e in catalog.mcp_servers.items() if args.query.lower() in n]
        results = curated + sorted(results, key=lambda r: r["score"], reverse=True)
    else:
        results = []
        for r in registry.search_skills(args.query, limit=args.limit):
            rep = score_skill(r["spec"], r["installs"], catalog.trusted_skill_owners).to_dict()
            results.append(rep | {"installs": r["installs"]})
        results.sort(key=lambda r: (r["score"], r["installs"]), reverse=True)
    out({"query": args.query, "kind": args.kind, "results": results})
    return 0


def cmd_inspect(args, project, catalog):
    if args.kind == "mcp":
        report, source = resolve_mcp(catalog, args.name)
        out(report | {"source": source})
    else:
        out(resolve_skill(catalog, args.name))
    return 0


def cmd_add(args, project, catalog):
    task = project.task()
    if args.kind == "mcp":
        report, source = resolve_mcp(catalog, args.name)
        if not report["config"]:
            raise HarnessError(f"{args.name} has no installable configuration")
        if report["requires_approval"] and not args.approve:
            return need_approval(f"add MCP server {args.name}", report)
        local = args.as_name or local_mcp_name(args.name)
        task.add_mcp(local, report["config"], source=source, permissions=report["permissions"],
                     trust={"score": report["score"], "level": report["level"]})
        secrets = report["permissions"].get("secrets", [])
        out({"status": "ok", "added": local, "config": report["config"],
             "secrets_to_set": secrets,
             "next": "MCP servers load at session start: ask the user to restart Claude Code (or run /mcp) and approve the project server."
                     + (f" The user must set env vars: {', '.join(secrets)}." if secrets else "")})
    else:
        report = resolve_skill(catalog, args.name)
        if report["requires_approval"] and not args.approve:
            return need_approval(f"add skill {args.name}", report)
        try:
            findings = task.add_skill(args.name, force=args.force,
                                      trust={"score": report["score"], "level": report["level"]})
        except SkillRejected as e:
            out({"status": "rejected", "skill": e.spec, "findings": [f.__dict__ for f in e.findings],
                 "next": "Do not install. Pick another skill, or show the findings to the user; --force only on their explicit say-so."})
            return 1
        out({"status": "ok", "added": parse_skill_spec(args.name)[1],
             "scan_findings": [f.__dict__ for f in findings],
             "next": "Skill is in .claude/skills; invoke it by name (restart the session if it does not show up)."})
    return 0


def cmd_remove(args, project, catalog):
    task = project.task()
    if args.kind == "mcp":
        task.remove_mcp(args.name)
    else:
        task.remove_skill(args.name)
    out({"status": "ok", "removed": args.name})
    return 0


def cmd_status(args, project, catalog):
    task = project.task(args.task_id)
    out({"task": task.info(), "mcp": task.mcp_lock(),
         "skills": {k: {"spec": v["spec"], "trust": v["trust"]} for k, v in task.skills_lock().items()},
         "log": (task.dir / "harness.md").read_text("utf-8")})
    return 0


def cmd_teardown(args, project, catalog):
    task = project.task(args.task_id)
    saved = str(recipes.save(task, args.save_recipe, args.notes or "")) if args.save_recipe else None
    removed = [] if args.keep else task.teardown()
    out({"status": "ok", "removed": removed, "recipe_saved": saved})
    return 0


def cmd_recipe(args, project, catalog):
    if args.action == "list":
        out([{"name": r["name"], "capabilities": r["capabilities"], "uses": r.get("uses", 0)} for r in recipes.load_all()])
    elif args.action == "match":
        out([{"score": s} | r for s, r in recipes.match(args.value)])
    elif args.action == "show":
        match = [r for r in recipes.load_all() if r["name"] == args.value]
        if not match:
            raise HarnessError(f"no recipe named {args.value!r}")
        recipes.mark_used(args.value)
        r = match[0]
        out(r | {"next": "Add each entry with `autoharness add mcp <name>` / `autoharness add skill <spec>`; trust checks still apply."})
    return 0


def cmd_install_skill(args, project, catalog):
    src = resources.files("autoharness").joinpath("skill/SKILL.md")
    base = Path.home() / ".claude" / "skills" if args.user else project.skills_dir
    dest = base / "autoharness" / "SKILL.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text("utf-8"), "utf-8")
    out({"status": "ok", "installed": str(dest)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autoharness", description=__doc__.split("\n")[0])
    p.add_argument("--project", default=".", help="project root (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("plan", help="suggest capabilities for a task (no side effects)")
    s.add_argument("task")
    s = sub.add_parser("init", help="start a task-scoped harness")
    s.add_argument("task")
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
    s = sub.add_parser("remove", help="remove something this task added")
    s.add_argument("kind", choices=["mcp", "skill"])
    s.add_argument("name")
    s = sub.add_parser("status", help="show the active task harness")
    s.add_argument("task_id", nargs="?")
    s = sub.add_parser("teardown", help="remove everything the task added")
    s.add_argument("task_id", nargs="?")
    s.add_argument("--save-recipe", metavar="NAME")
    s.add_argument("--notes")
    s.add_argument("--keep", action="store_true", help="save the recipe but keep the harness")
    s = sub.add_parser("recipe", help="saved harness recipes")
    s.add_argument("action", choices=["list", "match", "show"])
    s.add_argument("value", nargs="?", default="")
    s = sub.add_parser("install-skill", help="install the autoharness bootstrap skill")
    s.add_argument("--user", action="store_true", help="install to ~/.claude/skills instead of the project")
    return p


COMMANDS = {"plan": cmd_plan, "init": cmd_init, "need": cmd_need, "search": cmd_search, "inspect": cmd_inspect,
            "add": cmd_add, "remove": cmd_remove, "status": cmd_status, "teardown": cmd_teardown,
            "recipe": cmd_recipe, "install-skill": cmd_install_skill}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    project = Project(Path(args.project).resolve())
    try:
        return COMMANDS[args.cmd](args, project, Catalog.load())
    except HarnessError as e:
        out({"status": "error", "error": str(e)})
        return 1
    except OSError as e:
        out({"status": "error", "error": f"network or filesystem error: {e}"})
        return 1
