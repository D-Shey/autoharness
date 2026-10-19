"""`autoharness` command line. Every command prints JSON so an agent can parse it.

Exit codes: 0 ok, 1 error, 3 human approval required (re-run with --approve
only after the user has said yes).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import threading
from importlib import resources
from pathlib import Path

from autoharness import (__version__, agents, answers, approvals, config, egress, evidence, followups, history, lessons,
                         library, recipes, registry, reviewers, rulings, runs, text, vet)
from autoharness.catalog import Catalog
from autoharness.env import (ContentChanged, HarnessError, Project, SkillRejected, Task, missing_tools,
                             npx_stage_skill, parse_skill_spec, skill_dir)
from autoharness.trust import curated_report, score_registry_entry, score_skill

EXIT_APPROVAL = 3
VERIFY_KINDS = ("browser", "api", "api-client", "logs", "database")


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


def local_skills(catalog: Catalog, task_text: str) -> list[dict]:
    """Skills from the user's own library that fit the task: by the capabilities it matches, or its words.
    Each comes with its record in earlier tasks (`lessons.record`) when it has one."""
    return library.matching([c.name for c, _ in catalog.classify(task_text)], task_text)


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


def rejected_before(project, task_id: str, shown: dict) -> list[dict]:
    """Earlier tasks' standing rejections of the candidates this output lists."""
    candidates = [(d["kind"], r["name"]) for d in shown.get("discovered", []) for r in d.get("results", [])]
    candidates += [(kind, n) for s in shown.get("suggested", []) for kind in ("mcp", "skill")
                   for n in s[kind if kind == "mcp" else "skills"]]
    candidates += [(kind, n) for r in shown.get("recipes", []) for kind in ("mcp", "skill")
                   for n in r.get(kind if kind == "mcp" else "skills", [])]
    return history.for_candidates(history.rejections(project, exclude=task_id), candidates)


REJECTED_NEXT = ("`rejected_before` lists candidates earlier tasks skipped, removed or found breaking the user's "
                 "constraints, with the reason: pass over them unless the reason does not apply to this task. ")


# --- commands ---------------------------------------------------------------

def cmd_plan(args, project, catalog):
    out({"task": args.task, "suggested": suggestions(catalog, catalog.classify(args.task)),
         "recipes": [{"score": s, "name": r["name"], "capabilities": r["capabilities"]} for s, r in recipes.match(args.task)],
         "all_capabilities": {n: c.description for n, c in catalog.capabilities.items()}})
    return 0


LEVELS = ("light", "full", "reviewed")


def cmd_init(args, project, catalog):
    if args.profile and args.mode:
        raise HarnessError("use either --profile or --mode, not both")
    classified = catalog.classify(args.task)
    # --profile is the older spelling of the same levels
    selected = ({"quick": "light", "standard": "full", "reviewed": "reviewed"}[args.profile] if args.profile
                else args.mode or "full")
    if selected == "auto":
        selected = "full" if classified else "light"
    floor, floor_source = config.resolve(project.root, "min_mode")
    raised = floor != "any" and LEVELS.index(selected) < LEVELS.index(floor)
    if raised:
        selected = floor   # the user's setting, not the agent's judgement, sets the lowest level
    mode = "light" if selected == "light" else "full"
    checked = [c.name for c, _ in classified if c.requires]
    # an image to match is the case agents skip most (drawing from memory): light mode has to be argued for
    visual = [c.name for c, _ in classified if c.requires.get("media") == "image"]
    light_reason = (args.light_reason or "").strip()
    if mode == "light" and visual and not light_reason:
        raise HarnessError(
            f"the task text mentions {', '.join(visual)}: such a result has to match something real (a reference "
            "to look at, results to compare, a critic), and light mode skips all of that. Start with `--mode full` "
            "or `--mode reviewed`; if nothing real is to be matched here, say why: `--mode light --light-reason "
            "\"<why no reference is needed>\"`")
    check_owner(project, project.active_id())
    task = project.init_task(args.task, mode=mode)
    if mode == "light" and visual:
        task._update_info(light_reason=light_reason)
        task.log(f"light mode although the task mentions {', '.join(visual)}: {light_reason}")
    if owner := agents.session_id(agent=project.agent_name):
        task._update_info(owner=owner)
    if selected == "reviewed":
        task._update_info(profile="reviewed")
    result = {"status": "ok", "task_id": task.id, "dir": str(task.dir), "mode": selected}
    if raised:
        result["mode_raised"] = f"to {floor} by the min_mode setting ({floor_source})"
        task.log(f"mode raised to {floor} by the min_mode setting ({floor_source})")
    if mode == "light":
        # everything else init prints serves discovery: light mode does none, so it would only fill the context
        if classified:
            result["hint"] = ("the task text mentions " + ", ".join(c.name for c, _ in classified)
                              + ": if a tool for it is missing, run `autoharness mode full`")
            if checked:
                result["hint"] += (f". {', '.join(checked)} come with required references, results and a critic, "
                                   "enforced only in full or reviewed mode: if the result must match something real, "
                                   "run `autoharness mode full` now")
        result["next"] = ("Use the tools already available and verify the result. If you need a new capability, "
                          "run `autoharness mode full` before discovering or adding tools. End with `autoharness teardown`.")
    else:
        agent = task.agent
        matches = recipes.match(args.task)
        result |= {"agent": {"name": agent.name, "mcp_config": agent.mcp_file, "skills_dir": agent.skills_dir},
                   "suggested": suggestions(catalog, classified),
                   "recipes": [{"score": s, "name": r["name"], "capabilities": r["capabilities"],
                                "mcp": list(r["mcp"]), "skills": r["skills"], "queries": r.get("queries", [])}
                               for s, r in matches],
                   "all_capabilities": {n: c.description for n, c in catalog.capabilities.items()}}
        if local := local_skills(catalog, args.task):
            result["local"] = local
        for kind, items in (("skill", result.get("local", [])), ("recipe", result["recipes"])):
            for item in items:
                if rec := lessons.record(kind, item["name"]):   # earlier tasks only, before this offer counts
                    item["record"] = rec
                lessons.bump(kind, item["name"], "offered")
        result["discovered"] = discovered = discover(catalog, args.task, recipe_queries(matches))
        task.record_discovery(discovered)
        if rejected := rejected_before(project, task.id, result):
            result["rejected_before"] = rejected
        result["next"] = (
        "Read `discovered`: these skills and MCP servers were found for this task. Search more with "
        "`autoharness search mcp|skill \"<query>\"` where it falls short. Then record "
        "`autoharness brief --goal ... --inputs ... --constraints ... --verify ... --candidates ...`, "
        "with a take/skip verdict and reason for each candidate worth considering, and `--gaps`: for each "
        "capability no found skill covers, `create <skill-name>: <capability>` (you write that skill before "
        "producing the result), or `none: <why tools at hand cover it>`. Tools that do work stay blocked until "
        "the brief is recorded.")
        if result.get("local"):
            result["next"] = ("`local` lists skills you saved earlier that fit this task: prefer them "
                              "(`autoharness add skill local:<name>`); their \"Facts found here\" were measured "
                              "on another project: re-check them before relying on them. " + result["next"])
        if rejected:
            result["next"] = REJECTED_NEXT + result["next"]
    if mode == "full" and not result["suggested"]:
        result["next"] = "No keyword hints: pick capabilities from all_capabilities yourself. " + result["next"]
    if known := lessons.relevant(args.task):
        result["insights"] = known
        result["next"] = ("`insights` are conclusions earlier tasks reached (`confirmed`: by how many): apply the ones "
                          "that bear on this task, and if this task confirms one, `autoharness lesson add` it again. "
                          + result["next"])
    if pending := followups.listing(project):
        result["followups"] = pending[-followups.SHOWN:]
        result["next"] = (f"`followups` lists {len(pending)} open item(s) earlier tasks found outside their scope: "
                          "if one bears on this task, say so and take it in; close what this task settles with "
                          "`autoharness followup done <id> --note \"...\"`. " + result["next"])
    warnings = stale_skills(project) + unapproved_warning(project)
    if warnings:
        result["warnings"] = warnings
        result["next"] = "First tell the user: " + "; ".join(warnings) + ". Then: " + result["next"]
    out(result)
    return 0


def cmd_mode(args, project, catalog):
    task = project.task()
    if task.info().get("mode", "full") == "light":
        task._update_info(mode="full")
        matches = recipes.match(task.info().get("task", ""))
        task.record_discovery(discover(catalog, task.info().get("task", ""), recipe_queries(matches)))
        task.log("upgraded to full mode")
    if args.value == "reviewed" and task.info().get("profile") != "reviewed":
        task._update_info(profile="reviewed")
        task.log("upgraded to reviewed: an independent critic is required")
    info = task.info()
    level = "reviewed" if info.get("profile") == "reviewed" else "full"
    nxt = ("A brief is recorded; carry on." if info.get("brief") else
           "Review the discovered candidates and record `autoharness brief ...` before adding tools.")
    if level == "reviewed":
        nxt += " Before teardown an independent critic must pass the result: `autoharness critic`."
    local = local_skills(catalog, info.get("task", ""))
    if local:
        nxt = "`local` lists skills you saved earlier that fit this task: prefer them. " + nxt
    rejected = rejected_before(project, task.id, {"discovered": info.get("discovery", [])})
    if rejected:
        nxt = REJECTED_NEXT + nxt
    out({"status": "ok", "task_id": task.id, "mode": level, "discovered": info.get("discovery", []),
         **({"local": local} if local else {}), **({"rejected_before": rejected} if rejected else {}), "next": nxt})
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
    asked = f"{args.capability.replace('-', ' ')} {args.reason or ''}"
    local = library.matching([args.capability] + [c.name for c, _ in catalog.classify(asked)],
                             args.capability) or library.matching([], asked)
    if cap:
        out({"status": "ok", "capability": cap.name, "curated": {"mcp": cap.mcp, "skills": cap.skills}, "local": local}
            | ({"next": "Prefer a `local` skill: `autoharness add skill local:<name>`."} if local else {}))
    else:
        # the name may just be phrased differently from a curated capability
        related = suggestions(catalog, catalog.classify(f"{args.capability.replace('-', ' ')} {args.reason or ''}"))
        out({"status": "ok", "capability": args.capability, "curated": None, "local": local, "related": related,
             "next": (("Prefer a `local` skill: `autoharness add skill local:<name>`. " if local else "")
                      + "No curated entry under this name. Check `related` first; if none fits, try "
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
             "next": "Recorded as a new version of the whole result: the critic sees the latest diff, not earlier "
                     "ones. Review this diff: record your own pass with `autoharness review --by self ...`, then get "
                     "an independent one with `autoharness critic`."})
        return 0
    if not args.file or not args.kind:
        raise HarnessError("usage: autoharness evidence add <file> --kind reference|result [--note ...]")
    item = evidence.add(task, Path(args.file), args.kind, args.note or "", closeup=args.closeup)
    nxt = ("Study it and write down what the result must match."
           if args.kind == "reference" else
           "Review this result: record your own pass with `autoharness review --by self ...`, then get an "
           "independent one with `autoharness critic`.")
    if item.get("closeup"):
        nxt = (f"Recorded as a close-up or part of result #{item['closeup']}: the critic sees it with that whole "
               "result. If it is a new version of the whole, add it again without --closeup"
               + (", at the full size" if item.get("media") == "image" else "") + ". " + nxt)
    elif args.kind == "result":
        nxt = ("Recorded as a new version of the whole result: the critic judges the latest version of each sort "
               "of result and the close-ups or parts added after it with `--closeup`"
               + (" (a crop smaller than the whole is taken as one)" if item.get("media") == "image" else "")
               + "; earlier versions are drafts it does not see. " + nxt)
    if args.kind == "reference" and item.get("media") == "image":
        nxt = ("Check that it shows everything the task names, whole and uncropped; if a part is cut off or "
               "missing, find a reference that shows it and add it as well. " + nxt)
    warnings = ([f"this file is byte for byte the same as evidence #{item['same_as']}: a result must be what "
                 "you produced, not the reference itself; the critic is told"] if item.get("same_as") else [])
    if small := evidence.small_reference(task):
        ref, whole = small
        nxt = (f"The best reference (#{ref['n']}, {ref['size'][0]}x{ref['size'][1]} px) is far smaller than the "
               f"result (#{whole['n']}, {whole['size'][0]}x{whole['size'][1]} px): sizes, shapes and details cannot "
               "be taken from it at the result's scale. Find a larger version of it and add it too. " + nxt)
        warnings.append(f"reference #{ref['n']} is under half the size of result #{whole['n']}")
    out({"status": "ok", "evidence": item, "next": nxt} | ({"warnings": warnings} if warnings else {}))
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


def read_quality(task: Task, answer: dict, path: str | None) -> dict | None:
    """The code-quality reviewer's answer for the critic prompt `answer` answers, when that round had one."""
    wanted = task.info().get("quality_pairs", {}).get(answer["id"])
    if not wanted:
        if path:
            raise HarnessError("no code-quality reviewer was asked in this round (`brief --review pair` asks for "
                               "one): drop --quality-file")
        return None
    if not path:
        raise HarnessError("this round has two reviewers: record the code-quality reviewer's answer as well, "
                           "`--quality-file <file>` (its prompt came with the critic's, under `quality`)")
    quality = read_answer(task, path, "quality", evidence.SEVERITIES, critic_subject(evidence.latest(task, "result")))
    if quality["id"] != wanted:
        raise HarnessError(f"the code-quality answer {quality['id']} belongs to another round than the critic's: "
                           f"record the answer to {wanted}")
    return quality


def MATCHED(item: dict) -> str:
    """How a loop was detected, so a wrong match shows: the previous round's point it was taken for."""
    return (f" (Counted as the previous round's point \"{item['matched_previous']}\": if these are different "
            "problems, tell the user instead of changing an approach that works.)" if item.get("matched_previous") else "")


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
    quality = None
    if answer:
        quality = read_quality(task, answer, getattr(args, "quality_file", None))
        if quality:
            diff = answers.merged_diff(answer, quality)
            worst = max(worst, quality["verdict"], key=evidence.SEVERITIES.index)
    elif getattr(args, "quality_file", None):
        raise HarnessError("--quality-file goes with the critic's --answer-file")
    if not answer:
        if not args.diff or not (args.worst or args.fixable):
            raise HarnessError("record a review with --diff and --worst, or a critic's answer with "
                               "--by critic --answer-file <file>")
        diff, worst = args.diff, args.worst or ("major" if args.fixable == "yes" else "none")
        if args.by == "critic":
            warnings.append(RETOLD)
    ran = getattr(args, "critic_run", None)
    extra = {"answer_id": answer["id"]} | {k: answer[k] for k in ("coverage", "unrequested") if answer[k]} \
        | ({"previous_review": answer["previous"]} if answer["previous"] else {}) | ({"ran": ran} if ran else {}) \
        | ({"runs_judged": {str(n): v for n, v in answer["runs"].items()}} if answer.get("runs") else {}) \
        if answer else {}
    if quality:
        extra["quality"] = {"answer_id": quality["id"], "verdict": quality["verdict"],
                            "diff": answers.as_diff(quality)}
    pair = task.info().get("comparisons", {}).get(answer["id"]) if answer else None
    if pair and (cmp := evidence.compared(pair, answer["closer"])):
        extra["compared"] = cmp
    item = evidence.review(task, diff, worst, args.by, args.settled, extra | ({"warnings": warnings} if warnings else {}))
    for point in item.get("settled", []):
        rulings.add(project, point, "critic", task.id)   # later tasks in the project are told as well
    if answer:
        item["answer_file"] = answers.consume(task, answer)
    if quality:
        item["quality"]["answer_file"] = answers.consume(task, quality)
    if item.get("auto_limited"):
        out({"status": "limited", "review": item,
             "next": f"\"{item['auto_limited']}\" is ruled out of reach: the final arbiter's fix failed as well, on "
                     "that arbiter's own terms. It no longer blocks; critics count it as minor at most. Tell the user "
                     "about it when you finish. "
                     + ("Nothing else blocks: confirm with `autoharness critic` or finish."
                        if not item["fixable"] else
                        "Fix the other blocker and major differences, add the result, then `autoharness critic`.")})
        return 0
    if item.get("revert_to"):
        out({"status": "ask_user", "review": item, "trend": item["trend"],
             "next": f"Twice in a row the blind comparison prefers the earlier result #{item['revert_to']}: the "
                     "fixes since then are making the work worse, and more rounds would go further down. Stop "
                     f"fixing. Restore #{item['revert_to']} (its source, from version control or the copy you "
                     "kept), render it and add it again with `evidence add`. Then ask the user, showing that "
                     "result, `trend` and the points still open: accept it as it is (`autoharness teardown "
                     "--accepted \"<their words>\"`) or keep going from it. Do not run another critic before they "
                     "answer."})
        return EXIT_APPROVAL
    if item.get("needs_arbiter"):
        final = (" This is the final arbitration for that part: if its fix fails too, the part is ruled out of "
                 "reach automatically." if item.get("final_arbiter") else "")
        out({"status": "arbitrate", "review": item,
             "next": f"\"{item['needs_arbiter']}\" is still listed after {item['repeats']} critic rounds and "
                     f"{item['tried']} different approaches"
                     + (", and this round went backwards" if item.get("compared", {}).get("closer") == "earlier"
                        else "") + ". Do not decide yourself whether it can be fixed: run "
                     "`autoharness arbiter`, give its prompt unchanged to a fresh subagent that neither made nor "
                     "reviewed the work, save its answer unedited to a file and record it "
                     "with `autoharness arbitrate --answer-file <file>`. The next critic will not run before that."
                     + final + MATCHED(item)})
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
                     "carries it, or change the technique. Change the kind of method, not its numbers: from memory "
                     "-> taken from the reference or the docs with a tool; tuned by hand -> derived, computed or "
                     "generated; patched in place -> rebuilt from scratch; one piece -> separate parts, each checked "
                     "alone (or the reverse); judged by eye -> a check that fails while the part is wrong. 2) If you lack the know-how for that part, that is a "
                     "capability gap: `autoharness need <capability> --reason \"<part>\"`, `autoharness search "
                     "skill \"<part>\"`, or write down what you learn in a skill. 3) Record it: `autoharness "
                     "approach --part \"<part>\" --was \"<old method>\" --now \"<new method>\"`. 4) Rebuild the part, "
                     "add the result and a close-up of the part, then `autoharness critic`." + MATCHED(item)})
        return 0
    if item.get("loop_asked"):
        out({"status": "ask_user", "review": item, "trend": item["trend"],
             "next": f"{len(item['trend'])} critic rounds and the verdict still blocks. Stop and ask the user, showing "
                     "`trend` (verdict and blocking points per round) and what is still open: keep going (the next "
                     "rounds count from here), or accept the result as it is (`autoharness teardown --accepted "
                     "\"<their words>\"`). Do not run another critic before they answer."})
        return EXIT_APPROVAL
    if item["fixable"]:
        nxt = ("Fix the blocker and major differences first, produce the result again, `evidence add` it (plus "
               "close-ups of the parts in question), and review again. If you believe a critic point is wrong, "
               "do not drop it yourself: add a close-up and run `autoharness critic --dispute \"<the point>\"`.")
    else:
        nxt = ("No blocker or major difference is fixable in this review."
               + ("" if item["by"] == "critic" else " Confirm with an independent critic: `autoharness critic`."))
    if item.get("compared", {}).get("closer") == "earlier":
        nxt = (f"Went backwards: compared blind, the earlier result #{item['compared']['earlier']} is closer to the "
               "reference than this one. Find what got worse since then and restore it before fixing more. " + nxt)
    if short := [f"{k} {v}" for k, v in item.get("coverage", {}).items() if v != "met"]:
        nxt = f"The critic finds these brief items not met: {', '.join(short)}. " + nxt
    if empty := [f"#{n}" for n, v in item.get("runs_judged", {}).items() if v == "does not check"]:
        nxt = (f"The critic finds that run(s) {', '.join(empty)} do not check their item: they no longer count "
               "for it. Write a check that would fail if the item were not met, and `verify run` it. " + nxt)
    if item.get("unrequested"):
        nxt += (" The critic lists work the brief did not ask for (`unrequested`): keep what serves the quality "
                "bar and tell the user about it; drop only what breaks it, a constraint, or behaviour no one asked "
                "to change.")
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
    task = project.task()
    ran = None
    if args.run:
        if args.result or args.passed:
            raise HarnessError("with --run the exit code decides: drop --result and --pass")
        if not evidence.pending_check(task):
            raise HarnessError("no arbiter check is waiting to be run")
        if runs.is_trivial(args.run):
            raise HarnessError(runs.NOT_A_CHECK.format(command=args.run) + ", the one the arbiter asked for")
        ran = runs.run(task, args.run, "arbiter-check", "arbiter", args.timeout)
        args.passed = "yes" if ran["passed"] else "no"
        args.result = f"`{args.run}` exited {ran['exit']}; output in {ran['log']}:\n{ran['tail']}"
    elif not (args.result and args.passed):
        raise HarnessError("run the check with --run \"<command>\" (the exit code decides), or record what you "
                           "measured with --result \"<measured>\" --pass yes|no")
    with write_lock(project):
        item = evidence.record_check(task, args.result, args.passed == "yes", ran and ran["n"], args.deviation)
    nxt = ("The fix took effect by its own measure: run `autoharness critic`." if args.passed == "yes" else
           "The check failed: the fix has not taken effect. Keep working on it and record the check again when it "
           "passes; if it cannot pass, `autoharness critic` still runs, and the critic sees the failed measurement.")
    if not ran:
        nxt = ("Recorded on your word: the critic and teardown show it as reported by you, not run by autoharness. "
               "Where the check can be a script, run it with `autoharness check --run \"<command>\"`. ") + nxt
    out({"status": "ok", "arbitration": item, "next": nxt} | ({"run": ran} if ran else {}))
    return 0


def cmd_critic(args, project, catalog):
    if args.run and args.dispute:
        raise HarnessError("a dispute's ruling is recorded with your `--settled` summary of it: run that critic "
                           "yourself (`autoharness critic --dispute ...` without --run)")
    task = project.task()
    with write_lock(project):
        res = evidence.critic_prompt(catalog, task, args.dispute or "")
        res["answer_id"] = answers.issue(task, "critic", critic_subject(evidence.latest(task, "result")))
        if pair := res.pop("compare", None):
            # which copy is which stays with autoharness: the author does not see it either
            task._update_info(comparisons=task.info().get("comparisons", {}) | {res["answer_id"]: pair})
        res["prompt"] += answers.instruction(res["answer_id"])
        res["prompt_file"] = evidence.save_prompt(task, "critic", res["prompt"])
        if evidence.pair_review(task) and not args.dispute:
            quality = evidence.quality_prompt(task)
            quality_id = answers.issue(task, "quality", critic_subject(evidence.latest(task, "result")))
            quality["prompt"] += answers.instruction(quality_id)
            res["quality"] = {"prompt": quality["prompt"], "answer_id": quality_id,
                              "prompt_file": evidence.save_prompt(task, "quality", quality["prompt"])}
            task._update_info(quality_pairs=task.info().get("quality_pairs", {}) | {res["answer_id"]: quality_id})
        if args.run:
            task.log(f"critic run by {args.run} (answer {res['answer_id']})")
    if args.run:
        answer_file = task.dir / "answers" / f"run-critic-{res['answer_id']}.txt"
        answer_file.parent.mkdir(exist_ok=True)
        jobs = [(res["prompt_file"], answer_file, res["answer_id"])]
        quality_file = None
        if "quality" in res:
            quality_file = task.dir / "answers" / f"run-quality-{res['quality']['answer_id']}.txt"
            jobs.append((res["quality"]["prompt_file"], quality_file, res["quality"]["answer_id"]))
        # unlocked while the reviewers work: they may take many minutes, and they may call autoharness themselves;
        # the two of a pair work side by side, each on its own
        errors: list[Exception] = []

        def review_with(prompt_file, dest, answer_id):
            try:
                reviewers.run(args.run, project.root, Path(prompt_file), dest, args.timeout, answer_id)
            except Exception as e:  # noqa: BLE001 - reported below, after both finish
                errors.append(e)
        threads = [threading.Thread(target=review_with, args=job) for job in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            out({"status": "error", "error": str(errors[0]),
                 "next": "The reviewer could not be started from here, so run it yourself: `autoharness critic` "
                         "(without --run) gives the prompt; hand it unchanged to a fresh subagent and record its "
                         "answer with `autoharness review --by critic --answer-file <file>`."})
            return 1
        with write_lock(project):
            return cmd_review(argparse.Namespace(answer_file=str(answer_file), by="critic", diff=None, worst=None,
                                                 fixable=None, settled=[], critic_run=args.run,
                                                 quality_file=quality_file and str(quality_file)), project, catalog)
    settle = (" It rules on your dispute: record that ruling with the review, `--settled \"<the point and the "
              "ruling>\"`, so later critics do not raise the point again." if args.dispute else "")
    two = ("" if "quality" not in res else
           " This task has two reviewers: give `quality.prompt` (or `quality.prompt_file`) the same way to a second "
           "fresh subagent, in parallel, save its answer unedited too, and record both in one round: "
           "`autoharness review --by critic --answer-file <critic's> --quality-file <quality reviewer's>`.")
    out({"status": "ok"} | res | {
        "next": "Give `prompt` to a fresh subagent that did not make this work (Claude Code: the Agent tool; "
                "elsewhere a new session), unchanged: paste it, or tell the subagent to read `prompt_file` and "
                "follow it. Do not add your own opinion to it. Save its answer unedited to a file and record it: "
                "`autoharness review --by critic --answer-file <file>`; autoharness reads the list and the VERDICT."
                + two + settle})
    return 0


def cmd_brief(args, project, catalog):
    from autoharness import verification
    task = project.task()
    if bool(args.verify) == bool(args.verify_template):
        raise HarnessError("brief requires either --verify or --verify-template")
    verify = verification.guide(args.verify_template)["brief_verify"] if args.verify_template else args.verify
    planned = list(dict.fromkeys(CREATE_RE.findall(args.gaps)))
    if not planned and not args.gaps.strip().lower().startswith("none"):
        raise HarnessError("--gaps must name each skill you will create (`create <skill-name>: <capability>`) "
                           "or start with `none:` and say why the tools at hand cover every capability")
    for name in planned:
        library.validate_name(name)
    if evidence.requirements(catalog, task).get("media") and not (args.fidelity or "").strip():
        raise HarnessError(
            "this task is judged by eye: name the level it is judged at with --fidelity (e.g. \"stylised vector "
            "poster: composition, palette, mood and poses read at a glance; not photographic likeness\"). "
            "Without it a critic judges against the reference itself, which a different medium never reaches, "
            "and every round stays major")
    task.record_brief({"goal": args.goal, "inputs": args.inputs, "constraints": args.constraints,
                       "verify": verify, "candidates": args.candidates, "gaps": args.gaps}
                      | ({"verify_template": args.verify_template} if args.verify_template else {})
                      | ({"review": args.review} if args.review else {})
                      | ({"fidelity": args.fidelity} if args.fidelity else {})
                      | ({runs.PRESERVE: args.preserve} if args.preserve else {}))
    task._update_info(planned_skills=planned)
    warnings = [] if task.discovery_ran() else [
        "no search ran for this task; run `autoharness search skill|mcp \"<query>\"` before deciding nothing fits"]
    create = "".join(f" Create `{n}` now: `autoharness skill new {n} --description \"<when to use it>\"`, fill in "
                     f"When to use / Steps / How to verify from what you found, then follow it for this task and "
                     f"update it as you learn." for n in planned)
    if planned:
        create += " For how to write a good skill: `autoharness need skill-authoring`."
    need = evidence.requirements(catalog, task)
    owed = ", ".join(f"{need[k]} {k}{'s' if need[k] > 1 else ''}" for k in ("reference", "result", "critic")
                     if need.get(k))
    owed = (f" Teardown will insist on evidence for this task: {owed}"
            + (f" ({need['media']} files)" if need.get("media") else "") + "."
            + (f" If what you produce is not a {need['media']} file itself, getting a {need['media']} of it is a "
               "capability like any other: plan it now (`autoharness need <capability>`, or a tool at hand) and "
               "smoke-test it before relying on it." if need.get("media") else "")
            + (" Get the reference first." if need.get("reference") else "")
            if owed else "")
    if args.preserve:
        owed += (" The brief preserves existing behaviour: before teardown, run the regression check with "
                 f"`autoharness verify run \"<command>\" --for {runs.PRESERVE}` on the final code; it must pass.")
    items = runs.brief_items(task.info()["brief"])
    out({"status": "ok", "task_id": task.id, "planned_skills": planned, "warnings": warnings,
         "brief_items": [f"{i['id']}: {i['text']}" for i in items],
         "next": "Install what you decided to take and get the missing inputs first." + owed + create
                 + " Then do the work and verify it the way the brief says: a check that is a command (tests, a "
                   "build, a script) runs through `autoharness verify run \"<command>\" --for <brief item, e.g. B1>`, "
                   "so it counts as checked, not as reported. Teardown refuses while a planned "
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
    if task.info().get("mode", "full") == "light":
        raise HarnessError("adding tools requires full mode; run `autoharness mode full` first")
    if args.kind == "mcp" and (args.as_name or local_mcp_name(args.name)) in project.kept()["mcp"]:
        return already_kept(project, "mcp", args.as_name or local_mcp_name(args.name))
    if args.kind == "skill" and parse_skill_spec(args.name)[1] in project.kept()["skills"]:
        return already_kept(project, "skills", parse_skill_spec(args.name)[1])
    if (gate := vet_gate(task, args)) is not None:
        return gate
    vetted = vet.verdict(task, args.kind, args.name) or {}
    retold = [f"its vet verdict was recorded from your retelling, not from the reviewer's answer file: tell the user"
              ] if vetted and "answer_id" not in vetted else []
    retold += history.warning(history.rejections(project, exclude=task.id), args.kind, args.name)
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
                 "secrets_to_set": secrets, "warnings": warnings,
                 "next": ((" ".join(warnings) + ". Then call harness_tools / harness_call.") if warnings else
                          "Available now through the autoharness gateway, no restart: call harness_tools, then "
                          "harness_call (or the native <server>__<tool> tools if your client shows them; a client that "
                          "defers tools, such as Claude Code, needs them loaded with tool search first).") + env_note})
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
        if args.name.startswith("local:"):
            lessons.mark_used(task, "skill", skill)
        where = skill_dir(task.skills_dir, skill)
        missing = missing_tools(where)
        warnings = retold + ([f"its dependencies name {', '.join(missing)}, not found on PATH: check that the parts "
                              "you need run here before relying on it (a library listed there is not a command)"]
                             if missing else [])
        out({"status": "ok", "added": skill, "skill_md": str(where / "SKILL.md"),
             **({"missing_tools": missing} if missing else {}), **({"warnings": warnings} if warnings else {}),
             "scan_findings": [f.__dict__ for f in findings],
             "next": f"Read {where / 'SKILL.md'} now and follow it: a running session may not list a new skill "
                     "until it restarts. Smoke-test the parts you will use before relying on it."})
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
        task.remove_mcp(args.name, reason=args.reason or "requested")
    elif task and args.kind == "skill" and args.name in task.skills_lock():
        task.remove_skill(args.name, reason=args.reason or "requested")
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


STATUS_LOG_LINES = 30


def discovery_line(d: dict) -> str:
    """One search of `init`/`mode full` as a line of `status`."""
    if "skipped" in d:
        return f"skipped: {d['skipped']}"
    head = f"{d.get('kind', '?')}: {d.get('query', '?')}"
    tail = f"failed: {d['error']}" if "error" in d else f"{len(d.get('results', []))} found"
    return f"{head} ({tail})" + (f" [recipe {d['recipe']}]" if d.get("recipe") else "")


def cmd_status(args, project, catalog):
    unapproved = ({"gateway_unapproved": names}
                  if project.gateway_active() and (names := unapproved_servers(project)) else {})
    if not args.task_id and not project.active_id():
        out({"task": None, "kept": kept_summary(project)} | unapproved)
        return 0
    task = project.task(args.task_id)
    info, log = task.info(), (task.dir / "harness.md").read_text("utf-8")
    if not args.full:
        # init already showed the search results; repeating them here would only fill the agent's context
        info = {k: v for k, v in info.items() if k not in ("discovery", "started_epoch")} | (
            {"discovery": [discovery_line(d) for d in info["discovery"]]} if info.get("discovery") else {})
        lines = log.splitlines()
        if len(lines) > STATUS_LOG_LINES:
            log = "\n".join([f"... {len(lines) - STATUS_LOG_LINES} earlier lines: `autoharness status --full`",
                             *lines[-STATUS_LOG_LINES:]])
    out({"task": info, "mcp": task.mcp_lock(),
         "skills": {k: {"spec": v["spec"], "trust": v["trust"]} for k, v in task.skills_lock().items()},
         "kept": kept_summary(project)} | unapproved | {"checked": runs.summary(task), "log": log})
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
    if (tampered := task.tampered()) and not args.force:
        out({"status": "tampered", "task_id": task.id, "problems": tampered,
             "next": "The task's records were changed around autoharness, so what they say about this task cannot "
                     "be trusted. Tell the user exactly this, and what you changed there if it was you. Only they "
                     "decide: `autoharness teardown --force` ends the task anyway."})
        return 1
    blocking, skill_warnings = task.planned_skill_problems()
    if info.get("mode", "full") == "full" or info.get("brief", {}).get("review") in evidence.REVIEWS \
            or info.get("profile") == "reviewed":
        blocking += evidence.problems(catalog, task)
    blocking += runs.problems(task)
    if args.accepted:
        task._update_info(accepted_by_user=args.accepted)
        task.log(f"user accepted the result as it is: {args.accepted}")
    if blocking and not (args.force or args.accepted):
        out({"status": "incomplete", "task_id": task.id, "problems": blocking,
             "next": "Fix these, then tear down again. If the user, asked in this task, decides the result is "
                     "good enough as it is, run `autoharness teardown --accepted \"<their words>\"`; never on "
                     "your own judgment (with the hook installed it is refused until the user has spoken)."})
        return 1
    learned = lessons.friction(task)
    # the user ending the task (`--accepted`, or `--force` on their word) is not held up for lessons
    if learned and not info.get("lessons_seen") and not (args.accepted or args.force):
        task._update_info(lessons_seen=True)
        ripe = lessons.ripe()
        out({"status": "lessons", "task_id": task.id, "lessons": learned} | ({"ripe_insights": ripe} if ripe else {})
            | {"next": "Before you finish: this task taught something the next one would pay for again (`lessons`). "
                       "Write what failed and what fixed it, as steps and checks, into a skill: the one you created "
                       "or used for this task, or `autoharness skill new <name> --description \"<when to use>\"` "
                       "(values measured here go under \"Facts found here\"). Then `autoharness skill save <name>` (it "
                       "asks the user). A conclusion too small for a skill is an insight: `autoharness lesson add "
                       "\"<when X, do Y, because Z>\"` (one an earlier task reached is merged, and its confidence "
                       "grows). " + RIPE_NEXT * bool(ripe) + "Then run `autoharness teardown` again. If none of it "
                       "is reusable, run teardown again and say why to the user."})
        return 1
    warnings += blocking + skill_warnings
    if not task.discovery_ran():
        warnings.append("no discovery ran: no skill or MCP search was made for this task")
    if not info.get("brief"):
        warnings.append("no brief recorded: goal, missing inputs, constraints and verification were never written down")
    if info.get("light_reason") and info.get("mode") == "light":
        warnings.append(f"light mode on a task that mentions a reference or a visual check, because: "
                        f"{info['light_reason']}; no reference was required")
    best, last = info.get("best_result"), evidence.latest(task, "result")
    critics_done = [r for r in info.get("reviews", []) if r["by"] == "critic"]
    if best and critics_done and critics_done[-1].get("compared", {}).get("closer") == "earlier":
        warnings.append(f"compared blind, the earlier result #{best} is closer to the reference than the final one "
                        f"(#{last['n'] if last else '?'}): tell the user")
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
    checked = runs.summary(task)
    warnings += [f"run #{r['n']} for {r['for']}: the check {r['check_changed']}: tell the user why the check changed"
                 for r in checked["ran"] if r.get("check_changed")]
    warnings += [f"run #{r['n']}: {runs.NOT_A_CHECK.format(command=r['command'])}"
                 for r in checked["ran"] if r.get("not_a_check")]
    policy, source = config.teardown_policy(project.root, args.policy)
    result = task.teardown(policy)
    lessons.settle(task, result["kept"]["skills"], learned)
    kept = result["kept"]["mcp"] + result["kept"]["skills"]
    nxt = (f"Tell the user these stay installed in the project: {', '.join(kept)}. "
           "They can remove one with `autoharness remove mcp|skill <name>`, or all with `autoharness prune`."
           if kept else "Tell the user what was removed.")
    if result.get("unsaved_generated"):
        names = ", ".join(u["name"] for u in result["unsaved_generated"])
        nxt += (f" Skills you wrote are not in the library: {names}. If they are worth reusing, "
                "run `autoharness skill save <name>` and ask the user.")
    found = followups.from_task(project, task.id)
    if found:
        nxt += (" Tell the user the follow-ups this task left for later: "
                + "; ".join(f"#{i['id']} {i['text']}" for i in found) + ".")
    if learned and not info.get("lessons_seen"):
        nxt += (" The task ended on the user's word with friction left unrecorded (`lessons`): offer once to keep "
                "what it taught (`autoharness lesson add` or a skill); do nothing if they decline.")
    nxt += (" Report what was checked as `checked` has it, without upgrading it: `ran` are checks autoharness ran "
            "itself (a stale one ran on older code), `critic` says where each verdict came from, `reported_only` "
            "you only reported"
            + (f", and these brief items were never checked: {', '.join(checked['unchecked_items'])}"
               if checked["unchecked_items"] else "") + ".")
    out({"status": "ok", "policy": policy, "policy_source": source} | result | {"checked": checked}
        | ({"lessons": learned} if learned else {})
        | {"recipe_saved": saved} | ({"followups": found} if found else {}) | {"next": nxt}
        | ({"warnings": warnings} if warnings else {}))
    return 0


def cmd_gateway(args, project, catalog):
    if args.action == "serve":
        from autoharness.gateway import serve
        return serve(project.root)
    agent = project.agent
    if args.action == "install":
        server = {"command": sys.executable, "args": ["-m", "autoharness", "--project", str(project.root),
                                                       "gateway", "serve"]}
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
    updates, warnings = project.codex_gateway_update(project.gateway_registry())
    if updates:
        from autoharness import transactions
        transactions.commit(project.root, updates)
    out({"status": "ok", "approved": sorted(chosen), "warnings": warnings,
         "next": " ".join(warnings) if warnings else "The gateway starts them within a second; check with harness_tools."})
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
        out([{"name": r["name"], "capabilities": r["capabilities"], "uses": r.get("uses", 0)}
             | ({"record": rec} if (rec := lessons.record("recipe", r["name"])) else {}) for r in recipes.load_all()])
    elif args.action == "match":
        out([{"score": s} | r for s, r in recipes.match(args.value)])
    elif args.action == "show":
        match = [r for r in recipes.load_all() if r["name"] == args.value]
        if not match:
            raise HarnessError(f"no recipe named {args.value!r}")
        recipes.mark_used(args.value)
        active = project.active_id()
        lessons.mark_used(project.task(active) if active else None, "recipe", args.value)
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
    """Installed copies of the autoharness skill, for any agent, that differ from the one this version ships."""
    stale, seen = [], set()
    for agent in agents.AGENTS.values():
        for flag, base, user in (("", agent.skills_path(project.root), False),
                                 (" --user", agent.user_skills_path(), True)):
            path = base / "autoharness" / "SKILL.md"
            if path in seen:
                continue
            seen.add(path)
            want = render_skill(agent, user).replace("\r\n", "\n")
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
        dest.write_text(render_skill(agent, args.user), "utf-8")
        installed[name] = str(dest)
    out({"status": "ok", "installed": installed})
    return 0


HOOK_COMMAND = "autoharness hook"
HOOK_MIN_WORDS = 4
# tools that do work; reading and tool discovery stay open so the agent can look around
GATED_TOOLS = re.compile(r"^(Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|mcp__.*)$")
PRE_TOOL_MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|Skill|mcp__.*"
# Antigravity CLI tools that do work, as the Claude Code tool each stands for (shell and MCP are mapped in _agy_event)
AGY_TOOLS = {"write_to_file": "Write", "replace_file_content": "Edit", "multi_replace_file_content": "Edit",
             "edit_notebook": "NotebookEdit", "run_command": "", "send_command_input": "", "call_mcp_tool": ""}
AGY_PRE_TOOL_MATCHER = "|".join(AGY_TOOLS)
AGY_USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)


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
    return (f"autoharness: task {task.id} is active in this project and belongs to another agent session "
            f"({info.get('owner')}): \"{info.get('task', '')[:300]}\" (mode {info.get('mode', 'full')}"
            f"{', brief recorded' if info.get('brief') else ', no brief yet'}).{stale} Last steps: "
            + (" | ".join(log[-3:]) or "none") + ". Tools that do work stay blocked for this session, and it may "
            "neither start nor tear down a task while that one is active. Tell the user. If they want this session "
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
# the other kind each is a plain character (`"Gosling’s"`), as it always is in Bash
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
            and last[:1] == ["autoharness"])


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
                return RECORDS_DENIED
        return None
    if tool not in ("Bash", "PowerShell"):
        return None
    command = str(tool_input.get("command", ""))
    if KEY_FILE.search(command):
        return KEY_DENIED
    command = re.sub(r"\.autoharness[\\/]config\.json", "", _shell_code(command))
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
    request = ""
    try:
        with open(transcript or "", encoding="utf-8") as f:
            for line in f:
                step = json.loads(line)
                if step.get("type") == "USER_INPUT":
                    m = AGY_USER_REQUEST.search(step.get("content", ""))
                    request = m.group(1) if m else step.get("content", "")
    except (OSError, ValueError):
        pass
    return request


def _agy_event(data: dict) -> dict | None:
    """An Antigravity CLI hook payload in the shape Claude Code sends, or None when there is nothing to do.
    agy has no prompt event: the first model call of a turn (PreInvocation, invocationNum 0) stands for it,
    with the prompt read from the transcript. Its shell runs PowerShell on Windows."""
    common = {"session_id": data.get("conversationId", ""), "cwd": (data.get("workspacePaths") or [""])[0]}
    if "toolCall" in data:
        call = data["toolCall"] or {}
        name, call_args = call.get("name", ""), call.get("args") or {}
        if name in ("run_command", "send_command_input"):
            tool = "PowerShell" if os.name == "nt" else "Bash"
            tool_input = {"command": call_args.get("CommandLine", "")} if name == "run_command" else {}
        elif name == "call_mcp_tool":
            tool, tool_input = f"mcp__{call_args.get('ServerName', '')}__{call_args.get('ToolName', '')}", {}
        else:
            tool, tool_input = AGY_TOOLS.get(name) or name, call_args
        return {**common, "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}
    if data.get("invocationNum") != 0:
        return None
    return {**common, "hook_event_name": "UserPromptSubmit", "prompt": _agy_prompt(data.get("transcriptPath"))}


def cmd_hook(args, project, catalog):
    """Claude Code hook for UserPromptSubmit, PreToolUse and Stop: make every real task go through autoharness.
    Antigravity CLI calls it for PreInvocation and PreToolUse, translated by `_agy_event`.

    The first substantial prompt of a session puts it in `pending`; tools that do work are denied until the
    agent invokes the skill, starts a task, or the user waives it. Writes to the task records are denied
    always (`_writes_records`), and ending a turn with the session's task still open is sent back once
    (`_stop_gate`). Must never break the session, so any
    internal failure lets the call through (for agy too: it reads empty output as no decision, `{}` as deny).
    """
    try:
        data = json.loads(_read_stdin_utf8() or "{}")
        agy = "toolCall" in data or "invocationNum" in data
        if agy:
            data = _agy_event(data)
            if data is None:
                return 0
            if data["cwd"]:   # agy runs hooks in the folder of hooks.json, not the project
                project = Project(Path(data["cwd"]).resolve(), project.agent_name)
        event = data.get("hook_event_name", "UserPromptSubmit")
        sid = str(data.get("session_id") or "")
        saved_state = session_state(sid) if sid else {}
        state = saved_state.get("state")
        active = project.active_id()
        task_info = Task(project, active).info() if active else {}
        reviewer = task_info.get("answer_ids", {}).get(os.environ.get(reviewers.ENV_FLAG, ""), {})
        if reviewer and not reviewer.get("used") and not reviewer.get("retired"):
            return 0   # the reviewer `critic --run` started for its pending prompt: read-only, no task of its own
        owner = task_info.get("owner")
        # the task belongs to another session: this one is told about it and stays gated until it claims it
        foreign = bool(active and sid and owner and owner != sid and task_info.get("status") == "active")
        if active and sid and state != "waived":
            ready = task_info.get("mode") == "light" or bool(task_info.get("brief"))
            wanted = "pending" if foreign or not ready else "done"
            via = ("other session" if foreign else "light" if task_info.get("mode") == "light" else
                   "brief" if ready else "waiting")
            if state != wanted or saved_state.get("task_id") != active or saved_state.get("via", via) != via:
                set_session_state(sid, state=wanted, task_id=active, via=via, told=saved_state.get("told"),
                                  stop_seen=saved_state.get("stop_seen"))
                state = wanted
        if event == "Stop":
            return _stop_gate(data, sid, state, active, task_info)
        if event == "UserPromptSubmit":
            if foreign and state != "waived" and saved_state.get("told") != active:
                ctx = handoff(Task(project, active), sid)
                set_session_state(sid, state="pending", task_id=active, via="other session", told=active)
                out({"injectSteps": [{"ephemeralMessage": ctx}]} if agy else
                    {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}})
                return 0
            if active and not foreign and state != "waived":
                try:
                    lessons.note_user_message(Task(project, active), str(data.get("prompt", "")))
                except OSError:
                    pass   # a lost note must never block the user's message
            if state in ("done", "waived") or active:
                return 0
            ctx = hook_context(catalog, data.get("prompt", ""), sid)
            if not ctx:
                return 0
            if sid:
                set_session_state(sid, state="pending", prompt=data.get("prompt", "")[:300])
            out({"injectSteps": [{"ephemeralMessage": ctx}]} if agy else
                {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}})
            return 0
        if event == "PreToolUse":
            tool, tool_input = data.get("tool_name", ""), data.get("tool_input") or {}
            if sid and state != "waived" and (times := _repeated(sid, tool, tool_input)) > REPEAT_LIMIT:
                reason = (f"This is the same {tool} call, unchanged, for the {times}th time in a row: repeating it "
                          "will not give a different result. Change what you call (the command, its input, the "
                          "approach); if a tool, input or know-how is missing, `autoharness need <capability> "
                          "--reason \"<what blocks you>\"`; or stop and ask the user.")
                out({"decision": "deny", "reason": reason} if agy else
                    {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                            "permissionDecisionReason": reason}})
                return 0
            if _is_autoharness_call(tool, tool_input):
                if foreign and state != "waived" and _autoharness_subcommand(tool, tool_input) in ("init", "teardown"):
                    reason = handoff(Task(project, active), sid)
                    out({"decision": "deny", "reason": reason} if agy else
                        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                "permissionDecisionReason": reason}})
                if (active and not foreign and state != "waived"
                        and _autoharness_subcommand(tool, tool_input) == "teardown"
                        and (flags := _user_flags(tool, tool_input))
                        and not lessons.user_messages(Task(project, active))):
                    reason = (f"`teardown {' / '.join(flags)}` ends the task on the user's word, and the user has "
                              "said nothing since this task started: you cannot accept your own result for them. "
                              "Finish what teardown asks for (its `problems`), or stop and ask the user whether "
                              "they accept the result as it is; pass their words only after they answer.")
                    out({"decision": "deny", "reason": reason} if agy else
                        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                "permissionDecisionReason": reason}})
                return 0  # the skill and its CLI always pass; only a recorded brief opens the gate
            if reason := _writes_records(tool, tool_input, project.root):
                out({"decision": "deny", "reason": reason} if agy else
                    {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                            "permissionDecisionReason": reason}})
                return 0
            if state == "pending" and GATED_TOOLS.match(tool):
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
                out({"decision": "deny", "reason": reason} if agy else
                    {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                            "permissionDecisionReason": reason}})
        return 0
    except Exception:  # noqa: BLE001 - a hook must not break the session
        return 0


REPEAT_LIMIT = 5   # the same call this many times in a row may pass; one more is a loop, not progress


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


def _stop_gate(data: dict, sid: str, state: str | None, active: str | None, task_info: dict) -> int:
    """Claude Code's Stop event: the agent is ending its turn while its own task is still open. Once per state
    of the task it is sent back to finish (verify, critic, teardown) or to say it is waiting on the user; a
    second stop with nothing changed goes through, so a turn that must end (a question, an approval) still can."""
    if data.get("stop_hook_active") or not (active and sid) or state == "waived":
        return 0
    if task_info.get("status") != "active" or task_info.get("owner") != sid:
        return 0
    mark = hashlib.sha256(json.dumps(task_info, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    saved = session_state(sid)
    if saved.get("stop_seen") == mark:
        return 0
    set_session_state(sid, **(saved | {"stop_seen": mark}))
    left = []
    if task_info.get("mode") != "light" and not task_info.get("brief"):
        left.append("record the brief")
    if not task_info.get("runs") and not task_info.get("checks"):
        left.append("check the result (`autoharness verify run \"<command>\"`)")
    left.append("`autoharness teardown`")
    out({"decision": "block",
         "reason": f"autoharness task {active} is still open: {task_info.get('task', '')[:200]!r}. Before you end "
                   f"the turn: {'; '.join(left)} (teardown says what else it owes, such as a critic). If you are "
                   "stopping on purpose (a question for the user, an approval, an accepted result), say so in your "
                   "reply and end the turn again: this reminder comes once per state of the task."})
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
    wanted = {"PreInvocation": [handler], "PreToolUse": [{"matcher": AGY_PRE_TOOL_MATCHER, "hooks": [handler]}]}
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


def cmd_install_hook(args, project, catalog):
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


def cmd_eval(args, project, catalog):
    from autoharness import evals
    if args.action == "record":
        if args.case is None or args.variant is None or args.passed is None:
            raise HarnessError("eval record requires --case, --variant and --passed")
        entry = evals.record(project, args.case, args.variant, args.passed, args.seconds,
                             args.tokens, args.cost, args.task_id, args.tool_calls)
        out({"status": "ok", "run": entry})
    elif args.action == "batch":
        if not args.dataset or not args.variant:
            raise HarnessError("eval batch requires --dataset and --variant")
        command = args.command[1:] if args.command and args.command[0] == "--" else args.command
        out(evals.batch(project, Path(args.dataset), args.variant, command, args.timeout))
    else:
        out(evals.compare(project))
    return 0


def cmd_decision(args, project, catalog):
    from autoharness import audit
    task = project.task()
    event = audit.append(task, args.verdict, kind=args.kind, name=args.name, reason=args.reason)
    task.log(f"{args.verdict} {args.kind} `{args.name}`: {args.reason}")
    out({"status": "ok", "decision": event})
    return 0


def cmd_followup(args, project, catalog):
    if args.action == "list":
        out({"status": "ok", "followups": followups.listing(project, args.all)})
        return 0
    if not args.value:
        raise HarnessError("usage: autoharness followup add \"<what, where>\" | done <id> [--note ...]")
    task_id = project.active_id()
    if args.action == "add":
        item = followups.add(project, args.value, task_id)
        if task_id:
            project.task().log(f"follow-up #{item['id']}: {item['text']}")
        out({"status": "ok", "followup": item,
             "next": "Carry on with the task; teardown and the next init will show this follow-up."})
        return 0
    if not args.value.lstrip("#").isdigit():
        raise HarnessError("followup done takes the follow-up's number, e.g. `autoharness followup done 3`")
    item = followups.close(project, int(args.value.lstrip("#")), args.note or "", task_id)
    if task_id:
        project.task().log(f"follow-up #{item['id']} done" + (f": {item['done_note']}" if item["done_note"] else ""))
    out({"status": "ok", "followup": item})
    return 0


RIPE_NEXT = (f"`ripe_insights` were confirmed by {lessons.RIPE} or more tasks: fold each into the skill it belongs "
             "to (or a new one), then `autoharness lesson fold <id> --skill <name>`. ")


def cmd_lesson(args, project, catalog):
    if args.action == "list":
        out({"status": "ok", "insights": lessons.insights()})
        return 0
    if args.action == "fold":
        if not (args.value or "").lstrip("#").isdigit() or not args.skill:
            raise HarnessError("usage: autoharness lesson fold <id> --skill <name>")
        out({"status": "ok", "insight": lessons.fold(int(args.value.lstrip("#")), args.skill)})
        return 0
    task_id = project.active_id()
    item, merged = lessons.add_insight(args.value or "", task_id, project.root.name)
    if task_id:
        project.task().log(f"insight #{item['id']} ({'confirmed' if merged else 'new'}): {item['text']}")
    nxt = (f"Merged into insight #{item['id']}, now confirmed by {item['confirmed']} task(s)." if merged else
           f"Recorded as insight #{item['id']}; later tasks whose text shares its words are shown it.")
    if item["confirmed"] >= lessons.RIPE and not item.get("skill"):
        nxt += " " + RIPE_NEXT
    out({"status": "ok", "insight": item, "merged": merged, "next": nxt})
    return 0


def cmd_ruling(args, project, catalog):
    if args.action == "list":
        out({"status": "ok", "rulings": rulings.load(project)})
        return 0
    task_id = project.active_id()
    if args.action == "remove":
        if not (args.value or "").lstrip("#").isdigit():
            raise HarnessError("ruling remove takes the ruling's number, e.g. `autoharness ruling remove 3`")
        item = rulings.remove(project, int(args.value.lstrip("#")))
        out({"status": "ok", "removed": item, "next": "Later critics no longer see this ruling."})
        return 0
    if not args.value or not args.reason:
        raise HarnessError("usage: autoharness ruling add \"<the critic's point>\" --reason \"<the user's words>\"")
    item = rulings.add(project, args.value, "user", task_id, args.reason)
    if task_id:
        project.task().log(f"the user rejected a critic's point (ruling #{item['id']}): {item['point']}: "
                           f"{item['reason']}")
    out({"status": "ok", "ruling": item,
         "next": "Every later critic in this project is told the user rejected this point. If it was what kept the "
                 "last review blocking, run `autoharness critic` again, or finish with `autoharness teardown "
                 "--accepted \"<the user's words>\"`."})
    return 0


def cmd_audit(args, project, catalog):
    from autoharness import audit
    out({"status": "ok", "events": audit.read(project.task(args.task_id))})
    return 0


def cmd_verify(args, project, catalog):
    from autoharness import audit, verification
    if args.action == "run":
        if len(args.kind) > 1 or (args.kind and args.command):
            raise HarnessError(f"verify run got the command in pieces ({args.kind!r}): the shell split it at its "
                               "quotes. Put it in a JSON file, {\"command\": \"<the command>\"}, and pass "
                               "`autoharness verify run --args-file <file>`")
        command = args.command or (args.kind[0] if args.kind else "")
        task = project.task()
        info = task.info()
        if info.get("mode", "full") == "full" and not info.get("brief"):
            raise HarnessError("record the brief first: a check verifies the work the brief describes")
        if args.passed or args.evidence:
            raise HarnessError("verify run decides by the exit code and keeps the output itself: drop --passed "
                               "and --evidence")
        items = [i["id"] for i in runs.brief_items(info.get("brief", {}))]
        purpose = (args.purpose or "").strip()
        purpose = purpose.upper() if purpose.upper() in items else purpose
        if purpose and items and purpose not in items + [runs.PRESERVE]:
            raise HarnessError(f"--for {purpose!r} is not a brief item: use one of {', '.join(items)} "
                               f"or {runs.PRESERVE}")
        item = runs.run(task, command, args.name or "", purpose, args.timeout)
        with write_lock(project):
            audit.append(task, "run", **{k: item[k] for k in ("n", "command", "for", "exit", "passed")})
        if item.get("trivial"):
            out({"status": "not_a_check", "run": item, "next": runs.NOT_A_CHECK.format(command=command) + "."})
            return 1
        changed = runs.changed_after_failing(task.info().get("runs", [])).get(item["n"])
        if changed and item["passed"]:
            item["check_changed"] = changed
        out({"status": "passed" if item["passed"] else "failed", "run": item,
             "next": (f"The check {changed}: teardown shows this; tell the user why the check changed. "
                      if changed and item["passed"] else "")
                     + ("Recorded as checked on the current code. An edit to the code makes this run stale: run it "
                        "again after your last change." if item["passed"] else
                        f"The check failed (exit {item['exit']}); the whole output is in {item['log']}. Fix the "
                        "cause and run it again.")})
        return 0 if item["passed"] else 1
    if args.command or len(args.kind) != 1 or args.kind[0] not in VERIFY_KINDS:
        raise HarnessError(f"verify {args.action} takes one kind: {', '.join(VERIFY_KINDS)}")
    args.kind = args.kind[0]
    guide = verification.guide(args.kind)
    if args.action == "guide":
        out(guide | {"next": f"Use it with `autoharness brief --verify-template {args.kind} ...`; after running "
                             f"the checks, save their output and record it: `autoharness verify record "
                             f"{args.kind} --passed yes|no --evidence <file>`."})
        return 0
    if not args.passed or not args.evidence:
        raise HarnessError("verify record requires --passed yes|no and --evidence <file>")
    task = project.task()
    proof = evidence.add(task, Path(args.evidence), "result", f"{args.kind} verification", {"proof": args.kind})
    checks = task.info().get("checks", [])
    check = {"kind": args.kind, "passed": args.passed == "yes", "evidence": proof["n"]}
    task._update_info(checks=checks + [check])
    audit.append(task, "verify", **check)
    out({"status": "ok", "check": check, "guide": guide})
    return 0


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
    s = sub.add_parser("followup", help="keep work found outside the task for later tasks: add, list, done")
    s.add_argument("action", choices=["add", "list", "done"])
    s.add_argument("value", nargs="?", help="add: what is left to do and where; done: its number")
    s.add_argument("--note", help="done: what settled it")
    s.add_argument("--all", action="store_true", help="list: include done items")
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
    sub.add_parser("hook", help="Claude Code / Antigravity CLI hook: reads the hook JSON on stdin")
    s = sub.add_parser("evidence", help="record a file you checked: a reference, or what a round produced")
    s.add_argument("action", choices=["add", "list", "diff"])
    s.add_argument("file", nargs="?")
    s.add_argument("--kind", choices=list(evidence.KINDS))
    s.add_argument("--base", default="HEAD", help="for `diff`: the revision to compare the working tree with")
    s.add_argument("--note", help="what it shows; reviewers see it as your label, not as a finding")
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
                       ("--inputs", "missing inputs and how you will get them (references, docs, data)"),
                       ("--constraints", "the user's constraints, verbatim"),
                       ("--verify", "how the result will be checked, and against what"),
                       ("--candidates", "each discovered skill/MCP worth considering: take or skip, and why")]:
        s.add_argument(flag, required=flag != "--verify", help=what)
    s.add_argument("--fidelity", help="the level to judge the result at, e.g. `stylised poster, not photographic`")
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
    s = sub.add_parser("install-hook", help="add the prompt hook to Claude Code settings, or with --agent agy to agy hooks.json (project, or --user)")
    s.add_argument("--user", action="store_true", help="~/.claude/settings.json (agy: ~/.gemini/config/hooks.json) instead of the project")
    s.add_argument("--settings", help="explicit settings file (agy: hooks.json)")
    s.add_argument("--remove", action="store_true", help="take the hook out again")
    s = sub.add_parser("agents", help="supported agents and where they keep MCP servers and skills")
    return p


COMMANDS = {"plan": cmd_plan, "init": cmd_init, "mode": cmd_mode, "eval": cmd_eval, "decision": cmd_decision, "followup": cmd_followup, "lesson": cmd_lesson, "ruling": cmd_ruling, "audit": cmd_audit, "verify": cmd_verify, "need": cmd_need, "search": cmd_search, "inspect": cmd_inspect, "vet": cmd_vet,
            "add": cmd_add, "remove": cmd_remove, "prune": cmd_prune, "status": cmd_status,
            "teardown": cmd_teardown, "config": cmd_config, "skill": cmd_skill, "gateway": cmd_gateway,
            "recipe": cmd_recipe, "install-skill": cmd_install_skill, "agents": cmd_agents,
            "hook": cmd_hook, "install-hook": cmd_install_hook, "waive": cmd_waive, "brief": cmd_brief,
            "evidence": cmd_evidence, "review": cmd_review, "critic": cmd_critic, "approach": cmd_approach,
            "arbiter": cmd_arbiter, "arbitrate": cmd_arbitrate, "check": cmd_check, "claim": cmd_claim}


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
        args.kind += extra   # a command the shell split at its quotes: verify run explains instead of argparse
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
