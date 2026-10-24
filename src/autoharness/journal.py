"""The project's own journal: an entry a person reads for what each task did, measured and concluded.

A project that keeps one (`JOURNAL.md` at its root, or the path in the `journal` setting; `off` turns it off)
gets an entry from every task. `init` keeps a copy of the journal as it was; teardown compares, and until the
journal has a real entry (not a word, MIN_ENTRY characters at least) a full task is incomplete and a light one
warns. What autoharness knows goes into a draft for the agent to finish: the goal, the checks it ran and how
they ended, the lessons, the sources. What it cannot know (the values measured, where things live, the backups,
what was left for later) the agent adds. The draft takes the journal's own form: its heading level, its date
format, and whether new entries go at the top or the bottom.

    .autoharness/tasks/<id>/journal-start.md   the journal when the task started
    .autoharness/tasks/<id>/journal-draft.md   the draft teardown and `autoharness journal` point to
"""

from __future__ import annotations

import difflib
import re
import time
from pathlib import Path

from autoharness import config, lessons
from autoharness.env import Task

DEFAULT = "JOURNAL.md"
START = "journal-start.md"
DRAFT = "journal-draft.md"
MIN_ENTRY = 80   # non-space characters added: below this it is a remark, not an entry
SOURCE = re.compile(r"https?://[^\s;,)]+")
HEADING = re.compile(r"^(#{2,4})\s+(.*\S)", re.MULTILINE)
DATES = ((re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b"), "%d.%m.%Y", (3, 2, 1)),
         (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "%Y-%m-%d", (1, 2, 3)),
         (re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b"), "%d/%m/%Y", (3, 2, 1)))


def path(root: Path) -> Path | None:
    """The journal this project keeps, or None (no file, or the setting is `off`)."""
    try:
        value = config.resolve(root, "journal")[0].strip()
    except (ValueError, OSError):
        value = ""
    if value.lower() == "off":
        return None
    p = root / (value or DEFAULT)
    return p if p.is_file() else None


def snapshot(task: Task) -> None:
    """Keep the journal as it is when the task starts: teardown looks for what was added since."""
    if (p := path(task.project.root)) is not None:
        (task.dir / START).write_text(p.read_text("utf-8", errors="replace"), "utf-8")


def added(task: Task) -> str | None:
    """The text added to the journal since the task started, or None when there is nothing to compare."""
    p, start = path(task.project.root), task.dir / START
    if p is None or not start.is_file():
        return None
    before = start.read_text("utf-8", errors="replace").splitlines()
    now = p.read_text("utf-8", errors="replace").splitlines()
    return "\n".join(line[2:] for line in difflib.ndiff(before, now) if line.startswith("+ "))


def style(text: str) -> dict:
    """The journal's form, read from its entries: heading level, date format, where new entries go."""
    headings = HEADING.findall(text)
    level = headings[0][0] if headings else "##"
    fmt, dated = "%Y-%m-%d", []
    for pattern, f, order in DATES:
        found = [m for _, h in headings if (m := pattern.search(h))]
        if found:
            fmt = f
            dated = [tuple(int(m.group(i)) for i in order) for m in found]
            break
    newest_first = len(dated) < 2 or dated[0] >= dated[-1]
    return {"level": level, "date": fmt, "order": "newest first" if newest_first else "oldest first"}


def draft(task: Task) -> tuple[Path, dict]:
    """Write the draft entry from the task's record; returns where it is and the journal's form."""
    info = task.info()
    p = path(task.project.root)
    form = style(p.read_text("utf-8", errors="replace") if p else "")
    brief = info.get("brief", {})
    goal = (brief.get("goal") or info.get("task", "")).strip()
    lines = [f"{form['level']} {time.strftime(form['date'])} — {info.get('task', '').strip()[:80]}", "",
             f"Goal: {goal}", ""]
    runs = info.get("runs", [])
    if runs:
        lines.append("Checks:")
        last: dict[str, dict] = {}
        for r in runs:
            last[r.get("for") or r["command"]] = r
        for key, r in last.items():
            tries = sum(1 for x in runs if (x.get("for") or x["command"]) == key)
            lines.append(f"- {r.get('for') or 'check'}: `{r['command']}` — "
                         + ("passed" if r["passed"] else "failed") + (f" (after {tries} runs)" if tries > 1 else ""))
        lines.append("")
    if learned := lessons.friction(task):
        lines += ["What was hard:"] + [f"- {x}" for x in learned] + [""]
    if sources := list(dict.fromkeys(SOURCE.findall(brief.get("method", "")))):
        lines += ["Sources:"] + [f"- {s}" for s in sources] + [""]
    lines += ["<!-- add what autoharness cannot know: values measured, where things live, backups, what is left -->",
              ""]
    out = task.dir / DRAFT
    out.write_text("\n".join(lines), "utf-8")
    return out, form


def problem(task: Task) -> tuple[str, str] | None:
    """(what is missing, the draft's path) while the journal has no entry from this task, else None."""
    new = added(task)
    if new is None or len(re.sub(r"\s+", "", new)) >= MIN_ENTRY:
        return None
    p = path(task.project.root)
    out, form = draft(task)
    where = "at the top, under the journal's header" if form["order"] == "newest first" else "at the bottom"
    return (f"the project keeps a journal ({p.name}) and it has no entry from this task yet: finish the draft "
            f"in {out} (what was done, measured and concluded, in the journal's language), add it {where}, "
            "then tear down again", str(out))
