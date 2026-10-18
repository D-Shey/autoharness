import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from autoharness import answers, cli, reviewers, runs
from autoharness.env import Project

PY = f'"{sys.executable}"'
OK = f'{PY} -c "print(42)"'
FAIL = f'{PY} -c "import sys; print(7); sys.exit(3)"'
BRIEF = ["brief", "--goal", "1 totals row added; 2 title fixed", "--inputs", "the report", "--constraints", "none",
         "--verify", "run the tests", "--candidates", "none", "--gaps", "none: tools at hand"]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("AUTOHARNESS_HOME", str(home / ".autoharness"))
    monkeypatch.setenv("AUTOHARNESS_AGENT", "claude")
    monkeypatch.setenv("AUTOHARNESS_OFFLINE", "1")
    monkeypatch.delenv(reviewers.ENV_FLAG, raising=False)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    git = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)  # noqa: E731
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (root / "a.py").write_text("x = 1\n", "utf-8")
    git("add", "a.py")
    git("commit", "-qm", "init")
    p = Project(root, "claude")
    p.git = git
    return p


def ah(capsys, project, *argv):
    code = cli.main(["--project", str(project.root), *argv])
    return code, json.loads(capsys.readouterr().out)


def briefed(capsys, project, *extra):
    ah(capsys, project, "init", "fix the quarterly report")
    code, res = ah(capsys, project, *BRIEF, *extra)
    assert code == 0, res
    return res


# --- verify run: autoharness runs the check and binds it to the code ---------------------

def test_verify_run_decides_by_exit_code_and_keeps_the_output(project, capsys):
    ah(capsys, project, "init", "fix the quarterly report")
    code, res = ah(capsys, project, "verify", "run", OK)
    assert code == 1 and "brief first" in res["error"]   # a check before the brief would open the gate
    ah(capsys, project, *BRIEF)
    code, res = ah(capsys, project, "verify", "run", OK, "--name", "tests", "--for", "B1")
    assert code == 0 and res["status"] == "passed" and res["run"]["exit"] == 0 and "42" in res["run"]["tail"]
    assert res["run"]["fingerprint"]["base_sha"] and "42" in Path(res["run"]["log"]).read_text("utf-8")
    code, res = ah(capsys, project, "verify", "run", FAIL)
    assert code == 1 and res["status"] == "failed" and res["run"]["exit"] == 3 and not res["run"]["passed"]
    assert [r["passed"] for r in project.task().info()["runs"]] == [True, False]


def test_a_run_goes_stale_when_the_code_changes_but_not_when_it_is_committed(project, capsys):
    briefed(capsys, project)
    (project.root / "a.py").write_text("x = 2\n", "utf-8")
    ah(capsys, project, "verify", "run", OK)
    item = project.task().info()["runs"][-1]
    assert runs.is_fresh(item, project.root) is True
    project.git("commit", "-qam", "change")
    assert runs.is_fresh(item, project.root) is True     # same code, now committed
    (project.root / "b.py").write_text("y = 1\n", "utf-8")
    assert runs.is_fresh(item, project.root) is False    # a new file the run never saw
    (project.root / "b.py").unlink()
    (project.root / "a.py").write_text("x = 3\n", "utf-8")
    assert runs.is_fresh(item, project.root) is False


def test_the_project_lock_is_free_while_a_check_runs(project, capsys):
    briefed(capsys, project)
    nested = f'{PY} -m autoharness --project "{project.root}" status'
    code, res = ah(capsys, project, "verify", "run", nested, "--timeout", "25")
    assert code == 0 and '"task"' in Path(res["run"]["log"]).read_text("utf-8"), res["run"]["tail"]


def test_verify_record_and_guide_still_take_a_kind(project, capsys):
    code, res = ah(capsys, project, "verify", "guide", "browser")
    assert code == 0 and "brief_verify" in res
    code, res = ah(capsys, project, "verify", "guide", "pytest -q")
    assert code == 1 and "takes a kind" in res["error"]


def test_check_run_records_the_arbiter_check_by_exit_code(project, capsys):
    briefed(capsys, project)
    task = project.task()
    task._update_info(arbitrations=[{"n": 1, "verdict": "fix", "after_round": 0, "part": "totals", "check": "c"}])
    code, res = ah(capsys, project, "check", "--run", FAIL, "--pass", "yes")
    assert code == 1 and "exit code decides" in res["error"]
    code, res = ah(capsys, project, "check")
    assert code == 1 and "--run" in res["error"]
    code, res = ah(capsys, project, "check", "--run", FAIL)
    checked = res["arbitration"]["checked"]
    assert code == 0 and checked["passed"] is False and checked["run"] == 1 and "exited 3" in checked["result"]
    assert task.info()["runs"][0]["for"] == "arbiter"


# --- brief --preserve: a regression check before teardown ---------------------------------

def test_preserve_needs_a_fresh_passing_regression_run_before_teardown(project, capsys):
    res = briefed(capsys, project, "--preserve", "existing exports keep working")
    assert "--for preserve" in res["next"] and "P1: existing exports keep working" in res["brief_items"]
    code, res = ah(capsys, project, "teardown")
    assert code == 1 and any("verify run" in p for p in res["problems"])
    ah(capsys, project, "verify", "run", FAIL, "--for", "preserve")
    code, res = ah(capsys, project, "teardown")
    assert code == 1 and any("failed" in p for p in res["problems"])
    ah(capsys, project, "verify", "run", OK, "--for", "preserve")
    (project.root / "a.py").write_text("x = 5\n", "utf-8")
    code, res = ah(capsys, project, "teardown")
    assert code == 1 and any("older version" in p for p in res["problems"])
    ah(capsys, project, "verify", "run", OK, "--for", "preserve")
    code, res = ah(capsys, project, "teardown")
    assert code == 0, res


def test_brief_items_split_the_goal_and_what_to_preserve():
    items = runs.brief_items({"goal": "Quality: 1 totals row; 2 title fixed", "preserve": "exports\nimports"})
    assert [(i["id"], i["text"]) for i in items] == [
        ("B1", "Quality"), ("B2", "totals row"), ("B3", "title fixed"), ("P1", "exports"), ("P2", "imports")]
    assert runs.brief_items({"goal": "one thing; with a semicolon"}) == [{"id": "B1", "text": "one thing; with a semicolon"}]
    texts = lambda goal: [i["text"] for i in runs.brief_items({"goal": goal})]  # noqa: E731
    assert texts("1. retry up to 3. Then log it 2. report it") == ["retry up to 3. Then log it", "report it"]
    assert texts("keep 2 copies; 1 a; 2 b") == ["keep 2 copies", "a", "b"]
    assert texts("1) a\n2) b") == ["a", "b"]


# --- the honest summary at teardown -------------------------------------------------------

def test_teardown_says_what_was_run_reported_or_never_checked(project, capsys, tmp_path):
    briefed(capsys, project)
    ah(capsys, project, "verify", "run", OK, "--for", "B1")
    report = tmp_path / "check.log"
    report.write_text("all fine", "utf-8")
    ah(capsys, project, "verify", "record", "logs", "--passed", "yes", "--evidence", str(report))
    (project.root / "a.py").write_text("x = 9\n", "utf-8")   # the run is now on older code
    code, res = ah(capsys, project, "teardown")
    checked = res["checked"]
    assert code == 0 and checked["ran"][0]["code"] == "stale" and checked["ran"][0]["passed"]
    assert checked["reported_only"] and "verify record logs" in checked["reported_only"][0]
    assert checked["unchecked_items"] == ["B1", "B2"]     # B1's only run is stale
    assert "never checked: B1, B2" in res["next"]


# --- the critic: a verdict per brief item, and unrequested work ---------------------------

def critic_ready(capsys, project, tmp_path):
    briefed(capsys, project, "--review", "critic")
    (tmp_path / "report.md").write_text("# Q3\n", "utf-8")
    ah(capsys, project, "evidence", "add", str(tmp_path / "report.md"), "--kind", "result")


COVERED = ("1. title still says Q2 (major, fixable)\n\n## Brief coverage\nB1: met - totals row in line 4\n"
           "B2: missing - title unchanged\n\n## Unrequested\n- reformatted the whole table\n\n"
           "## Previous review\n- none\n\nVERDICT: major\n")


def test_the_critic_judges_each_brief_item_and_names_unrequested_work(project, capsys, tmp_path):
    critic_ready(capsys, project, tmp_path)
    code, res = ah(capsys, project, "critic")
    assert "B1: totals row added" in res["prompt"] and "B2: title fixed" in res["prompt"]
    assert '"Brief coverage"' in res["prompt"] and '"Unrequested"' in res["prompt"]
    assert res["prompt"].index('"Brief coverage"') < res["prompt"].index("VERDICT: blocker | major | minor | none")
    f = tmp_path / "a.txt"
    f.write_text(f"ANSWER-ID: {res['answer_id']}\n{COVERED}", "utf-8")
    code, res = ah(capsys, project, "review", "--answer-file", str(f))
    review = res["review"]
    assert code == 0 and review["coverage"] == {"B1": "met", "B2": "missing"}
    assert review["unrequested"] == "- reformatted the whole table"
    assert review["diff"] == "1. title still says Q2 (major, fixable)"   # the sections are not points
    assert "B2 missing" in res["next"] and "unrequested" in res["next"]
    assert runs.summary(project.task())["unchecked_items"] == ["B2"]


def test_for_must_name_a_brief_item(project, capsys):
    briefed(capsys, project)
    code, res = ah(capsys, project, "verify", "run", OK, "--for", "B9")
    assert code == 1 and "B1, B2" in res["error"]
    code, res = ah(capsys, project, "verify", "run", OK, "--for", "b2")
    assert code == 0 and res["run"]["for"] == "B2"
    code, res = ah(capsys, project, "verify", "run", OK, "--passed", "yes")
    assert code == 1 and "exit code" in res["error"]


def test_a_critics_item_verdicts_go_stale_with_the_diff_it_judged(project, capsys, tmp_path):
    briefed(capsys, project, "--review", "critic")
    (project.root / "a.py").write_text("x = 2\n", "utf-8")
    ah(capsys, project, "evidence", "diff")
    code, res = ah(capsys, project, "critic")
    f = tmp_path / "a.txt"
    f.write_text(f"ANSWER-ID: {res['answer_id']}\nNo differences.\n\n## Brief coverage\nB1: met - x\nB2: met - y\n\n"
                 "VERDICT: none\n", "utf-8")
    assert ah(capsys, project, "review", "--answer-file", str(f))[0] == 0
    assert runs.summary(project.task())["unchecked_items"] == []
    (project.root / "a.py").write_text("x = 3\n", "utf-8")
    summary = runs.summary(project.task())
    assert summary["unchecked_items"] == ["B1", "B2"] and summary["brief_items"][0]["critic"] == "met (stale)"


def test_answer_sections_are_optional_and_none_means_none(project, capsys, tmp_path):
    critic_ready(capsys, project, tmp_path)
    task = project.task()
    nonce = answers.issue(task, "critic", "result #1")
    f = tmp_path / "a.txt"
    f.write_text(f"ANSWER-ID: {nonce}\n1. a (minor)\n\nUnrequested: none\n\nVERDICT: minor\n", "utf-8")
    answer = answers.read(task, f, "critic", "result #1")
    assert answer["items"] == ["a (minor)"] and answer["unrequested"] == "" and answer["coverage"] == {}


def test_critic_run_starts_the_reviewer_and_records_its_answer(project, capsys, tmp_path, monkeypatch):
    critic_ready(capsys, project, tmp_path)
    seen = {}

    def fake(agent, root, prompt_file, answer_file, timeout, answer_id):
        prompt = Path(prompt_file).read_text("utf-8")
        seen.update(agent=agent, root=root)
        nonce = prompt.rsplit("ANSWER-ID: ", 1)[1].strip()
        assert nonce == answer_id
        answer_file.write_text(f"ANSWER-ID: {nonce}\n1. typo (minor)\nVERDICT: minor\n", "utf-8")
        return answer_file.read_text("utf-8")

    monkeypatch.setattr(reviewers, "run", fake)
    code, res = ah(capsys, project, "critic", "--run", "codex")
    assert code == 0 and seen == {"agent": "codex", "root": project.root}
    assert res["review"]["by"] == "critic" and res["review"]["ran"] == "codex" and res["review"]["worst"] == "minor"
    assert res["review"]["answer_id"]
    code, res = ah(capsys, project, "teardown")
    assert code == 0 and res["checked"]["critic"][0]["answer"] == "run by autoharness: codex"
    code = cli.main(["--project", str(project.root), "critic", "--run", "claude", "--dispute", "x"])
    assert code == 1


def test_reviewer_commands_are_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(reviewers.shutil, "which", lambda name: f"/bin/{name}")
    prompt = tmp_path / "p.txt"
    prompt.write_text("judge it", "utf-8")
    argv, stdin = reviewers.command("claude", tmp_path, prompt, tmp_path / "a.txt")
    assert "-p" in argv and argv[argv.index("--tools") + 1] == "Read,Grep,Glob" and stdin == "judge it"
    assert "--strict-mcp-config" in argv
    assert "bypassPermissions" not in argv and "--dangerously-skip-permissions" not in argv
    argv, stdin = reviewers.command("codex", tmp_path, prompt, tmp_path / "a.txt")
    assert argv[argv.index("--sandbox") + 1] == "read-only" and argv[-1] == "-" and stdin == "judge it"
    argv, stdin = reviewers.command("agy", tmp_path, prompt, tmp_path / "a.txt")
    assert "--sandbox" in argv and argv[argv.index("--mode") + 1] == "plan" and str(prompt) in argv[2]
    monkeypatch.setattr(reviewers.shutil, "which", lambda name: None)
    with pytest.raises(Exception, match="not on PATH"):
        reviewers.command("claude", tmp_path, prompt, tmp_path / "a.txt")


def test_reviewer_run_strips_the_authors_session(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw["env"])
        return subprocess.CompletedProcess(argv, 0, b"ANSWER-ID: ah-0\nVERDICT: none\n", b"")

    monkeypatch.setattr(reviewers.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(reviewers.subprocess, "run", fake_run)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "author")
    prompt = tmp_path / "p.txt"
    prompt.write_text("judge it", "utf-8")
    text = reviewers.run("claude", tmp_path, prompt, tmp_path / "a.txt")
    assert "VERDICT: none" in text and (tmp_path / "a.txt").read_text("utf-8").startswith("ANSWER-ID")
    assert seen[reviewers.ENV_FLAG] == "1" and "CLAUDECODE" not in seen and "CLAUDE_CODE_SESSION_ID" not in seen


# --- the task belongs to the session that started it -------------------------------------

def test_another_session_can_neither_change_nor_end_the_task_until_it_claims_it(project, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "owner-1")
    briefed(capsys, project)
    assert project.task().info()["owner"] == "owner-1"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other-2")
    for argv in (["teardown"], ["init", "another task"], ["verify", "run", OK], ["mode", "reviewed"]):
        code, res = ah(capsys, project, *argv)
        assert code == 1 and "belongs to another agent session (owner-1)" in res["error"], argv
        assert "autoharness claim other-2" in res["error"]
    code, res = ah(capsys, project, "status")
    assert code == 0 and res["task"]["owner"] == "owner-1"
    code, res = ah(capsys, project, "claim", "--reason", "the first session is gone")
    assert code == 0 and res["previous_owner"] == "owner-1" and res["owner"] == "other-2"
    assert ah(capsys, project, "verify", "run", OK)[0] == 0
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "owner-1")
    code, res = ah(capsys, project, "teardown")
    assert code == 1 and "(other-2)" in res["error"]
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert ah(capsys, project, "teardown")[0] == 0   # the user's own terminal is not an agent session


def hook(capsys, monkeypatch, project, event):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert cli.main(["--project", str(project.root), "hook"]) == 0
    raw = capsys.readouterr().out
    return json.loads(raw) if raw.strip() else None


def denied(res):
    return res and res["hookSpecificOutput"].get("permissionDecision") == "deny"


def test_hook_hands_another_session_the_task_and_keeps_it_gated(project, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "owner-1")
    briefed(capsys, project)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "other-2", "prompt": "please refactor the module"}
    res = hook(capsys, monkeypatch, project, prompt)
    ctx = res["hookSpecificOutput"]["additionalContext"]
    assert "belongs to another agent session (owner-1)" in ctx and "fix the quarterly report" in ctx
    assert "autoharness claim other-2" in ctx and "brief:" in ctx
    assert hook(capsys, monkeypatch, project, prompt) is None   # told once per task
    write = {"hook_event_name": "PreToolUse", "session_id": "other-2", "tool_name": "Write", "tool_input": {}}
    res = hook(capsys, monkeypatch, project, write)
    assert denied(res) and "owner-1" in res["hookSpecificOutput"]["permissionDecisionReason"]
    for command, blocked in (("autoharness teardown", True), ("autoharness --agent claude init 'x y'", True),
                             ("autoharness status", False), ("autoharness claim other-2", False)):
        call = {"hook_event_name": "PreToolUse", "session_id": "other-2", "tool_name": "Bash",
                "tool_input": {"command": command}}
        assert bool(denied(hook(capsys, monkeypatch, project, call))) is blocked, command
    owner = dict(write, session_id="owner-1")
    assert hook(capsys, monkeypatch, project, owner) is None
    project.task()._update_info(owner="other-2")             # claimed
    assert hook(capsys, monkeypatch, project, write) is None


def test_hook_leaves_alone_only_the_reviewer_of_a_pending_prompt(project, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "owner-1")
    briefed(capsys, project)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    nonce = answers.issue(project.task(), "critic", "result #1")
    call = {"hook_event_name": "PreToolUse", "session_id": "reviewer", "tool_name": "Bash",
            "tool_input": {"command": "git diff"}}
    hook(capsys, monkeypatch, project, dict(call, hook_event_name="UserPromptSubmit", prompt="judge this work now"))
    for flag, passes in (("1", False), ("ah-000000000000", False), (nonce, True)):
        monkeypatch.setenv(reviewers.ENV_FLAG, flag)
        assert (hook(capsys, monkeypatch, project, call) is None) is passes, flag
    answers.issue(project.task(), "critic", "result #1")    # a newer prompt retires the old id
    assert denied(hook(capsys, monkeypatch, project, call))


def test_tasks_without_an_owner_keep_the_old_gate(project, capsys, monkeypatch):
    briefed(capsys, project)
    assert "owner" not in project.task().info()
    write = {"hook_event_name": "PreToolUse", "session_id": "any", "tool_name": "Write", "tool_input": {}}
    assert hook(capsys, monkeypatch, project, write) is None
