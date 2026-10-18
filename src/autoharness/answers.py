"""Reviewer answers, recorded as the reviewer wrote them.

A critic, arbiter or vetting reviewer answers a prompt the author hands on. If the author then
retells the answer (`--diff "<its list>" --worst <its VERDICT>`), nothing stops a softer list or a
milder severity. So every such prompt carries a one-time answer id; the author saves the reviewer's
raw answer to a file and passes it with `--answer-file`, and autoharness checks the id and reads the
verdict, the list and the CHECK line itself.
"""

from __future__ import annotations

import re
import secrets
import shutil
import time
from pathlib import Path

from autoharness.env import HarnessError, Task

KINDS = ("critic", "arbiter", "vet")
ID_LINE = re.compile(r"^\W*ANSWER-ID:\s*(ah-[0-9a-f]{12})\b", re.MULTILINE | re.IGNORECASE)
VERDICT_LINE = re.compile(r"^\W*VERDICT:(.*)$", re.MULTILINE | re.IGNORECASE)
CHECK_LINE = re.compile(r"^\W*CHECK:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
REASON_LINE = re.compile(r"^\W*REASON:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
# the critic's account of the previous review is a section of its own after its list: a heading line
# (a line at the margin that names it, such as "Previous review" or "## Check of the previous review";
# never an indented detail line, a bullet or a numbered point)
HEADING = r"^(?![ \t])(?![-*+][ \t])(?!(?:#+[ \t]*)?(?:\*\*)?\d+[.)])[^\n]{0,40}?\b%s\b.*$"
PREVIOUS = re.compile(HEADING % "previous review", re.MULTILINE | re.IGNORECASE)
# the critic's verdict per brief item and the work nobody asked for, each a section after its list
COVERAGE = re.compile(HEADING % "brief coverage", re.MULTILINE | re.IGNORECASE)
UNREQUESTED = re.compile(HEADING % "unrequested", re.MULTILINE | re.IGNORECASE)
COVERAGE_LINE = re.compile(r"^\W*([BP]\d+)\W+(met|partial|missing|contradicts)\b", re.MULTILINE | re.IGNORECASE)
# "1. x", "1) x", "### 1. x", "**1.** x", "**1. x**"
ITEM = re.compile(r"^\s{0,3}(?:#+\s*)?(?:\*\*)?(\d+)[.)](?:\*\*)?\s+(.*)$")


def issue(task: Task, kind: str, subject: str) -> str:
    """A fresh answer id for one prompt of `kind` about `subject` (the result judged, the part arbitrated,
    the candidate vetted). Earlier unused ids of the same kind and subject are retired: of several
    prompts, only the answer to the latest counts, so the author cannot pick the mildest."""
    if kind not in KINDS:
        raise HarnessError(f"answer kind must be one of {', '.join(KINDS)}")
    nonce = f"ah-{secrets.token_hex(6)}"
    ids = task.info().get("answer_ids", {})
    for other in ids.values():
        if other["kind"] == kind and other["subject"] == subject and not other.get("used"):
            other["retired"] = nonce
    ids[nonce] = {"kind": kind, "subject": subject, "issued": time.strftime("%Y-%m-%dT%H:%M:%S")}
    task._update_info(answer_ids=ids)
    return nonce


def instruction(nonce: str) -> str:
    return f"\n\nStart your answer with this line, exactly as written:\nANSWER-ID: {nonce}"


def read(task: Task, path: Path, kind: str, subject: str) -> dict:
    """Check the answer file's id and take it apart:
    {"id", "kind", "text", "body", "items", "previous", "coverage", "unrequested", "verdict", "check", "reason",
    "source"}.

    The id must have been issued for the latest prompt of this kind about this subject, and not used."""
    if not path.is_file():
        raise HarnessError(f"no such answer file: {path}")
    text = path.read_text("utf-8", errors="replace").strip()
    m = ID_LINE.search(text)
    if not m:
        raise HarnessError("the answer has no ANSWER-ID line: save the reviewer's own answer, unedited, "
                           "for a prompt autoharness made")
    nonce = m.group(1).lower()
    ids = task.info().get("answer_ids", {})
    entry = ids.get(nonce)
    if entry is None or entry["kind"] != kind:
        raise HarnessError(f"ANSWER-ID {nonce} was not issued for a {kind} prompt in this task")
    if entry["subject"] != subject:
        raise HarnessError(f"ANSWER-ID {nonce} belongs to a {kind} prompt about {entry['subject']}, not {subject}: "
                           f"run `autoharness {kind}` again for it")
    if entry.get("used"):
        raise HarnessError(f"ANSWER-ID {nonce} was already recorded: a new answer needs a new prompt")
    if entry.get("retired"):
        raise HarnessError(f"ANSWER-ID {nonce} was replaced by a later prompt ({entry['retired']}): record the "
                           "answer to that one")
    verdicts = VERDICT_LINE.findall(text)
    if not verdicts:
        raise HarnessError("the answer has no VERDICT line: ask the reviewer to finish its answer")
    words = re.findall(r"[A-Za-z]+", verdicts[-1])
    if len(words) != 1:
        raise HarnessError(f"the VERDICT line must name exactly one value, not {verdicts[-1].strip()!r}")
    body = text[m.end():]
    body = body[:VERDICT_LINE.search(body).start()] if VERDICT_LINE.search(body) else body
    found = sorted((m.start(), m.end(), name) for name, rx in
                   (("previous", PREVIOUS), ("coverage", COVERAGE), ("unrequested", UNREQUESTED))
                   if (m := rx.search(body)))
    sections = {name: body[end:(found[i + 1][0] if i + 1 < len(found) else len(body))].strip()
                for i, (_, end, name) in enumerate(found)}
    body = body[:found[0][0]] if found else body
    unrequested = sections.get("unrequested", "")
    check, reason = CHECK_LINE.findall(text), REASON_LINE.findall(text)
    return {"id": nonce, "kind": kind, "text": text, "body": body.strip(), "items": items(body),
            "previous": sections.get("previous", ""),
            "coverage": {k.upper(): v.lower() for k, v in COVERAGE_LINE.findall(sections.get("coverage", ""))},
            "unrequested": "" if re.fullmatch(r"\W*none\W*", unrequested, re.IGNORECASE) else unrequested,
            "verdict": words[0].lower(),
            "check": check[-1].strip() if check else "", "reason": reason[-1].strip() if reason else "",
            "source": str(path)}


def items(body: str) -> list[str]:
    """The numbered points of a reviewer's list, each with its indented details folded in on one line;
    preamble and headings around the list are not points."""
    found: list[str] = []
    in_point = False
    for line in body.splitlines():
        m = ITEM.match(line)
        if m:
            found.append(m.group(2))
            in_point = True
        elif not line.strip():
            continue
        elif in_point and (line[:1].isspace() or line.lstrip()[:1] in "-*"):
            found[-1] += " " + line.strip().lstrip("-* ")
        else:
            in_point = False  # a line at the margin (a preamble, a heading, a note) ends the point
    # issues() splits a review on `;` and on a number that starts a new point
    return [re.sub(r"\s+", " ", i.replace(";", ",")).replace("**", "").strip() for i in found]


def as_diff(answer: dict) -> str:
    """The critic's list in the form the loop detection reads: one numbered point per line."""
    if not answer["items"]:
        return answer["body"]
    return "\n".join(f"{n}. {i}" for n, i in enumerate(answer["items"], 1))


def consume(task: Task, answer: dict) -> str:
    """Once the answer is recorded: keep a copy of it and retire its id. Returns the copy's path."""
    dest = task.dir / "answers"
    dest.mkdir(exist_ok=True)
    kept = dest / f"{answer['kind']}-{answer['id']}.txt"
    shutil.copyfile(answer["source"], kept)
    ids = task.info().get("answer_ids", {})
    ids[answer["id"]] |= {"used": time.strftime("%Y-%m-%dT%H:%M:%S"), "file": str(kept)}
    task._update_info(answer_ids=ids)
    return str(kept)
