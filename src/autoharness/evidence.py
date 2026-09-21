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
import shutil
import time
from pathlib import Path

from autoharness.env import HarnessError, Task

KINDS = ("reference", "result")
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


def review(task: Task, diff: str, fixable: bool, by: str) -> dict:
    result = latest(task, "result")
    if not result:
        raise HarnessError("no result yet: `autoharness evidence add <file> --kind result` first")
    reviews = task.info().get("reviews", [])
    item = {"round": len(reviews) + 1, "by": by, "result": result["n"], "diff": diff, "fixable": fixable,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    task._update_info(reviews=reviews + [item])
    task.log(f"review {item['round']} by {by} of result #{result['n']}: "
             f"{'fixable differences left' if fixable else 'nothing fixable left'}: {diff}")
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


def critic_prompt(catalog, task: Task) -> dict:
    """A self-contained prompt for a fresh reviewer that has not seen the author's reasoning."""
    info = task.info()
    result = latest(task, "result")
    if not result:
        raise HarnessError("no result yet: `autoharness evidence add <file> --kind result` first")
    refs = [e for e in info.get("evidence", []) if e["kind"] == "reference"]
    brief = info.get("brief", {})
    focus = "; ".join(c.review for c in _caps(catalog, task) if c.review) or GENERIC_FOCUS
    ref_lines = "\n".join(f"- {e['file']}" + (f" ({e['note']})" if e["note"] else "") for e in refs) \
        or "- none: judge against the task and the quality bar only"
    prompt = f"""You are an independent critic. You did not make this work and owe its author nothing.

Task given to the author:
{info.get("task", "")}

Quality bar: {brief.get("goal", "(not recorded)")}
The user's constraints: {brief.get("constraints", "(not recorded)")}

Open and study these files yourself:
Reference:
{ref_lines}
Result to judge (#{result['n']}):
- {result['file']}

List the most important differences between the result and the reference / quality bar,
most important first, at most 8. Look in particular at: {focus}.
For each one say whether it can be fixed within the user's constraints and the medium
(fixable) or not. Do not praise, do not soften, do not suggest changing the constraints.

End with exactly one line:
VERDICT: fixable   (if any difference above is fixable)
VERDICT: done      (if nothing fixable is left)"""
    return {"prompt": prompt, "result": result["file"], "references": [e["file"] for e in refs]}


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
        elif critics[-1]["fixable"]:
            out.append("the last critic review lists fixable differences: fix them, add the new result, and ask "
                       "the critic again")
    return out
