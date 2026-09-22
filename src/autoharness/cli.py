"""`autoharness` command line. Every command prints JSON so an agent can parse it.

Exit codes: 0 ok, 1 error, 3 human approval required (re-run with --approve
only after the user has said yes).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import threading
from importlib import resources
from pathlib import Path

from autoharness import agents, config, evidence, library, recipes, registry, text
from autoharness.catalog import Catalog
from autoharness.env import HarnessError, Project, SkillRejected, Task, npx_stage_skill, parse_skill_spec
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
    if spec.startswith("local:"):
        name = parse_skill_spec(spec)[1]
        if not library.exists(name):
            raise HarnessError(f"no skill {name!r} in the library; see `autoharness skill list`")
        return library.trust_report(spec)
    curated ={s for c in catalog.capabilities.values() for s in c.skills}
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


DISCOVERY_QUERIES = 4
DISCOVERY_RESULTS = 5
DISCOVERY_BUDGET = 15.0  # seconds for all searches together


def discovery_queries(catalog: Catalog, task_text: str) -> list[str]:
    """Search terms for a task: each matched capability's curated queries, then the words that matched it."""
    queries = []
    caps = catalog.classify(task_text)[:3]
    for cap, _ in caps:
        queries += cap.search[:1] or [cap.name.replace("-", " ")]
    for cap, matched in caps:
        queries += cap.search[1:2] + [" ".join(matched[:2])]
    if not queries:
        queries.append(" ".join([w for w in text.words(task_text) if len(w) >= 5 and w not in text.STOP][:4]))
    return list(dict.fromkeys(q for q in queries if q.strip()))[:DISCOVERY_QUERIES]


def _search(catalog: Catalog, kind: str, query: str) -> dict:
    fetch = lambda url: registry.http_get_json(url, timeout=8, retries=0)  # noqa: E731
    try:
        if kind == "skill":
            found = [score_skill(r["spec"], r["installs"], catalog.trusted_skill_owners).to_dict() | {"installs": r["installs"]}
                     for r in registry.search_skills(query, limit=DISCOVERY_RESULTS, fetch=fetch)]
            found.sort(key=lambda r: (r["score"], r["installs"]), reverse=True)
            results = [{"name": r["name"], "score": r["score"], "level": r["level"], "installs": r["installs"]} for r in found]
        else:
            results = [score_registry_entry(e, catalog.trusted_mcp_publishers).to_dict()
                       | {"description": e["server"].get("description", "")[:140]}
                       for e in registry.search_mcp(query, limit=DISCOVERY_RESULTS, fetch=fetch)]
            results = [{"name": r["name"], "score": r["score"], "level": r["level"], "description": r["description"]}
                       for r in sorted(results, key=lambda r: r["score"], reverse=True)]
        return {"kind": kind, "query": query, "results": results}
    except Exception as e:  # noqa: BLE001 - one failed source must not stop init
        return {"kind": kind, "query": query, "error": str(e)}


def discover(catalog: Catalog, task_text: str) -> list[dict]:
    """Search skills.sh and the MCP registry for the task, in parallel. Never raises."""
    if os.environ.get("AUTOHARNESS_OFFLINE"):
        return [{"skipped": "AUTOHARNESS_OFFLINE is set"}]
    queries = discovery_queries(catalog, task_text)
    # the MCP registry matches one substring of a server name and slows down under parallel load,
    # so it gets at most two single-word queries
    mcp_queries = list(dict.fromkeys(q.split()[0] for q in queries))[:2]
    jobs = [("skill", q) for q in queries] + [("mcp", q) for q in mcp_queries]
    results: list[dict | None] = [None] * len(jobs)

    def run(i: int, kind: str, query: str) -> None:
        results[i] = _search(catalog, kind, query)

    # daemon threads: a slow registry must not hold up init past the budget, nor the process exit
    threads = [threading.Thread(target=run, args=(i, *j), daemon=True) for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + DISCOVERY_BUDGET
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return [r or {"kind": k, "query": q, "error": f"timed out ({DISCOVERY_BUDGET:.0f}s budget)"}
            for r, (k, q) in zip(results, jobs)]


# --- commands ---------------------------------------------------------------

def cmd_plan(args, project, catalog):
    out({"task": args.task, "suggested": suggestions(catalog, catalog.classify(args.task)),
         "recipes": [{"score": s, "name": r["name"], "capabilities": r["capabilities"]} for s, r in recipes.match(args.task)],
         "all_capabilities": {n: c.description for n, c in catalog.capabilities.items()}})
    return 0


def cmd_init(args, project, catalog):
    task = project.init_task(args.task)
    agent = task.agent
    suggested = suggestions(catalog, catalog.classify(args.task))
    result = {"status": "ok", "task_id": task.id, "dir": str(task.dir),
              "agent": {"name": agent.name, "mcp_config": agent.mcp_file, "skills_dir": agent.skills_dir},
              "suggested": suggested,
              "recipes": [{"score": s, "name": r["name"], "capabilities": r["capabilities"],
                           "mcp": list(r["mcp"]), "skills": r["skills"]} for s, r in recipes.match(args.task)],
              "all_capabilities": {n: c.description for n, c in catalog.capabilities.items()}}
    result["discovered"] = discovered = discover(catalog, args.task)
    task.record_discovery(discovered)
    result["next"] = (
        "Read `discovered`: these skills and MCP servers were found for this task. Search more with "
        "`autoharness search mcp|skill \"<query>\"` where it falls short. Then record "
        "`autoharness brief --goal ... --inputs ... --constraints ... --verify ... --candidates ...`, "
        "with a take/skip verdict and reason for each candidate worth considering, and `--gaps`: for each "
        "capability no found skill covers, `create <skill-name>: <capability>` (you write that skill before "
        "producing the result), or `none: <why tools at hand cover it>`. Tools that do work stay blocked until "
        "the brief is recorded.")
    if not suggested:
        result["next"] = "No keyword hints: pick capabilities from all_capabilities yourself. " + result["next"]
    out(result)
    return 0


def cmd_need(args, project, catalog):
    task = project.task()
    task.note_capability(args.capability, args.reason or "")
    cap = catalog.capabilities.get(args.capability)
    local = library.with_capability(args.capability)
    if cap:
        out({"status": "ok", "capability": cap.name, "curated": {"mcp": cap.mcp, "skills": cap.skills}, "local": local})
    else:
        # the name may just be phrased differently from a curated capability
        related = suggestions(catalog, catalog.classify(f"{args.capability.replace('-', ' ')} {args.reason or ''}"))
        out({"status": "ok", "capability": args.capability, "curated": None, "local": local, "related": related,
             "next": ("No curated entry under this name. Check `related` first; if none fits, try "
                      f"`autoharness search mcp \"{args.capability}\"` and `autoharness search skill \"{args.capability}\"`.")})
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
    if project.active_id():
        project.task().note_search(args.kind, args.query, [r["name"] for r in results[:DISCOVERY_RESULTS]])
    out({"query": args.query, "kind": args.kind, "results": results})
    return 0


CREATE_RE = re.compile(r"\bcreate\s+`?([a-z0-9][a-z0-9-]{1,63})`?")


def cmd_evidence(args, project, catalog):
    task = project.task()
    if args.action == "list":
        out({"evidence": task.info().get("evidence", []), "reviews": task.info().get("reviews", [])})
        return 0
    if not args.file or not args.kind:
        raise HarnessError("usage: autoharness evidence add <file> --kind reference|result [--note ...]")
    item = evidence.add(task, Path(args.file), args.kind, args.note or "")
    nxt = ("Study it and write down what the result must match."
           if args.kind == "reference" else
           "Review this result: record your own pass with `autoharness review --by self ...`, then get an "
           "independent one with `autoharness critic`.")
    out({"status": "ok", "evidence": item, "next": nxt})
    return 0


def cmd_review(args, project, catalog):
    worst = args.worst or ("major" if args.fixable == "yes" else "none")
    task = project.task()
    item = evidence.review(task, args.diff, worst, args.by, args.settled)
    if item.get("auto_limited"):
        out({"status": "limited", "review": item,
             "next": f"\"{item['auto_limited']}\" is ruled out of reach: the final arbiter's fix failed as well, on "
                     "that arbiter's own terms. It no longer blocks; critics count it as minor at most. Tell the user "
                     "about it when you finish. "
                     + ("Nothing else blocks: confirm with `autoharness critic` or finish."
                        if not item["fixable"] else
                        "Fix the other blocker and major differences, add the result, then `autoharness critic`.")})
        return 0
    if item.get("needs_arbiter"):
        final = (" This is the final arbitration for that part: if its fix fails too, the part is ruled out of "
                 "reach automatically." if item.get("final_arbiter") else "")
        out({"status": "arbitrate", "review": item,
             "next": f"\"{item['needs_arbiter']}\" is still listed after {item['repeats']} critic rounds and "
                     f"{item['tried']} different approaches. Do not decide yourself whether it can be fixed: run "
                     "`autoharness arbiter`, give its prompt unchanged to a fresh subagent that neither made nor "
                     "reviewed the work, and record its answer with `autoharness arbitrate --verdict fix|limit "
                     "--text \"<its answer>\" --check \"<its CHECK line>\"`. The next critic will not run before that."
                     + final})
        return 0
    if item.get("needs_approach"):
        part = item["needs_approach"]
        tried = evidence.tried_approaches(task, part)
        done = "; ".join(f"{a['now']}" for a in tried) or "none recorded yet"
        out({"status": "change_approach", "review": item,
             "next": f"The same problem is still listed after {item['repeats']} critic rounds in a row: "
                     f"\"{part}\". What you are doing is not fixing it, so the next critic will not run until you "
                     "change the approach for that part. Approaches tried so far: "
                     f"{done} ({len(tried)} of {evidence.MAX_APPROACHES} before an arbiter decides). "
                     "1) Name the method you used and a different one: rebuild that part from the reference piece "
                     "by piece (measure or quote the reference instead of working from memory), reduce it to what "
                     "carries it, or change the technique. 2) If you lack the know-how for that part, that is a "
                     "capability gap: `autoharness need <capability> --reason \"<part>\"`, `autoharness search "
                     "skill \"<part>\"`, or write down what you learn in a skill. 3) Record it: `autoharness "
                     "approach --part \"<part>\" --was \"<old method>\" --now \"<new method>\"`. 4) Rebuild the part, "
                     "add the result and a close-up of the part, then `autoharness critic`."})
        return 0
    if item["fixable"]:
        nxt = ("Fix the blocker and major differences first, produce the result again, `evidence add` it (plus "
               "close-ups of the parts in question), and review again. If you believe a critic point is wrong, "
               "do not drop it yourself: add a close-up and run `autoharness critic --dispute \"<the point>\"`.")
    else:
        nxt = ("No blocker or major difference is fixable in this review."
               + ("" if item["by"] == "critic" else " Confirm with an independent critic: `autoharness critic`."))
    out({"status": "ok", "review": item, "next": nxt})
    return 0


def cmd_approach(args, project, catalog):
    task = project.task()
    part = args.part or evidence.pending_approach(task)
    if not part:
        out({"status": "error", "error": "no part given and no loop pending: pass --part \"<part>\""})
        return 1
    item = evidence.approach(task, part, args.was, args.now)
    out({"status": "ok", "approach": item,
         "next": f"Rebuild \"{part}\" the new way. Add the whole result and a close-up of that part with "
                 "`autoharness evidence add <file> --kind result`, then run `autoharness critic`: the critic "
                 "will judge that part first against the approaches tried."})
    return 0


def cmd_arbiter(args, project, catalog):
    task = project.task()
    res = evidence.arbiter_prompt(catalog, task, args.part or "")
    res["prompt_file"] = evidence.save_prompt(task, "arbiter", res["prompt"])
    out({"status": "ok"} | res | {
        "next": "Give the prompt unchanged to a fresh subagent that neither made nor reviewed this work (or tell it "
                "to read `prompt_file` and follow it). Record its answer: `autoharness arbitrate --verdict <its "
                "VERDICT> --text \"<its answer>\"`."})
    return 0


def cmd_arbitrate(args, project, catalog):
    task = project.task()
    part = args.part or evidence.pending_arbiter(task)
    if not part:
        out({"status": "error", "error": "no part given and no arbiter pending: pass --part \"<part>\""})
        return 1
    item = evidence.arbitrate(task, part, args.verdict, args.text, args.check or "")
    nxt = (f"Follow the arbiter's technique for \"{part}\" (it is now the next approach): rebuild that part and add "
           f"the result and a close-up of it. Then run the arbiter's check on it (\"{item.get('check')}\") and record "
           "what it measured with `autoharness check --result \"<measured>\" --pass yes|no`: if it fails, the fix "
           "did not take effect, so keep working on it first. Only then run `autoharness critic`."
           if args.verdict == "fix" else
           f"\"{part}\" no longer blocks: critics will count it as minor at most. Fix the other blocker and major "
           "differences, add the result, then run `autoharness critic`.")
    out({"status": "ok", "arbitration": item, "next": nxt})
    return 0


def cmd_check(args, project, catalog):
    item = evidence.record_check(project.task(), args.result, args.passed == "yes")
    nxt = ("The fix took effect by its own measure: run `autoharness critic`." if args.passed == "yes" else
           "The check failed: the fix has not taken effect. Keep working on it and record the check again when it "
           "passes; if it cannot pass, `autoharness critic` still runs, and the critic sees the failed measurement.")
    out({"status": "ok", "arbitration": item, "next": nxt})
    return 0


def cmd_critic(args, project, catalog):
    task = project.task()
    res = evidence.critic_prompt(catalog, task, args.dispute or "")
    res["prompt_file"] = evidence.save_prompt(task, "critic", res["prompt"])
    out({"status": "ok"} | res | {
        "next": "Give `prompt` to a fresh subagent that did not make this work (Claude Code: the Agent tool; "
                "elsewhere a new session), unchanged: paste it, or tell the subagent to read `prompt_file` and "
                "follow it. Do not add your own opinion to it. Record its answer verbatim in "
                "substance: `autoharness review --by critic --diff \"<its list>\" --worst <its VERDICT>`."})
    return 0


def cmd_brief(args, project, catalog):
    task = project.task()
    planned = list(dict.fromkeys(CREATE_RE.findall(args.gaps)))
    if not planned and not args.gaps.strip().lower().startswith("none"):
        raise HarnessError("--gaps must name each skill you will create (`create <skill-name>: <capability>`) "
                           "or start with `none:` and say why the tools at hand cover every capability")
    for name in planned:
        library.validate_name(name)
    task.record_brief({"goal": args.goal, "inputs": args.inputs, "constraints": args.constraints,
                       "verify": args.verify, "candidates": args.candidates, "gaps": args.gaps}
                      | ({"review": args.review} if args.review else {})
                      | ({"fidelity": args.fidelity} if args.fidelity else {}))
    task._update_info(planned_skills=planned)
    warnings = [] if task.discovery_ran() else [
        "no search ran for this task; run `autoharness search skill|mcp \"<query>\"` before deciding nothing fits"]
    create = "".join(f" Create `{n}` now: `autoharness skill new {n} --description \"<when to use it>\"`, fill in "
                     f"When to use / Steps / How to verify from what you found, then follow it for this task and "
                     f"update it as you learn." for n in planned)
    out({"status": "ok", "task_id": task.id, "planned_skills": planned, "warnings": warnings,
         "next": "Install what you decided to take and get the missing inputs first." + create
                 + " Then do the work and verify it the way the brief says. Teardown refuses while a planned "
                   "skill is missing or unfilled."})
    return 0


def cmd_inspect(args, project, catalog):
    if args.kind == "mcp":
        report, source = resolve_mcp(catalog, args.name)
        out(report | {"source": source})
    else:
        out(resolve_skill(catalog, args.name))
    return 0


def already_kept(project, kind: str, name: str) -> int:
    out({"status": "ok", "already_installed": name, "kept": project.kept()[kind][name].get("task"),
         "next": "An earlier task kept this in the project; use it as is."})
    return 0


def cmd_add(args, project, catalog):
    task = project.task()
    if args.kind == "mcp" and (args.as_name or local_mcp_name(args.name)) in project.kept()["mcp"]:
        return already_kept(project, "mcp", args.as_name or local_mcp_name(args.name))
    if args.kind == "skill" and parse_skill_spec(args.name)[1] in project.kept()["skills"]:
        return already_kept(project, "skills", parse_skill_spec(args.name)[1])
    if args.kind == "mcp":
        report, source = resolve_mcp(catalog, args.name)
        if not report["config"]:
            raise HarnessError(f"{args.name} has no installable configuration")
        if report["requires_approval"] and not args.approve:
            return need_approval(f"add MCP server {args.name}", report)
        local = args.as_name or local_mcp_name(args.name)
        warnings = task.add_mcp(local, report["config"], source=source, permissions=report["permissions"],
                                trust={"score": report["score"], "level": report["level"]}, direct=args.direct)
        secrets = report["permissions"].get("secrets", [])
        agent = task.agent
        env_note = (f" The user must set env vars: {', '.join(secrets)} (visible to the agent process)." if secrets else "")
        if task.mcp_lock()[local].get("via") == "gateway":
            out({"status": "ok", "added": local, "agent": agent.name, "via": "gateway", "file": ".autoharness/gateway.json",
                 "secrets_to_set": secrets,
                 "next": "Available now through the autoharness gateway, no restart: call harness_tools, then "
                         "harness_call (or the native <server>__<tool> tools if your client shows them)." + env_note})
        else:
            tip = ("" if project.gateway_active() else
                   " Tip: suggest the user run `autoharness gateway install` once; after one last restart, "
                   "servers added later are available without restarting.")
            out({"status": "ok", "added": local, "agent": agent.name, "file": agent.mcp_file,
                 "config": agent.translate(report["config"])[0], "warnings": warnings,
                 "secrets_to_set": secrets,
                 "next": f"MCP servers load at session start: ask the user to {agent.reload_hint}." + env_note
                         + (" Resolve the warnings before relying on this server." if warnings else "") + tip})
    else:
        report = resolve_skill(catalog, args.name)
        if report["requires_approval"] and not args.approve:
            return need_approval(f"add skill {args.name}", report)
        try:
            stage = library.stage_from_library if args.name.startswith("local:") else npx_stage_skill
            findings = task.add_skill(args.name, force=args.force, stage=stage,
                                      trust={"score": report["score"], "level": report["level"]})
        except SkillRejected as e:
            out({"status": "rejected", "skill": e.spec, "findings": [f.__dict__ for f in e.findings],
                 "next": "Do not install. Pick another skill, or show the findings to the user; --force only on their explicit say-so."})
            return 1
        out({"status": "ok", "added": parse_skill_spec(args.name)[1],
             "scan_findings": [f.__dict__ for f in findings],
             "next": f"Skill is in {task.agent.skills_dir}; invoke it by name (restart the session if it does not show up)."})
    return 0


def cmd_remove(args, project, catalog):
    """Remove from the active task if it added this, else from what earlier tasks kept in the project."""
    task = project.task() if project.active_id() else None
    if task and args.kind == "mcp" and args.name in task.mcp_lock():
        task.remove_mcp(args.name)
    elif task and args.kind == "skill" and args.name in task.skills_lock():
        task.remove_skill(args.name)
    else:
        out({"status": "ok"} | project.remove_kept("mcp" if args.kind == "mcp" else "skills", args.name))
        return 0
    out({"status": "ok", "removed": args.name})
    return 0


def cmd_prune(args, project, catalog):
    out({"status": "ok", "removed": project.prune()})
    return 0


def kept_summary(project) -> dict:
    kept = project.kept()
    return {"mcp": {k: {"task": v.get("task"), "agent": v.get("agent")} for k, v in kept["mcp"].items()},
            "skills": {k: {"spec": v["spec"], "task": v.get("task")} for k, v in kept["skills"].items()}}


def cmd_status(args, project, catalog):
    if not args.task_id and not project.active_id():
        out({"task": None, "kept": kept_summary(project)})
        return 0
    task = project.task(args.task_id)
    out({"task": task.info(), "mcp": task.mcp_lock(),
         "skills": {k: {"spec": v["spec"], "trust": v["trust"]} for k, v in task.skills_lock().items()},
         "kept": kept_summary(project), "log": (task.dir / "harness.md").read_text("utf-8")})
    return 0


def cmd_teardown(args, project, catalog):
    task = project.task(args.task_id)
    warnings = []
    saved = None
    if args.save_recipe:
        saved = str(recipes.save(task, args.save_recipe, args.notes or ""))
        warnings = [f"recipe refers to {v['spec']}, which is not in the library yet; run `autoharness skill save "
                    f"{parse_skill_spec(v['spec'])[1]}` or the recipe cannot install it"
                    for v in task.skills_lock().values()
                    if v["spec"].startswith("local:") and not library.exists(parse_skill_spec(v["spec"])[1])]
    info = task.info()
    blocking, skill_warnings = task.planned_skill_problems()
    blocking += evidence.problems(catalog, task)
    if blocking and args.accepted:
        task._update_info(accepted_by_user=args.accepted)
        task.log(f"user accepted the result as it is: {args.accepted}")
    if blocking and not (args.force or args.accepted):
        out({"status": "incomplete", "task_id": task.id, "problems": blocking,
             "next": "Fix these, then tear down again. If the user decides the result is good enough as it is, "
                     "run `autoharness teardown --accepted \"<their words>\"`."})
        return 1
    warnings += blocking + skill_warnings
    if not task.discovery_ran():
        warnings.append("no discovery ran: no skill or MCP search was made for this task")
    if not info.get("brief"):
        warnings.append("no brief recorded: goal, missing inputs, constraints and verification were never written down")
    policy, source = config.teardown_policy(project.root, args.policy)
    result = task.teardown(policy)
    kept = result["kept"]["mcp"] + result["kept"]["skills"]
    nxt = (f"Tell the user these stay installed in the project: {', '.join(kept)}. "
           "They can remove one with `autoharness remove mcp|skill <name>`, or all with `autoharness prune`."
           if kept else "Tell the user what was removed.")
    if result.get("unsaved_generated"):
        names = ", ".join(u["name"] for u in result["unsaved_generated"])
        nxt += (f" Skills you wrote are not in the library: {names}. If they are worth reusing, "
                "run `autoharness skill save <name>` and ask the user.")
    out({"status": "ok", "policy": policy, "policy_source": source} | result
        | {"recipe_saved": saved, "next": nxt} | ({"warnings": warnings} if warnings else {}))
    return 0


def cmd_gateway(args, project, catalog):
    if args.action == "serve":
        from autoharness.gateway import serve
        return serve(project.root)
    agent = project.agent
    if args.action == "install":
        server = ({"command": "autoharness", "args": ["gateway", "serve"]} if shutil.which("autoharness")
                  else {"command": sys.executable, "args": ["-m", "autoharness", "gateway", "serve"]})
        project.install_gateway(server)
        out({"status": "ok", "installed": agent.name, "file": agent.mcp_file, "config": server,
             "next": f"One last restart: ask the user to {agent.reload_hint}. After that, servers added with "
                     "`add mcp` are available at once through harness_tools / harness_call."})
    elif args.action == "uninstall":
        project.uninstall_gateway()
        out({"status": "ok", "uninstalled": agent.name,
             "next": f"Ask the user to {agent.reload_hint} to drop the gateway."})
    else:
        reg = project.gateway_registry()
        state_file = project.state / "gateway.state.json"
        state = json.loads(state_file.read_text("utf-8")) if state_file.exists() else None
        out({"installed": project.gateway_active(), "agents": reg["installed"], "servers": sorted(reg["servers"]),
             "running": state and state.get("servers"), "state_updated": state and state.get("updated")})
    return 0


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


def cmd_skill(args, project, catalog):
    if args.action == "list":
        entries, warnings = library.list_skills()
        out({"skills": entries} | ({"warnings": warnings} if warnings else {}))
        return 0
    if not args.name:
        raise HarnessError(f"usage: autoharness skill {args.action} <name>")
    if args.action == "delete":
        library.delete(args.name)
        out({"status": "ok", "deleted": args.name, "note": "copies already installed in projects are untouched"})
        return 0
    if args.action == "new":
        if not args.description:
            raise HarnessError("skill new needs --description: when should an agent use this skill?")
        path = project.task().new_skill(args.name, args.description)
        out({"status": "ok", "created": str(path),
             "next": "Fill in every section of SKILL.md now, generalised (no absolute paths, secrets or people's "
                     "names), from the references, docs and findings you have. It is your procedure for this task: "
                     "follow it, and update it when you learn something. At the end run `autoharness skill save "
                     + args.name + "` and ask the user."})
        return 0

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


def cmd_config(args, project, catalog):
    if args.action == "set":
        if not args.value:
            raise HarnessError("usage: autoharness config set <key> <value> [--global]")
        config.set_value(config.global_path() if args.glob else config.project_path(project.root), args.key, args.value)
    keys = [args.key] if args.key else list(config.SETTINGS)
    out({k: dict(zip(("value", "source"), config.resolve(project.root, k))) for k in keys})
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
    text = resources.files("autoharness").joinpath("skill/SKILL.md").read_text("utf-8")
    names = list(agents.AGENTS) if args.all else [project.agent.name]
    installed = {}
    for name in names:
        agent = agents.AGENTS[name]
        base = agent.user_skills_path() if args.user else agent.skills_path(project.root)
        dest = base / "autoharness" / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text.replace("{{AGENT}}", agent.name).replace("{{MCP_FILE}}", agent.mcp_file)
                        .replace("{{RELOAD}}", agent.reload_hint), "utf-8")
        installed[name] = str(dest)
    out({"status": "ok", "installed": installed})
    return 0


HOOK_COMMAND = "autoharness hook"
HOOK_MIN_WORDS = 4
# tools that do work; reading and tool discovery stay open so the agent can look around
GATED_TOOLS = re.compile(r"^(Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|mcp__.*)$")
PRE_TOOL_MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|Skill|mcp__.*"


def session_file(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:80]
    return recipes.home() / "sessions" / f"{safe}.json"


def session_state(session_id: str) -> dict:
    try:
        return json.loads(session_file(session_id).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def set_session_state(session_id: str, **state) -> None:
    f = session_file(session_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state, ensure_ascii=False), "utf-8")


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


def _is_autoharness_call(tool: str, tool_input: dict) -> bool:
    if tool == "Skill":
        return str(tool_input.get("skill", "")).split(":")[-1] == "autoharness"
    return tool in ("Bash", "PowerShell") and str(tool_input.get("command", "")).lstrip().startswith("autoharness")


def cmd_hook(args, project, catalog):
    """Claude Code hook for UserPromptSubmit and PreToolUse: make every real task go through autoharness.

    The first substantial prompt of a session puts it in `pending`; tools that do work are denied until the
    agent invokes the skill, starts a task, or the user waives it. Must never break the session, so any
    internal failure lets the call through.
    """
    try:
        data = json.loads(sys.stdin.read() or "{}")
        event = data.get("hook_event_name", "UserPromptSubmit")
        sid = str(data.get("session_id") or "")
        state = session_state(sid).get("state") if sid else None
        active = project.active_id()
        if active and Task(project, active).info().get("brief") and sid and state != "done":
            set_session_state(sid, state="done", via="brief")
            state = "done"
        if event == "UserPromptSubmit":
            if state in ("done", "waived") or active:
                return 0
            ctx = hook_context(catalog, data.get("prompt", ""), sid)
            if not ctx:
                return 0
            if sid:
                set_session_state(sid, state="pending", prompt=data.get("prompt", "")[:300])
            out({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}})
            return 0
        if event == "PreToolUse":
            tool, tool_input = data.get("tool_name", ""), data.get("tool_input") or {}
            if _is_autoharness_call(tool, tool_input):
                return 0  # the skill and its CLI always pass; only a recorded brief opens the gate
            if state == "pending" and GATED_TOOLS.match(tool):
                out({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "autoharness first: invoke the `autoharness` skill, run `autoharness init`, review the skills and "
                        "MCP servers it discovers, and record `autoharness brief ...` before using " + tool + ". "
                        "Reading files is still allowed. If the user explicitly said not to use it, run "
                        f"`autoharness waive {sid} --reason \"<their words>\"` and tell the user.")}})
        return 0
    except Exception:  # noqa: BLE001 - a hook must not break the session
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
    wanted = {"UserPromptSubmit": None, "PreToolUse": PRE_TOOL_MATCHER}
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


def cmd_install_hook(args, project, catalog):
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autoharness", description=__doc__.split("\n")[0])
    p.add_argument("--project", default=".", help="project root (default: cwd)")
    p.add_argument("--agent", choices=list(agents.AGENTS),
                   help="target coding agent (default: the task's agent, else $AUTOHARNESS_AGENT, else auto-detect, else claude)")
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
    s.add_argument("--direct", action="store_true", help="for mcp: write to the agent's config even if the gateway is installed")
    s = sub.add_parser("remove", help="remove something the active task added or an earlier task kept")
    s.add_argument("kind", choices=["mcp", "skill"])
    s.add_argument("name")
    sub.add_parser("prune", help="remove everything earlier tasks kept in the project")
    s = sub.add_parser("status", help="show the active task harness and what the project keeps")
    s.add_argument("task_id", nargs="?")
    s = sub.add_parser("teardown", help="end the task: keep its harness in the project, or remove it (setting `teardown`)")
    s.add_argument("task_id", nargs="?")
    s.add_argument("--save-recipe", metavar="NAME")
    s.add_argument("--notes")
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
    s.add_argument("action", choices=["new", "save", "list", "delete"])
    s.add_argument("name", nargs="?")
    s.add_argument("--description", help="for `new`: when an agent should use the skill")
    s.add_argument("--approve", action="store_true", help="for `save`: the user explicitly agreed")
    s = sub.add_parser("gateway", help="one MCP server that hot-adds the servers you add, no agent restarts")
    s.add_argument("action", choices=["install", "uninstall", "status", "serve"])
    s = sub.add_parser("recipe", help="saved harness recipes")
    s.add_argument("action", choices=["list", "match", "show"])
    s.add_argument("value", nargs="?", default="")
    s = sub.add_parser("install-skill", help="install the autoharness bootstrap skill")
    s.add_argument("--user", action="store_true", help="install to the agent's user skills dir instead of the project")
    s.add_argument("--all", action="store_true", help="install for every supported agent")
    sub.add_parser("hook", help="Claude Code UserPromptSubmit hook: reads the hook JSON on stdin")
    s = sub.add_parser("evidence", help="record a file you checked: a reference, or what a round produced")
    s.add_argument("action", choices=["add", "list"])
    s.add_argument("file", nargs="?")
    s.add_argument("--kind", choices=list(evidence.KINDS))
    s.add_argument("--note", help="what it shows")
    s = sub.add_parser("review", help="record one review round of the latest result")
    s.add_argument("--diff", required=True, help="the differences found, most important first")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--worst", choices=list(evidence.SEVERITIES),
                   help="worst severity among the fixable differences (the critic's VERDICT)")
    g.add_argument("--fixable", choices=["yes", "no"], help="short form: yes = major, no = none")
    s.add_argument("--by", choices=["self", "critic"], default="self")
    s.add_argument("--settled", action="append", default=[], metavar="POINT",
                   help="a disputed point this critic settled from measurements in the files, and how; "
                        "later critics see it (repeatable)")
    s = sub.add_parser("critic", help="a prompt for an independent reviewer of the latest result")
    s.add_argument("--dispute", help="a point of the last critic review you believe is wrong; a fresh critic decides")
    s = sub.add_parser("approach", help="record a change of approach for a part that keeps failing review")
    s.add_argument("--part", help="the part that loops (default: the one the last review named)")
    s.add_argument("--was", required=True, help="the method used so far")
    s.add_argument("--now", required=True, help="the different method you will use now")
    s = sub.add_parser("arbiter", help="a prompt for an independent arbiter on a part that outlasted its approaches")
    s.add_argument("--part", help="the part to arbitrate (default: the one the last review sent to an arbiter)")
    s = sub.add_parser("arbitrate", help="record an arbiter's verdict: fix (next approach) or limit (stops blocking)")
    s.add_argument("--part", help="the part (default: the one waiting for an arbiter)")
    s.add_argument("--verdict", required=True, choices=["fix", "limit"])
    s.add_argument("--text", required=True, help="the arbiter's answer: the technique, or why it is out of reach")
    s.add_argument("--check", help="for a fix: the arbiter's measurable CHECK line")
    s = sub.add_parser("check", help="record what the arbiter's check measured on the new result")
    s.add_argument("--result", required=True, help="what the check measured")
    s.add_argument("--pass", dest="passed", required=True, choices=["yes", "no"])
    s = sub.add_parser("brief", help="record what a good result is, what is missing, and the verdict on candidates")
    for flag, what in [("--gaps", "`create <skill-name>: <capability>` for each capability no skill covers, "
                                  "or `none: <why>`"),
                       ("--goal", "what a good result looks like (the quality bar)"),
                       ("--inputs", "missing inputs and how you will get them (references, docs, data)"),
                       ("--constraints", "the user's constraints, verbatim"),
                       ("--verify", "how the result will be checked, and against what"),
                       ("--candidates", "each discovered skill/MCP worth considering: take or skip, and why")]:
        s.add_argument(flag, required=True, help=what)
    s.add_argument("--fidelity", help="the level to judge the result at, e.g. `stylised poster, not photographic`")
    s.add_argument("--review", choices=["critic"],
                   help="require an independent critic for this task even if its capabilities do not")
    s = sub.add_parser("waive", help="let one agent session skip autoharness (only when the user said so)")
    s.add_argument("session_id")
    s.add_argument("--reason", required=True, help="the user's words")
    s = sub.add_parser("install-hook", help="add the prompt hook to Claude Code settings (project, or --user)")
    s.add_argument("--user", action="store_true", help="~/.claude/settings.json instead of the project")
    s.add_argument("--settings", help="explicit settings file")
    s.add_argument("--remove", action="store_true", help="take the hook out again")
    s = sub.add_parser("agents", help="supported agents and where they keep MCP servers and skills")
    return p


COMMANDS = {"plan": cmd_plan, "init": cmd_init, "need": cmd_need, "search": cmd_search, "inspect": cmd_inspect,
            "add": cmd_add, "remove": cmd_remove, "prune": cmd_prune, "status": cmd_status,
            "teardown": cmd_teardown, "config": cmd_config, "skill": cmd_skill, "gateway": cmd_gateway,
            "recipe": cmd_recipe, "install-skill": cmd_install_skill, "agents": cmd_agents,
            "hook": cmd_hook, "install-hook": cmd_install_hook, "waive": cmd_waive, "brief": cmd_brief,
            "evidence": cmd_evidence, "review": cmd_review, "critic": cmd_critic, "approach": cmd_approach,
            "arbiter": cmd_arbiter, "arbitrate": cmd_arbitrate, "check": cmd_check}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    project = Project(Path(args.project).resolve(), args.agent)
    try:
        return COMMANDS[args.cmd](args, project, Catalog.load())
    except HarnessError as e:
        out({"status": "error", "error": str(e)})
        return 1
    except OSError as e:
        out({"status": "error", "error": f"network or filesystem error: {e}"})
        return 1
