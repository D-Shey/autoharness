"""Checks autoharness runs itself, and what teardown can honestly say was checked.

`verify run "<command>"` executes the check in the project root, decides pass or fail by its exit code,
keeps the whole output, and records the code version it ran on: the commit and a fingerprint of the
changed files (`evidence.tree_state`). A later edit makes the run stale, so a green run on old code
does not pass for a check of the code at teardown. What the agent only reports (`verify record`,
`check --pass` without `--run`, a review by itself) is kept apart from what autoharness saw.

    .autoharness/tasks/<id>/runs/<n>-<name>.log   output of each run
    task.json: "runs": [...]
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from autoharness import evidence, transactions
from autoharness.env import HarnessError, Task

TAIL_LINES = 20
DEFAULT_TIMEOUT = 600
PRESERVE = "preserve"
VERDICTS = ("met", "partial", "missing", "contradicts")
# where a numbered item may start: `1 a; 2 b`, `intro: 1 a`, `1. a 2. b` (a bare `2 ` only after `;` or `:`)
ITEM_START = re.compile(r"(^\s*|[;:]\s*|\s)(\d+)([.)]?)\s")


# a command that exits 0 whatever the code does: a run of it shows nothing, so it does not count as a check
_TRIVIAL_PART = re.compile(r"\s*(true|:|exit(\s+0)?|cd\s+\S+|(echo|printf|Write-Output|Write-Host)(\s.*)?"
                           r"|cmd(\.exe)?\s+/c\s+exit\s+0)\s*", re.I)
# python -c with nothing in it that could fail: `pass`, `print("ok")`, `sys.exit(0)`
_TRIVIAL_PYTHON = re.compile(r"""\s*(python3?|py)(\.exe)?\s+(-\w+\s+)*-c\s+(["'])(\s*(pass|print\(\s*('[^']*'|"[^"]*"|\d*)\s*\)|import sys"""
                             r"""|(sys\.)?exit\(0?\))\s*;?)+\4\s*""", re.I)
_SEPARATORS = re.compile(r"&&|\|\||;")


def is_trivial(command: str) -> bool:
    """`true`, `exit 0`, `echo ok`, `python -c "pass"` and chains of them: a command that cannot fail."""
    if _TRIVIAL_PYTHON.fullmatch(command):
        return True
    if "|" in command.replace("||", ""):
        return False   # piped into something: that something may check
    return all(_TRIVIAL_PART.fullmatch(part) for part in _SEPARATORS.split(command))


NOT_A_CHECK = ("`{command}` exits 0 whatever the code does, so it checks nothing and does not count as a check: "
               "run the command that tests the behavior")


# git's empty tree: the base for a repository with no commit yet, where every file counts as changed
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def fingerprint(root: Path) -> dict | None:
    """The code version a run sees: HEAD and the state of every file changed since it. None outside git."""
    try:
        try:
            head = evidence._git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").decode().strip()
        except HarnessError:
            evidence._git(root, "rev-parse", "--git-dir")   # a repository, just no commit yet
            head = EMPTY_TREE
        return {"base_sha": head, "tree_state": evidence.tree_state(root, head)}
    except (HarnessError, OSError):
        return None


def is_fresh(item: dict, root: Path) -> bool | None:
    """Whether the code is still the version the run saw (committing it since does not count as a change);
    None when that cannot be told (no git, or the commit is gone)."""
    seen = item.get("fingerprint")
    if not seen:
        return None
    try:
        return evidence.tree_state(root, seen["base_sha"]) == seen["tree_state"]
    except (HarnessError, OSError):
        return None


def _tail(output: str) -> str:
    return "\n".join(output.rstrip().splitlines()[-TAIL_LINES:])


def run(task: Task, command: str, name: str = "", purpose: str = "", timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Run `command` in the project root through the shell and record it. Passed means exit code 0."""
    if not command.strip():
        raise HarnessError("give the command to run, e.g. `autoharness verify run \"pytest -q\"`")
    root = task.project.root
    name = name or command.split()[0]
    started = time.monotonic()
    # the project lock is not held while the check runs (a test suite may take minutes): only for the record
    try:
        proc = subprocess.run(command, shell=True, cwd=root, capture_output=True, stdin=subprocess.DEVNULL,
                              timeout=timeout)
        code, raw, timed_out = proc.returncode, proc.stdout + proc.stderr, False
    except subprocess.TimeoutExpired as e:
        code, raw, timed_out = None, (e.stdout or b"") + (e.stderr or b""), True
    seconds = round(time.monotonic() - started, 2)
    output = raw.decode("utf-8", "replace")
    with transactions.locked(root):
        items = task.info().get("runs", [])
        n = len(items) + 1
        return _record(task, items, n, command, name, purpose, code, timed_out, seconds, output)


def _record(task: Task, items: list, n: int, command: str, name: str, purpose: str, code: int | None,
            timed_out: bool, seconds: float, output: str) -> dict:
    root = task.project.root
    dest = task.dir / "runs"
    dest.mkdir(exist_ok=True)
    log = dest / f"{n:02d}-{re.sub(r'[^A-Za-z0-9_.-]', '_', name)[:40]}.log"
    log.write_text(f"$ {command}\n# exit {code}{' (timed out)' if timed_out else ''}, {seconds}s\n\n{output}",
                   "utf-8")
    # taken after the run: a check that writes tracked files (formatters, snapshots) is judged on what it left
    item = {"n": n, "name": name, "command": command, "for": purpose, "exit": code, "passed": code == 0,
            "seconds": seconds, "log": str(log), "fingerprint": fingerprint(root),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S")} | ({"timed_out": True} if timed_out else {})         | ({"trivial": True} if is_trivial(command) else {})
    task._update_info(runs=items + [item])
    task.log(f"run #{n} `{command}`" + (f" for {purpose}" if purpose else "")
             + f": {'passed' if item['passed'] else 'failed'} (exit {code})")
    return item | {"tail": _tail(output)}


def latest_for(task: Task, purpose: str) -> dict | None:
    found = [r for r in task.info().get("runs", []) if r.get("for") == purpose]
    return found[-1] if found else None


def brief_items(brief: dict) -> list[dict]:
    """The brief's goal and what it must preserve, split into numbered items: B1, B2, ... for the goal,
    P1, P2, ... for what must keep working."""
    out = []
    for prefix, key in (("B", "goal"), ("P", PRESERVE)):
        parts, expected = [], 1
        for line in (brief.get(key) or "").splitlines():
            found, expected = _numbered(line, expected)
            parts += found
        out += [{"id": f"{prefix}{i}", "text": p} for i, p in enumerate(parts, 1)]
    return out


def _numbered(line: str, expected: int) -> tuple[list[str], int]:
    """A line split at its numbered items: only at the next number in turn (1, 2, 3, ... across lines), so
    `retry up to 3. Then` stays whole. Text before the first number (an introduction) is an item of its own."""
    cuts = []
    for m in ITEM_START.finditer(line):
        before, number, mark = m.groups()
        if int(number) == expected and (mark or m.start() == 0 or before.strip() in (";", ":")):
            cuts.append((m.start(), m.end()))
            expected += 1
    if not cuts:
        return ([line.strip(" ;")] if line.strip(" ;") else []), expected
    pieces = [line[:cuts[0][0]]] + [line[end:(cuts[i + 1][0] if i + 1 < len(cuts) else len(line))]
                                    for i, (_, end) in enumerate(cuts)]
    return [p.strip(" ;:") for p in pieces if p.strip(" ;:")], expected


def problems(task: Task) -> list[str]:
    """Teardown blockers: the brief asked to preserve something and no fresh passing run shows it was."""
    brief = task.info().get("brief", {})
    if not brief.get(PRESERVE):
        return []
    last = latest_for(task, PRESERVE)
    how = (f"run the regression check with `autoharness verify run \"<command>\" --for {PRESERVE}` "
           f"(the brief says to preserve: {brief[PRESERVE]})")
    if not last:
        return [f"nothing shows that what the brief preserves still works: {how}"]
    if last.get("trivial"):
        return [NOT_A_CHECK.format(command=last["command"]) + f" (run #{last['n']}): {how}"]
    if not last["passed"]:
        return [f"the regression check failed (run #{last['n']}, exit {last['exit']}, output in {last['log']}): "
                f"fix it and {how}"]
    if is_fresh(last, task.project.root) is False:
        return [f"the regression check (run #{last['n']}) passed on an older version of the code: {how} again"]
    return []


def changed_after_failing(items: list) -> dict[int, str]:
    """Passing runs whose check is not the one that failed before for the same brief item: the check may have
    been changed until it passed instead of the code. Run number -> what changed."""
    found, failed = {}, {}
    for r in items:
        purpose = r.get("for")
        if not purpose:
            continue
        if not r["passed"]:
            failed.setdefault(purpose, [])
            if r["command"] not in failed[purpose]:
                failed[purpose].append(r["command"])
        elif failed.get(purpose) and r["command"] not in failed[purpose]:
            found[r["n"]] = f"changed after it failed: was `{failed[purpose][-1]}`, now `{r['command']}`"
    return found


def summary(task: Task) -> dict:
    """What was actually checked, kept apart from what was only reported."""
    info, root = task.info(), task.project.root
    ran = []
    changed = changed_after_failing(info.get("runs", []))
    # the latest critic's word on each run: one it found empty no longer counts for its item
    judged: dict[str, str] = {}
    for r in info.get("reviews", []):
        judged |= r.get("runs_judged", {})
    for r in info.get("runs", []):
        fresh = is_fresh(r, root)
        empty = judged.get(str(r["n"])) == "does not check"
        ran.append({"n": r["n"], "command": r["command"], "for": r.get("for", ""),
                    "passed": r["passed"], "code": {True: "current", False: "stale", None: "unknown"}[fresh]}
                   | ({"not_a_check": True} if r.get("trivial") or empty else {})
                   | ({"critic": "does not check"} if empty else {})
                   | ({"check_changed": changed[r["n"]]} if r["n"] in changed else {}))
    critics = [{"round": r["round"], "worst": r["worst"],
                "answer": ("run by autoharness: " + r["ran"]) if r.get("ran") else
                          "from the critic's answer file" if r.get("answer_id") else "retold by the agent"}
               for r in info.get("reviews", []) if r["by"] == "critic"]
    reported = [f"verify record {c['kind']}: {'passed' if c['passed'] else 'failed'} (evidence #{c['evidence']})"
                for c in info.get("checks", [])]
    reported += [f"arbiter check on `{a['part']}`: {'passed' if a['checked']['passed'] else 'failed'}: "
                 f"{a['checked']['result']}" for a in info.get("arbitrations", [])
                 if a.get("checked") and "run" not in a["checked"]]
    reported += [f"deviation from the arbiter check on `{a['part']}`: {d}" for a in info.get("arbitrations", [])
                 for d in (a.get("checked") or {}).get("deviations", [])]
    reported += [f"self review round {r['round']}: worst {r['worst']}"
                 for r in info.get("reviews", []) if r["by"] == "self"]
    covered = next((r for r in reversed(info.get("reviews", [])) if r.get("coverage")), None)
    coverage = dict(covered["coverage"]) if covered else {}
    judged = next((e for e in info.get("evidence", []) if covered and e["n"] == covered["result"]), None)
    if judged and "tree_state" in judged and is_fresh({"fingerprint": judged}, root) is False:
        # the critic judged a diff the code has moved on from: its verdicts are as stale as an old run
        coverage = {k: f"{v} (stale)" for k, v in coverage.items()}
    items = []
    for item in brief_items(info.get("brief", {})):
        runs = [r for r in ran if r["for"] in (item["id"], PRESERVE if item["id"].startswith("P") else None)]
        good = [r for r in runs if r["passed"] and r["code"] != "stale" and not r.get("not_a_check")]
        items.append({"id": item["id"], "text": item["text"],
                      "run": f"run #{good[-1]['n']} passed" if good else
                             f"run #{runs[-1]['n']} {_why_not(runs[-1])}" if runs else "",
                      "critic": coverage.get(item["id"], "")})
    unchecked = [i["id"] for i in items if not i["run"].endswith("passed") and i["critic"] != "met"]
    return {"ran": ran, "critic": critics, "reported_only": reported, "brief_items": items,
            "unchecked_items": unchecked,
            "unrequested": next((r["unrequested"] for r in reversed(info.get("reviews", []))
                                 if r.get("unrequested")), "")}


def _why_not(r: dict) -> str:
    return ("not a check, says the critic" if r.get("critic") else "not a check") if r.get("not_a_check") else "failed" if not r["passed"] else "stale"
