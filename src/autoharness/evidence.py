"""Evidence that a result was checked: references, results, review rounds, an independent critic.

Works for any kind of task. A *reference* is what the result must match (a film still, a
design, a spec, a sample API response, the old report); a *result* is what one round
produced (a screenshot, a test log, a generated file, a benchmark). autoharness has no eyes
and no LLM: it keeps copies of what the agent says it looked at, what each review round
found, and whether an independent critic, not the author, saw the latest result. Teardown
uses that to refuse a task whose result was never compared with anything.

What a task must show comes from its capabilities' `requires` in the catalog, e.g.
`{"reference": 1, "media": "image"}`, `{"result": 2, "critic": 1}`, plus `brief --review
critic` for any task. `media: image` limits the counted evidence to image files.

    .autoharness/tasks/<id>/evidence/<n>-<kind>-<name>   copies of the files
    task.json: "evidence": [...], "reviews": [...]
"""

from __future__ import annotations

import hashlib
import re
import shutil
import time
from pathlib import Path

from autoharness import text
from autoharness.env import HarnessError, Task

KINDS = ("reference", "result")
# worst difference still fixable in a review; the task may stop once it is at most "minor"
SEVERITIES = ("none", "minor", "major", "blocker")
BLOCKING = ("major", "blocker")
LOOP_ROUNDS = 3  # critic reviews in a row that still list the same blocking problem
MAX_APPROACHES = 3  # different approaches to a looping part before an arbiter is called
MAX_ARBITRATIONS = 2  # arbiter fixes that may fail before the final arbiter is called
ARBITER_ROUNDS = 8  # latest critic descriptions of the part shown to the arbiter
SAME_APPROACH = 0.75  # word overlap at which a "new" approach is the old one reworded
MEDIA = {"image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}}
GENERIC_FOCUS = ("does it meet the quality bar and match the reference, what is missing or wrong, "
                 "which of the user's constraints it breaks")


def media_of(path: str) -> str | None:
    suffix = Path(path).suffix.lower()
    return next((m for m, suffixes in MEDIA.items() if suffix in suffixes), None)


def add(task: Task, path: Path, kind: str, note: str = "") -> dict:
    if kind not in KINDS:
        raise HarnessError(f"kind must be one of {', '.join(KINDS)}")
    if not path.is_file():
        raise HarnessError(f"no such file: {path}")
    items = task.info().get("evidence", [])
    dest_dir = task.dir / "evidence"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / f"{len(items) + 1:02d}-{kind}-{path.name}"
    shutil.copy2(path, dest)
    item = {"n": len(items) + 1, "kind": kind, "file": str(dest), "source": str(path), "note": note,
            "media": media_of(path.name), "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
            "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
    task._update_info(evidence=items + [item])
    task.log(f"evidence #{item['n']}: {kind} `{path.name}`" + (f" ({note})" if note else ""))
    return item


def latest(task: Task, kind: str) -> dict | None:
    items = [e for e in task.info().get("evidence", []) if e["kind"] == kind]
    return items[-1] if items else None


def top_issue(diff: str) -> str:
    """The first (most important) difference in a review's list."""
    first = re.split(r"\s2[.)]?\s|;|\n", diff.strip(), maxsplit=1)[0]
    return re.sub(r"^\s*1[.)]?\s*", "", first).strip()


def same_issue(a: str, b: str) -> bool:
    sa, sb = text.stems(a), text.stems(b)
    return bool(sa and sb) and len(sa & sb) / min(len(sa), len(sb)) >= 0.5


def headline(item: str) -> str:
    """The name of a listed problem, without the details a critic adds after a colon, comma or bracket."""
    return re.split(r"[:,(;]|\s[-–—]\s", item, maxsplit=1)[0].strip()


def same_problem(a: str, b: str) -> bool:
    """Two listed items name the same problem: critics describe it in new words each round, so the
    details differ while the headline stays."""
    return same_issue(headline(a), headline(b)) or same_issue(a, b)


def issues(diff: str) -> list[str]:
    """The differences of a review that still block: every listed item except those tagged minor only."""
    items = re.split(r";|\n|\s(?=\d+[.)]\s)", diff.strip())
    out = []
    for raw in items:
        item = re.sub(r"^\s*\d+[.)]?\s*", "", raw).strip()
        tags = set(re.findall(r"\b(minor|major|blocker)\b", item.lower()))
        if item and not (tags and tags <= {"minor"}):
            out.append(item)
    return out


def recurring(reviews: list[dict], diff: str, skip=lambda issue: False) -> tuple[int, str]:
    """How many critic rounds in a row, this one included, still list the same blocking problem.

    A critic may reorder its list from round to round, so a problem counts wherever it sits in
    each list, not only at the top. The count stops at the round where the user was last asked.
    `skip` leaves out problems that no longer block (ruled out of reach by an arbiter).
    """
    critics = [r for r in reviews if r["by"] == "critic"]
    best, which = 1, top_issue(diff)
    for issue in issues(diff):
        if skip(issue):
            continue
        count = 1
        for prev in reversed(critics):
            if prev.get("loop_asked") or not any(same_problem(issue, p) for p in issues(prev["diff"])):
                break
            count += 1
        if count > best:
            best, which = count, issue
    return best, which


def _last_asked(reviews: list[dict]) -> int:
    """Round of the last review at which the user was asked about a loop (0 if never)."""
    return max((r["round"] for r in reviews if r.get("loop_asked")), default=0)


def tried_approaches(task: Task, part: str) -> list[dict]:
    """Approaches recorded for `part` since the user was last asked about a loop."""
    info = task.info()
    since = _last_asked(info.get("reviews", []))
    return [a for a in info.get("approaches", []) if a["after_round"] >= since and same_problem(a["part"], part)]


def pending_approach(task: Task) -> str | None:
    """The part a loop asked a new approach for, if none has been recorded since that review."""
    info = task.info()
    critics = [r for r in info.get("reviews", []) if r["by"] == "critic"]
    if not critics or not critics[-1].get("needs_approach"):
        return None
    last = critics[-1]
    if any(a["after_round"] >= last["round"] for a in info.get("approaches", [])):
        return None
    return last["needs_approach"]


def approach(task: Task, part: str, was: str, now: str) -> dict:
    """Record a change of approach for a part that keeps failing review."""
    if not (part.strip() and was.strip() and now.strip()):
        raise HarnessError("part, --was and --now are all needed")
    for prev in tried_approaches(task, part):
        for old in (prev["now"], prev["was"]):
            sa, sb = text.stems(old), text.stems(now)
            if sa and sb and len(sa & sb) / min(len(sa), len(sb)) >= SAME_APPROACH:
                raise HarnessError(f"that is the approach already tried (\"{old}\"): pick a different technique, "
                                   "not a variation of the same edit")
    info = task.info()
    items = info.get("approaches", [])
    item = {"n": len(items) + 1, "after_round": len(info.get("reviews", [])), "part": part, "was": was,
            "now": now, "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    task._update_info(approaches=items + [item])
    task.log(f"approach #{item['n']} for `{part}`: was {was}; now {now}")
    return item


def limits(task: Task) -> list[dict]:
    """Parts an independent arbiter judged out of reach at the target fidelity: they no longer block."""
    return task.info().get("limits", [])


def is_limited(task: Task, issue: str) -> bool:
    return any(same_problem(issue, lim["part"]) for lim in limits(task))


def arbitrations(task: Task, part: str) -> list[dict]:
    """Arbiter verdicts on `part` since the user was last asked about a loop."""
    info = task.info()
    since = _last_asked(info.get("reviews", []))
    return [a for a in info.get("arbitrations", []) if a["after_round"] >= since and same_problem(a["part"], part)]


def pending_arbiter(task: Task) -> str | None:
    """The part a loop sent to an arbiter, if no verdict has been recorded since that review."""
    info = task.info()
    critics = [r for r in info.get("reviews", []) if r["by"] == "critic"]
    if not critics or not critics[-1].get("needs_arbiter"):
        return None
    last = critics[-1]
    if any(a["after_round"] >= last["round"] for a in info.get("arbitrations", [])):
        return None
    return last["needs_arbiter"]


def review(task: Task, diff: str, worst: str, by: str, settled: list[str] | None = None) -> dict:
    if worst not in SEVERITIES:
        raise HarnessError(f"severity must be one of {', '.join(SEVERITIES)}")
    result = latest(task, "result")
    if not result:
        raise HarnessError("no result yet: `autoharness evidence add <file> --kind result` first")
    reviews = task.info().get("reviews", [])
    item = {"round": len(reviews) + 1, "by": by, "result": result["n"], "diff": diff, "worst": worst,
            "fixable": worst in BLOCKING, "top": top_issue(diff), "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if settled:
        if by != "critic":
            raise HarnessError("only a critic settles a disputed point: record --settled with --by critic")
        item["settled"] = [p for p in settled if p.strip()]
    if by == "critic" and worst in BLOCKING and limits(task):
        # an arbiter settled some parts as out of reach: if nothing else blocks, the review does not
        open_items = [i for i in issues(diff) if not is_limited(task, i)]
        if not open_items:
            item.update(worst="minor", fixable=False, worst_given=worst, limited=True)
    if by == "critic":
        item["repeats"], item["recurring"] = (recurring(reviews, diff, lambda i: is_limited(task, i))
                                              if item["worst"] in BLOCKING else (1, item["top"]))
        if item["repeats"] >= LOOP_ROUNDS:
            part = item["recurring"]
            item["tried"] = len(tried_approaches(task, part))
            item["arbitrated"] = len(arbitrations(task, part))
            # the harness settles a loop by itself, without the user: a different approach, then
            # independent arbiters who name a fix with a measurable check or rule the part out of
            # reach, then a final arbiter whose failed fix rules the part out of reach as agreed
            if item["tried"] < MAX_APPROACHES:
                item["needs_approach"] = part
            elif item["arbitrated"] <= MAX_ARBITRATIONS:
                item["needs_arbiter"] = part
                item["final_arbiter"] = item["arbitrated"] == MAX_ARBITRATIONS
            else:
                reason = (f"{item['arbitrated']} arbiter fixes failed, the last one on the final arbiter's "
                          "own terms: out of reach at this fidelity")
                task._update_info(limits=limits(task) + [{"part": part, "reason": reason,
                                                          "after_round": item["round"], "auto": True}])
                item["auto_limited"] = part
                if not [i for i in issues(diff) if not is_limited(task, i)]:
                    item.update(worst="minor", fixable=False, worst_given=worst, limited=True)
    task._update_info(reviews=reviews + [item])
    task.log(f"review {item['round']} by {by} of result #{result['n']}: worst fixable {item['worst']}: {diff}")
    return item


def arbiter_prompt(catalog, task: Task, part: str = "") -> dict:
    """A prompt for a fresh arbiter: is the part that keeps failing fixable at this fidelity, and how."""
    part = part or pending_arbiter(task) or ""
    if not part:
        raise HarnessError("nothing to arbitrate: no loop is waiting for an arbiter; name the part with --part")
    info = task.info()
    brief = info.get("brief", {})
    refs = [e for e in info.get("evidence", []) if e["kind"] == "reference"]
    lines = lambda items: "\n".join(f"- {e['file']}" + (f" ({e['note']})" if e["note"] else "")  # noqa: E731
                                     for e in items)
    # the whole history of this part, not only since the user was last asked: the arbiter judges
    # whether it can be fixed at all, and every failed attempt is evidence for that
    tried = [a for a in info.get("approaches", []) if same_problem(a["part"], part)]
    history = "\n".join(f"  {a['n']}. was: {a['was']} -> now: {a['now']}" for a in tried) or "  (none recorded)"
    mentions = [r for r in info.get("reviews", []) if r["by"] == "critic"
                and any(same_problem(i, part) for i in issues(r["diff"]))][-ARBITER_ROUNDS:]
    said = "\n".join(f"- round {r['round']}: " + next(i for i in issues(r["diff"]) if same_problem(i, part))
                     for r in mentions)
    earlier = arbitrations(task, part)
    final = len(earlier) >= MAX_ARBITRATIONS
    failed = "\n".join(f"  - fix {a['n']}: {a['text']}\n    check: {a.get('check') or '(none)'}; measured: "
                       f"{(a.get('checked') or {}).get('result', '(not recorded)')}" for a in earlier)
    final_note = "" if not final else f"""
This is the final arbitration for this part: {len(earlier)} earlier arbiter fixes failed (below).
If you choose FIX and it fails as well, the part is ruled out of reach automatically, on your terms.
Earlier arbiter fixes and what their checks measured:
{failed}
"""
    prompt = f"""You are an independent arbiter. You did not make this work and you did not review it before.

Task given to the author:
{info.get("task", "")}

Quality bar: {brief.get("goal", "(not recorded)")}
The user's constraints: {brief.get("constraints", "(not recorded)")}
Target fidelity: {brief.get("fidelity") or "the level the quality bar implies"}

One part keeps failing review: {part}

What critics said about it, round by round:
{said or "- (no critic text recorded)"}

Approaches the author already tried for it:
{history}

Open and study the reference and the latest result yourself:
Reference:
{lines(refs) or "- none"}
Latest result (the whole result, then close-ups):
{lines(latest_round(task))}
{final_note}
Decide one of two:
- FIX: the part can reach the quality bar at the target fidelity within the constraints. Name one
  concrete technique that differs from every approach tried, as short steps the author can follow,
  grounded in what the reference shows. Then give, on its own line starting with "CHECK:", a
  measurable check the author must run on the new result before it goes back to review (values,
  measurements, comparisons against the reference that pass or fail), so a fix that did not take
  effect is caught. The check must cover every aspect the critics named for this part above, not
  only the easiest one to measure: a part can pass one number and still fail the others. Measure
  the reference yourself and make sure it passes your own check: give the reference's value for
  each condition, so the author is not sent after a target the reference does not have.
- LIMIT: the part cannot reach the bar at this fidelity within the constraints, whatever the
  technique. Say why in one or two sentences.
Do not suggest changing the constraints. End with exactly one line:
VERDICT: fix | limit"""
    return {"prompt": prompt, "part": part, "final": final}


def arbitrate(task: Task, part: str, verdict: str, answer: str, check: str = "") -> dict:
    """Record an arbiter's verdict: a fix becomes the next approach; a limit stops the part blocking."""
    if verdict not in ("fix", "limit"):
        raise HarnessError("verdict must be fix or limit")
    if not answer.strip():
        raise HarnessError("record the arbiter's answer with --text")
    if verdict == "fix" and not check.strip():
        raise HarnessError("a fix needs the arbiter's measurable check: pass its CHECK line with --check")
    info = task.info()
    rounds = len(info.get("reviews", []))
    items = info.get("arbitrations", [])
    item = {"n": len(items) + 1, "after_round": rounds, "part": part, "verdict": verdict, "text": answer,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if verdict == "fix":
        item["check"] = check
    update = {"arbitrations": items + [item]}
    if verdict == "fix":
        approaches = info.get("approaches", [])
        update["approaches"] = approaches + [{"n": len(approaches) + 1, "after_round": rounds, "part": part,
                                              "was": "the approaches tried before", "now": answer, "by": "arbiter",
                                              "time": item["time"]}]
    else:
        update["limits"] = info.get("limits", []) + [{"part": part, "reason": answer, "after_round": rounds}]
    task._update_info(**update)
    task.log(f"arbiter on `{part}`: {verdict}: {answer}")
    return item


def pending_check(task: Task) -> dict | None:
    """The newest arbiter fix whose measurable check the author has not run yet."""
    info = task.info()
    critics = [r for r in info.get("reviews", []) if r["by"] == "critic"]
    since = critics[-1]["round"] if critics else 0
    fixes = [a for a in info.get("arbitrations", []) if a["verdict"] == "fix" and a["after_round"] >= since]
    return next((a for a in reversed(fixes) if not a.get("checked")), None)


def record_check(task: Task, result: str, passed: bool) -> dict:
    """Record what the arbiter's check measured on the new result."""
    arb = pending_check(task)
    if not arb:
        raise HarnessError("no arbiter check is waiting to be run")
    if not result.strip():
        raise HarnessError("say what the check measured with --result")
    items = task.info().get("arbitrations", [])
    for a in items:
        if a["n"] == arb["n"]:
            a["checked"] = {"result": result, "passed": passed, "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
            arb = a
    task._update_info(arbitrations=items)
    task.log(f"check of arbiter fix {arb['n']}: {'passed' if passed else 'failed'}: {result}")
    return arb


def _caps(catalog, task: Task) -> list:
    info = task.info()
    names = {c.name for c, _ in catalog.classify(info.get("task", ""))} | set(info.get("capabilities", []))
    return [catalog.capabilities[n] for n in sorted(names) if n in catalog.capabilities]


def requirements(catalog, task: Task) -> dict:
    """Merged `requires` of the task's capabilities, plus what the brief opted into."""
    need: dict = {}
    for cap in _caps(catalog, task):
        for key, value in cap.requires.items():
            need[key] = value if key == "media" else max(need.get(key, 0), value)
    if task.info().get("brief", {}).get("review") == "critic":
        need["critic"] = max(need.get("critic", 0), 1)
        need["result"] = max(need.get("result", 0), 1)
    return need


def round_results(task: Task) -> list[dict]:
    """Results added since the last critic review: the whole result plus any close-ups of this round."""
    info = task.info()
    critics = [r for r in info.get("reviews", []) if r["by"] == "critic"]
    after = critics[-1]["result"] if critics else 0
    fresh = [e for e in info.get("evidence", []) if e["kind"] == "result" and e["n"] > after]
    return fresh or [latest(task, "result")]


def save_prompt(task: Task, kind: str, prompt: str) -> str:
    """Keep a prompt as a file, so it reaches the reviewer unchanged instead of retyped."""
    dest = task.dir / "prompts"
    dest.mkdir(exist_ok=True)
    n = len(list(dest.glob(f"{kind}-*.txt"))) + 1
    path = dest / f"{kind}-{n:02d}.txt"
    path.write_text(prompt, "utf-8")
    return str(path)


def latest_round(task: Task) -> list[dict]:
    """The newest round of results: new ones not yet reviewed, else the round the last critic saw."""
    info = task.info()
    critics = [r for r in info.get("reviews", []) if r["by"] == "critic"]
    results = [e for e in info.get("evidence", []) if e["kind"] == "result"]
    after = critics[-1]["result"] if critics else 0
    fresh = [e for e in results if e["n"] > after]
    if fresh or not critics:
        return fresh or results[-1:]
    before = critics[-2]["result"] if len(critics) > 1 else 0
    return [e for e in results if before < e["n"] <= after] or results[-1:]


def critic_prompt(catalog, task: Task, dispute: str = "") -> dict:
    """A self-contained prompt for a fresh reviewer that has not seen the author's reasoning."""
    info = task.info()
    result = latest(task, "result")
    if not result:
        raise HarnessError("no result yet: `autoharness evidence add <file> --kind result` first")
    refs = [e for e in info.get("evidence", []) if e["kind"] == "reference"]
    results = round_results(task)
    brief = info.get("brief", {})
    critics = [r for r in info.get("reviews", []) if r["by"] == "critic"]
    if dispute and not critics:
        raise HarnessError("nothing to dispute: no critic review yet")
    waiting = pending_arbiter(task)
    if waiting:
        raise HarnessError(f"a loop on \"{waiting}\" outlasted {MAX_APPROACHES} approaches and waits for an "
                           "arbiter: run `autoharness arbiter`, give its prompt to a fresh subagent, and record "
                           "the verdict with `autoharness arbitrate --verdict fix|limit --text \"<its answer>\"`")
    unchecked = pending_check(task)
    if unchecked:
        raise HarnessError(f"the arbiter's fix comes with a check the author has not run: \"{unchecked['check']}\". "
                           "Run it on the new result and record what it measured with `autoharness check --result "
                           "\"<what you measured>\" --pass yes|no`; if it fails, keep working until it passes or "
                           "record the failure")
    pending = pending_approach(task)
    if pending:
        raise HarnessError(f"a loop on \"{pending}\" needs a different approach before another review: record it "
                           "with `autoharness approach --part \"<part>\" --was \"<what you did>\" --now \"<the new "
                           "technique>\"`, rebuild that part, and add the new result with a close-up of it")
    focus = "; ".join(c.review for c in _caps(catalog, task) if c.review) or GENERIC_FOCUS
    lines = lambda items: "\n".join(f"- {e['file']}" + (f" ({e['note']})" if e["note"] else "")  # noqa: E731
                                     for e in items)
    ref_lines = lines(refs) or "- none: judge against the task and the quality bar only"
    fidelity = brief.get("fidelity") or "the level the quality bar implies"
    disputed = "" if not dispute else f"""
A previous critic listed these differences:
{critics[-1]["diff"]}
The author disputes this point: {dispute}
Check that point first, closely, in the files (close-ups are included where the author made them).
Keep it only if the files show it is true. Then give your own full list as below.
"""
    # every change of approach since the user was last asked, whether a loop demanded it or the
    # author made it on their own: the critic judges those parts first
    since = _last_asked(info.get("reviews", []))
    active = [a for a in info.get("approaches", []) if a["after_round"] >= since]
    parts: list[str] = []
    for a in active:
        if not any(same_problem(a["part"], p) for p in parts):
            parts.append(a["part"])
    focused = ""
    if parts:
        blocks = []
        for part in parts:
            history = "\n".join(f"  {a['n']}. was: {a['was']} -> now: {a['now']}"
                                for a in active if same_problem(a["part"], part))
            blocks.append(f"- {part}\n{history}")
        focused = f"""
These parts kept failing earlier reviews, and the author changed the approach for them:
{chr(10).join(blocks)}
Judge these parts first, in their close-ups: is each still wrong, and if so, how is it wrong now?
Name what would fix it at the target fidelity. Then give your full list as below.
"""
    last_round = critics[-1]["round"] if critics else 0
    checked = [a for a in info.get("arbitrations", []) if a.get("checked") and a["after_round"] >= last_round]
    if checked:
        focused += "\nThe arbiter's fix came with a measurable check, and the author ran it:\n" + "\n".join(
            f"- check: {a['check']}\n  measured: {a['checked']['result']} "
            f"({'passed' if a['checked']['passed'] else 'failed'})" for a in checked) + \
            "\nVerify that measurement yourself in the files before you rely on it.\n"
    settled_points = [p for r in critics for p in r.get("settled", [])]
    if settled_points:
        focused += ("\nEarlier critics settled these disputed points from measurements in the files. Do not raise "
                    "them again unless the files show they changed; if you think one is wrong, give the "
                    "measurement:\n" + "\n".join(f"- {p}" for p in settled_points) + "\n")
    ruled = limits(task)
    limited = "" if not ruled else """
An independent arbiter ruled these parts out of reach at the target fidelity. List them at most
as minor:
""" + "\n".join(f"- {lim['part']}: {lim['reason']}" for lim in ruled) + "\n"
    # fresh critics start from zero and can contradict each other round after round; after its own
    # list, each one accounts for the previous list, so fixed, open and contested points are explicit
    previous = "" if dispute or not critics else f"""
Only after your own list, check the previous review of an earlier result (by another critic):
{critics[-1]["diff"]}
Under the heading "Previous review", give one line per point: fixed, still wrong, or wrong
(what the reference actually shows, from the files). Do not change your own list because of it.
Where your fix for a point goes the opposite way from the previous review's (lighter vs darker,
more vs less, bigger vs smaller), say so and settle it from what the reference shows, with a value
or a measurement, so the author is not sent back and forth.
"""
    prompt = f"""You are an independent critic. You did not make this work and owe its author nothing.

Task given to the author:
{info.get("task", "")}

Quality bar: {brief.get("goal", "(not recorded)")}
The user's constraints: {brief.get("constraints", "(not recorded)")}

Target fidelity: {fidelity}. Judge at that level: detail beyond it (for example photographic
detail in a stylised drawing) is at most minor.

Open and study these files yourself:
Reference:
{ref_lines}
Result to judge (the whole result, then any close-ups of it):
{lines(results)}
{disputed}{focused}{limited}
List the most important differences between the result and the reference / quality bar,
most important first, at most 8. Look in particular at: {focus}.
Tag each one:
- severity: blocker (the result fails its purpose), major (clearly wrong, noticeable at a glance),
  or minor (polish, or detail beyond the target fidelity);
- fixable or not within the user's constraints and the medium.
Describe what you see, not what you expect; if a close-up contradicts the whole view, trust the close-up.
Do not praise, do not soften, do not suggest changing the constraints.
{previous}
End with exactly one line naming the worst severity among the FIXABLE differences:
VERDICT: blocker | major | minor | none"""
    return {"prompt": prompt, "result": result["file"], "results": [e["file"] for e in results],
            "references": [e["file"] for e in refs]}


def problems(catalog, task: Task) -> list[str]:
    """Blocking problems for teardown: the evidence and reviews the task requires."""
    need = requirements(catalog, task)
    if not any(v for k, v in need.items() if k != "media"):
        return []
    media = need.get("media")
    info = task.info()
    counted = [e for e in info.get("evidence", []) if not media or e.get("media") == media]
    of = f" {media}" if media else ""
    out = []
    refs = [e for e in counted if e["kind"] == "reference"]
    results = [e for e in counted if e["kind"] == "result"]
    if len(refs) < need.get("reference", 0):
        out.append(f"add at least {need['reference']}{of} reference you actually looked at "
                   f"(`autoharness evidence add <file> --kind reference`); a description of it is not a reference")
    if len(results) < need.get("result", 0):
        out.append(f"{len(results)}{of} result(s) recorded, {need['result']} needed: add what each round produced "
                   f"(`autoharness evidence add <file> --kind result`)")
    if need.get("critic", 0):
        critics = [r for r in info.get("reviews", []) if r["by"] == "critic"]
        last = latest(task, "result")
        if not critics:
            out.append("no independent review: run `autoharness critic`, give its prompt to a fresh subagent that "
                       "did not make the work, and record the answer with `autoharness review --by critic ...`")
        elif last and critics[-1]["result"] != last["n"]:
            out.append(f"the latest result (#{last['n']}) was not reviewed by the critic: run `autoharness critic` again")
        elif critics[-1].get("worst", "major" if critics[-1]["fixable"] else "none") in BLOCKING:
            out.append(f"the last critic review lists fixable differences up to "
                       f"{critics[-1].get('worst', 'major')}: fix them, add the new result, and ask the critic again")
    return out
