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

from autoharness import agents, answers, approvals, config, egress, evidence, library, recipes, registry, text, vet
from autoharness.catalog import Catalog
from autoharness.env import (ContentChanged, HarnessError, Project, SkillRejected, Task, npx_stage_skill,
                             parse_skill_spec)
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
    return score_registry_entry(entry, catalog.trusted_mcp_publishers, catalog.trusted_mcp_namespaces).to_dict(), "registry.modelcontextprotocol.io"


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
RECIPE_REUSE = 0.3  # recipe match score from which init re-runs the recipe's saved queries
RECIPE_QUERIES = 4


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
            results = [score_registry_entry(e, catalog.trusted_mcp_publishers, catalog.trusted_mcp_namespaces).to_dict()
                       | {"description": e["server"].get("description", "")[:140]}
                       for e in registry.search_mcp(query, limit=DISCOVERY_RESULTS, fetch=fetch)]
            results = [{"name": r["name"], "score": r["score"], "level": r["level"], "description": r["description"]}
                       for r in sorted(results, key=lambda r: r["score"], reverse=True)]
        return {"kind": kind, "query": query, "results": results}
    except Exception as e:  # noqa: BLE001 - one failed source must not stop init
        return {"kind": kind, "query": query, "error": str(e)}


def run_searches(catalog: Catalog, jobs: list[tuple[str, str]]) -> list[dict]:
    """Run (kind, query) searches in parallel within DISCOVERY_BUDGET. Never raises."""
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


def discover(catalog: Catalog, task_text: str, recipe_queries: list[tuple[str, dict]] = ()) -> list[dict]:
    """Search skills.sh and the MCP registry for the task, in parallel. Never raises.

    `recipe_queries` are (recipe name, {"kind", "query"}) pairs from matching recipes, re-run so that
    candidates published since the recipe was saved show up next to its pinned entries."""
    if os.environ.get("AUTOHARNESS_OFFLINE"):
        return [{"skipped": "AUTOHARNESS_OFFLINE is set"}]
    queries = discovery_queries(catalog, task_text)
    # the MCP registry matches one substring of a server name and slows down under parallel load,
    # so it gets at most two single-word queries
    mcp_queries = list(dict.fromkeys(q.split()[0] for q in queries))[:2]
    jobs = [("skill", q) for q in queries] + [("mcp", q) for q in mcp_queries]
    origin = {}
    for name, q in recipe_queries:
        job = (q["kind"], q["query"])
        if job not in jobs and len(origin) < RECIPE_QUERIES:
            jobs.append(job)
            origin[job] = name
    found = run_searches(catalog, jobs)
    return [d | ({"recipe": origin[j]} if j in origin else {}) for d, j in zip(found, jobs)]


def recipe_queries(matches: list[tuple[float, dict]]) -> list[tuple[str, dict]]:
    """The saved queries of recipes that match the task well enough to reuse."""
    return [(r["name"], q) for s, r in matches if s >= RECIPE_REUSE for q in r.get("queries", [])]


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
    matches = recipes.match(args.task)
    result = {"status": "ok", "task_id": task.id, "dir": str(task.dir),
              "agent": {"name": agent.name, "mcp_config": agent.mcp_file, "skills_dir": agent.skills_dir},
              "suggested": suggested,
              "recipes": [{"score": s, "name": r["name"], "capabilities": r["capabilities"],
                           "mcp": list(r["mcp"]), "skills": r["skills"], "queries": r.get("queries", [])}
                          for s, r in matches],
              "all_capabilities": {n: c.description for n, c in catalog.capabilities.items()}}
    result["discovered"] = discovered = discover(catalog, args.task, recipe_queries(matches))
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
    warnings = stale_skills(project) + unapproved_warning(project)
    if warnings:
        result["warnings"] = warnings
        result["next"] = "First tell the user: " + "; ".join(warnings) + ". Then: " + result["next"]
    out(result)
    return 0


def unapproved_servers(project) -> list[str]:
    """Servers in gateway.json the gateway will not run: not approved on this machine (a pull, an older
    autoharness, or the project moved)."""
    servers = project.gateway_registry()["servers"]
    return sorted(n for n, c in servers.items() if not approvals.approved(project.root, n, c))


def unapproved_warning(project) -> list[str]:
    """Only while this agent has the gateway: without it nothing would run them anyway."""
    names = unapproved_servers(project) if project.gateway_active() else []
    return [f"the gateway does not run {', '.join(names)}: not approved on this machine (a pull, an older "
            "autoharness, or a moved project); show the user `autoharness gateway approve --all` and its "
            "configs"] if names else []


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
        results = [score_registry_entry(e, catalog.trusted_mcp_publishers, catalog.trusted_mcp_namespaces).to_dict() | {"description": e["server"].get("description", "")}
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
    if args.action == "diff":
        item = evidence.add_diff(task, project.root, args.base, args.note or "")
        out({"status": "ok", "evidence": item,
             "next": "Review this diff: record your own pass with `autoharness review --by self ...`, then get an "
                     "independent one with `autoharness critic`."})
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


def critic_subject(result: dict | None) -> str:
    return f"result #{result['n']}" if result else "no result"


RETOLD = ("recorded from your retelling of the reviewer's answer: save its answer unedited to a file and pass "
          "--answer-file, so autoharness reads the verdict itself")


def read_answer(task: Task, path: str, kind: str, allowed, subject: str) -> dict:
    answer = answers.read(task, Path(path), kind, subject)
    if answer["verdict"] not in allowed:
        raise HarnessError(f"the answer's VERDICT is {answer['verdict']!r}, not one of {', '.join(allowed)}")
    return answer


def cmd_review(args, project, catalog):
    task = project.task()
    warnings, answer = [], None
    args.by = args.by or ("critic" if args.answer_file else "self")
    if args.answer_file:
        if args.by != "critic":
            raise HarnessError("--answer-file records a critic's answer, not your own review: drop --by self")
        result = evidence.latest(task, "result")
        answer = read_answer(task, args.answer_file, "critic", evidence.SEVERITIES,
                             critic_subject(result))
        diff, worst = answers.as_diff(answer), answer["verdict"]
        given = args.worst or ({"yes": "major", "no": "none"}.get(args.fixable) if args.fixable else None)
        if given and evidence.SEVERITIES.index(given) < evidence.SEVERITIES.index(worst):
            flag = f"--worst {args.worst}" if args.worst else f"--fixable {args.fixable} ({given})"
            raise HarnessError(f"{flag} is milder than the critic's own VERDICT: {worst}")
        if args.diff or given:
            warnings.append("--diff/--worst/--fixable were not used: the list and the verdict are the critic's own, "
                            "from the answer file")
    else:
        if not args.diff or not (args.worst or args.fixable):
            raise HarnessError("record a review with --diff and --worst, or a critic's answer with "
                               "--by critic --answer-file <file>")
        diff, worst = args.diff, args.worst or ("major" if args.fixable == "yes" else "none")
        if args.by == "critic":
            warnings.append(RETOLD)
    extra = {"answer_id": answer["id"]} | ({"previous_review": answer["previous"]} if answer["previous"] else {}) \
        if answer else {}
    item = evidence.review(task, diff, worst, args.by, args.settled, extra | ({"warnings": warnings} if warnings else {}))
    if answer:
        item["answer_file"] = answers.consume(task, answer)
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
    res["answer_id"] = answers.issue(task, "arbiter", res["part"])
    res["prompt"] += answers.instruction(res["answer_id"])
    res["prompt_file"] = evidence.save_prompt(task, "arbiter", res["prompt"])
    out({"status": "ok"} | res | {
        "next": "Give the prompt unchanged to a fresh subagent that neither made nor reviewed this work (or tell it "
                "to read `prompt_file` and follow it). Save its answer unedited to a file and record it: "
                "`autoharness arbitrate --answer-file <file>`; autoharness reads the VERDICT and the CHECK line."})
    return 0


def cmd_arbitrate(args, project, catalog):
    task = project.task()
    part = args.part or evidence.pending_arbiter(task)
    if not part:
        out({"status": "error", "error": "no part given and no arbiter pending: pass --part \"<part>\""})
        return 1
    answer, ignored = None, []
    if args.answer_file:
        answer = read_answer(task, args.answer_file, "arbiter", ("fix", "limit"), part)
        verdict, text_, check = answer["verdict"], answer["body"], answer["check"]
        if args.verdict or args.text or args.check:
            ignored = ["--verdict/--text/--check were not used: they come from the answer file"]
    elif not (args.verdict and args.text):
        raise HarnessError("record the arbiter's answer with --answer-file <file> (or --verdict and --text)")
    else:
        verdict, text_, check = args.verdict, args.text, args.check or ""
    extra = {"answer_id": answer["id"]} | ({"warnings": ignored} if ignored else {}) if answer else \
        {"warnings": [RETOLD]}
    item = evidence.arbitrate(task, part, verdict, text_, check, extra)
    if answer:
        item["answer_file"] = answers.consume(task, answer)
    args.verdict = verdict
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
    res["answer_id"] = answers.issue(task, "critic", critic_subject(evidence.latest(task, "result")))
    res["prompt"] += answers.instruction(res["answer_id"])
    res["prompt_file"] = evidence.save_prompt(task, "critic", res["prompt"])
    out({"status": "ok"} | res | {
        "next": "Give `prompt` to a fresh subagent that did not make this work (Claude Code: the Agent tool; "
                "elsewhere a new session), unchanged: paste it, or tell the subagent to read `prompt_file` and "
                "follow it. Do not add your own opinion to it. Save its answer unedited to a file and record it: "
                "`autoharness review --by critic --answer-file <file>`; autoharness reads the list and the VERDICT."})
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
        reach = (egress.check(report["config"], config.allowed_hosts(project.root)) if report["config"]
                 else {"host": None, "enforced": False, "ok": False, "reason": "no installable configuration"})
        out(report | {"source": source, "egress": reach})
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
    if (gate := vet_gate(task, args)) is not None:
        return gate
    vetted = vet.verdict(task, args.kind, args.name) or {}
    retold = [f"its vet verdict was recorded from your retelling, not from the reviewer's answer file: tell the user"
              ] if vetted and "answer_id" not in vetted else []
    if args.kind == "mcp":
        report, source = resolve_mcp(catalog, args.name)
        if not report["config"]:
            raise HarnessError(f"{args.name} has no installable configuration")
        vetted = vet.verdict(task, "mcp", args.name)
        if vetted and vetted["sha256"] != vet.mcp_digest(report["config"]):
            out({"status": "vet_required", "what": f"mcp {args.name}",
                 "next": f"Its configuration changed since it was vetted: run `autoharness vet mcp {args.name}` again."})
            return 1
        reach = egress.check(report["config"], config.allowed_hosts(project.root))
        if not reach["ok"]:
            report = report | {"requires_approval": True, "reasons": report["reasons"] + [reach["reason"]]}
        if report["requires_approval"] and not args.approve:
            return need_approval(f"add MCP server {args.name}", report | {"egress": reach})
        local = args.as_name or local_mcp_name(args.name)
        warnings = retold + task.add_mcp(local, report["config"], source=source, permissions=report["permissions"],
                                trust={"score": report["score"], "level": report["level"]}, direct=args.direct)
        secrets = report["permissions"].get("env") or report["permissions"].get("secrets", [])
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
            findings = task.add_skill(args.name, force=args.force, stage=stage, vetted_sha256=vetted.get("sha256"),
                                      trust={"score": report["score"], "level": report["level"]})
        except ContentChanged:
            out({"status": "vet_required", "what": f"skill {args.name}",
                 "next": f"Its files changed since they were vetted (upstream, between the two downloads): run "
                         f"`autoharness vet skill {args.name}` again and give the new prompt to a fresh reviewer."})
            return 1
        except SkillRejected as e:
            out({"status": "rejected", "skill": e.spec, "findings": [f.__dict__ for f in e.findings],
                 "next": "Do not install. Pick another skill, or show the findings to the user; --force only on their explicit say-so."})
            return 1
        skill = parse_skill_spec(args.name)[1]
        out({"status": "ok", "added": skill, **({"warnings": retold} if retold else {}),
             "scan_findings": [f.__dict__ for f in findings],
             "next": f"Skill is in {task.agent.skills_dir}; invoke it by name (restart the session if it does not show up)."})
    return 0


def vet_gate(task: Task, args) -> int | None:
    """While the brief has constraints, a candidate needs a vet verdict, and a `breaks` one the user's yes."""
    if not vet.constraints(task) or args.name.startswith("local:"):
        return None
    v = vet.verdict(task, args.kind, args.name)
    if v is None:
        out({"status": "vet_required", "what": f"{args.kind} {args.name}",
             "next": f"The brief has constraints. Run `autoharness vet {args.kind} {args.name}`, give its prompt to a "
                     "fresh subagent, record its answer, then add again."})
        return 1
    if v["verdict"] == "breaks" and not args.approve:
        out({"status": "approval_required", "what": f"add {args.kind} {args.name}", "vet": v,
             "next": "The vetting reviewer found that it breaks the user's constraints. Pick another candidate, "
                     "or show the user the verdict; re-run with --approve only if they agree."})
        return EXIT_APPROVAL
    return None


def cmd_vet(args, project, catalog):
    task = project.task()
    if not vet.constraints(task):
        raise HarnessError("the brief records no constraints to vet against; record `autoharness brief "
                           "--constraints \"<the user's words>\"` first (or there is nothing to vet)")
    if args.answer_file or args.verdict:
        subject = f"{args.kind}:{args.name}"
        answer = read_answer(task, args.answer_file, "vet", vet.VERDICTS, subject) if args.answer_file else None
        verdict, reason = (answer["verdict"], answer["reason"] or answer["body"]) if answer else (args.verdict,
                                                                                                  args.text or "")
        ignored = (["--verdict/--text were not used: they come from the answer file"]
                   if answer and (args.verdict or args.text) else [])
        v = vet.record(task, args.kind, args.name, verdict, reason,
                       {"answer_id": answer["id"]} | ({"warnings": ignored} if ignored else {}) if answer
                       else {"warnings": [RETOLD]})
        if answer:
            v["answer_file"] = answers.consume(task, answer)
        out({"status": "ok", "vetted": args.name} | v | {
            "next": f"`autoharness add {args.kind} {args.name}`." if v["verdict"] == "fits" else
                    "Pick another candidate; install this one only if the user agrees after seeing the verdict."})
        return 0
    if args.kind == "mcp":
        report, source = resolve_mcp(catalog, args.name)
        if not report["config"]:
            raise HarnessError(f"{args.name} has no installable configuration")
        report = report | {"egress": egress.check(report["config"], config.allowed_hosts(project.root))}
        content = vet.mcp_content(args.name, report, source)
        vet.note_pending(task, "mcp", args.name, vet.mcp_digest(report["config"]))
    else:
        repo, skill = parse_skill_spec(args.name)
        stage = library.stage_from_library if args.name.startswith("local:") else npx_stage_skill
        content, digest = vet.skill_content(args.name, stage, repo, skill)
        vet.note_pending(task, "skill", args.name, digest)
    nonce = answers.issue(task, "vet", f"{args.kind}:{args.name}")
    text = vet.prompt(task, args.kind, args.name, content) + answers.instruction(nonce)
    out({"status": "ok", "prompt": text, "answer_id": nonce, "prompt_file": evidence.save_prompt(task, "vet", text),
         "next": "Give `prompt` to a fresh subagent that did not pick this candidate (Claude Code: the Agent tool), "
                 "unchanged. Save its answer unedited to a file and record it: `autoharness vet "
                 f"{args.kind} {args.name} --answer-file <file>`."})
    return 0


def cmd_remove(args, project, catalog):
    """Remove from the active task if it added this, else from what earlier tasks kept in the project."""
    task = project.task() if project.active_id() else None
    if task and args.kind == "mcp" and args.name in task.mcp_lock():
        task.remove_mcp(args.name)
    elif task and args.kind == "skill" and args.name in task.skills_lock():
        task.remove_skill(args.name)
    else:
        result = project.remove_kept("mcp" if args.kind == "mcp" else "skills", args.name, approved=args.approve)
        if "approval_required" in result:
            return kept_elsewhere([args.name], {"entry": result["entry"]})
        out({"status": "ok"} | result)
        return 0
    out({"status": "ok", "removed": args.name})
    return 0


def kept_elsewhere(names: list[str], extra: dict) -> int:
    out({"status": "approval_required", "what": f"uninstall {', '.join(names)}"} | extra
        | {"next": "kept.json lists these, but no task on this machine kept them as listed: a pull, an older "
                   "autoharness, another clone, or an entry changed since. They may be the user's own. Show the "
                   "user the names (and entries). Re-run with --approve only if they agree to all of them; for "
                   "some, `autoharness remove mcp|skill <name> --approve` one at a time. The rest stay installed "
                   "and listed."})
    return EXIT_APPROVAL


def cmd_prune(args, project, catalog):
    removed = project.prune(approved=args.approve)
    waiting = removed.pop("approval_required", [])
    if waiting:
        return kept_elsewhere(waiting, {"removed": removed, "waiting": waiting})
    out({"status": "ok", "removed": removed})
    return 0


def kept_summary(project) -> dict:
    kept = project.kept()
    return {"mcp": {k: {"task": v.get("task"), "agent": v.get("agent")} for k, v in kept["mcp"].items()},
            "skills": {k: {"spec": v["spec"], "task": v.get("task")} for k, v in kept["skills"].items()}}


def cmd_status(args, project, catalog):
    unapproved = ({"gateway_unapproved": names}
                  if project.gateway_active() and (names := unapproved_servers(project)) else {})
    if not args.task_id and not project.active_id():
        out({"task": None, "kept": kept_summary(project)} | unapproved)
        return 0
    task = project.task(args.task_id)
    out({"task": task.info(), "mcp": task.mcp_lock(),
         "skills": {k: {"spec": v["spec"], "trust": v["trust"]} for k, v in task.skills_lock().items()},
         "kept": kept_summary(project)} | unapproved | {"log": (task.dir / "harness.md").read_text("utf-8")})
    return 0


def cmd_teardown(args, project, catalog):
    task = project.task(args.task_id)
    warnings = []
    saved = None
    if args.save_recipe:
        try:
            queries = [recipes.parse_query(q) for q in args.query or []]
        except ValueError as e:
            raise HarnessError(str(e)) from e
        saved = str(recipes.save(task, args.save_recipe, args.notes or "", queries))
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
    if drift := evidence.changed_since_review(task, project.root):
        warnings.append(drift)
    critics = [r for r in info.get("reviews", []) if r["by"] == "critic"]
    if critics and "answer_id" not in critics[-1]:
        warnings.append(f"the last critic review (round {critics[-1]['round']}) was recorded from your retelling, "
                        "not from the critic's answer file: tell the user its verdict is unconfirmed")
    retold = [f"arbiter on `{a['part']}`" for a in info.get("arbitrations", []) if "answer_id" not in a]
    retold += [f"vet of {k}" for k, v in info.get("vetted", {}).items() if "answer_id" not in v]
    if retold:
        warnings.append("recorded from your retelling, not from the reviewer's answer file: " + ", ".join(retold)
                        + "; tell the user these verdicts are unconfirmed")
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
    elif args.action == "approve":
        return gateway_approve(args, project)
    else:
        reg = project.gateway_registry()
        state_file = project.state / "gateway.state.json"
        state = json.loads(state_file.read_text("utf-8")) if state_file.exists() else None
        unapproved = unapproved_servers(project)
        out({"installed": project.gateway_active(), "agents": reg["installed"], "servers": sorted(reg["servers"]),
             "unapproved": unapproved, "running": state and state.get("servers"),
             "state_updated": state and state.get("updated")}
            | ({"next": "The gateway does not run unapproved servers (gateway.json may come from a pull). "
                        "Show the user each one's config: `autoharness gateway approve <name>`."} if unapproved else {}))
    return 0


def gateway_approve(args, project) -> int:
    """Let the gateway run servers in gateway.json that were not added on this machine, after the user's yes:
    one by name, or with --all every one waiting (after upgrading, servers added by an older autoharness)."""
    servers = project.gateway_registry()["servers"]
    if args.all:
        chosen = {n: c for n, c in servers.items() if not approvals.approved(project.root, n, c)}
        if not chosen:
            out({"status": "ok", "approved": [], "next": "Nothing is waiting for approval."})
            return 0
    elif not args.name:
        raise HarnessError("name the server (`autoharness gateway approve <name>`) or pass --all")
    elif args.name not in servers:
        raise HarnessError(f"{args.name!r} is not in .autoharness/gateway.json")
    else:
        chosen = {args.name: servers[args.name]}
    allow = config.allowed_hosts(project.root)
    if not args.approve:
        out({"status": "approval_required", "what": f"let the gateway run {', '.join(chosen)}",
             "servers": {n: {"config": c, "egress": egress.check(c, allow)} for n, c in chosen.items()},
             "next": "These servers were not approved on this machine: gateway.json may have come from a pull, or "
                     "they were added before autoharness asked for approval. Show the user each config (the command "
                     "it runs, the env vars it reads, where it connects). Re-run with --approve only if they agree."})
        return EXIT_APPROVAL
    for name, server in chosen.items():
        approvals.approve(project.root, name, server)
    out({"status": "ok", "approved": sorted(chosen),
         "next": "The gateway starts them within a second; check with harness_tools."})
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
        if args.value is None or (not args.value and config.SETTINGS.get(args.key) is not None):
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
        nxt = "Add each entry with `autoharness add mcp <name>` / `autoharness add skill <spec>`; trust checks still apply."
        if r.get("queries"):
            pinned = set(r["mcp"]) | set(r["skills"])
            fresh = [d | {"results": [x for x in d["results"] if x["name"] not in pinned
                                      and local_mcp_name(x["name"]) not in pinned]} if "results" in d else d
                     for d in ([{"skipped": "AUTOHARNESS_OFFLINE is set"}] if os.environ.get("AUTOHARNESS_OFFLINE")
                               else run_searches(catalog, [(q["kind"], q["query"]) for q in r["queries"]]))]
            r = r | {"fresh": fresh}
            nxt += " `fresh` holds what the recipe's queries find now besides its entries: prefer one that beats a pinned entry."
        out(r | {"next": nxt})
    return 0


def render_skill(agent) -> str:
    text = resources.files("autoharness").joinpath("skill/SKILL.md").read_text("utf-8")
    return (text.replace("{{AGENT}}", agent.name).replace("{{MCP_FILE}}", agent.mcp_file)
            .replace("{{RELOAD}}", agent.reload_hint))


def stale_skills(project) -> list[str]:
    """Installed copies of the autoharness skill, for any agent, that differ from the one this version ships."""
    stale, seen = [], set()
    for agent in agents.AGENTS.values():
        want = render_skill(agent).replace("\r\n", "\n")
        for flag, base in (("", agent.skills_path(project.root)), (" --user", agent.user_skills_path())):
            path = base / "autoharness" / "SKILL.md"
            if path in seen:
                continue
            seen.add(path)
            try:
                if path.is_file() and path.read_text("utf-8").replace("\r\n", "\n") != want:
                    stale.append(f"{path} is out of date with this autoharness: run "
                                 f"`autoharness --agent {agent.name} install-skill{flag}`")
            except OSError:
                continue
    return stale


def cmd_install_skill(args, project, catalog):
    names = list(agents.AGENTS) if args.all else [project.agent.name]
    installed = {}
    for name in names:
        agent = agents.AGENTS[name]
        base = agent.user_skills_path() if args.user else agent.skills_path(project.root)
        dest = base / "autoharness" / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_skill(agent), "utf-8")
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


SMART_QUOTES = "‘’‚‛“”„"   # PowerShell reads these as quotes
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
    if any(q in command for q in SMART_QUOTES):
        return None
    commands, words, word, quote = [], [], None, None
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                word += ch
        elif quote == '"':
            if ch == '"':
                quote = None
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
        elif ch in SHELL_SPECIAL:
            return None
        else:
            word = (word or "") + ch
        i += 1
    if quote:
        return None
    if word is not None:
        words.append(word)
    return [*commands, words] if words or not commands else commands   # a trailing `;` ends nothing new


def _is_autoharness_call(tool: str, tool_input: dict) -> bool:
    """The skill, or one plain `autoharness ...` command (after any `cd <dir> &&`). Nothing may run after it
    in the same call, so the gate is not passed by chaining another command on. The gate keeps the agent to
    the procedure; it is not a sandbox (the agent may also `waive` it)."""
    if tool == "Skill":
        return str(tool_input.get("skill", "")).split(":")[-1] == "autoharness"
    if tool not in ("Bash", "PowerShell"):
        return False
    command = TRAILING_OUTPUT.sub("", str(tool_input.get("command", "")).strip())
    commands = _plain_commands(command, bash=tool == "Bash")
    if not commands:
        return False
    *prefix, last = commands
    while tool == "Bash" and last and ENV_PREFIX.match(last[0]):
        last = last[1:]
    return (all(len(c) == 2 and c[0] == "cd" for c in prefix)
            and last[:1] == ["autoharness"])


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
                        "An autoharness command passes only on its own: `cd <dir> &&` (or `;`) before it is fine, "
                        "and at the end `2>&1` and read-only filters (`| head`, `| tail`, `| jq`, `| grep`, "
                        "`| Select-Object -First N`, `| Out-String`, `| ConvertFrom-Json`); outside quotes no other "
                        "`;`, `|`, `&&`, `#`, redirection or brackets; `$(...)` and backticks only inside single "
                        "quotes; `\\\"` only in Bash. "
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
    s = sub.add_parser("vet", help="check a candidate against the brief's constraints by an independent reviewer")
    s.add_argument("kind", choices=["mcp", "skill"])
    s.add_argument("name")
    s.add_argument("--answer-file", help="record the reviewer's answer, saved unedited")
    s.add_argument("--verdict", choices=list(vet.VERDICTS), help="record the reviewer's VERDICT")
    s.add_argument("--text", help="with --verdict: the reviewer's REASON")
    s = sub.add_parser("remove", help="remove something the active task added or an earlier task kept")
    s.add_argument("kind", choices=["mcp", "skill"])
    s.add_argument("name")
    s.add_argument("--approve", action="store_true", help="the user agreed to uninstall a kept item no task here kept")
    s = sub.add_parser("prune", help="remove everything earlier tasks kept in the project")
    s.add_argument("--approve", action="store_true", help="the user agreed to uninstall kept items no task here kept")
    s = sub.add_parser("status", help="show the active task harness and what the project keeps")
    s.add_argument("task_id", nargs="?")
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
    s.add_argument("action", choices=["new", "save", "list", "delete"])
    s.add_argument("name", nargs="?")
    s.add_argument("--description", help="for `new`: when an agent should use the skill")
    s.add_argument("--approve", action="store_true", help="for `save`: the user explicitly agreed")
    s = sub.add_parser("gateway", help="one MCP server that hot-adds the servers you add, no agent restarts")
    s.add_argument("action", choices=["install", "uninstall", "status", "serve", "approve"])
    s.add_argument("name", nargs="?", help="for `approve`: the server in .autoharness/gateway.json")
    s.add_argument("--approve", action="store_true", help="for `approve`: the user read the config and agreed")
    s.add_argument("--all", action="store_true", help="for `approve`: every server waiting for approval")
    s = sub.add_parser("recipe", help="saved harness recipes")
    s.add_argument("action", choices=["list", "match", "show"])
    s.add_argument("value", nargs="?", default="")
    s = sub.add_parser("install-skill", help="install the autoharness bootstrap skill")
    s.add_argument("--user", action="store_true", help="install to the agent's user skills dir instead of the project")
    s.add_argument("--all", action="store_true", help="install for every supported agent")
    sub.add_parser("hook", help="Claude Code UserPromptSubmit hook: reads the hook JSON on stdin")
    s = sub.add_parser("evidence", help="record a file you checked: a reference, or what a round produced")
    s.add_argument("action", choices=["add", "list", "diff"])
    s.add_argument("file", nargs="?")
    s.add_argument("--kind", choices=list(evidence.KINDS))
    s.add_argument("--base", default="HEAD", help="for `diff`: the revision to compare the working tree with")
    s.add_argument("--note", help="what it shows")
    s = sub.add_parser("review", help="record one review round of the latest result")
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
    s.add_argument("--answer-file", help="the arbiter's answer, saved unedited; autoharness reads VERDICT and CHECK")
    s.add_argument("--verdict", choices=["fix", "limit"])
    s.add_argument("--text", help="the arbiter's answer: the technique, or why it is out of reach")
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


COMMANDS = {"plan": cmd_plan, "init": cmd_init, "need": cmd_need, "search": cmd_search, "inspect": cmd_inspect, "vet": cmd_vet,
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
