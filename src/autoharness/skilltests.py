"""Testing a skill the agent wrote: does an agent with it do the work better than one without?

A skill written from what the author read can still teach nothing, or teach the wrong thing. So the
same realistic tasks are done twice by fresh agents, once with the skill and once without, and a
judge that is told neither which copy had the skill compares each pair. The labels A and B are drawn
per task and stay with autoharness until the judge's answer is recorded.
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path

from autoharness import answers
from autoharness.env import HarnessError, Task

MAX_TASKS = 3
PICK_LINE = re.compile(r"^\W*T(\d+)\W+(A|B|same)\b\W*(.*)$", re.MULTILINE | re.IGNORECASE)


def _state(task: Task, name: str) -> dict:
    return task.info().get("skill_tests", {}).get(name, {})


def _store(task: Task, name: str, state: dict) -> None:
    task._update_info(skill_tests=task.info().get("skill_tests", {}) | {name: state})


def start(task: Task, name: str, skill_dir: Path, tasks: list[str]) -> dict:
    """A new round: one pair of runs per task, with the skill and without it, under labels drawn at random."""
    if not (skill_dir / "SKILL.md").is_file():
        raise HarnessError(f"no skill {name!r} at {skill_dir}: create it with `autoharness skill new` first")
    tasks = [t.strip() for t in tasks if t.strip()]
    if not tasks:
        state = _state(task, name)
        if state and "result" not in state:   # the round is still open: show its runs again
            return {"round": state["round"], "runs": state["runs"], "repeated": True}
        tasks = state.get("tasks", [])        # the skill changed since: the same tasks, a new round
    if not tasks:
        raise HarnessError("give 1-3 test tasks: --task \"<a realistic request this skill is for>\" (repeat --task)")
    if len(tasks) > MAX_TASKS:
        raise HarnessError(f"at most {MAX_TASKS} test tasks per round; pick the ones that differ the most")
    n = _state(task, name).get("round", 0) + 1
    base = task.dir / "skill-tests" / name / f"round-{n}"
    with_skill = [random.choice("AB") for _ in tasks]
    skill_md = skill_dir.resolve() / "SKILL.md"   # absolute: a subagent may start in another folder
    runs = []
    for i, (text, label) in enumerate(zip(tasks, with_skill), 1):
        for side in "AB":
            out_dir = base / f"t{i}" / side
            out_dir.mkdir(parents=True, exist_ok=True)
            runs.append({"task": f"T{i}", "label": side, "dir": str(out_dir),
                         "prompt": runner_prompt(text, out_dir, skill_md if side == label else None)})
    _store(task, name, {"round": n, "tasks": tasks, "with_skill": with_skill, "dir": str(base), "runs": runs,
                        "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
    task.log(f"skill test of `{name}`, round {n}: {len(tasks)} task(s)")
    return {"round": n, "runs": runs}


def runner_prompt(text: str, out_dir: Path, skill_md: Path | None) -> str:
    how = (f"Before you start, read the skill at {skill_md} and the files it points to, and work the way it "
           "says.\n\n" if skill_md else
           "Work from what you know and what you can look up. Do not open any SKILL.md file in this project.\n\n")
    # the notes go to a judge who must not learn which run had the skill
    return (f"{how}Task:\n{text}\n\nPut everything you produce in {out_dir} and nowhere else. Add a file "
            "notes.md there about the work itself: the steps you took, what you checked and anything you were "
            "unsure about. A reviewer will compare the work with another agent's, so keep the notes to the "
            "work rather than how you were briefed. Do not change other files in the project.")


def judge_prompt(task: Task, name: str) -> dict:
    """The blind comparison of the round's pairs; refuses while a run left nothing behind."""
    state = _state(task, name)
    if not state:
        raise HarnessError(f"no test round for {name!r}: run `autoharness skill test {name} --task \"...\"` first")
    base = Path(state["dir"])
    empty = [f"T{i}/{side}" for i in range(1, len(state["tasks"]) + 1) for side in "AB"
             if not any(p.is_file() for p in (base / f"t{i}" / side).rglob("*"))]
    if empty:
        raise HarnessError(f"these runs left nothing in their folder yet: {', '.join(empty)}; run their prompts "
                           "first (`autoharness skill test " + name + "` without --task shows them again)")
    pairs = "\n\n".join(f"T{i}. Task: {text}\n   A: {base / f't{i}' / 'A'}\n   B: {base / f't{i}' / 'B'}"
                        for i, text in enumerate(state["tasks"], 1))
    prompt = (
        "Two agents did each task below on their own; their work is in folders A and B. Judge each pair: whose "
        "work does the task better, as the person who asked would see it? Open what they produced (run it, render "
        "it, read it), not only their notes. Size and effort do not count, only how well the task is done. "
        "If a note says how an agent was instructed, ignore that and judge the work.\n\n"
        f"{pairs}\n\n"
        "For each task write one line `T<n>: A`, `T<n>: B` or `T<n>: same`, then on the next lines what the weaker "
        "one got wrong or missed that the stronger one did not, most important first. End with one line "
        "`VERDICT: done`.")
    nonce = answers.issue(task, "skilltest", f"{name}:round-{state['round']}")
    return {"answer_id": nonce, "prompt": prompt + answers.instruction(nonce)}


def record(task: Task, name: str, path: Path) -> dict:
    """Read the judge's answer and score the round for the skill: passed when it won a task and lost none."""
    state = _state(task, name)
    if not state:
        raise HarnessError(f"no test round for {name!r}")
    answer = answers.read(task, path, "skilltest", f"{name}:round-{state['round']}")
    picks: dict[int, str] = {}
    for n, pick, _ in PICK_LINE.findall(answer["text"]):   # the first line per task is the pick; later ones explain
        picks.setdefault(int(n), "same" if pick.lower() == "same" else pick.upper())
    missing = [f"T{i}" for i in range(1, len(state["tasks"]) + 1) if i not in picks]
    if missing:
        raise HarnessError(f"the judge named no winner for {', '.join(missing)}: ask it to finish its answer")
    outcome = []
    for i, label in enumerate(state["with_skill"], 1):
        pick = picks[i]
        outcome.append("tie" if pick == "same" else "skill" if pick == label else "baseline")
    wins, losses = outcome.count("skill"), outcome.count("baseline")
    result = {"wins": wins, "losses": losses, "ties": outcome.count("tie"), "per_task": outcome,
              "passed": wins > 0 and losses == 0, "answer_file": answers.consume(task, answer)}
    _store(task, name, state | {"result": result})
    task.log(f"skill test of `{name}`, round {state['round']}: {wins} won, {losses} lost, {result['ties']} tied")
    return result | {"with_skill": state["with_skill"]}


def status(task: Task | None, name: str) -> dict:
    """The last result for a skill in this task: {} when it was never tested."""
    return _state(task, name).get("result", {}) if task else {}
