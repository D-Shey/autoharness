"""Tests for Consequence-Aware Approval & Blast Radius Simulation (Hugging Face arXiv:2610.40753)."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from autoharness import consequence, cli, gateway
from autoharness.consequence import ReversibilityTier


def test_reversibility_tiers_classification(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    # 1. READ_ONLY
    sim_ro = consequence.simulate_action("query", "SELECT * FROM users", project_root=repo)
    assert sim_ro["reversibility_tier"] == ReversibilityTier.READ_ONLY.value
    assert sim_ro["risk_score"] <= 10

    sim_cmd_ro = consequence.simulate_action("command", "pytest tests/", project_root=repo)
    assert sim_cmd_ro["reversibility_tier"] == ReversibilityTier.READ_ONLY.value

    # 2. IRREVERSIBLE
    sim_irr_cmd = consequence.simulate_action("command", "rm -rf /var/data", project_root=repo)
    assert sim_irr_cmd["reversibility_tier"] == ReversibilityTier.IRREVERSIBLE.value
    assert sim_irr_cmd["risk_score"] >= 85

    sim_drop = consequence.simulate_action("command", "DROP TABLE users;", project_root=repo)
    assert sim_drop["reversibility_tier"] == ReversibilityTier.IRREVERSIBLE.value

    # Untracked file delete is irreversible
    untracked_file = repo / "temp.txt"
    untracked_file.write_text("temp", encoding="utf-8")
    sim_del_untracked = consequence.simulate_action("file_delete", str(untracked_file), project_root=repo)
    assert sim_del_untracked["reversibility_tier"] == ReversibilityTier.IRREVERSIBLE.value

    # 3. HIGH_RISK: Sensitive file edit
    sim_gateway = consequence.simulate_action("file_modify", ".autoharness/gateway.json", project_root=repo)
    assert sim_gateway["reversibility_tier"] == ReversibilityTier.HIGH_RISK.value
    assert sim_gateway["blast_radius"]["touches_sensitive_paths"] is True

    # 4. REVERSIBLE: Regular safe modification
    sim_safe = consequence.simulate_action("file_modify", "src/utils.py", project_root=repo)
    assert sim_safe["reversibility_tier"] == ReversibilityTier.REVERSIBLE.value
    assert "git restore" in sim_safe["rollback_plan"]


def test_blast_radius_impact_analysis(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "calc.py").write_text(
        "def base_add(a, b):\n"
        "    return a + b\n\n"
        "def compute_total(items):\n"
        "    return base_add(10, len(items))\n",
        encoding="utf-8"
    )
    (repo / "test_calc.py").write_text(
        "from calc import base_add\n"
        "def test_base_add():\n"
        "    assert base_add(1, 2) == 3\n",
        encoding="utf-8"
    )

    sim = consequence.simulate_action("file_modify", "calc.py", project_root=repo)
    assert "calc.py" in sim["blast_radius"]["affected_files"]
    assert "compute_total" in sim["blast_radius"]["impacted_symbols"]
    assert "test_calc.py" in sim["blast_radius"]["impacted_tests"]

    manifest = consequence.format_consequence_manifest(sim)
    assert "# Consequence Simulation Manifest" in manifest
    assert "compute_total" in manifest
    assert "test_calc.py" in manifest


def test_cli_simulate_and_permissions(tmp_path, capsys):
    repo = tmp_path / "cli_repo"
    repo.mkdir()

    class FakeArgs:
        target = "git clean -fdx"
        action_type = "command"
        json = True

    class FakeProject:
        root = repo

    ret = cli.cmd_simulate(FakeArgs(), FakeProject(), None)
    assert ret == 0
    out = json.loads(capsys.readouterr().out)
    assert out["reversibility_tier"] == "IRREVERSIBLE"
    assert out["risk_score"] >= 85

    # Test permissions simulate
    class FakePermArgs:
        action = "simulate"
        action_type = "file_modify"
        target = "safe_script.py"
        json = True

    ret_perm = cli.cmd_permissions(FakePermArgs(), FakeProject(), None)
    assert ret_perm == 0
    out_perm = json.loads(capsys.readouterr().out)
    assert out_perm["reversibility_tier"] == "REVERSIBLE"


def test_gateway_simulate_action(tmp_path):
    repo = tmp_path / "gw_repo"
    repo.mkdir()

    gw = gateway.Gateway(repo, write=lambda s: None, log=lambda s: None)

    # 1. Markdown manifest
    res = gw._call({
        "name": "harness_simulate_action",
        "arguments": {"action_type": "command", "target": "rm -rf build/"}
    })
    text = res["content"][0]["text"]
    assert "# Consequence Simulation Manifest" in text
    assert "IRREVERSIBLE" in text

    # 2. Raw JSON
    res_json = gw._call({
        "name": "harness_simulate_action",
        "arguments": {"action_type": "file_modify", "target": ".env", "raw_json": True}
    })
    data = json.loads(res_json["content"][0]["text"])
    assert data["reversibility_tier"] == "HIGH_RISK"
    assert data["blast_radius"]["touches_sensitive_paths"] is True
