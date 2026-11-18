"""Evidence, review, critic, approach, arbiter, and check commands."""
from __future__ import annotations

import argparse
import hashlib
import re
import threading
from pathlib import Path

from autoharness import answers, evidence, reviewers, rulings, runs
from autoharness.commands.briefing import _skill_sources, _skills_not_installed
from autoharness.commands.common import EXIT_APPROVAL, out
from autoharness.env import HarnessError, Task
from autoharness.hooks import write_lock


def cmd_evidence(args, project, catalog):
    task = project.task()
    if args.action == "list":
        out({"evidence": task.info().get("evidence", []), "reviews": task.info().get("reviews", [])})
        return 0
    if args.kind == "facts" and not any(e["kind"] == "reference" for e in task.info().get("evidence", [])):
        raise HarnessError("facts are taken from a reference, not from memory: add the reference first (`autoharness "
                           "evidence add <file> --kind reference`), then measure, crop or extract from it and add "
                           "what you took as facts")
    if (args.action == "diff" or args.kind in ("result", "component")) and not args.closeup \
            and not any(e["kind"] in ("result", "component") for e in task.info().get("evidence", [])) \
            and (why := evidence.needs_facts(catalog, task)):
        raise HarnessError(why)
    if (args.action == "diff" or args.kind in ("result", "component")) and not args.closeup \
            and (why := _unresearched_review(task)):
        raise HarnessError(why)
    if (args.action == "diff" or args.kind in ("result", "component")) \
            and not any(e["kind"] in ("result", "component") for e in task.info().get("evidence", [])) \
            and (unready := task.planned_skill_problems()[0] + _skills_not_installed(project, task)):
        raise HarnessError("prepare before the first result or component: the skills the brief creates hold the know-how the "
                           "work is built on, from the sources you read. " + "; ".join(unready))
    if args.action == "diff":
        item = evidence.add_diff(task, project.root, args.base, args.note or "")
        out({"status": "ok", "evidence": item,
             "next": "Recorded as a new version of the whole result: the critic sees the latest diff, not earlier "
                     "ones. " + evidence.self_review_hint(task)})
        return 0
    if not args.file or not args.kind:
        raise HarnessError("usage: autoharness evidence add <file> --kind reference|facts|component|result [--note ...]")
    extra = {"for": args.for_item.upper()} if getattr(args, "for_item", None) else None
    item = evidence.add(task, Path(args.file), args.kind, args.note or "", extra=extra, closeup=args.closeup, catalog=catalog)
    first = args.kind == "result" and not any(e["kind"] == "result" and e["n"] != item["n"]
                                              for e in task.info().get("evidence", []))
    for_tag = f" for {item['for']}" if item.get("for") else ""
    nxt = ("Study it, then take from it with a tool what the result must match (measure, crop, extract, quote; for code, exact AST definitions), "
           "save those facts to a file and `autoharness evidence add <file> --kind facts`."
           if args.kind == "reference" else
           "Decompose before building: identify 2-4 primary isolated components or layers. "
           "Start small: build and verify each component in isolation (record with `autoharness evidence add <file> --kind component` "
           "and test with `autoharness verify run`). Do not attempt a monolithic full build in one pass."
           if args.kind == "facts" else
           f"Component #{item['n']}{for_tag} recorded: verify it in isolation (`autoharness verify run ... --for {item.get('for', 'C1')}`). "
           "Build and verify remaining components, then programmatically assemble the integrated result and record with `autoharness evidence add <file> --kind result`."
           if args.kind == "component" else
           "This is the base: run `autoharness critic` on it now, before any detail. " + evidence.CLAIMS
           + evidence.reliability(task)
           if first else
           evidence.self_review_hint(task))
    if item.get("closeup"):
        nxt = (f"Recorded as a close-up or part of result #{item['closeup']}: the critic sees it with that whole "
               "result. If it is a new version of the whole, add it again without --closeup"
               + (", at the full size" if item.get("media") == "image" else "") + ". " + nxt)
    elif args.kind == "result":
        nxt = ("Recorded as a new version of the whole result: the critic judges the latest version of each sort "
               "of result and the close-ups or parts added after it with `--closeup`"
               + (" (a crop smaller than the whole is taken as one)" if item.get("media") == "image" else "")
               + "; earlier versions are drafts it does not see. " + nxt)
    if args.kind == "reference":
        if item.get("stencil_id"):
            nxt = (f"Structural stencil #{item['stencil_id']} ({item.get('stencil_mode', 'blueprint')}) automatically "
                   f"scanned and registered in .autoharness/stencils/. Inspect with `autoharness stencil show {item['stencil_id']}` "
                   f"or verify target alignment with `autoharness stencil check {item['stencil_id']} <target>`. ") + nxt
        nxt = ("Check that it is the thing itself the task names (the original, as published or as it really "
               "is), not someone's version of it (fan art, a remake, a mockup, an example from a blog): a "
               "version is only a hint, find the original. A page that only shows it among other things (search "
               "results, a gallery, a collage, thumbnails, a snippet) is not it either: open the thing itself and "
               "keep it whole, at its own size. " + nxt)
    if args.kind == "reference" and item.get("media") == "image":
        nxt = ("Check that it shows everything the task names, whole and uncropped; if a part is cut off or "
               "missing, find a reference that shows it and add it as well. " + nxt)
    warnings = ([f"this file is byte for byte the same as evidence #{item['same_as']}: a result must be what "
                 "you produced, not the reference itself; the critic is told"] if item.get("same_as") else [])
    if item.get("hollow"):
        why_h = (item.get("hollow_details") or {}).get("reason", "artifact lacks minimal substance")
        warnings.append(f"result #{item['n']} is hollow/primitive ({why_h}): substantive detail and structure are required")
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
    if answer and answer.get("accept", {}).get("accept") == "no" and worst not in evidence.BLOCKING:
        # a list of small points can add up to work no one would hand over: the whole-work answer decides
        why = answer["accept"]["why"] or "the critic would not hand it over as finished work"
        diff += f"\nNot finished work as a whole (major, fixable): {why}"
        warnings.append(f"the critic's VERDICT was {worst}, but it would not accept the work as finished: "
                        "recorded as major")
        worst = "major"
    if not answer:
        if not args.diff or not (args.worst or args.fixable):
            raise HarnessError("record a review with --diff and --worst, or a critic's answer with "
                               "--by critic --answer-file <file>")
        diff, worst = args.diff, args.worst or ("major" if args.fixable == "yes" else "none")
        if args.by == "self" and worst == "none":
            last_res = evidence.latest(task, "result")
            if last_res and last_res.get("hollow"):
                why_h = (last_res.get("hollow_details") or {}).get("reason", "artifact is primitive/hollow")
                raise HarnessError(f"cannot record self-review with --worst none: result #{last_res['n']} is hollow ({why_h}). Substantive work is required before declaring the result finished.")
            hollow_runs = [r for r in task.info().get("runs", []) if r.get("hollow_check")]
            if hollow_runs:
                last_h = hollow_runs[-1]
                raise HarnessError(f"cannot record self-review with --worst none: run #{last_h['n']} for {last_h.get('for', '?')} is a hollow check ({last_h.get('hollow_reason')}). Write substantive tests before claiming zero defects.")
            if task.info().get("profile") == "reviewed" or task.info().get("mode") == "reviewed":
                raise HarnessError("in reviewed mode you cannot self-certify with --worst none: an independent critic must evaluate the result (`autoharness critic`)")
        if args.by == "critic":
            warnings.append(RETOLD)
        elif evidence.code_changed(task) and not re.search(r"\bR[1-8]\b", diff):
            warnings.append("the diff changes code and your review names no item of the reliability list (R1-R8, "
                            "in the hint of `evidence diff` and `evidence add`): go through it, or say which items do "
                            "not apply. "
                            + evidence.CLAIMS)
    ran = getattr(args, "critic_run", None)
    extra = {"answer_id": answer["id"]} | {k: answer[k] for k in ("coverage", "unrequested") if answer[k]} \
        | ({"previous_review": answer["previous"]} if answer["previous"] else {}) | ({"ran": ran} if ran else {}) \
        | ({"runs_judged": {str(n): v for n, v in answer["runs"].items()}} if answer.get("runs") else {}) \
        if answer else {}
    if quality:
        extra["quality"] = {"answer_id": quality["id"], "verdict": quality["verdict"],
                            "diff": answers.as_diff(quality)}
        if quality.get("lean"):
            extra["quality"]["lean"] = quality["lean"]
    pair = task.info().get("comparisons", {}).get(answer["id"]) if answer else None
    if pair and (cmp := evidence.compared(pair, answer["closer"])):
        extra["compared"] = cmp
    if args.by == "critic":
        extra["skill_sources"] = sorted(_skill_sources(task))  # what the skills cited when this review came in
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
        nxt = (("First research each point your skills do not already teach: how that part is done well, from "
                "pages you search and read, written into the skill with the page's address; and, with a reference, "
                "measure the parts the critic names from it and add that as facts (the next result is refused "
                "until a skill cites a new page and the new facts are in). " if item["by"] == "critic" else "")
               + "Fix the blocker and major differences first, produce the result again, `evidence add` it (plus "
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
            with write_lock(project):
                answers.cancel(task, res["answer_id"])
                if "quality" in res:
                    answers.cancel(task, res["quality"]["answer_id"])
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


def _unresearched_review(task) -> str | None:
    """Why the next result is refused yet: the latest critic found blocking problems in the latest result, and no
    skill of this task has been changed since. A fix of what the work lacks know-how for, made from memory, comes
    back as crude as the first try; the technique is looked up and written into a skill first."""
    info = task.info()
    reviews = [r for r in info.get("reviews", []) if r["by"] == "critic"]
    if not reviews or reviews[-1]["worst"] not in evidence.BLOCKING:
        return None
    last = reviews[-1]
    max_reviewed_res = max(last.get("round_results", [last.get("result", 0) or 0]))
    if any(e["kind"] == "result" and e["n"] > max_reviewed_res for e in info.get("evidence", [])):
        return None  # the round after this review already has its result
    if not task.skills_lock():
        return None  # task does not lock skills: research guard applies when skills are in use
    owed = []
    has_new_sources = bool(_skill_sources(task) - set(last.get("skill_sources", [])))
    skills_edited = any(
        (task.skills_dir / name / "SKILL.md").is_file() and
        hashlib.sha256((task.skills_dir / name / "SKILL.md").read_bytes()).hexdigest() != task.skills_lock().get(name, {}).get("sha256")
        for name in task.skills_lock()
    )
    if "skill_sources" in last and not (has_new_sources or skills_edited):
        owed.append(
            "find out how each point is done well before fixing it. For every point your skills do not already teach "
            "(how to build that part, the technique, the known pitfalls), search for it (the web, docs, worked "
            "examples, `autoharness search skill \"<query>\"`), read the best sources and write the technique into "
            "the skill it belongs to (" + ", ".join(task.skills_lock()) + ") or a new one (`autoharness skill new`), "
            "with the address of the page, standard, or book you took it from. The critic's words "
            "restated as rules are not research: a skill counts as changed when it cites a new source or adds concrete technique instructions "
            "since the review")
    refs = [e["n"] for e in info.get("evidence", []) if e["kind"] == "reference"]
    has_prior_facts = any(e["kind"] == "facts" for e in info.get("evidence", []))
    has_subsequent_facts = any(e["kind"] == "facts" and e["n"] > max_reviewed_res for e in info.get("evidence", []))
    if refs and has_prior_facts and not has_subsequent_facts:
        owed.append(
            "take the parts the critic names from the reference itself before you rebuild them: for each, measure "
            "it with a tool (crop it, lay a grid over it, read off the coordinates of its outline and key points, "
            "its proportions; for data or code, the exact values and output), save that to a file and `autoharness "
            "evidence add <file> --kind facts`, then build the part through those points, not by eye")
    if not owed:
        return None
    return "the critic found blocking problems. Before the next result: " + "; and ".join(owed) + "."


__all__ = [name for name in globals() if not name.startswith("__")]
