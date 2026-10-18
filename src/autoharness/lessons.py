"""What a task's own record says it taught, and whether the skills and recipes saved earlier helped.

A task that went smoothly teaches little; one where a check failed before it passed, a part needed a
new approach, the critic sent the work back, or the user had to step in, holds know-how the next task
would pay for again. Teardown reads those signals from the task record and asks for them to be
written into a skill once, naming them, instead of a generic "save what you learned".

Saved skills and recipes are offered again to later tasks; `outcomes.json` next to the recipes keeps,
for each, how often it was offered, used, kept or removed, and whether the tasks that used it went
smoothly, so `init` shows that record and a skill that keeps being dropped stands out.

    .autoharness/tasks/<id>/user-messages.jsonl   what the user wrote while the task was active (the hook)
    ~/.autoharness/outcomes.json                   {"skill:<name>": {"offered": 3, "used": 2, ...}}
    ~/.autoharness/insights.json                   small conclusions, each with the tasks that confirmed it

A lesson too small for a skill of its own is an insight (`lesson add`): the same conclusion reached again in
another task is merged into it and its confidence (`confirmed`) grows; `init` shows the ones that bear on a
task, and once enough tasks confirmed one, teardown asks for it to be folded into a skill.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from autoharness import recipes, text
from autoharness.env import HarnessError, Task

USER_MESSAGES = "user-messages.jsonl"
MANY_ROUNDS = 4  # critic rounds from which the number of rounds is itself a lesson
EVENTS = ("offered", "used", "kept", "removed", "smooth", "friction")


def note_user_message(task: Task, prompt: str) -> None:
    """Record a message the user sent while the task was active: corrections and new input are lessons.
    Appended without the project lock: the hook has seconds, and a line per message cannot clash."""
    with (task.dir / USER_MESSAGES).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "text": prompt[:300]},
                           ensure_ascii=False) + "\n")


def user_messages(task: Task) -> list[dict]:
    path = task.dir / USER_MESSAGES
    if not path.is_file():
        return []
    out = []
    for line in path.read_text("utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def friction(task: Task) -> list[str]:
    """The signals in the task record that it taught something, one line each, most telling first."""
    info = task.info()
    out = []
    for part in dict.fromkeys(a["part"] for a in info.get("approaches", [])):
        steps = " -> ".join([next(a["was"] for a in info["approaches"] if a["part"] == part)]
                            + [a["now"] for a in info["approaches"] if a["part"] == part])
        out.append(f"\"{part}\" kept failing and needed a new approach: {steps}")
    out += [f"\"{lim['part']}\" was ruled out of reach: {lim['reason']}" for lim in info.get("limits", [])]
    out += [f"an arbiter's fix for \"{a['part']}\" {'worked' if a['checked']['passed'] else 'failed'}: {a['text']}"
            for a in info.get("arbitrations", []) if a.get("checked") and a.get("text")]
    by_check: dict[str, list[dict]] = {}
    for r in info.get("runs", []):
        by_check.setdefault(r.get("for") or r["command"], []).append(r)
    for check, items in by_check.items():
        failed = [r for r in items if not r["passed"]]
        what = f"the check for {check}" if items[-1].get("for") else f"`{check}`"
        if failed and items[-1]["passed"]:
            out.append(f"{what} failed {len(failed)} time(s) before `{items[-1]['command']}` passed (run "
                       f"#{items[-1]['n']}): what fixed it?")
        elif failed:
            out.append(f"{what} still fails (run #{items[-1]['n']})")
    critics = [r for r in info.get("reviews", []) if r["by"] == "critic"]
    back = [r for r in critics if r.get("compared", {}).get("closer") == "earlier"]
    if back:
        out.append(f"compared blind, {len(back)} round(s) went backwards from an earlier result")
    if len(critics) >= MANY_ROUNDS:
        out.append(f"{len(critics)} critic rounds: {' -> '.join(r['worst'] for r in critics)}")
    said = user_messages(task)
    if said:
        out.append(f"the user stepped in {len(said)} time(s) during the task: "
                   + "; ".join(f"\"{m['text'][:120]}\"" for m in said[-3:]))
    return out


# --- what became of saved skills and recipes ---------------------------------------------------

def _path() -> Path:
    return recipes.home() / "outcomes.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def bump(kind: str, name: str, *events: str) -> None:
    """Count `events` for a saved skill or recipe. Never fails the command it is called from."""
    data = _load()
    entry = data.setdefault(f"{kind}:{name}", {})
    for e in events:
        entry[e] = entry.get(e, 0) + 1
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
    except OSError:
        pass


def record(kind: str, name: str) -> dict:
    """{"offered": n, ...} with a "note" when the record argues against reusing it."""
    entry = _load().get(f"{kind}:{name}", {})
    out = {e: entry.get(e, 0) for e in EVENTS if entry.get(e)}
    if entry.get("used", 0) >= 2 and entry.get("removed", 0) > entry.get("kept", 0):
        out["note"] = "removed in most tasks that used it: check why before relying on it"
    elif entry.get("used", 0) >= 2 and entry.get("friction", 0) == entry.get("used", 0):
        out["note"] = "every task that used it ran into trouble: check it is still right"
    return out


def settle(task: Task, kept_skills: list[str], lessons: list[str]) -> None:
    """At teardown: what became of each saved skill and recipe this task used."""
    outcome = "friction" if lessons else "smooth"
    for kind, name in task.info().get("library_used", []):
        if kind == "skill":
            bump(kind, name, "kept" if name in kept_skills else "removed", outcome)
        else:
            bump(kind, name, outcome)


def mark_used(task: Task | None, kind: str, name: str) -> None:
    """A saved skill or recipe taken into a task: counted once per task, settled at its teardown."""
    if task is not None:
        used = task.info().get("library_used", [])
        if [kind, name] in used:
            return
        task._update_info(library_used=used + [[kind, name]])
    bump(kind, name, "used")


# --- insights: small conclusions whose confidence grows with each task that confirms them ---------

INSIGHTS = "insights.json"
RIPE = 3          # tasks that confirmed an insight before it belongs in a skill
SHOWN = 5         # insights init shows for a task
MAX_LENGTH = 400  # an insight is a sentence or two; a procedure belongs in a skill


def _insights_path() -> Path:
    return recipes.home() / INSIGHTS


def insights() -> list[dict]:
    try:
        return json.loads(_insights_path().read_text("utf-8"))["items"]
    except (OSError, ValueError, KeyError):
        return []


def _save_insights(items: list[dict]) -> None:
    _insights_path().parent.mkdir(parents=True, exist_ok=True)
    _insights_path().write_text(json.dumps({"items": items}, indent=2, ensure_ascii=False) + "\n", "utf-8")


def _similar(a: str, b: str) -> bool:
    """The same conclusion in other words: most content words of the shorter one are in the other."""
    sa, sb = text.stems(a), text.stems(b)
    shared = len(sa & sb)
    return bool(sa and sb) and shared / min(len(sa), len(sb)) >= 0.6 and (shared >= 3 or sa == sb)


def add_insight(conclusion: str, task_id: str | None, project: str) -> tuple[dict, bool]:
    """Record a conclusion, or confirm the one it repeats: (the insight, whether it was merged into one)."""
    conclusion = " ".join(conclusion.split())
    if not conclusion:
        raise HarnessError("an insight needs its text: when X, do Y, because Z")
    if len(conclusion) > MAX_LENGTH:
        raise HarnessError(f"an insight is a sentence or two (at most {MAX_LENGTH} characters); steps and checks "
                           "belong in a skill: `autoharness skill new <name>`")
    items = insights()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    found = next((i for i in items if _similar(i["text"], conclusion)), None)
    if found:
        if task_id and task_id in found["tasks"]:
            return found, True   # one task confirms a conclusion once
        found["confirmed"] += 1
        found["tasks"].append(task_id)
        found["projects"] = sorted(set(found["projects"]) | {project})
        found["last"] = now
        if conclusion != found["text"] and conclusion not in found.setdefault("variants", []):
            found["variants"] = (found["variants"] + [conclusion])[-3:]
        _save_insights(items)
        return found, True
    item = {"id": max((i["id"] for i in items), default=0) + 1, "text": conclusion, "confirmed": 1,
            "tasks": [task_id], "projects": [project], "first": now, "last": now}
    _save_insights(items + [item])
    return item, False


def fold(item_id: int, skill: str) -> dict:
    """The insight now lives in a skill: it is no longer offered on its own."""
    items = insights()
    found = next((i for i in items if i["id"] == item_id), None)
    if not found:
        raise HarnessError(f"no insight #{item_id}: `autoharness lesson list` shows them")
    found["skill"] = skill
    _save_insights(items)
    return found


def relevant(task_text: str) -> list[dict]:
    """Insights not yet in a skill that share words with the task, the most confirmed first."""
    words = text.stems(task_text)
    hits = [(len(words & text.stems(i["text"])), i) for i in insights() if not i.get("skill")]
    hits = [(n, i) for n, i in hits if n >= 2]
    hits.sort(key=lambda p: (p[1]["confirmed"], p[0]), reverse=True)
    return [{"id": i["id"], "text": i["text"], "confirmed": i["confirmed"]} for _, i in hits[:SHOWN]]


def ripe() -> list[dict]:
    """Insights confirmed by enough tasks that they belong in a skill."""
    return [{"id": i["id"], "text": i["text"], "confirmed": i["confirmed"]}
            for i in insights() if i["confirmed"] >= RIPE and not i.get("skill")]
