from __future__ import annotations

import re
from pathlib import Path
import pytest

from autoharness import answers, evidence, followups
from autoharness.env import Project


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOHARNESS_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    root.mkdir()
    return Project(root, "claude")


def test_quality_prompt_lean_heuristics_and_answers_parsing(project, monkeypatch):
    task = project.init_task("Test task")
    task._update_info(
        task="Test task",
        brief={"review": "pair", "constraints": "none"},
        evidence=[{"n": 1, "kind": "result", "file": "dummy.diff", "source": "dummy.diff", "closeup": None}]
    )
    prompt_dict = evidence.quality_prompt(task)
    prompt = prompt_dict["prompt"]

    assert "needless complexity, over-engineering, or duplication" in prompt
    assert "code that should not exist (dead code, unrequested speculative options" in prompt
    assert "reinvented helpers where the codebase or standard library" in prompt
    assert "new dependencies added where standard library" in prompt
    assert "Lean: -<N> lines possible" in prompt

    answer_text = """ANSWER-ID: ah-112233445566
1. Unneeded cache class (cache.py:L10) - minor, fixable
Lean: -45 lines possible in cache.py by using dict
VERDICT: minor"""

    answer_file = project.root / "test_answer.txt"
    answer_file.write_text(answer_text, encoding="utf-8")

    task._update_info(answer_ids={"ah-112233445566": {"kind": "quality", "subject": "dummy.diff"}})
    res = answers.read(task, answer_file, "quality", "dummy.diff")

    assert res["id"] == "ah-112233445566"
    assert res["verdict"] == "minor"
    assert res["lean"] == "-45 lines possible in cache.py by using dict"


def test_followup_harvest_shortcuts(project):
    src_dir = project.root / "src_test"
    src_dir.mkdir(exist_ok=True)
    code_file = src_dir / "service.py"
    code_file.write_text(
        "# shortcut: in-memory list up to 500 items, upgrade to SQLite when exceeding 500\n"
        "def run(*args):\n"
        "    pass\n"
        "# shortcut: mock auth token\n"
        "# shortcut: pointer *ptr, upgrade to unique_ptr\n"
        "# shortcut: *args and **kwargs, fix signature\n"
        "# shortcut: \n",
        encoding="utf-8"
    )

    harvest_res = followups.harvest(project)
    assert harvest_res["found"] == 4
    assert harvest_res["added"] == 4
    assert harvest_res["no_trigger"] == 1

    active_items = followups.listing(project)
    shortcut_items = [i for i in active_items if "service.py:" in i["text"]]
    assert len(shortcut_items) == 4

    with_trigger = next(i for i in shortcut_items if "upgrade to SQLite" in i["text"])
    without_trigger = next(i for i in shortcut_items if "[no-trigger]" in i["text"])
    ptr_item = next(i for i in shortcut_items if "pointer *ptr" in i["text"])
    args_item = next(i for i in shortcut_items if "*args and **kwargs" in i["text"])

    assert "in-memory list up to 500 items" in with_trigger["text"]
    assert "upgrade to SQLite" in with_trigger["text"]
    assert "mock auth token" in without_trigger["text"]
    assert "upgrade trigger 'upgrade to unique_ptr'" in ptr_item["text"]
    assert "upgrade trigger 'fix signature'" in args_item["text"]

    # Test idempotence
    harvest_again = followups.harvest(project)
    assert harvest_again["found"] == 4
    assert harvest_again["added"] == 0


def test_code_frugality_skill_structure():
    skill_path = Path(".agents/skills/code-frugality/SKILL.md")
    assert skill_path.is_file()
    text = skill_path.read_text("utf-8")

    assert "name: code-frugality" in text
    assert "## When to use" in text
    assert "## Steps" in text
    assert "The 7-Step Resolution Ladder" in text
    assert "## How to verify" in text
    assert "## Facts found here" in text


def test_followup_harvest_cli_command(project, monkeypatch, capsys):
    from autoharness import cli
    import json
    src_dir = project.root / "app"
    src_dir.mkdir(exist_ok=True)
    (src_dir / "worker.py").write_text("# shortcut: sync batch, make async later\n", encoding="utf-8")

    monkeypatch.chdir(project.root)
    rc = cli.main(["followup", "harvest", "--marker", "shortcut"])
    assert rc == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "ok"
    assert data["found"] >= 1
    assert data["harvested"] >= 1


def test_teardown_auto_harvests_and_includes_lean(project, monkeypatch, capsys):
    from autoharness import cli
    import json
    task = project.init_task("Sample task", mode="light")
    task._update_info(reviews=[{"by": "quality", "lean": "-20 lines in utils.py"}])

    src_dir = project.root / "pkg"
    src_dir.mkdir(exist_ok=True)
    (src_dir / "calc.py").write_text("# shortcut: fixed precision 2 decimal places, support arbitrary precision\n", encoding="utf-8")

    monkeypatch.chdir(project.root)
    rc = cli.main(["teardown"])
    assert rc == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "ok"
    assert data["checked"].get("lean") == "-20 lines in utils.py"
    assert any("calc.py" in f["text"] for f in data.get("followups", []))
    assert "Lean optimization: -20 lines in utils.py" in data.get("next", "")


def test_cmd_review_propagates_lean_to_teardown(project, monkeypatch, capsys):
    from autoharness import cli
    import json
    task = project.init_task("Sample pair task", mode="light")
    task._update_info(
        brief={"review": "pair"},
        evidence=[{"n": 1, "kind": "result", "file": "diff.patch", "source": "diff.patch", "closeup": None}],
        quality_pairs={"ah-c0ffee112233": "ah-c0ffee445566"},
        answer_ids={
            "ah-c0ffee112233": {"kind": "critic", "subject": "result #1"},
            "ah-c0ffee445566": {"kind": "quality", "subject": "result #1"},
        }
    )

    critic_file = project.root / "critic.txt"
    critic_file.write_text("ANSWER-ID: ah-c0ffee112233\nNo differences.\nVERDICT: none\n", encoding="utf-8")
    quality_file = project.root / "quality.txt"
    quality_file.write_text("ANSWER-ID: ah-c0ffee445566\nNo differences.\nLean: -30 lines in cache.py\nVERDICT: none\n", encoding="utf-8")

    monkeypatch.chdir(project.root)
    rc = cli.main(["review", "--by", "critic", "--answer-file", str(critic_file), "--quality-file", str(quality_file)])
    assert rc == 0

    revs = task.info().get("reviews", [])
    assert len(revs) == 1
    assert revs[0].get("quality", {}).get("lean") == "-30 lines in cache.py"

    capsys.readouterr()
    rc_td = cli.main(["teardown", "--keep", "--force"])
    assert rc_td == 0
    captured = capsys.readouterr()
    td_data = json.loads(captured.out)
    assert td_data["status"] == "ok"
    assert td_data.get("checked", {}).get("lean") == "-30 lines in cache.py"
    assert "Lean optimization: -30 lines in cache.py" in td_data.get("next", "")


def test_teardown_clears_stale_lean_on_subsequent_round(project, monkeypatch, capsys):
    from autoharness import cli
    import json
    task = project.init_task("Multi round task", mode="light")
    # Round 1 had lean findings, but Round 2 fixed them and quality reviewer found none
    task._update_info(
        reviews=[
            {"by": "critic", "round": 1, "worst": "minor", "verdict": "minor", "quality": {"verdict": "minor", "lean": "-50 lines in old.py"}},
            {"by": "critic", "round": 2, "worst": "none", "verdict": "none", "quality": {"verdict": "none", "lean": None}},
        ]
    )

    monkeypatch.chdir(project.root)
    rc = cli.main(["teardown", "--keep", "--force"])
    assert rc == 0
    captured = capsys.readouterr()
    td_data = json.loads(captured.out)
    assert td_data["status"] == "ok"
    assert td_data.get("checked", {}).get("lean") is None
    assert "Lean optimization" not in td_data.get("next", "")
