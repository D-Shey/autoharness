"""Meta, audit, evaluation and journaling commands: eval, decision, followup, journal, lesson, ruling, audit."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from autoharness import followups, journal, lessons, rulings, vague
from autoharness.env import HarnessError

RIPE_NEXT = (f"`ripe_insights` were confirmed by {lessons.RIPE} or more tasks: fold each into the skill it belongs "
             "to (or a new one), then `autoharness lesson fold <id> --skill <name>`. ")


def out(data) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


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
    task_id = project.active_id()
    if args.action == "harvest":
        marker = getattr(args, "marker", None) or "shortcut"
        res = followups.harvest(project, task_id, marker=marker)
        if task_id and res["added"] > 0:
            project.task().log(f"harvested {res['added']} shortcut(s) into followups")
        out({
            "status": "ok",
            "harvested": res["added"],
            "found": res["found"],
            "no_trigger": res["no_trigger"],
            "new_followups": res["new_followups"],
            "next": f"{res['found']} shortcut marker(s) found ({res['no_trigger']} without trigger), {res['added']} added to followups."
        })
        return 0
    if not args.value:
        raise HarnessError("usage: autoharness followup add \"<what, where>\" | done <id> [--note ...] | harvest [--marker ...]")
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


def cmd_journal(args, project, catalog):
    task = project.task()
    if (p := journal.path(project.root)) is None:
        out({"status": "ok", "journal": None,
             "next": f"This project keeps no journal ({journal.DEFAULT}, or `autoharness config set journal <path>`); "
                     "nothing to add."})
        return 0
    draft, form = journal.draft(task)
    out({"status": "ok", "journal": str(p), "order": form["order"], "draft_file": str(draft),
         "draft": draft.read_text("utf-8"),
         "next": "Finish the draft with what autoharness cannot know (values measured, where things live, backups, "
                 "what is left), in the journal's language, and add it to the journal "
                 + ("at the top, under its header." if form["order"] == "newest first" else "at the bottom.")})
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
    loose = vague.vague(args.value or "")
    out({"status": "ok", "insight": item, "merged": merged, "next": nxt}
        | ({"warnings": [f"the insight leans on {', '.join(loose)} and names nothing concrete (a file, a command, "
                         "a number, an identifier): say what exactly to do, or the next task cannot act on it"]}
           if loose else {}))
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




__all__ = [name for name in globals() if not name.startswith("__")]
