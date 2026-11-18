"""Brief recording, domain validation, constraint matching, and tool coverage checks."""
from __future__ import annotations

import re

from autoharness import agents, evidence, lessons, library, runs, text, vague
from autoharness.commands.common import cli_ref, out
from autoharness.commands.discovery import local_mcp_name
from autoharness.env import HarnessError
from autoharness.hooks import _agent_mcp_servers, _agent_skills

CREATE_RE = re.compile(r"\bcreate\s+`?([a-z0-9][a-z0-9-]{1,63})`?")


DOMAIN = re.compile(r"^(?P<area>[^:]+):\s*(?P<how>skill|mcp|create)\s+(?P<name>[\w@./-]+)[^(]*"
                    r"\(\s*searched:\s*(?P<query>[^)]+)\)\s*$", re.IGNORECASE)
MIN_DOMAINS = 2


def _check_domains(text: str, planned: list[str], candidates: str, searches: list[dict], builtin=(),
                   kept=(), have=(), have_mcp=()) -> str:
    """The brief's areas of know-how, each covered by a skill or a server found by a search of this task (or built
    into the agent, or a skill an earlier task kept in the project, or one the agent already has: `have`, or an MCP
    server already configured or built in: `have_mcp`), or by a skill this task creates. Raises when an area is
    covered by nothing, or its search was never run."""
    usage = ("--domains splits the task into the areas of know-how it takes, the way an engineer splits building "
             "a machine into its disciplines, and says how each is covered: `<area>: skill <name> (searched: "
             "<query>); <area>: mcp <name> (searched: <query>); <area>: create <skill-name> (searched: <query>)`. "
             f"At least {MIN_DOMAINS} areas. For each, first run `autoharness search skill|mcp \"<query>\"` (and "
             "`autoharness need`), then take what fits (`skill`/`mcp`, also named as `take` in --candidates) or, "
             "when nothing fits, create the skill from the sources you read (`create`, also in --gaps). Knowing it "
             "already is not a cover: what you know goes into the skill you create. The areas are what the work "
             "itself is made of and takes skill in (for a picture: say figures, anatomy, light; for a service: say "
             "its protocol, data model, concurrency), named as specifically as the task allows; how you check it "
             "goes in --verify.")
    entries = [e.strip() for e in (text or "").split(";") if e.strip()]
    if len(entries) < MIN_DOMAINS:
        raise HarnessError(usage)
    run = {s["query"].strip().lower() for s in searches}
    builtin_lower = {b.lower() for b in builtin}
    have_mcp_lower = {s.lower() for s in have_mcp} | {local_mcp_name(s).lower() for s in have_mcp}
    for entry in entries:
        m = DOMAIN.match(entry)
        if not m:
            raise HarnessError(f"cannot read the area `{entry}`. " + usage)
        how, name, query = m["how"].lower(), m["name"], m["query"].strip().lower()
        if query not in run:
            raise HarnessError(f"area `{m['area'].strip()}`: no search `{query}` was run in this task. Run "
                               f"`autoharness search skill \"{query}\"` (or `mcp`), read what it finds, then record "
                               "the brief again. " + ("Searches run: " + "; ".join(sorted(run)) if run else ""))
        if how == "create" and name not in planned:
            raise HarnessError(f"area `{m['area'].strip()}` creates `{name}`: name it in --gaps too "
                               f"(`create {name}: <capability>`)")
        found = next((s.get("results", []) for s in searches if s["query"].strip().lower() == query), [])
        real = (how == "skill" and (library.exists(name) or name in kept or name in have)) or (
            how == "mcp" and (name.lower() in builtin_lower or name.lower() in have_mcp_lower or local_mcp_name(name).lower() in have_mcp_lower)) or any(
            r.lower() == name.lower() or re.search(rf"[@/]{re.escape(name.lower())}$", r.lower()) for r in found)
        if how in ("skill", "mcp") and not real:
            raise HarnessError(f"area `{m['area'].strip()}`: `{name}` is not among what the search `{query}` found"
                               + (" nor a skill you have (your library, kept in this project, or the agent's project, user "
                                  "and plugin skills)" if how == "skill" else
                                  " nor an MCP server already configured or built into your agent")
                               + ": name a "
                               f"{how} that search returned (" + (", ".join(found) or "it found nothing") + "), "
                               "or create the skill (`create <skill-name>`)")
        if how in ("skill", "mcp") and not re.search(rf"{re.escape(name)}\s*:\s*take\b", candidates, re.IGNORECASE):
            raise HarnessError(f"area `{m['area'].strip()}` is covered by `{name}`: name it in --candidates as "
                               f"`{name}: take, <why>`")
    return "; ".join(entries)


SOURCE = re.compile(r"https?://[^\s/)\]>\"'`]+/[^\s)\]>\"'`]*[\w-][^\s)\]>\"'`]*")
SPEC_CITATION = re.compile(r"\b(?:RFC|ISO|IEEE|PEP|W3C|ECMA|ANSI|NIST)[- ]\d+\b|\b(?:man\s+\w+|book:\s*[^;\n\)]+|spec:\s*[^;\n\)]+)", re.IGNORECASE)


def _skill_sources(task) -> set[str]:
    """The page addresses or formal technical references the task's skills cite."""
    found: set[str] = set()
    for name in task.skills_lock():
        md = task.skills_dir / name / "SKILL.md"
        if md.is_file():
            content = md.read_text("utf-8", errors="replace")
            found |= {u.rstrip(".,;:") for u in SOURCE.findall(content)}
            found |= {c.strip(".,;:").lower() for c in SPEC_CITATION.findall(content)}
    return found


def _plain(text: str) -> str:
    """Text compared for quoting: case, quote marks, punctuation at the ends and runs of spaces do not count."""
    text = re.sub(r"[\"'`«»“”„‘’]", "", text.lower())
    return re.sub(r"\s+", " ", text).strip(" .,;:!?")


CONTRACTIONS = {
    "don't": "do not", "doesn't": "does not", "didn't": "did not",
    "won't": "will not", "can't": "cannot", "couldn't": "could not",
    "shouldn't": "should not", "isn't": "is not", "aren't": "are not",
    "wasn't": "was not", "weren't": "were not",
}


def _normalize_constraint_tokens(s: str) -> list[str]:
    s_low = s.lower()
    for k, v in CONTRACTIONS.items():
        s_low = s_low.replace(k, v)
    words = re.findall(r"\b[a-z0-9_-]+\b", s_low)
    stopwords = {"the", "a", "an", "and", "or", "to", "in", "on", "of", "for", "with", "at", "by", "from", "using", "use", "into", "this", "each"}
    return [w for w in words if len(w) >= 3 and w not in stopwords]


def _constraint_matches(piece: str, said: str) -> bool:
    if _plain(piece) in said:
        return True
    p_tokens = _normalize_constraint_tokens(piece)
    if not p_tokens:
        return True
    said_tokens = set(_normalize_constraint_tokens(said))
    matched = sum(1 for t in p_tokens if t in said_tokens)
    ratio = matched / len(p_tokens)
    return ratio >= 0.75


def _check_constraints(text: str, task) -> None:
    """The brief's constraints are the user's own words: each, between `;`, is quoted from the task or from what
    the user wrote since, or the whole is `none`. Candidates are vetted against them, so a constraint the agent
    made up ("no bash") rejects tools the user never ruled out."""
    if re.fullmatch(r"\W*none\b.*", text.strip(), re.IGNORECASE | re.DOTALL):
        return
    said = _plain(" \n ".join([str(task.info().get("task", "")), str(task.info().get("prompt", ""))]
                              + [m.get("text", "") for m in lessons.user_messages(task)]))
    for piece in (p for p in text.split(";") if _plain(p)):
        if not _constraint_matches(piece, said):
            raise HarnessError(
                f"--constraints are the user's own words, quoted: `{piece.strip()}` is not in the task or in what "
                "the user wrote. Quote each constraint as the user put it, separated by `;`, or `none` if they set "
                "none. Facts about the environment (the OS, the shell, what is installed) go in --inputs, and the "
                "tools and commands you choose are your decisions, not the user's constraints: candidates are "
                "vetted against these words.")
        if len(_plain(piece)) > max(len(_plain(str(task.info().get(k, "")))) for k in ("task", "prompt")) / 2:
            raise HarnessError(
                f"--constraints: `{piece.strip()[:80]}...` quotes most of the task, which is the goal, not a limit "
                "on it. Quote only the user's words that rule something out or require it (a version, a library to "
                "use or avoid, offline, a deadline), or `none` if they set none.")


def _skills_not_installed(project, task, templates: bool = True) -> list[str]:
    """The skills and MCP servers the brief's areas are covered by that are not installed (for this task, kept in
    the project, in the user's library, or already in the agent's config): an area is covered by what is in use,
    not by its name. `templates` also names generated skills still left unfilled."""
    lock = task.skills_lock()
    have = set(lock) | set(project.kept()["skills"]) | {p.parent.name for p in _agent_skills(project)}
    missing = []
    for name, entry in (lock.items() if templates else ()):
        skill_md = task.skills_dir / name / "SKILL.md"
        if (entry.get("source") == "generated" and name not in task.info().get("planned_skills", [])
                and skill_md.is_file() and library.FILL_MARKER in skill_md.read_text("utf-8")):
            missing.append(f"skill `{name}` is still an unfilled template: fill in every section from the sources "
                           "you read before it counts for an area")
    for entry in task.info().get("brief", {}).get("domains", "").split(";"):
        m = DOMAIN.match(entry.strip())
        if m and m["how"].lower() == "skill":
            name = m["name"].split("@")[-1]
            if name not in have and not library.exists(name) and name not in task.info().get("planned_skills", []):
                missing.append(f"area `{m['area'].strip()}` is covered by skill `{m['name']}`, not installed: "
                               f"`autoharness inspect skill {m['name']}`, then `autoharness add skill {m['name']}`; if it "
                               "cannot be installed or vetting rejects it, record the brief again with another "
                               "candidate from your searches for that area, or `create` a skill for it")
        elif m and m["how"].lower() == "mcp":
            raw_name = m["name"].strip()
            name = local_mcp_name(raw_name)
            installed = {s.lower() for s in _agent_mcp_servers(project)} | {s.lower() for s in task.mcp_lock()}
            stores = [project.agent.mcp_store(project.root), project.agent.user_mcp_store()]
            if not (name.lower() in installed or raw_name.lower() in installed
                    or any(s is not None and (s.has(name) or s.has(raw_name)) for s in stores)):
                missing.append(f"area `{m['area'].strip()}` is covered by MCP server `{m['name']}`, not installed: "
                               f"`autoharness inspect mcp {m['name']}`, then `autoharness add mcp {m['name']}`, and "
                               "use it (through harness_call when it was added through the gateway); if it cannot "
                               "be installed, record the brief again with another server from your searches")
    return missing


def _mcp_not_used(project, task) -> list[str]:
    """The MCP servers the brief covers an area with that were installed but never called since this task added
    them (or, for one it found installed, since the task started): installing a server to pass teardown is not
    using it. Calls are counted by the gateway and, for servers in the agent's own config, by the hook."""
    from autoharness.gateway import calls
    made, lock, info = calls(project.root), task.mcp_lock(), task.info()
    unused = []
    for entry in info.get("brief", {}).get("domains", "").split(";"):
        m = DOMAIN.match(entry.strip())
        if not (m and m["how"].lower() == "mcp"):
            continue
        raw_name = m["name"].strip()
        name = local_mcp_name(raw_name)
        installed = {s.lower() for s in _agent_mcp_servers(project)} | {s.lower() for s in lock}
        stores = [project.agent.mcp_store(project.root), project.agent.user_mcp_store()]
        if not (name.lower() in installed or raw_name.lower() in installed
                or any(s is not None and (s.has(name) or s.has(raw_name)) for s in stores)):
            continue   # not installed at all: _skills_not_installed says so
        since = lock.get(name, {}).get("added") or info.get("created", "")
        last_call = ""
        for s_key, s_data in made.items():
            if s_key.lower() in (name.lower(), raw_name.lower()) and isinstance(s_data, dict):
                last_call = max(last_call, s_data.get("last", ""))
        if last_call < since:
            unused.append(f"area `{m['area'].strip()}` is covered by MCP server `{m['name']}`, which was never called "
                          "in this task: do the work for that area with it (" +
                          ("harness_tools, then harness_call" if lock.get(name, {}).get("via") == "gateway" else
                           "its own tools") + ") and check the result again; if it cannot do the job, say why and "
                          "record the brief again with another server")
    return unused


EXPECTS_ITEM = re.compile(r"\S\s*:\s*\S.*\S\s+(?:-|—|because|since)\s+\S", re.IGNORECASE)
EXPECTS_HOW = (
    "--expects must name the choices the task leaves open and, for each, what the person who asked most likely "
    "expects: `<open choice>: <your pick> - <why they would expect it>; ...`, or `none: <why the task's words "
    "settle every choice>`. A requirement is what that person wants; where the words leave a choice open (a format, "
    "a value or a formula, what a column holds, where the output goes) and they cannot be asked, pick what they "
    "would take for granted on first look: the plain, simple form, where what they see is what is there, nothing "
    "they did not ask for, clean and pleasant to look at. A choice a tool's or skill's advice makes for you is still "
    "your choice: check it against what they would expect.")


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
    kept = project.kept()["skills"]
    for name in planned:
        library.validate_name(name)
        if name in kept:
            # `skill new` refuses a kept name and teardown would wait for it forever: say so before the work starts
            raise HarnessError(f"skill `{name}` is already kept in this project (an earlier task made it): do not "
                               f"create it again. Cover its areas with `<area>: skill {name} (searched: <query>)`, "
                               f"name it in --candidates as `{name}: take, <why>`, drop it from --gaps, and edit "
                               f"{project.agent.skills_dir}/{name}/SKILL.md in place with what this task teaches")
    _check_constraints(args.constraints, task)
    domains = cli_ref("_check_domains", _check_domains)(args.domains, planned, args.candidates,
                                                        task.info().get("searches", []),
                                                        [b.name for b in agents.builtin_mcp(project.agent.name)], kept,
                                                        [p.parent.name for p in _agent_skills(project)],
                                                        _agent_mcp_servers(project))
    live = [c.name for c, _ in catalog.classify(task.about()) if c.interactive]
    if live and not any((m := DOMAIN.match(e.strip())) and m["how"].lower() == "mcp" for e in domains.split(";")):
        raise HarnessError(
            f"the task needs {', '.join(live)}: you have to see and operate it live, step by step, and a skill is "
            "know-how, not a tool for that, while a script written blind does not show you what is on screen. Cover "
            "it with an MCP server: `autoharness search mcp \"<query>\"`, then `<area>: mcp <name> (searched: "
            "<query>)` in --domains and `<name>: take, <why>` in --candidates. Skills may cover other areas.")
    method = args.method.strip()
    if "searched:" not in method.lower() or not (SOURCE.search(method)
                                                 or re.search(r"nothing usable", method, re.IGNORECASE)):
        raise HarnessError(
            "--method must say how this kind of work is done well, from sources you found and read before starting, "
            "not from memory: `<technique or steps>: <url of the page you read, not a site's front page>; ...; "
            "searched: <queries>`. Search the web (and the docs, "
            "papers, repos, tutorials of the field) for how others solved it: the algorithm, the workflow, the "
            "pitfalls. Only if nothing usable turned up: `nothing usable; searched: <queries>`.")
    reqs = evidence.requirements(catalog, task)
    is_media = bool(reqs.get("media"))
    if is_media and not (args.fidelity or "").strip():
        raise HarnessError(
            "this task is judged by eye: name the level it is judged at with --fidelity (e.g. \"stylised vector "
            "poster: composition, palette, mood and poses read at a glance; not photographic likeness\"). "
            "Without it a critic judges against the reference itself, which a different medium never reaches, "
            "and every round stays major")

    components = (getattr(args, "components", None) or "").strip()
    if is_media and (not components or components.lower().startswith("none")):
        raise HarnessError(
            "this task produces visual assets and requires decomposition: "
            "--components '<comp1>: <what it does / how to verify alone>; <comp2>: ...' is mandatory. "
            "Decompose the task into 2-4 primary isolated components (e.g. background, character silhouette, lighting/effects). "
            "Monolithic one-pass builds are strictly forbidden.")

    techniques = (getattr(args, "techniques", None) or "").strip()
    if is_media and (not techniques or techniques.lower().startswith("none")):
        raise HarnessError(
            "this task produces visual assets: professional craftsmanship contracts are mandatory. "
            "--techniques '<technique: principle; ...>' is required (e.g. multi-layer-compositing: separate lighting and ambient; "
            "volumetric-gradients: no flat fills; no-raster-embeds: pure vector primitives and procedural filters). "
            "Zero tolerance for amateur shortcuts.")

    if is_media or reqs.get("reference"):
        from autoharness import stencil
        stencils_dir = stencil.get_stencils_dir(project.root)
        scanned_stencils = list(stencils_dir.glob("*.json")) if stencils_dir.is_dir() else []
        refs_dir = project.root / "refs"
        refs_found = [f for f in refs_dir.iterdir() if f.is_file() and not f.name.startswith(".")] if refs_dir.is_dir() else []
        if refs_found and not scanned_stencils:
            ref_names = ", ".join(f.name for f in refs_found[:3])
            raise HarnessError(
                f"reference files found in refs/ ({ref_names}), but no structural stencils have been scanned. "
                "Run `autoharness stencil scan refs/<file>` to extract geometric landmarks, contours, and "
                "structural invariants into .autoharness/stencils/ before recording the brief."
            )
    expects = args.expects.strip()
    if not EXPECTS_ITEM.search(expects) and not re.match(r"none\s*:\s*\S", expects, re.IGNORECASE):
        raise HarnessError(EXPECTS_HOW)
    task.record_brief({"goal": args.goal, "domains": domains, "method": method, "inputs": args.inputs,
                       "constraints": args.constraints, "expects": expects,
                       "verify": verify, "candidates": args.candidates, "gaps": args.gaps}
                      | ({"verify_template": args.verify_template} if args.verify_template else {})
                      | ({"review": args.review} if args.review else {})
                      | ({"fidelity": args.fidelity} if args.fidelity else {})
                      | ({"components": args.components} if getattr(args, "components", None) else {})
                      | ({"techniques": args.techniques.strip()} if getattr(args, "techniques", None) and args.techniques.strip() else {})
                      | ({runs.PRESERVE: args.preserve} if args.preserve else {}))
    task._update_info(planned_skills=planned)
    warnings = [] if task.discovery_ran() else [
        "no search ran for this task; run `autoharness search skill|mcp \"<query>\"` before deciding nothing fits"]
    warnings += [w for e in domains.split(";") if (m := DOMAIN.match(e.strip())) and m["how"].lower() == "create"
                 and (w := vague.hollow_create(m["area"], m["name"], m["query"]))]
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
    decomp_hint = (" Decompose first: build and verify each planned component (C items) in isolation "
                   "(`autoharness evidence add <file> --kind component`, `autoharness verify run ... --for C1`) "
                   "before full assembly. Do not attempt a monolithic build in one pass." if getattr(args, "components", None) else "")
    items = runs.brief_items(task.info()["brief"])
    out({"status": "ok", "task_id": task.id, "planned_skills": planned, "warnings": warnings,
         "brief_items": [f"{i['id']}: {i['text']}" for i in items],
         "next": "Install what you decided to take and get the missing inputs first. Then work by the method you "
                 "found, step by step: the critic checks the result against it." + decomp_hint + owed + create
                 + " Then do the work and verify it the way the brief says: a check that is a command (tests, a "
                   "build, a script) runs through `autoharness verify run \"<command>\" --for <brief item, e.g. B1>`, "
                   "so it counts as checked, not as reported. Teardown refuses while a planned "
                   "skill is missing or unfilled."})
    return 0


__all__ = [name for name in globals() if not name.startswith("__")]
