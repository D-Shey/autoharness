"""Integration tests for AI-Native judgment, bounded autonomy, and ROI upgrades (C3)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import pytest

from autoharness import cli, consequence, evals, evidence, followups, gateway, memory, ontology, runs, truth
from autoharness.catalog import Catalog
from autoharness.commands import briefing, common, lifecycle
from autoharness.env import Project


def test_critic_prompt_injects_ontology_and_devils_advocate(tmp_path: Path):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    src = proj_root / "src"
    src.mkdir()
    target = src / "app.py"
    target.write_text("def handle():\n    return 'ok'\n", encoding="utf-8")

    ontology.init_ontology(proj_root)
    ontology.add_ontology_entry(
        proj_root,
        "Glossary",
        "**PaymentLedger** — immutable ledger in `src/app.py`",
    )

    proj = Project(proj_root, "claude")
    task = proj.init_task("Build PaymentLedger handler", mode="full")
    task.record_brief({
        "goal": "Implement deterministic PaymentLedger handler",
        "domains": "app: skill python-architecture (searched: python architecture)",
        "method": "clean handler: https://docs.python.org/3/; searched: python handler",
        "inputs": "none",
        "expects": "errors: typed exception",
        "constraints": "none",
        "verify": "pytest tests/test_app.py",
        "candidates": "python-architecture: take, domain layout",
        "gaps": "none: covered",
        "components": "C1: app handler in src/app.py - verify pytest",
    })
    evidence.add(task, target, "result", note="initial handler")

    prompt_data = evidence.critic_prompt(Catalog.load(), task)
    prompt = prompt_data["prompt"]
    assert "Project Ontology 2.0" in prompt
    assert "PaymentLedger" in prompt
    assert "Devil's Advocate & Counter-Evidence Check" in prompt
    assert "Fragile Assumptions" in prompt
    assert "VERDICT: blocker | major | minor | none" in prompt


def test_anti_cognitive_surrender_and_bounded_autonomy(tmp_path: Path, capsys):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    sensitive = proj_root / ".autoharness" / "gateway.json"
    sensitive.parent.mkdir(parents=True)
    sensitive.write_text("{}", encoding="utf-8")

    sim = consequence.simulate_action("gateway_reconfig", str(sensitive), project_root=proj_root)
    assert sim["reversibility_tier"] == "HIGH_RISK"
    assert "judgment_focus" in sim
    assert sim["judgment_focus"]["attention_level"] == "critical"
    assert sim["judgment_focus"]["human_signoff_required"] is True

    manifest = consequence.format_consequence_manifest(sim)
    assert "## Human Judgment Focus (Anti-Cognitive-Surrender)" in manifest

    rc = common.need_approval("gateway_reconfig", sim)
    assert rc == common.EXIT_APPROVAL
    payload = json.loads(capsys.readouterr().out)
    assert "judgment_focus" in payload
    assert payload["judgment_focus"]["counter_question"]

    bounds = consequence.parse_autonomy_bounds("allowed_paths=src/,tests/; max_tokens=10000; escalate_on=delete,push")
    ok_check = consequence.check_autonomy_bounds(
        proj_root, target="src/module.py", bounds=bounds, tokens_used=5000, action_type="edit_file"
    )
    assert ok_check["bounded"] is True
    assert ok_check["within_bounds"] is True

    bad_check = consequence.check_autonomy_bounds(
        proj_root, target=".env", bounds=bounds, tokens_used=25000, action_type="git_push"
    )
    assert bad_check["within_bounds"] is False
    assert len(bad_check["violations"]) == 3


def test_brief_bounds_assumptions_and_lifecycle_postmortem(tmp_path: Path, monkeypatch, capsys):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    src = proj_root / "src"
    src.mkdir()
    (src / "worker.py").write_text("def work():\n    return 1\n", encoding="utf-8")
    monkeypatch.chdir(proj_root)

    proj = Project(proj_root, "claude")
    catalog = Catalog.load()
    t1 = proj.init_task("Build worker", mode="full")
    args = argparse.Namespace(
        goal="Worker runs safely",
        domains="layout: create test-layout (searched: test); checks: create test-checks (searched: test)",
        method="build, then compare: https://example.com/guide/how; searched: how it is done",
        inputs="none",
        expects="retry: exponential backoff - prevents thundering herd on transient failures",
        constraints="none",
        verify="pytest",
        verify_template=None,
        candidates="test-layout: take, worker; test-checks: take, checks",
        gaps="create test-layout: layout; create test-checks: checks",
        fidelity=None,
        components="C1: worker module in src/worker.py - verify pytest",
        techniques="clean-architecture: zero deps",
        review=None,
        preserve=None,
        bounds="allowed_paths=src/; max_tokens=20000; escalate_on=delete",
        assumptions="Queue never overflows `src/worker.py` under burst load",
    )
    rc = briefing.cmd_brief(args, proj, catalog)
    assert rc == 0
    capsys.readouterr()

    info = t1.info()
    assert info["brief"]["bounds"]["max_tokens"] == 20000
    assert info["brief"]["assumptions"] == ["Queue never overflows `src/worker.py` under burst load"]
    t1.teardown("keep")

    followups.add(proj, "Burst load dropped messages in `src/worker.py` queue", task_id="other_task")
    followups.add(proj, "Retry storm in `src/worker.py` queue", task_id="other_task")

    time.sleep(0.02)
    assert cli.main(["init", "Harden worker queue", "--mode", "light"]) == 0
    init_out = json.loads(capsys.readouterr().out)
    assert "relevant_assumptions" in init_out
    assert any("worker.py" in a["text"] for a in init_out["relevant_assumptions"])

    assert cli.main(["teardown", "--keep"]) == 0
    capsys.readouterr()
    truth.run_postmortem(proj_root, min_subsequent_tasks=1)
    time.sleep(0.02)
    assert cli.main(["init", "Follow-up check", "--mode", "light"]) == 0
    capsys.readouterr()
    assert cli.main(["teardown", "--keep"]) == 0
    td_out = json.loads(capsys.readouterr().out)
    assert td_out["postmortem"]["falsified"] == 1


def test_evals_compare_includes_compute_roi_and_workslop(tmp_path: Path):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    proj = Project(proj_root, "claude")

    evals.record(proj, "case-a", "baseline", "yes", seconds=10.0, tokens=1000, cost=0.10, tool_calls=2)
    evals.record(proj, "case-a", "harness", "yes", seconds=12.0, tokens=2000, cost=0.20, tool_calls=5)
    report = evals.compare(proj)

    assert "compute_roi" in report
    assert report["compute_roi"]["harness_pass_per_1k_tokens"] == 0.5
    assert report["compute_roi"]["harness_pass_per_dollar"] == 5.0
    assert report["compute_roi"]["token_ratio_vs_baseline"] == 2.0
    assert report["compute_roi"]["workslop_risk"] is True


def test_memory_cli_and_gateway_integration(tmp_path: Path, monkeypatch, capsys):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    src = proj_root / "src"
    src.mkdir()
    (src / "billing.py").write_text("def invoice():\n    return 42\n", encoding="utf-8")

    monkeypatch.chdir(proj_root)
    assert cli.main(["ontology", "init"]) == 0
    capsys.readouterr()
    assert cli.main(["ontology", "add", "Immutable billing ledger (`src/billing.py`)", "--term", "BillingLedger"]) == 0
    capsys.readouterr()
    assert cli.main(["ontology", "check"]) == 0
    check_out = json.loads(capsys.readouterr().out)
    assert check_out["status"] == "healthy"

    counts = memory.index_project_memory(proj_root)
    assert counts["ontology"] >= 1
    con = memory.get_memory_db(proj_root)
    try:
        hits = memory.search_memory(con, "BillingLedger", kind="ontology")
    finally:
        con.close()
    assert hits and hits[0]["kind"] == "ontology"

    rec = memory.reconcile_memory_drift(proj_root)
    assert rec["ontology"]["valid"] is True

    assert cli.main(["truth", "audit", "--json"]) == 0
    truth_out = json.loads(capsys.readouterr().out)
    assert truth_out["status"] == "healthy"

    gw = gateway.Gateway(proj_root, lambda _m: None, lambda _l: None)
    tool_names = {t["name"] for t in gw._tool_list()}
    assert {"harness_ontology", "harness_truth_audit"} <= tool_names

    onto_res = gw._call({"name": "harness_ontology", "arguments": {"action": "query", "query": "BillingLedger", "raw_json": True}})
    assert not onto_res.get("isError")
    assert "BillingLedger" in onto_res["content"][0]["text"]

    truth_res = gw._call({"name": "harness_truth_audit", "arguments": {"action": "audit", "raw_json": True}})
    assert not truth_res.get("isError")
    assert "healthy" in truth_res["content"][0]["text"]


def test_verify_run_kills_process_tree_on_timeout(tmp_path: Path):
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    proj = Project(proj_root, "claude")
    task = proj.init_task("Timeout tree cleanup", mode="light")
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "spawn_tree.py"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        f"p = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(30)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid), encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    res = runs.run(task, f'"{sys.executable}" "{script}"', timeout=1.0)
    assert res["timed_out"] is True
    assert res["passed"] is False
    assert pid_file.is_file()
    grandchild_pid = int(pid_file.read_text("utf-8").strip())
    # Give OS a moment to finalize process termination, then confirm grandchild is gone
    time.sleep(0.2)
    alive = True
    if os.name == "nt":
        import subprocess
        q = subprocess.run(["tasklist", "/FI", f"PID eq {grandchild_pid}"], capture_output=True, text=True, check=False)
        alive = str(grandchild_pid) in q.stdout
    else:
        try:
            os.kill(grandchild_pid, 0)
        except OSError:
            alive = False
    assert alive is False


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object KILL_ON_JOB_CLOSE verification")
def test_verify_run_kills_grandchild_when_parent_killed_abruptly(tmp_path: Path):
    import subprocess
    import sys

    pid_file = tmp_path / "grandchild.pid"
    child_script = tmp_path / "spawn_sleeper.py"
    child_script.write_text(
        "import pathlib, subprocess, sys, time\n"
        f"p = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(30)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid), encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    runner_script = tmp_path / "run_parent.py"
    runner_script.write_text(
        "import pathlib, sys\n"
        "from autoharness import runs\n"
        f"runs._run_shell_command('\"' + {sys.executable!r} + '\" \"' + {str(child_script)!r} + '\"', pathlib.Path({str(tmp_path)!r}), 30.0)\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    parent = subprocess.Popen([sys.executable, str(runner_script)], env=env)
    try:
        deadline = time.monotonic() + 10.0
        while not pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.is_file(), "grandchild pid file was not written in time"
        grandchild_pid = int(pid_file.read_text("utf-8").strip())
        parent.kill()
        parent.wait(timeout=5)
        time.sleep(0.3)
        q = subprocess.run(["tasklist", "/FI", f"PID eq {grandchild_pid}"], capture_output=True, text=True, check=False)
        assert str(grandchild_pid) not in q.stdout
    finally:
        if parent.poll() is None:
            parent.kill()
        if pid_file.is_file():
            try:
                gpid = int(pid_file.read_text("utf-8").strip())
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(gpid)], capture_output=True, check=False)
            except ValueError:
                pass


def test_hook_allows_process_diagnostics_cleanup_and_rollback_when_pending(tmp_path: Path, capsys, monkeypatch):
    import io
    from autoharness import cli, hooks

    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("AUTOHARNESS_HOME", str(home / ".autoharness"))
    monkeypatch.setenv("AUTOHARNESS_OFFLINE", "1")

    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    proj = Project(proj_root, "agy")
    # Active task owned by another session -> foreign session sits in `pending` state
    monkeypatch.setenv("AUTOHARNESS_SESSION_ID", "other-sess")
    t = proj.init_task("heavy job", mode="full")
    t._update_info(owner="other-sess")

    def call_hook(command: str) -> tuple[int, str]:
        payload = json.dumps({
            "conversationId": "current-sess",
            "workspacePaths": [str(proj_root)],
            "toolCall": {"name": "run_command", "arguments": {"CommandLine": command}},
        })
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")), encoding="utf-8"))
        rc = cli.main(["--project", str(proj_root), "--agent", "agy", "hook"])
        return rc, capsys.readouterr().out.strip()

    tool_name = "PowerShell" if os.name == "nt" else "Bash"
    # Diagnostics and recovery helpers must pass when gated/foreign
    allowed_cmds = [
        "Get-Process | Sort-Object CPU -Descending | Select-Object Id,ProcessName,CPU -First 10 | Format-Table -AutoSize",
        "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name",
        "tasklist",
        "Stop-Process -Id 999999 -Force",
        "taskkill /F /T /PID 999999",
        "git restore src/app.py",
        "git checkout -- src/app.py",
    ]
    for cmd in allowed_cmds:
        rc, out_txt = call_hook(cmd)
        assert rc == 0 and out_txt == "", f"expected allow for {cmd!r}, got {out_txt!r}"

    # Dangerous or mutating commands must still be blocked
    blocked_cmds = [
        "Remove-Item src/app.py",
        "Get-Process | ForEach-Object { $_ }",
        "git restore .autoharness/config.json",
        "taskkill /F /PID 999999; Remove-Item src/app.py",
        "git checkout main",
        "autoharness status | sort -o leak.txt",
    ]
    for cmd in blocked_cmds:
        assert not hooks._is_autoharness_call(tool_name, {"command": cmd}) or "autoharness" in cmd
        rc, out_txt = call_hook(cmd)
        assert rc == 0 and json.loads(out_txt)["decision"] == "deny", f"expected deny for {cmd!r}"


def test_bounded_autonomy_strict_validation_traversal_and_hook_enforcement(tmp_path: Path, capsys, monkeypatch):
    import io

    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("AUTOHARNESS_HOME", str(home / ".autoharness"))
    monkeypatch.setenv("AUTOHARNESS_OFFLINE", "1")

    proj_root = tmp_path / "proj"
    (proj_root / "src").mkdir(parents=True)
    (proj_root / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")

    # Reject malformed bounds instead of silently dropping
    for bad in ("just_a_token", "max_tokens=abc", "max_tokens=0", "allowed_paths="):
        with pytest.raises(ValueError):
            consequence.parse_autonomy_bounds(bad)

    assert cli.main(["--project", str(proj_root), "init", "Bounded test", "--mode", "full"]) == 0
    capsys.readouterr()
    assert cli.main([
        "--project", str(proj_root), "brief",
        "--goal", "Update src only",
        "--domains", "app: create demo (searched: none)",
        "--method", "clean: none",
        "--inputs", "src/ok.py",
        "--expects", "none: clean",
        "--constraints", "none",
        "--verify", "pytest",
        "--candidates", "none",
        "--gaps", "none: demo",
        "--bounds", "max_tokens=abc",
    ]) == 1
    capsys.readouterr()

    # Path traversal and dot-prefix normalization
    assert consequence.check_autonomy_bounds(proj_root, target="../src/secret.py", bounds={"allowed_paths": ["src/"]})["within_bounds"] is False
    assert consequence.check_autonomy_bounds(proj_root, target=".env", bounds={"allowed_paths": ["src/"]})["within_bounds"] is False
    assert consequence.check_autonomy_bounds(proj_root, target="src/ok.py", bounds={"allowed_paths": ["./"]})["within_bounds"] is True
    assert consequence.check_autonomy_bounds(proj_root, target="src/ok.py", bounds={"allowed_paths": ["."]})["within_bounds"] is True

    # Empty eval comparison preserves compute_roi key
    proj = Project(proj_root, "agy")
    empty_eval = evals.compare(proj)
    assert "compute_roi" in empty_eval and empty_eval["compute_roi"] is None

    # Runtime hook enforcement on allowed_paths, shell writes, escalate_on, and max_tokens
    task = proj.task()
    task.record_brief({
        "goal": "Update src only",
        "domains": "app: create demo (searched: none)",
        "method": "clean: none",
        "inputs": "src/ok.py",
        "expects": "none: clean",
        "constraints": "none",
        "verify": "pytest",
        "candidates": "none",
        "gaps": "none: demo",
        "bounds": {"allowed_paths": ["src/"], "max_tokens": 100, "escalate_on": ["drop_table"]},
    })
    task._update_info(owner="sess-bounds")

    def hook_tool(tool_call: dict) -> dict | None:
        payload = json.dumps({"conversationId": "sess-bounds", "workspacePaths": [str(proj_root)], "toolCall": tool_call})
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")), encoding="utf-8"))
        rc = cli.main(["--project", str(proj_root), "--agent", "agy", "hook"])
        assert rc == 0
        raw = capsys.readouterr().out.strip()
        return json.loads(raw) if raw else None

    assert hook_tool({"name": "edit_file", "arguments": {"Path": "src/ok.py"}}) is None
    assert hook_tool({"name": "run_command", "arguments": {"CommandLine": 'Set-Content src/ok.py "hello"'}}) is None
    assert hook_tool({"name": "run_command", "arguments": {"CommandLine": "cd src && rm ok.py"}}) is None
    assert hook_tool({"name": "edit_file", "arguments": {"Path": ".env"}})["decision"] == "deny"
    assert hook_tool({"name": "run_command", "arguments": {"CommandLine": "Set-Content .env SECRET"}})["decision"] == "deny"
    assert hook_tool({"name": "run_command", "arguments": {"CommandLine": "python -c \"open('.env','w').write('x')\""}})["decision"] == "deny"
    assert hook_tool({"name": "run_command", "arguments": {"CommandLine": "git restore .env"}})["decision"] == "deny"
    assert hook_tool({"name": "run_command", "arguments": {"CommandLine": "echo drop_table"}})["decision"] == "deny"

    # Real session transcript size enforces max_tokens without mutating task.json / breaking task.sig
    transcript = tmp_path / "session_transcript.jsonl"
    transcript.write_text("x" * 600, encoding="utf-8")
    over_payload = json.dumps({
        "conversationId": "sess-bounds",
        "workspacePaths": [str(proj_root)],
        "transcriptPath": str(transcript),
        "toolCall": {"name": "edit_file", "arguments": {"Path": "src/ok.py"}},
    })
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(over_payload.encode("utf-8")), encoding="utf-8"))
    assert cli.main(["--project", str(proj_root), "--agent", "agy", "hook"]) == 0
    assert json.loads(capsys.readouterr().out.strip())["decision"] == "deny"
