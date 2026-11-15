import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from autoharness import cli, evidence, runs
from autoharness.env import Project
from conftest import TEST_DOMAINS, TEST_METHOD

PY = f'"{sys.executable}"'


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("AUTOHARNESS_HOME", str(home / ".autoharness"))
    monkeypatch.setenv("AUTOHARNESS_AGENT", "claude")
    monkeypatch.setenv("AUTOHARNESS_OFFLINE", "1")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    git = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)
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


def test_detect_hollow_check_identifies_shallow_scripts(tmp_path):
    # Shallow script with only file existence and truthy constant checks
    shallow_script = tmp_path / "shallow.py"
    shallow_script.write_text("""import os
assert os.path.exists("test.txt")
assert True
assert not False
""", encoding="utf-8")

    is_hollow, reason = runs.detect_hollow_check(f'python "{shallow_script}"', root=tmp_path)
    assert is_hollow is True
    assert "shallow checks" in reason

    # Empty script with no assertions
    empty_script = tmp_path / "empty.py"
    empty_script.write_text("print('running tests')", encoding="utf-8")
    is_hollow_empty, reason_empty = runs.detect_hollow_check(f'python "{empty_script}"', root=tmp_path)
    assert is_hollow_empty is True
    assert "no assertions" in reason_empty

    # Substantive script with invariant checks across arbitrary domains
    real_script = tmp_path / "substantive.py"
    real_script.write_text("""import os
assert os.path.exists("data.json")
data = open("data.json").read()
assert "status" in data
assert len(data) >= 20
""", encoding="utf-8")

    is_hollow_real, reason_real = runs.detect_hollow_check(f'python "{real_script}"', root=tmp_path)
    assert is_hollow_real is False


def test_hollow_artifact_integrity(tmp_path):
    # Empty file (0 bytes) is hollow
    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("", encoding="utf-8")
    res_empty = evidence.inspect_artifact_complexity(empty_file)
    assert res_empty["is_hollow"] is True
    assert "empty (0 bytes)" in res_empty["reason"]

    # Vacant root XML/SVG is hollow
    vacant_svg = tmp_path / "vacant.svg"
    vacant_svg.write_text("<svg></svg>", encoding="utf-8")
    res_vacant = evidence.inspect_artifact_complexity(vacant_svg)
    assert res_vacant["is_hollow"] is True
    assert "empty root element" in res_vacant["reason"]

    # Malformed XML/SVG is hollow
    broken_svg = tmp_path / "broken.svg"
    broken_svg.write_text("<svg><unclosed>", encoding="utf-8")
    res_broken = evidence.inspect_artifact_complexity(broken_svg)
    assert res_broken["is_hollow"] is True
    assert "malformed" in res_broken["reason"]

    # Valid simple SVG (e.g. icon or badge) is NOT hollow
    valid_svg = tmp_path / "icon.svg"
    valid_svg.write_text("""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M0 0h24v24H0z"/></svg>""", encoding="utf-8")
    res_valid = evidence.inspect_artifact_complexity(valid_svg)
    assert res_valid["is_hollow"] is False

    # Empty JSON is hollow
    empty_json = tmp_path / "empty.json"
    empty_json.write_text("{}", encoding="utf-8")
    res_json = evidence.inspect_artifact_complexity(empty_json)
    assert res_json["is_hollow"] is True
    assert "empty container" in res_json["reason"]

    # Code file with only comments is hollow
    comment_code = tmp_path / "stub.py"
    comment_code.write_text("# Just comments\n# nothing executable\n", encoding="utf-8")
    res_code = evidence.inspect_artifact_complexity(comment_code)
    assert res_code["is_hollow"] is True
    assert "only comments" in res_code["reason"]

    # Valid code is NOT hollow
    valid_code = tmp_path / "app.py"
    valid_code.write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    res_app = evidence.inspect_artifact_complexity(valid_code)
    assert res_app["is_hollow"] is False


def test_self_review_blocked_on_hollow_artifact(project, capsys, tmp_path):
    brief_cmd = [
        "brief", "--goal", "generate report data", "--inputs", "reference report",
        "--domains", TEST_DOMAINS, "--method", TEST_METHOD,
        "--expects", "none: task settles all choices", "--constraints", "none",
        "--verify", "run validation", "--candidates", "none", "--gaps", "none: tools cover it"
    ]
    ah(capsys, project, "init", "fix the quarterly report")
    ah(capsys, project, "search", "skill", "test")
    code, b_res = ah(capsys, project, *brief_cmd)
    assert code == 0, b_res

    # Add empty/vacant artifact
    vacant_svg = tmp_path / "output.svg"
    vacant_svg.write_text("<svg></svg>", encoding="utf-8")
    code, ev_res = ah(capsys, project, "evidence", "add", str(vacant_svg), "--kind", "result")
    assert code == 0, ev_res
    assert ev_res["evidence"].get("hollow") is True

    # Attempt self-certification with --worst none on hollow artifact must be blocked
    code, rev_res = ah(capsys, project, "review", "--by", "self", "--diff", "everything passes", "--worst", "none")
    assert code == 1
    assert "hollow" in rev_res["error"]


def test_self_review_blocked_on_hollow_check(project, capsys, tmp_path):
    brief_cmd = [
        "brief", "--goal", "implement algorithm", "--inputs", "spec doc",
        "--domains", TEST_DOMAINS, "--method", TEST_METHOD,
        "--expects", "none: task settles all choices", "--constraints", "none",
        "--verify", "run validation", "--candidates", "none", "--gaps", "none: tools cover it"
    ]
    ah(capsys, project, "init", "implement algorithm")
    ah(capsys, project, "search", "skill", "test")
    code, b_res = ah(capsys, project, *brief_cmd)
    assert code == 0, b_res

    # Run hollow check (only file existence)
    hollow_script = tmp_path / "check_exists.py"
    hollow_script.write_text("import os; assert os.path.exists('some_file.txt')\n", encoding="utf-8")
    (project.root / "some_file.txt").write_text("ok", encoding="utf-8")

    code, v_res = ah(capsys, project, "verify", "run", f'{PY} "{hollow_script}"', "--for", "B1")
    assert code == 0
    assert v_res["run"].get("hollow_check") is True

    # Add valid result
    valid_res = tmp_path / "result.json"
    valid_res.write_text('{"status": "ok", "items": [1, 2, 3]}', encoding="utf-8")
    ah(capsys, project, "evidence", "add", str(valid_res), "--kind", "result")

    # Self review with --worst none must be rejected due to hollow check
    code, rev_res = ah(capsys, project, "review", "--by", "self", "--diff", "looks good", "--worst", "none")
    assert code == 1
    assert "hollow check" in rev_res["error"]


def test_verify_run_command_pieces_reassembly(project, capsys, tmp_path):
    brief_cmd = [
        "brief", "--goal", "build tool", "--inputs", "spec",
        "--domains", TEST_DOMAINS, "--method", TEST_METHOD,
        "--expects", "none: task settles all choices", "--constraints", "none",
        "--verify", "run validation", "--candidates", "none", "--gaps", "none: tools cover it"
    ]
    ah(capsys, project, "init", "build tool")
    ah(capsys, project, "search", "skill", "test")
    ah(capsys, project, *brief_cmd)

    # Single quoted command string with --for
    test_file = tmp_path / "test_app.py"
    test_file.write_text("assert 2 + 2 == 4\n", encoding="utf-8")

    code = cli.main(["--project", str(project.root), "verify", "run", f'"{sys.executable}" "{test_file}"', "--for", "B1"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["run"]["for"] == "B1"
    assert "test_app.py" in out["run"]["command"]


def test_constraints_fuzzy_token_overlap():
    from autoharness.cli import _constraint_matches
    # Direct match
    assert _constraint_matches("Don't use external svg generators", "Don't use external svg generators")
    # Case insensitive and punctuation
    assert _constraint_matches("don't use external generators", "Don't use external generators!")
    # Contraction normalization (Don't -> Do not)
    assert _constraint_matches("Do not use external tools", "Don't use external tools")
    # Substring / token overlap
    assert _constraint_matches("no external generators", "Constraints: no external generators allowed in project")
    # Totally unrelated phrase
    assert not _constraint_matches("must use postgresql", "only sqlite in memory")
