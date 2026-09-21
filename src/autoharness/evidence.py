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
LOOP_ROUNDS = 3  # critic reviews in a row with the same top problem
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


def review(task: Task, diff: str, worst: str, by: str) -> dict:
    if worst not in SEVERITIES:
        raise HarnessError(f"severity must be one of {', '.join(SEVERITIES)}")
    result = latest(task, "result")
    if not result:
        raise HarnessError("no result yet: `autoharness evidence add <file> --kind result` first")
    reviews = task.info().get("reviews", [])
    item = {"round": len(reviews) + 1, "by": by, "result": result["n"], "diff": diff, "worst": worst,
            "fixable": worst in BLOCKING, "top": top_issue(diff), "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if by == "critic":
        repeats = 1
        for prev in reversed([r for r in reviews if r["by"] == "critic"]):
            if not same_issue(prev.get("top") or top_issue(prev["diff"]), item["top"]):
                break
            repeats += 1
        item["repeats"] = repeats
    task._update_info(reviews=reviews + [item])
    task.log(f"review {item['round']} by {by} of result #{result['n']}: worst fixable {worst}: {diff}")
    return item


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
{disputed}
List the most important differences between the result and the reference / quality bar,
most important first, at most 8. Look in particular at: {focus}.
Tag each one:
- severity: blocker (the result fails its purpose), major (clearly wrong, noticeable at a glance),
  or minor (polish, or detail beyond the target fidelity);
- fixable or not within the user's constraints and the medium.
Describe what you see, not what you expect; if a close-up contradicts the whole view, trust the close-up.
Do not praise, do not soften, do not suggest changing the constraints.

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
