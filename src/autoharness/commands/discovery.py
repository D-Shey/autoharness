"""Discovery, catalog search, and recipe commands: plan, need, search, inspect, recipe."""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from autoharness import agents, config, egress, history, lessons, library, recipes, registry, text
from autoharness.catalog import Catalog
from autoharness.commands.common import cli_ref, out
from autoharness.commands.subsystems import unapproved_servers
from autoharness.env import HarnessError, parse_skill_spec
from autoharness.trust import curated_report, score_marketplace, score_registry_entry, score_skill


# --- resolution helpers -----------------------------------------------------

def resolve_mcp(catalog: Catalog, name: str) -> tuple[dict, str]:
    """Curated short names win; then `claude-plugins-official/<plugin>` from Anthropic's marketplace;
    otherwise the name must be an exact MCP registry server name."""
    if name in catalog.mcp_servers:
        return curated_report(name, catalog.mcp_servers[name]).to_dict(), "catalog"
    if name.startswith(registry.MARKETPLACE_PREFIX):
        found = registry.get_marketplace(name)
        if not found:
            raise HarnessError(f"{name!r} is not in Anthropic's plugin marketplace: `autoharness search mcp <query>`")
        return score_marketplace(name, found["plugin"], found["servers"]).to_dict(), "claude-plugins-official"
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


def suggestions(catalog: Catalog, caps, builtin=()) -> list[dict]:
    """Curated entries for the matched capabilities; a server built into the agent comes first."""
    return [{"capability": c.name, "description": c.description, "matched": m,
             "mcp": [b.name for b in builtin if c.name in b.capabilities] + c.mcp, "skills": c.skills}
            for c, m in caps]


def builtin_results(catalog: Catalog, builtin, query: str) -> list[dict]:
    """The agent's built-in servers a search for `query` finds: by name, or by a capability the query names."""
    caps = {c.name for c, _ in catalog.classify(query)}
    words = query.lower().split()
    return [{"name": b.name, "score": 100, "level": "builtin", "reasons": ["built into the agent: nothing to install"],
             "requires_approval": False, "description": b.description} for b in builtin
            if caps & set(b.capabilities) or any(w in b.name for w in words)]


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


def search_mcp_sources(catalog: Catalog, query: str, limit: int, timeout: float = 8.0) -> tuple[list[dict], list[str]]:
    """MCP servers for a query, as trust reports with a description: Anthropic's curated plugin marketplace
    first (one fast file), then the official registry, whose search can take longer than a budget allows.
    Both sources, and each plugin's server file, are fetched at once; one source failing leaves the other's
    results: (results, the errors)."""
    fetch = lambda url: registry.quick_get_json(url, timeout)  # noqa: E731

    def from_marketplace() -> list[dict]:
        plugins = registry.search_marketplace(query, limit=limit * 2, fetch=fetch)
        with ThreadPoolExecutor(max_workers=8) as pool:
            servers = list(pool.map(lambda p: registry.marketplace_servers(p, fetch), plugins))
        return [score_marketplace(registry.MARKETPLACE_PREFIX + p["name"], p, found).to_dict()
                | {"description": p.get("description", "")} for p, found in zip(plugins, servers) if found][:limit]

    def from_registry() -> list[dict]:
        found = [score_registry_entry(e, catalog.trusted_mcp_publishers, catalog.trusted_mcp_namespaces).to_dict()
                 | {"description": e["server"].get("description", "")}
                 for e in registry.search_mcp(query, limit=limit, fetch=fetch)]
        return sorted(found, key=lambda r: r["score"], reverse=True)

    results, errors = [], []
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [("claude-plugins-official", pool.submit(from_marketplace)),
                ("registry.modelcontextprotocol.io", pool.submit(from_registry))]
        for source, job in jobs:
            try:
                results += job.result()
            except Exception as e:  # noqa: BLE001 - one failed source must not hide the other
                errors.append(f"{source}: {e}")
    return results, errors


def _search(catalog: Catalog, kind: str, query: str) -> dict:
    fetch = lambda url: registry.quick_get_json(url, 8)  # noqa: E731
    try:
        if kind == "skill":
            raw_skills = registry.search_skills(query, limit=DISCOVERY_RESULTS, fetch=fetch)
            found = [score_skill(r["spec"], r["installs"], catalog.trusted_skill_owners).to_dict()
                     | {"installs": r["installs"], "_rank": idx}
                     for idx, r in enumerate(raw_skills)]
            found.sort(key=lambda r: (r["score"] >= 80, -r["_rank"]), reverse=True)
            results = [{"name": r["name"], "score": r["score"], "level": r["level"], "installs": r["installs"]} for r in found]
            return {"kind": kind, "query": query, "results": results}
        found, errors = cli_ref("search_mcp_sources", search_mcp_sources)(catalog, query, DISCOVERY_RESULTS)
        if errors and not found:
            return {"kind": kind, "query": query, "error": "; ".join(errors)}
        results = [{"name": r["name"], "score": r["score"], "level": r["level"], "description": r["description"][:140]}
                   for r in found]
        return {"kind": kind, "query": query, "results": results} | ({"partial": errors} if errors else {})
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
    budget = cli_ref("DISCOVERY_BUDGET", DISCOVERY_BUDGET)
    deadline = time.monotonic() + budget
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return [r or {"kind": k, "query": q, "error": f"timed out ({budget:.0f}s budget)"}
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
    out({"task": args.task, "suggested": suggestions(catalog, catalog.classify(args.task),
                                                     agents.builtin_mcp(project.agent.name)),
         "recipes": [{"score": s, "name": r["name"], "capabilities": r["capabilities"]} for s, r in recipes.match(args.task)],
         "all_capabilities": {n: c.description for n, c in catalog.capabilities.items()}})
    return 0


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
    builtin = agents.builtin_mcp(project.agent.name)
    if cap:
        mcp = [b.name for b in builtin if cap.name in b.capabilities] + cap.mcp
        out({"status": "ok", "capability": cap.name, "curated": {"mcp": mcp, "skills": cap.skills}, "local": local}
            | ({"next": "Prefer a `local` skill: `autoharness add skill local:<name>`."} if local else {}))
    else:
        # the name may just be phrased differently from a curated capability
        related = suggestions(catalog, catalog.classify(f"{args.capability.replace('-', ' ')} {args.reason or ''}"),
                              builtin)
        out({"status": "ok", "capability": args.capability, "curated": None, "local": local, "related": related,
             "next": (("Prefer a `local` skill: `autoharness add skill local:<name>`. " if local else "")
                      + "No curated entry under this name. Check `related` first; if none fits, try "
                      f"`autoharness search mcp \"{args.capability}\"` and `autoharness search skill \"{args.capability}\"`.")})
    return 0


def cmd_search(args, project, catalog):
    if args.kind == "mcp":
        found, errors = cli_ref("search_mcp_sources", search_mcp_sources)(catalog, args.query, args.limit, timeout=20)
        if errors and not found:
            raise HarnessError("; ".join(errors))
        curated = [curated_report(n, e).to_dict() for n, e in catalog.mcp_servers.items() if args.query.lower() in n]
        results = builtin_results(catalog, agents.builtin_mcp(project.agent.name), args.query) + curated + found
    else:
        errors = []
        raw_skills = registry.search_skills(args.query, limit=args.limit)
        results = [score_skill(r["spec"], r["installs"], catalog.trusted_skill_owners).to_dict()
                   | {"installs": r["installs"], "_rank": idx}
                   for idx, r in enumerate(raw_skills)]
        results.sort(key=lambda r: (r["score"] >= 80, -r["_rank"]), reverse=True)
    if project.active_id():
        project.task().note_search(args.kind, args.query, [r["name"] for r in results[:DISCOVERY_RESULTS]])
    out({"query": args.query, "kind": args.kind, "results": results} | ({"partial": errors} if errors else {}))
    return 0


def cmd_inspect(args, project, catalog):
    if args.kind == "mcp":
        report, source = cli_ref("resolve_mcp", resolve_mcp)(catalog, args.name)
        reach = (egress.check(report["config"], config.allowed_hosts(project.root)) if report["config"]
                 else {"host": None, "enforced": False, "ok": False, "reason": "no installable configuration"})
        out(report | {"source": source, "egress": reach})
    else:
        out(cli_ref("resolve_skill", resolve_skill)(catalog, args.name))
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


__all__ = [name for name in globals() if not name.startswith("__")]
