import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from autoharness import answers, cli, reviewers, runs
from autoharness.env import Project

PY = f'"{sys.executable}"'
OK = f'{PY} -c "import json; print(42)"'
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
    assert code == 1 and "takes one kind" in res["error"]


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
    assert code == 1 and res["status"] == "lessons"
    assert any("check for preserve failed 1 time(s) before" in line for line in res["lessons"])
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


def test_a_run_the_critic_finds_empty_no_longer_counts_for_its_item(project, capsys, tmp_path):
    critic_ready(capsys, project, tmp_path)
    ah(capsys, project, "verify", "run", OK, "--for", "B1")
    ah(capsys, project, "verify", "run", OK, "--for", "B2")
    code, res = ah(capsys, project, "critic")
    assert "#1 for B1" in res["prompt"] and '"Runs"' in res["prompt"]
    f = tmp_path / "a.txt"
    f.write_text(f"ANSWER-ID: {res['answer_id']}\n1. title still says Q2 (major, fixable)\n\n## Runs\n"
                 "#1: does not check - it only prints 42\n#2: checks\n\n## Brief coverage\nB1: partial - x\n"
                 "B2: partial - y\n\nVERDICT: major\n", "utf-8")
    code, res = ah(capsys, project, "review", "--answer-file", str(f))
    assert res["review"]["runs_judged"] == {"1": "does not check", "2": "checks"}
    assert "#1 do not check" in res["next"] and res["review"]["diff"] == "1. title still says Q2 (major, fixable)"
    summary = runs.summary(project.task())
    assert summary["unchecked_items"] == ["B1"]
    assert summary["brief_items"][0]["run"] == "run #1 not a check, says the critic"


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


def test_version_prints_the_package_version(capsys):
    from autoharness import __version__
    with pytest.raises(SystemExit) as done:
        cli.main(["--version"])
    assert done.value.code == 0 and capsys.readouterr().out.strip() == f"autoharness {__version__}"


def test_a_reviewer_without_a_login_is_named_as_such(tmp_path, monkeypatch):
    monkeypatch.setattr(reviewers.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(reviewers.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, b"Not logged in - Please run /login\n", b""))
    prompt = tmp_path / "p.txt"
    prompt.write_text("judge it", "utf-8")
    with pytest.raises(Exception, match="not logged in here.*child processes"):
        reviewers.run("claude", tmp_path, prompt, tmp_path / "a.txt")


def test_critic_run_that_cannot_start_points_to_a_subagent(project, capsys, monkeypatch):
    briefed(capsys, project)
    ah(capsys, project, "verify", "run", OK, "--for", "B1")
    (project.root / "out.txt").write_text("result\n", "utf-8")
    ah(capsys, project, "evidence", "add", str(project.root / "out.txt"), "--kind", "result")
    monkeypatch.setattr(reviewers, "run", lambda *a, **k: (_ for _ in ()).throw(reviewers.HarnessError("no login")))
    code, res = ah(capsys, project, "critic", "--run", "claude")
    assert code == 1 and res["error"] == "no login" and "fresh subagent" in res["next"]


def test_a_code_critic_checks_an_interface_the_task_left_open(project, capsys):
    from autoharness import evidence
    from autoharness.catalog import Catalog
    briefed(capsys, project)
    (project.root / "out.txt").write_text("result\n", "utf-8")
    ah(capsys, project, "evidence", "add", str(project.root / "out.txt"), "--kind", "result")
    prompt = evidence.critic_prompt(Catalog.load(), project.task())["prompt"]
    assert "the author's own guess" in prompt and "major difference" in prompt


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


def test_verify_run_takes_the_command_from_an_args_file(project, capsys, tmp_path):
    briefed(capsys, project)
    spec = tmp_path / "cmd.json"
    spec.write_text(json.dumps({"command": 'python -c "import json; print(1)"'}), "utf-8")
    code, res = ah(capsys, project, "verify", "run", "--args-file", str(spec))
    assert code == 0 and res["run"]["passed"]


def test_verify_run_explains_a_command_the_shell_split(project, capsys):
    briefed(capsys, project)
    code, res = ah(capsys, project, "verify", "run", "python", "-c", "print(1)")
    assert code == 1 and "in pieces" in res["error"] and "--args-file" in res["error"]


# --- fixes from a long visual task's report ------------------------------------------------

def test_severity_labels_alone_do_not_make_two_points_the_same():
    from autoharness import evidence
    assert not evidence.same_problem("hair too dark - severity: major, fixable: yes",
                                     "sky gradient banding - severity: major, fixable: yes")
    assert evidence.same_problem("hair too dark - severity: major, fixable: yes",
                                 "hair still too dark at the crown (major, fixable)")


def test_light_mode_on_a_visual_task_needs_a_reason(project, capsys):
    task = "draw an SVG of the film scene matching the reference image"
    code, res = ah(capsys, project, "init", task, "--mode", "light")
    assert code == 1 and "--light-reason" in res["error"]
    code, res = ah(capsys, project, "init", task, "--mode", "light", "--light-reason", "a placeholder, no likeness")
    assert code == 0
    assert project.task().info()["light_reason"] == "a placeholder, no likeness"


def test_args_file_from_stdin(project, capsys, monkeypatch):
    ah(capsys, project, "init", "fix the quarterly report")
    data = {"goal": "1 totals row added", "inputs": "the report", "constraints": "none", "verify": "tests",
            "candidates": "none", "gaps": "none: tools at hand"}
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(data).encode("utf-8"))))
    code, res = ah(capsys, project, "brief", "--args-file", "-")
    assert code == 0, res
    assert project.task().info()["brief"]["goal"] == "1 totals row added"


def test_many_blocking_critic_rounds_stop_to_ask_the_user(project, capsys):
    from autoharness import evidence
    briefed(capsys, project)
    words = ["sky", "rain", "neon", "bridge", "coat", "hair", "glow", "fog", "car", "sign"]
    for n in range(evidence.ROUND_BUDGET):
        f = project.root / f"r{n}.txt"
        f.write_text(str(n), "utf-8")
        ah(capsys, project, "evidence", "add", str(f), "--kind", "result")
        code, res = ah(capsys, project, "review", "--by", "critic", "--worst", "major",
                       "--diff", f"1 {words[n]} {words[n + 1]} wrong - major")
        if n < evidence.ROUND_BUDGET - 1:
            assert res["status"] != "ask_user", res
    assert code == cli.EXIT_APPROVAL and res["status"] == "ask_user" and len(res["trend"]) == evidence.ROUND_BUDGET


def test_a_run_in_a_repository_without_commits_is_tied_to_the_code(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "flag.py").write_text("x = 1\n", "utf-8")
    seen = runs.fingerprint(root)
    assert seen and seen["base_sha"] == runs.EMPTY_TREE
    assert runs.is_fresh({"fingerprint": seen}, root) is True
    (root / "flag.py").write_text("x = 2\n", "utf-8")
    assert runs.is_fresh({"fingerprint": seen}, root) is False


# --- lessons from friction, and what became of saved skills ---------------------------------

def test_the_hook_notes_what_the_user_writes_during_their_own_task(project, capsys, monkeypatch):
    from autoharness import lessons
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "owner-1")
    briefed(capsys, project)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    said = {"hook_event_name": "UserPromptSubmit", "session_id": "owner-1", "prompt": "no, the title must be bold"}
    hook(capsys, monkeypatch, project, said)
    hook(capsys, monkeypatch, project, dict(said, session_id="other-2", prompt="something else entirely"))
    task = project.task()
    assert [m["text"] for m in lessons.user_messages(task)] == ["no, the title must be bold"]
    assert any("stepped in 1 time(s)" in line and "title must be bold" in line for line in lessons.friction(task))


def test_the_agent_cannot_accept_its_own_result_until_the_user_has_spoken(project, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "owner-1")
    briefed(capsys, project)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")

    def call(command):
        return hook(capsys, monkeypatch, project, {"hook_event_name": "PreToolUse", "session_id": "owner-1",
                                                   "tool_name": "Bash", "tool_input": {"command": command}})

    accepted = 'autoharness teardown --accepted "small fix, tests pass" 2>&1 | tail -15'
    res = call(accepted)
    assert denied(res) and "user has said nothing" in res["hookSpecificOutput"]["permissionDecisionReason"]
    assert denied(call("autoharness teardown --force"))
    assert denied(call("autoharness teardown --accepted=fine"))
    assert not denied(call("autoharness teardown"))
    assert not denied(call('autoharness review --by self --diff "1 x" --worst none'))
    hook(capsys, monkeypatch, project, {"hook_event_name": "UserPromptSubmit", "session_id": "owner-1",
                                        "prompt": "fine, take it as it is"})
    assert not denied(call(accepted))


def test_a_smooth_task_has_no_lessons_and_tears_down_at_once(project, capsys):
    briefed(capsys, project)
    ah(capsys, project, "verify", "run", OK, "--for", "B1")
    code, res = ah(capsys, project, "teardown")
    assert code == 0 and "lessons" not in res


def test_saved_skills_and_recipes_keep_a_record_of_what_became_of_them(project, capsys):
    from autoharness import lessons
    briefed(capsys, project)
    task = project.task()
    lessons.bump("skill", "svg-scenes", "offered")
    lessons.mark_used(task, "skill", "svg-scenes")
    lessons.mark_used(task, "skill", "svg-scenes")          # once per task
    assert task.info()["library_used"] == [["skill", "svg-scenes"]]
    lessons.settle(task, ["svg-scenes"], [])
    assert lessons.record("skill", "svg-scenes") == {"offered": 1, "used": 1, "kept": 1, "smooth": 1}
    for _ in range(2):
        lessons.bump("skill", "svg-scenes", "used", "removed", "friction")   # two later tasks dropped it
    assert "removed in most tasks" in lessons.record("skill", "svg-scenes")["note"]
    assert lessons.record("recipe", "never-used") == {}


# --- the task records: only autoharness writes them -------------------------------------

def test_teardown_refuses_a_task_record_changed_around_autoharness(project, capsys):
    briefed(capsys, project)
    ah(capsys, project, "verify", "run", OK, "--for", "B1")
    task = project.task()
    assert (task.dir / "task.sig").exists() and not task.tampered()
    record = task.dir / "task.json"
    info = json.loads(record.read_text("utf-8"))
    info["runs"][0]["command"] = "pytest -q"                 # an edit made with a write tool, not autoharness
    record.write_text(json.dumps(info, indent=2), "utf-8")
    code, res = ah(capsys, project, "teardown")
    assert code == 1 and res["status"] == "tampered" and "changed outside autoharness" in res["problems"][0]
    assert "--force" in res["next"]
    assert ah(capsys, project, "teardown", "--force")[0] == 0


def test_an_evidence_copy_changed_after_it_was_recorded_shows(project, capsys, tmp_path):
    briefed(capsys, project)
    (tmp_path / "r.md").write_text("# Q3\n", "utf-8")
    ah(capsys, project, "evidence", "add", str(tmp_path / "r.md"), "--kind", "result")
    task = project.task()
    Path(task.info()["evidence"][0]["file"]).write_text("# better\n", "utf-8")
    assert "evidence #1 changed" in task.tampered()[0]


def test_a_task_record_without_a_seal_or_key_is_not_judged(project, capsys, monkeypatch, tmp_path):
    briefed(capsys, project)
    task = project.task()
    (task.dir / "task.sig").unlink()                       # a task from before the seal
    assert task.tampered() == []
    task._update_info(note="x")
    monkeypatch.setenv("AUTOHARNESS_HOME", str(tmp_path / "elsewhere"))   # another machine: no key here
    assert task.tampered() == []


@pytest.mark.parametrize("tool,tool_input,blocked", [
    ("Write", {"file_path": ".autoharness/tasks/x/task.json"}, True),
    ("Edit", {"file_path": "{root}/.autoharness/kept.json"}, True),
    ("Write", {"file_path": "{root}/.autoharness/config.json"}, False),   # the user's settings
    ("Write", {"file_path": "{root}/a.py"}, False),
    ("Write", {"TargetFile": "{root}/.autoharness/tasks/x/task.sig"}, True),   # agy names it TargetFile
    ("Bash", {"command": "echo '{}' > .autoharness/tasks/x/task.json"}, True),
    ("Bash", {"command": "sed -i 's/false/true/' .autoharness/tasks/x/task.json"}, True),
    ("Bash", {"command": "rm -rf .autoharness"}, True),
    ("PowerShell", {"command": r"Set-Content .autoharness\tasks\x\task.json '{}'"}, True),
    ("Bash", {"command": "python -c \"open('.autoharness/tasks/x/task.json','w').write('{}')\""}, True),
    ("Bash", {"command": "cat ~/.autoharness/integrity.key"}, True),
    ("Bash", {"command": "cat .autoharness/tasks/x/task.json 2>&1 | head"}, False),
    ("PowerShell", {"command": r"Get-Content .autoharness\tasks\x\task.json | ConvertFrom-Json"}, False),
    ("Bash", {"command": "python -c \"import json; print(open('.autoharness/x/task.json').read())\""}, False),
    ("Bash", {"command": "grep -r totals .autoharness > found.txt"}, False),
    ("Bash", {"command": "python -c \"import json; i=json.load(open('.autoharness/x/task.json')); "
                         "print(json.dumps(i))\""}, False),          # json.dumps only builds a string
    ("Bash", {"command": "python -c \"import json; json.dump({}, open('.autoharness/x/task.json','w'))\""},
     True),
    ("Bash", {"command": "echo '.autoharness/' >> .gitignore"}, False),
    ("Bash", {"command": "git commit -q -F - <<'EOF'\nfix: a copy changed in .autoharness/; rm it\nEOF\ngit log"},
     False),                                                        # a message about the records is no write
    ("Bash", {"command": 'git commit -m "x; rm .autoharness/tasks too"'}, False),
    ("Bash", {"command": "python - <<'EOF'\nimport pathlib\npathlib.Path('.autoharness/t/task.json')"
                         ".write_text('{}')\nEOF"}, True),         # a heredoc an interpreter runs is code
    ("Bash", {"command": "cd x && rm .autoharness/tasks/t/task.sig"}, True),
])
def test_the_hook_denies_writes_to_the_task_records(project, capsys, monkeypatch, tool, tool_input, blocked):
    briefed(capsys, project)
    tool_input = {k: v.replace("{root}", str(project.root)) for k, v in tool_input.items()}
    call = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": tool, "tool_input": tool_input}
    res = hook(capsys, monkeypatch, project, call)
    assert bool(denied(res)) is blocked, (tool, tool_input, res)


# --- Stop: the agent's own open task sends it back once ----------------------------------

def test_stop_sends_the_owner_back_once_per_state_of_its_task(project, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "owner-1")
    briefed(capsys, project)
    stop = {"hook_event_name": "Stop", "session_id": "owner-1", "stop_hook_active": False}
    res = hook(capsys, monkeypatch, project, stop)
    assert res["decision"] == "block" and "autoharness teardown" in res["reason"] and "verify run" in res["reason"]
    assert hook(capsys, monkeypatch, project, stop) is None              # nothing changed: the turn may end
    assert hook(capsys, monkeypatch, project, dict(stop, session_id="other-2")) is None   # not its task
    ah(capsys, project, "verify", "run", OK, "--for", "B1")
    assert hook(capsys, monkeypatch, project, dict(stop, stop_hook_active=True)) is None
    res = hook(capsys, monkeypatch, project, stop)                        # the task moved on: once more
    assert res["decision"] == "block" and "verify run" not in res["reason"]
    assert ah(capsys, project, "teardown")[0] == 0
    assert hook(capsys, monkeypatch, project, stop) is None              # torn down


def test_install_hook_adds_the_stop_event(tmp_path):
    settings = tmp_path / "settings.json"
    assert cli.install_hook(settings) == "installed"
    hooks = json.loads(settings.read_text("utf-8"))["hooks"]
    assert set(hooks) == {"UserPromptSubmit", "PreToolUse", "Stop"} and "matcher" not in hooks["Stop"][0]
    assert cli.install_hook(settings) == "already_installed"
    assert cli.install_hook(settings, remove=True) == "removed"
    assert not json.loads(settings.read_text("utf-8")).get("hooks")


# --- checks that cannot fail, and checks changed until they passed -----------------------

@pytest.mark.parametrize("command,trivial", [
    ("true", True), (":", True), ("exit 0", True), ("echo ok", True), ("Write-Output done", True),
    ('python -c "pass"', True), ('python -c "import sys; sys.exit(0)"', True), ("python3 -c \"print('ok')\"", True),
    ("cd tests && true", True), ("pytest -q", False), ("echo x | grep x", False), ("npm test", False),
    ('python -c "import app; app.check()"', False), ("python -c \"print(open('x').read())\"", False),
    ('python -c "pass" && pytest', False), ("exit 1", False),
])
def test_trivial_commands(command, trivial):
    assert runs.is_trivial(command) is trivial


def test_a_check_that_cannot_fail_does_not_count(project, capsys):
    briefed(capsys, project)
    code, res = ah(capsys, project, "verify", "run", "echo all good", "--for", "B1")
    assert code == 1 and res["status"] == "not_a_check" and "checks nothing" in res["next"]
    task = project.task()
    task._update_info(arbitrations=[{"n": 1, "verdict": "fix", "after_round": 0, "part": "totals", "check": "c"}])
    code, res = ah(capsys, project, "check", "--run", "true")
    assert code == 1 and "checks nothing" in res["error"]
    task._update_info(arbitrations=[])
    code, res = ah(capsys, project, "teardown")
    assert code == 0 and "B1" in res["checked"]["unchecked_items"]
    assert res["checked"]["brief_items"][0]["run"] == "run #1 not a check"
    assert any("checks nothing" in w for w in res["warnings"])


def test_a_check_changed_after_it_failed_is_flagged(project, capsys):
    briefed(capsys, project)
    ah(capsys, project, "verify", "run", FAIL, "--for", "B1")
    other = f'{PY} -c "import json; print(1)"'
    code, res = ah(capsys, project, "verify", "run", other, "--for", "B1")
    assert code == 0 and "changed after it failed" in res["run"]["check_changed"]
    assert "tell the user why the check changed" in res["next"]
    ah(capsys, project, "verify", "run", FAIL, "--for", "B2")
    ah(capsys, project, "verify", "run", FAIL.replace("sys.exit(3)", "sys.exit(0)"), "--for", "B2")
    code, res = ah(capsys, project, "teardown")
    if res["status"] == "lessons":
        code, res = ah(capsys, project, "teardown")
    flagged = [r for r in res["checked"]["ran"] if r.get("check_changed")]
    assert [r["n"] for r in flagged] == [2, 4]
    assert sum("changed after it failed" in w for w in res["warnings"]) == 2


# --- the same call over and over is a loop -----------------------------------------------

def test_the_hook_stops_the_same_call_made_over_and_over(project, capsys, monkeypatch):
    briefed(capsys, project)
    call = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
            "tool_input": {"command": "curl -s http://localhost:8000/health"}}
    for _ in range(cli.REPEAT_LIMIT):
        assert hook(capsys, monkeypatch, project, call) is None
    res = hook(capsys, monkeypatch, project, call)
    assert denied(res) and "same Bash call" in res["hookSpecificOutput"]["permissionDecisionReason"]
    other = dict(call, tool_input={"command": "cat server.log"})
    assert hook(capsys, monkeypatch, project, other) is None           # something else breaks the streak
    assert hook(capsys, monkeypatch, project, call) is None
    assert hook(capsys, monkeypatch, project, dict(call, session_id="s2")) is None   # per session


# --- rulings: settled points travel to later tasks of the project ------------------------

def test_settled_points_and_the_users_rulings_reach_later_critics(project, capsys, tmp_path):
    critic_ready(capsys, project, tmp_path)
    code, res = ah(capsys, project, "critic")
    f = tmp_path / "a.txt"
    f.write_text(f"ANSWER-ID: {res['answer_id']}\n1. title spacing (minor)\nVERDICT: minor\n", "utf-8")
    code, res = ah(capsys, project, "review", "--answer-file", str(f),
                   "--settled", "the header font matches the reference: measured 14px in both")
    assert code == 0
    code, res = ah(capsys, project, "ruling", "add", "totals should be bold", "--reason", "the user: keep them plain")
    assert code == 0 and res["ruling"]["by"] == "user"
    code, res = ah(capsys, project, "ruling", "list")
    assert [r["by"] for r in res["rulings"]] == ["critic", "user"]
    assert "!rulings.json" in (project.state / ".gitignore").read_text("utf-8")
    ah(capsys, project, "teardown", "--force")
    ah(capsys, project, "init", "fix the yearly report too")
    ah(capsys, project, *BRIEF, "--review", "critic")
    (tmp_path / "r2.md").write_text("# Y\n", "utf-8")
    ah(capsys, project, "evidence", "add", str(tmp_path / "r2.md"), "--kind", "result")
    code, res = ah(capsys, project, "critic")
    assert "header font matches the reference" in res["prompt"] and "a critic settled it" in res["prompt"]
    assert "totals should be bold (the user rejected this point: the user: keep them plain)" in res["prompt"]
    code, res = ah(capsys, project, "ruling", "add", "totals must be bold", "--reason", "fine, bold")
    assert res["ruling"]["replaces"] == [2]                           # the same point ruled again
    code, res = ah(capsys, project, "ruling", "remove", "3")
    assert code == 0 and ah(capsys, project, "ruling", "list")[1]["rulings"][0]["by"] == "critic"


# --- a pair of reviewers: what was asked, and how it is done ------------------------------

def pair_round(capsys, project, tmp_path, critic_text, quality_text):
    code, res = ah(capsys, project, "critic")
    assert code == 0 and "quality" in res and "--quality-file" in res["next"]
    a, q = tmp_path / "a.txt", tmp_path / "q.txt"
    a.write_text(f"ANSWER-ID: {res['answer_id']}\n{critic_text}", "utf-8")
    q.write_text(f"ANSWER-ID: {res['quality']['answer_id']}\n{quality_text}", "utf-8")
    return res, a, q


def test_a_pair_review_needs_both_answers_and_takes_the_worse_verdict(project, capsys, tmp_path):
    briefed(capsys, project, "--review", "pair")
    (project.root / "a.py").write_text("x = 2\n", "utf-8")
    ah(capsys, project, "evidence", "diff")
    res, a, q = pair_round(capsys, project, tmp_path, "No differences.\nVERDICT: none\n",
                           "1. a.py:1 crashes on empty input (major, fixable)\nVERDICT: major\n")
    assert "code reviewer" in res["quality"]["prompt"] and "Not your job" in res["quality"]["prompt"]
    code, err = ah(capsys, project, "review", "--answer-file", str(a))
    assert code == 1 and "--quality-file" in err["error"]
    code, err = ah(capsys, project, "review", "--answer-file", str(q))
    assert code == 1                                                  # the quality answer is not a critic's
    code, res = ah(capsys, project, "review", "--answer-file", str(a), "--quality-file", str(q))
    review = res["review"]
    assert code == 0 and review["worst"] == "major" and review["quality"]["verdict"] == "major"
    assert review["diff"] == "1. [code quality] a.py:1 crashes on empty input (major, fixable)"
    code, res = ah(capsys, project, "teardown")
    assert code == 1 and any("major" in p for p in res["problems"])
    # after the fix both run again; the next quality prompt sees the previous quality review
    (project.root / "a.py").write_text("x = 3\n", "utf-8")
    ah(capsys, project, "evidence", "diff")
    res, a, q = pair_round(capsys, project, tmp_path, "No differences.\nVERDICT: none\n",
                           "No differences.\nVERDICT: none\n")
    assert "crashes on empty input" in res["quality"]["prompt"]
    code, res = ah(capsys, project, "review", "--answer-file", str(a), "--quality-file", str(q))
    assert code == 0 and res["review"]["worst"] == "none"
    code, res = ah(capsys, project, "teardown")
    if res["status"] == "lessons":
        code, res = ah(capsys, project, "teardown")
    assert code == 0, res


def test_a_single_critic_round_does_not_satisfy_a_pair_brief(project, capsys, tmp_path):
    briefed(capsys, project, "--review", "pair")
    (tmp_path / "r.md").write_text("# Q3\n", "utf-8")
    ah(capsys, project, "evidence", "add", str(tmp_path / "r.md"), "--kind", "result")
    ah(capsys, project, "critic")
    ah(capsys, project, "review", "--by", "critic", "--diff", "none", "--worst", "none")   # retold, no pair
    code, res = ah(capsys, project, "teardown")
    assert code == 1 and any("no code-quality reviewer" in p for p in res["problems"])


def test_critic_run_runs_both_reviewers_of_a_pair(project, capsys, tmp_path, monkeypatch):
    briefed(capsys, project, "--review", "pair")
    (tmp_path / "r.md").write_text("# Q3\n", "utf-8")
    ah(capsys, project, "evidence", "add", str(tmp_path / "r.md"), "--kind", "result")
    started = []

    def fake(agent, root, prompt_file, answer_file, timeout, answer_id):
        started.append(Path(prompt_file).name)
        verdict = "1. leaks a file handle (minor)\nVERDICT: minor" if "quality" in Path(prompt_file).name else \
            "No differences.\nVERDICT: none"
        answer_file.write_text(f"ANSWER-ID: {answer_id}\n{verdict}\n", "utf-8")
        return answer_file.read_text("utf-8")

    monkeypatch.setattr(reviewers, "run", fake)
    code, res = ah(capsys, project, "critic", "--run", "claude")
    assert code == 0 and sorted(started) == ["critic-01.txt", "quality-01.txt"]
    assert res["review"]["worst"] == "minor" and "leaks a file handle" in res["review"]["diff"]


# --- insights: small conclusions whose confidence grows -----------------------------------

def test_an_insight_reached_again_is_merged_and_shown_to_related_tasks(project, capsys):
    from autoharness import lessons
    briefed(capsys, project)
    code, res = ah(capsys, project, "lesson", "add", "When the quarterly report totals look wrong, check the "
                                                   "currency rounding in the export first")
    assert code == 0 and not res["merged"] and res["insight"]["confirmed"] == 1
    code, res = ah(capsys, project, "lesson", "add", "check the export currency rounding first when report totals "
                                                   "are wrong")
    assert res["merged"] and res["insight"]["confirmed"] == 1         # one task confirms it once
    for n in (2, 3):
        assert ah(capsys, project, "teardown", "--force")[0] == 0
        code, res = ah(capsys, project, "init", f"the quarterly report totals are wrong again, take {n}")
        assert res["insights"][0]["confirmed"] == n - 1 and "insights" in res["next"]
        ah(capsys, project, *BRIEF)
        code, res = ah(capsys, project, "lesson", "add", "report totals wrong: check currency rounding in the export")
        assert res["merged"] and res["insight"]["confirmed"] == n
    assert "lesson fold" in res["next"] and lessons.ripe()[0]["confirmed"] == 3
    code, res = ah(capsys, project, "lesson", "add", "a different conclusion about svg gradients and filters")
    assert not res["merged"]
    code, res = ah(capsys, project, "lesson", "fold", "1", "--skill", "report-totals")
    assert res["insight"]["skill"] == "report-totals" and lessons.ripe() == []
    code, res = ah(capsys, project, "init", "the quarterly report totals are wrong again")
    assert "insights" not in res or all(i["id"] != 1 for i in res["insights"])


def test_the_user_ending_the_task_is_not_held_up_for_lessons(project, capsys):
    briefed(capsys, project)
    ah(capsys, project, "verify", "run", FAIL, "--for", "B1")
    ah(capsys, project, "verify", "run", OK, "--for", "B1")       # friction: failed, then passed
    code, res = ah(capsys, project, "teardown", "--accepted", "stop, that is enough")
    assert code == 0 and res["status"] == "ok" and res["lessons"]
    assert "offer once" in res["next"]


def test_min_mode_raises_a_lighter_choice(project, capsys, monkeypatch):
    monkeypatch.setenv("AUTOHARNESS_MIN_MODE", "full")
    code, res = ah(capsys, project, "init", "fix the quarterly report", "--mode", "light")
    assert code == 0 and res["mode"] == "full" and "min_mode" in res["mode_raised"]
    assert project.task().info()["mode"] == "full"
    ah(capsys, project, "teardown", "--force")
    monkeypatch.setenv("AUTOHARNESS_MIN_MODE", "any")
    code, res = ah(capsys, project, "init", "fix the yearly report", "--mode", "light")
    assert res["mode"] == "light" and "mode_raised" not in res


def test_an_agent_started_from_another_agents_shell_is_its_own_session(project, capsys, monkeypatch):
    from autoharness import agents
    env = {"CLAUDE_CODE_SESSION_ID": "outer", "ANTIGRAVITY_CONVERSATION_ID": "inner"}
    assert agents.session_id(env, agent="agy") == "inner" and agents.session_id(env) == "outer"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "outer")
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "inner")
    code = cli.main(["--agent", "agy", "--project", str(project.root), "init", "fix the quarterly report"])
    capsys.readouterr()
    assert code == 0 and Project(project.root, "agy").task().info()["owner"] == "inner"
