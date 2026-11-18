"""Task lifecycle and verification commands: init, mode, teardown, verify."""
from __future__ import annotations

from pathlib import Path

from autoharness import agents, config, evidence, followups, journal, lessons, library, recipes, runs
from autoharness.commands.briefing import _mcp_not_used, _skills_not_installed
from autoharness.commands.common import cli_ref, out
from autoharness.commands.discovery import (REJECTED_NEXT, discover, local_skills, recipe_queries,
                                            rejected_before, suggestions, unapproved_warning)
from autoharness.commands.meta import RIPE_NEXT
from autoharness.commands.skills import stale_skills
from autoharness.env import HarnessError, parse_skill_spec
from autoharness.hooks import _earlier_level, _routine, check_owner, session_state, write_lock

VERIFY_KINDS = ("browser", "api", "api-client", "logs", "database")


def _format_light_raised_why(classified, task_text: str, prompt_text: str, catalog) -> str:
    """Format why light mode was raised to full, attributing trigger words to task text or user message."""
    in_task = {c.name: set(words) for c, words in catalog.classify(task_text)}
    t_lower = task_text.lower()
    p_lower = prompt_text.lower() if prompt_text else ""
    parts = []
    for c, words in classified:
        task_words = []
        user_words = []
        task_known = in_task.get(c.name, set())
        for w in words:
            w_clean = w.strip().lower()
            if w in task_known or (w_clean and w_clean in t_lower):
                task_words.append(w)
            elif p_lower and w_clean in p_lower:
                user_words.append(w)
            else:
                task_words.append(w)
        groups = []
        if task_words:
            w_str = ", ".join(f'"{w}"' for w in task_words)
            groups.append(f"{w_str} in the task text")
        if user_words:
            w_str = ", ".join(f'"{w}"' for w in user_words)
            groups.append(f"{w_str} in the user's message")
        if groups:
            parts.append(f"{c.name} ({'; '.join(groups)})")
    return ", ".join(parts)


LEVELS = ("light", "full", "reviewed")


def cmd_init(args, project, catalog):
    if args.profile and args.mode:
        raise HarnessError("use either --profile or --mode, not both")
    # the user's own words too: an agent may pass init a summary that drops what the task needs ("browser")
    sid = agents.session_id(agent=project.agent_name)
    prompt = session_state(sid).get("prompt", "") if sid else ""
    about = "\n".join(t for t in (args.task, prompt) if t)
    classified = catalog.classify(about)
    # --profile is the older spelling of the same levels
    selected = ({"quick": "light", "standard": "full", "reviewed": "reviewed"}[args.profile] if args.profile
                else args.mode or "full")
    if selected == "auto":
        selected = "full" if classified else "light"
    requested = selected
    floor, floor_source = config.resolve(project.root, "min_mode")
    raised = floor != "any" and LEVELS.index(selected) < LEVELS.index(floor)
    if raised:
        selected = floor   # the user's setting, not the agent's judgement, sets the lowest level
    # the same task started again: it does not restart below the level it was started at, or a brief that is
    # hard to write is escaped by ending the task and starting it light
    before = _earlier_level(project, args.task)
    restarted = before and LEVELS.index(selected) < LEVELS.index(before)
    if restarted:
        selected = before
    # light is for chores: a task that needs any capability of the catalog (an API, a browser, tests, debugging,
    # a reference) is not one, whatever the agent chose; skipping the harness altogether is the user's call (waive)
    # A routine is still a chore: a skill the agent has, named in the task, or the same task run light before
    needs = [c.name for c, _ in classified]
    routine = selected == "light" and bool(needs) and _routine(project, args.task)
    lightened = selected == "light" and bool(needs) and not routine
    if lightened:
        selected = "full"
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
    task._update_info(requested=requested)
    journal.snapshot(task)
    if mode == "light" and visual:
        task._update_info(light_reason=light_reason)
        task.log(f"light mode although the task mentions {', '.join(visual)}: {light_reason}")
    if owner := sid:
        task._update_info(owner=owner)
        if prompt:   # the user's own words: the agent may pass init a summary, and the brief quotes the user
            task._update_info(prompt=prompt)
    if selected == "reviewed":
        task._update_info(profile="reviewed")
    result = {"status": "ok", "task_id": task.id, "dir": str(task.dir), "mode": selected}
    if raised:
        result["mode_raised"] = f"to {floor} by the min_mode setting ({floor_source})"
        task.log(f"mode raised to {floor} by the min_mode setting ({floor_source})")
    if lightened:
        why = _format_light_raised_why(classified, args.task, prompt, catalog)
        result["mode_raised"] = (f"to full: the task needs {why}, and light mode is for chores that need none (a "
                                 "typo, a commit). Prepare as full mode asks")
        task.log(f"light raised to full: the task needs {why}")
    if routine:
        task.log(f"light kept although the task needs {', '.join(needs)}: {routine}")
    if restarted:
        result["mode_raised"] = (f"to {before}: this task was started at that level before, and starting it again "
                                 "does not lower it. Do what the earlier start asked for (its brief) instead")
        task.log(f"mode raised to {before}: the same task was started at that level before")
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
                   "suggested": suggestions(catalog, classified, agents.builtin_mcp(agent.name)),
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
        result["discovered"] = discovered = cli_ref("discover", discover)(catalog, about, recipe_queries(matches))
        task.record_discovery(discovered)
        if rejected := rejected_before(project, task.id, result):
            result["rejected_before"] = rejected
        result["next"] = (
        "Read `discovered`: these skills and MCP servers were found for this task. Search more with "
        "`autoharness search mcp|skill \"<query>\"` where it falls short. Then record "
        "`autoharness brief --goal ... --inputs ... --expects ... --constraints ... --verify ... --candidates ...`, "
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
        task.record_discovery(cli_ref("discover", discover)(catalog, task.info().get("task", ""),
                                                               recipe_queries(matches)))
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
    if info.get("mode", "full") != "light" and info.get("brief"):
        # what the brief said covers each area is in use, whether or not a result was ever recorded
        blocking += [p for p in _skills_not_installed(project, task) if p not in blocking]
        blocking += _mcp_not_used(project, task)
    blocking += runs.problems(task)
    draft = None
    if missing := journal.problem(task):
        # a chore is not held up for an entry; anything that took preparation is
        (warnings if info.get("mode") == "light" else blocking).append(missing[0])
        draft = missing[1]
    if args.accepted:
        task._update_info(accepted_by_user=args.accepted)
        task.log(f"user accepted the result as it is: {args.accepted}")
    if blocking and not (args.force or args.accepted):
        out({"status": "incomplete", "task_id": task.id, "problems": blocking}
            | ({"journal_draft": draft} if draft else {}) | {
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
    harvested = followups.harvest(project, task.id)
    if harvested["added"] > 0:
        task.log(f"teardown auto-harvested {harvested['added']} shortcut(s) into followups")
    found = followups.from_task(project, task.id)
    if found:
        nxt += (" Tell the user the follow-ups this task left for later: "
                + "; ".join(f"#{i['id']} {i['text']}" for i in found) + ".")
    last_quality_rev = next((r for r in reversed(info.get("reviews", []))
                             if "quality" in r or r.get("by") == "quality"), None)
    lean_savings = ((last_quality_rev.get("quality", {}) or {}).get("lean")
                    or last_quality_rev.get("lean")) if last_quality_rev else None
    if lean_savings:
        checked["lean"] = lean_savings
        nxt += f" Lean optimization: {lean_savings}."
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


def cmd_verify(args, project, catalog):
    from autoharness import audit, verification
    if args.action == "run":
        if len(args.kind) > 1 or (args.kind and args.command):
            raise HarnessError(f"verify run got the command in pieces ({args.kind!r}): the shell split it at its "
                               "quotes. Put it in a JSON file, {{\"command\": \"<the command>\"}}, and pass "
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
        item = runs.run(task, command, args.name or "", purpose, args.timeout,
                        redact=not getattr(args, "no_redact", False))
        with write_lock(project):
            audit.append(task, "run", **{k: item[k] for k in ("n", "command", "for", "exit", "passed")})
        if item.get("trivial"):
            out({"status": "not_a_check", "run": item, "next": runs.NOT_A_CHECK.format(command=command) + "."})
            return 1
        changed = runs.changed_after_failing(task.info().get("runs", [])).get(item["n"])
        if changed and item["passed"]:
            item["check_changed"] = changed
        comp_hint = ""
        if item["passed"] and purpose and purpose.startswith("C"):
            comp_hint = f" Component {purpose} verified (exit 0): record with `autoharness evidence add <file> --kind component --for {purpose}`."
        warnings = []
        if item.get("hollow_check"):
            warnings.append(f"hollow check: {item.get('hollow_reason')}")
        hollow_hint = (f" Warning: run #{item['n']} is a hollow check ({item.get('hollow_reason')}); rigorous assertions are required before self-review."
                       if item.get("hollow_check") else "")
        res_data = {"status": "passed" if item["passed"] else "failed", "run": item,
                    "next": (f"The check {changed}: teardown shows this; tell the user why the check changed. "
                             if changed and item["passed"] else "")
                            + comp_hint + hollow_hint
                            + (" Recorded as checked on the current code. An edit to the code makes this run stale: run it "
                               "again after your last change." if item["passed"] else
                               f"The check failed (exit {item['exit']}); the whole output is in {item['log']}. Fix the "
                               "cause and run it again.")}
        if warnings:
            res_data["warnings"] = warnings
        out(res_data)
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


__all__ = [name for name in globals() if not name.startswith("__")]
