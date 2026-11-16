# -*- coding: utf-8 -*-
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


def test_detect_hollow_check_identifies_token_membership_scripts(tmp_path):
    """Ensure scripts that only assert key/token membership without inspecting values are flagged as hollow."""
    membership_script = tmp_path / "membership_only.py"
    membership_script.write_text("""
data = {"status": "ok", "items": [1, 2]}
assert "status" in data
assert "items" in data
for required_id in ["a", "b"]:
    assert required_id in data
""", encoding="utf-8")

    is_hollow, reason = runs.detect_hollow_check(f'python "{membership_script}"', root=tmp_path)
    assert is_hollow is True
    assert "token/key presence" in reason or "shallow checks" in reason

    # Substantive script checking actual values and domain properties
    substantive_script = tmp_path / "substantive_domain.py"
    substantive_script.write_text("""
data = {"status": "ok", "count": 42}
assert data["status"] == "ok"
assert data["count"] > 10
""", encoding="utf-8")

    is_hollow_sub, reason_sub = runs.detect_hollow_check(f'python "{substantive_script}"', root=tmp_path)
    assert is_hollow_sub is False


def test_brief_with_techniques_records_and_injects_into_critic_prompt(project, capsys, tmp_path):
    """Ensure --techniques is recorded in the brief and injected into critic prompts with commercial rubric."""
    brief_cmd = [
        "brief", "--goal", "format quarterly totals and title", "--inputs", "the specification",
        "--domains", TEST_DOMAINS, "--method", TEST_METHOD,
        "--techniques", "clean-architecture: domain isolated from IO; idempotent-upsert: safe retry on duplicates",
        "--expects", "none: task settles all choices", "--constraints", "none",
        "--verify", "run validation", "--candidates", "none", "--gaps", "none: tools cover it",
        "--review", "critic"
    ]
    ah(capsys, project, "init", "format quarterly totals and title")
    ah(capsys, project, "search", "skill", "test")
    code, res = ah(capsys, project, *brief_cmd)
    assert code == 0

    task = project.task()
    b = task.info()["brief"]
    assert "techniques" in b
    assert "clean-architecture" in b["techniques"]
    assert "idempotent-upsert" in b["techniques"]

    # Add a dummy result so critic prompt can be generated
    res_file = project.root / "api.py"
    res_file.write_text("def run(): return 42\n", encoding="utf-8")
    res_code, res_out = ah(capsys, project, "evidence", "add", str(res_file), "--kind", "result")
    assert res_code == 0, res_out

    # Generate critic prompt
    code_critic, critic_res = ah(capsys, project, "critic")
    assert code_critic == 0, critic_res
    prompt_text = critic_res["prompt"]

    # Invariants verification in prompt
    assert "Declared professional implementation techniques: clean-architecture" in prompt_text
    assert "idempotent-upsert" in prompt_text
    assert "Commercial production standard & zero-tolerance for amateur shortcuts (calligraphy principle)" in prompt_text
    assert "finished commercial deliverable" in prompt_text


def test_visual_brief_requires_components_techniques_and_stencils(project, capsys):
    """Ensure media/visual tasks strictly enforce --components, --techniques, and stencil scanning."""
    ah(capsys, project, "init", "Draw a scene from the movie Blade Runner 2049 using SVG graphics")
    ah(capsys, project, "search", "skill", "test")

    # Missing components -> fails
    base_brief = [
        "brief", "--goal", "movie scene SVG", "--inputs", "scene stills",
        "--domains", TEST_DOMAINS, "--method", TEST_METHOD,
        "--expects", "none: settled", "--constraints", "none",
        "--verify", "visual render", "--candidates", "none", "--gaps", "none: tools cover it",
        "--fidelity", "stylised vector poster"
    ]
    code, res = ah(capsys, project, *base_brief)
    assert code == 1 and "requires decomposition" in res["error"]

    # Missing techniques -> fails
    brief_with_comp = base_brief + ["--components", "catwalk: layout; hologram: lighting"]
    code, res = ah(capsys, project, *brief_with_comp)
    assert code == 1 and "craftsmanship contracts" in res["error"]

    # Missing stencil scan when refs/ exist -> fails
    refs_dir = project.root / "refs"
    refs_dir.mkdir()
    (refs_dir / "ref.jpg").write_text("fake image", encoding="utf-8")
    brief_with_tech = brief_with_comp + ["--techniques", "multi-layer-compositing: separate lighting and ambient"]
    code, res = ah(capsys, project, *brief_with_tech)
    assert code == 1 and "no structural stencils have been scanned" in res["error"]

    # Once stencil is scanned -> succeeds!
    stencils_dir = project.root / ".autoharness" / "stencils"
    stencils_dir.mkdir(parents=True, exist_ok=True)
    (stencils_dir / "stc_ref_01.json").write_text("{}", encoding="utf-8")
    code, res = ah(capsys, project, *brief_with_tech)
    assert code == 0, res
    b = project.task().info()["brief"]
    assert "components" in b
    assert "techniques" in b

