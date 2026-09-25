"""Record measured task runs and compare matched baseline/harness cases."""
from __future__ import annotations

import json
import math
import re
import subprocess
import time
from pathlib import Path
from statistics import mean

from autoharness.env import HarnessError, Project, ensure_state_dir
from autoharness import telemetry


def _path(project: Project) -> Path:
    return project.state / "evals.jsonl"


def record(project: Project, case: str, variant: str, passed: str, seconds: float | None,
           tokens: int | None = None, cost: float | None = None, task_id: str | None = None,
           tool_calls: int | None = None) -> dict:
    case = case.strip()
    if not case:
        raise HarnessError("--case must name a non-empty task shared by baseline and harness runs")
    if variant not in ("baseline", "harness"):
        raise HarnessError("--variant must be baseline or harness")
    if passed not in ("yes", "no"):
        raise HarnessError("--passed must be yes or no")
    if task_id:
        if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", task_id):
            raise HarnessError("--task-id must be an autoharness task ID")
        task = project.task(task_id)
        info = task.info()
        if seconds is None:
            if info.get("started_epoch") is None or info.get("finished_epoch") is None:
                raise HarnessError("this task has no completed duration; supply --seconds")
            seconds = info["finished_epoch"] - info["started_epoch"]
        cli = telemetry.summary(project.root, task.id)
        installed = {"mcp_installed": info.get("mcp_installed"),
                     "skills_installed": info.get("skills_installed")}
    else:
        cli = None
        installed = {"mcp_installed": None, "skills_installed": None}
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        raise HarnessError("--seconds must be a finite, non-negative number (or supply a completed --task-id)")
    if tokens is not None and tokens < 0:
        raise HarnessError("--tokens must be non-negative")
    if tool_calls is not None and (not isinstance(tool_calls, int) or isinstance(tool_calls, bool) or tool_calls < 0):
        raise HarnessError("--tool-calls must be a non-negative integer")
    if cost is not None and (not math.isfinite(cost) or cost < 0):
        raise HarnessError("--cost must be a finite, non-negative number")
    entry = {"case": case, "variant": variant, "passed": passed == "yes", "seconds": round(seconds, 6),
             "tokens": tokens, "cost": cost, "tool_calls": tool_calls, "task_id": task_id, "cli": cli,
             "recorded_epoch": time.time(), "assessment": "reported", **installed}
    ensure_state_dir(project.state)
    from autoharness import transactions
    with transactions.locked(project.root), _path(project).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry | ({"cli_calls": cli["cli_calls"]} if cli else {"cli_calls": None})


def _average(entries: list[dict], key: str) -> float | None:
    values = [entry.get(key) for entry in entries]
    return round(mean(values), 6) if values and all(value is not None for value in values) else None


def _difference(harness: float | None, baseline: float | None) -> float | None:
    return round(harness - baseline, 6) if harness is not None and baseline is not None else None


def _nonnegative_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def batch(project: Project, dataset: Path, variant: str, command: list[str], timeout: float = 300) -> dict:
    """Run an external JSON-in/JSON-out agent once per case and record measured results."""
    if variant not in ("baseline", "harness"):
        raise HarnessError("--variant must be baseline or harness")
    if not command:
        raise HarnessError("eval batch needs a runner after --")
    if not _nonnegative_number(timeout) or timeout == 0:
        raise HarnessError("--timeout must be a positive number")
    try:
        lines = dataset.read_text("utf-8").splitlines()
    except OSError as error:
        raise HarnessError(f"cannot read dataset: {error}") from error
    cases, names = [], set()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            raise HarnessError(f"dataset line {number} is not JSON") from error
        if (not isinstance(item, dict) or not isinstance(item.get("case"), str) or
            not item["case"].strip() or item["case"] in names or "input" not in item):
            raise HarnessError(f"dataset line {number} needs a unique case and input")
        names.add(item["case"])
        cases.append(item)
    if not cases:
        raise HarnessError("dataset has no cases")
    measurements = []
    for item in cases:
        started = time.monotonic()
        try:
            proc = subprocess.run(command, input=json.dumps(item, ensure_ascii=False) + "\n",
                                  capture_output=True, text=True, cwd=project.root, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HarnessError(f"runner failed for {item['case']!r}: {error}") from error
        seconds = time.monotonic() - started
        if proc.returncode:
            raise HarnessError(f"runner exited {proc.returncode} for {item['case']!r}")
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError as error:
            raise HarnessError(f"runner returned invalid JSON for {item['case']!r}") from error
        if (not isinstance(result, dict) or not isinstance(result.get("passed"), bool) or
            (result.get("tokens") is not None and
             (not isinstance(result["tokens"], int) or isinstance(result["tokens"], bool) or result["tokens"] < 0)) or
            (result.get("tool_calls") is not None and
             (not isinstance(result["tool_calls"], int) or isinstance(result["tool_calls"], bool) or result["tool_calls"] < 0)) or
            (result.get("cost") is not None and not _nonnegative_number(result["cost"]))):
            raise HarnessError(f"runner returned invalid measurements for {item['case']!r}")
        task_id = result.get("task_id")
        if task_id is not None:
            if not isinstance(task_id, str) or not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", task_id):
                raise HarnessError(f"runner returned invalid task ID for {item['case']!r}")
            task = project.task(task_id)
            if task.info().get("finished_epoch") is None:
                raise HarnessError(f"runner task {task_id} is not completed")
        measurements.append((item["case"], "yes" if result["passed"] else "no", seconds,
                             result.get("tokens"), result.get("cost"), task_id,
                             result.get("tool_calls")))
    runs = [record(project, case, variant, passed, seconds, tokens, cost, task_id, tool_calls)
            for case, passed, seconds, tokens, cost, task_id, tool_calls in measurements]
    return {"status": "ok", "variant": variant, "recorded": len(runs), "cases": [r["case"] for r in runs]}


def compare(project: Project) -> dict:
    path = _path(project)
    rows = []
    if path.exists():
        for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if line.strip():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as error:
                    raise HarnessError(f"{path}:{number} is not valid JSON ({error})") from error
                if (not isinstance(entry, dict) or
                    not isinstance(entry.get("case"), str) or not entry["case"].strip() or
                    entry.get("variant") not in ("baseline", "harness") or
                    not isinstance(entry.get("passed"), bool) or
                    not _nonnegative_number(entry.get("seconds")) or
                    any(key not in entry or (entry[key] is not None and not _nonnegative_number(entry[key]))
                        for key in ("tokens", "cost")) or
                    (entry.get("tool_calls") is not None and not _nonnegative_number(entry["tool_calls"]))):
                    raise HarnessError(f"{path}:{number} is not a valid eval record")
                rows.append(entry)
    groups: dict[str, dict[str, list[dict]]] = {}
    for entry in rows:
        groups.setdefault(entry["case"], {"baseline": [], "harness": []})[entry["variant"]].append(entry)
    details = []
    unpaired = []
    for name in sorted(groups):
        sides = groups[name]
        if not sides["baseline"] or not sides["harness"]:
            unpaired.append(name)
            continue
        detail = {"case": name}
        for variant in ("baseline", "harness"):
            runs = sides[variant]
            detail[variant] = {"runs": len(runs), "pass_rate": round(mean(int(r["passed"]) for r in runs), 6),
                               "seconds": _average(runs, "seconds"), "tokens": _average(runs, "tokens"),
                               "cost": _average(runs, "cost"), "tool_calls": _average(runs, "tool_calls")}
        details.append(detail)
    if not details:
        return {"status": "ok", "paired_cases": 0, "unpaired_cases": unpaired,
                "baseline": None, "harness": None, "delta": None, "cases": []}
    totals = {}
    for variant in ("baseline", "harness"):
        totals[variant] = {key: _average([d[variant] for d in details], key)
                           for key in ("pass_rate", "seconds", "tokens", "cost", "tool_calls")}
        totals[variant]["runs"] = sum(d[variant]["runs"] for d in details)
    delta = {key: _difference(totals["harness"][key], totals["baseline"][key])
             for key in ("pass_rate", "seconds", "tokens", "cost", "tool_calls")}
    return {"status": "ok", "paired_cases": len(details), "unpaired_cases": unpaired,
            "baseline": totals["baseline"], "harness": totals["harness"], "delta": delta, "cases": details}
