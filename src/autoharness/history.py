"""What earlier tasks turned down: skipped candidates, tools removed mid-task, vet verdicts of `breaks`.

Recipes remember what worked; this remembers what did not, so a later task does not vet, install and
smoke-test the same candidate again only to drop it for the same reason. The latest word wins: a
candidate a later task selected or kept is no longer reported.
"""
from __future__ import annotations

from autoharness import audit
from autoharness.env import HarnessError, Project, Task

SHOWN = 3  # rejections reported per candidate, newest first


def key(kind: str, name: str) -> tuple[str, str]:
    """Candidates are named differently by different commands (`owner/repo@skill` or its directory name,
    a registry name or its local name): compare by the last part."""
    kind = "skill" if kind.startswith("skill") else "mcp"
    short = name.rsplit("@", 1)[-1] if kind == "skill" else name.rsplit("/", 1)[-1]
    return kind, short.split(":")[-1].lower()


def rejections(project: Project, exclude: str | None = None) -> dict[tuple[str, str], list[dict]]:
    """(kind, short name) -> rejections still standing, newest first. Never raises on one bad task."""
    events = []
    tasks = project.state / "tasks"
    for d in sorted(tasks.iterdir()) if tasks.is_dir() else []:
        if d.name == exclude or not d.is_dir():
            continue
        task = Task(project, d.name)
        try:
            entries = audit.read(task)
            vetted = task.info().get("vetted", {})
        except (HarnessError, ValueError, OSError):
            continue
        events += [e | {"task": d.name} for e in entries if e.get("kind") and e.get("name")]
        # a vet verdict carries no time of its own: it counts as the task's first event
        start = entries[0]["time"] if entries else 0
        events += [{"time": start, "action": "vet", "verdict": v.get("verdict"), "kind": k.split(":", 1)[0],
                    "name": k.split(":", 1)[1], "reason": v.get("text", ""), "task": d.name}
                   for k, v in vetted.items() if ":" in k]
    standing: dict[tuple[str, str], list[dict]] = {}
    for e in sorted(events, key=lambda e: e.get("time", 0)):
        k = key(e["kind"], e["name"])
        negative = ((e["action"] == "skip") or (e["action"] == "vet" and e.get("verdict") == "breaks")
                    or (e["action"] == "remove" and e.get("reason") != "teardown"))
        if negative:
            standing.setdefault(k, []).insert(0, {"task": e["task"], "name": e["name"], "action": e["action"],
                                                  "reason": e.get("reason") or "no reason given"})
        elif e["action"] in ("select", "install") or (e["action"] == "vet" and e.get("verdict") == "fits"):
            standing.pop(k, None)
    return {k: v[:SHOWN] for k, v in standing.items()}


def for_candidates(found: dict, candidates) -> list[dict]:
    """The rejections among (kind, name) candidates, one entry per candidate."""
    seen, result = set(), []
    for kind, name in candidates:
        k = key(kind, name)
        if k in found and k not in seen:
            seen.add(k)
            result.append({"kind": k[0], "candidate": name, "rejected": found[k]})
    return result


def warning(found: dict, kind: str, name: str) -> list[str]:
    past = found.get(key(kind, name))
    if not past:
        return []
    said = "; ".join(f"{p['action']} of `{p['name']}` in task {p['task']}: {p['reason']}" for p in past)
    return [f"an earlier task turned this down ({said}): check that the reason does not apply here"]
