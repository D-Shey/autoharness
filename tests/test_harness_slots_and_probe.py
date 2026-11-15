"""Tests for 8-Slot Modular Harness Scaffolding and Propose–Probe–Compose Contrastive Loop.
Based on Google Research Harness² (arXiv:2610.14224).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from autoharness import cli, gateway, probe, slots


def test_canonical_8_slots_inspection(tmp_path):
    # Setup dummy project structure
    agents_dir = tmp_path / ".agents" / "skills" / "sample-skill"
    agents_dir.mkdir(parents=True)
    (agents_dir / "SKILL.md").write_text("# Sample Skill\n", encoding="utf-8")

    mem_dir = tmp_path / ".autoharness"
    mem_dir.mkdir(parents=True)
    (mem_dir / "memory.db").write_text("", encoding="utf-8")

    inspection = slots.inspect_harness_slots(tmp_path, active_task_info={"mode": "full", "agent": "agy"})

    assert inspection["slots_version"] == "harness2-8slot-v1"
    res_slots = inspection["slots"]

    # Verify all 8 canonical slots exist
    for expected_slot in slots.CANONICAL_SLOTS:
        assert expected_slot in res_slots, f"Missing slot: {expected_slot}"

    assert res_slots["system_prompt"]["agent"] == "agy"
    assert res_slots["system_prompt"]["mode"] == "full"
    assert res_slots["skills"]["total_skills"] == 1
    assert "sample-skill" in res_slots["skills"]["installed"]
    assert res_slots["guardrails"]["total_rules"] >= 3
    assert res_slots["tool_guidance"]["strict_powershell"] is True
    assert res_slots["subagents"]["default_topology"] == "coordinator_with_independent_critic"
    assert res_slots["middleware"]["secret_masking_interceptor"] is True


def test_slots_export_and_diff(tmp_path):
    base_data = slots.inspect_harness_slots(tmp_path)
    exported = slots.export_harness_slots(tmp_path)
    assert base_data == exported

    # Create modified head configuration
    head_data = json.loads(json.dumps(base_data))
    head_data["slots"]["guardrails"]["rules"].append({"name": "CustomConstraint", "active": True})
    head_data["slots"]["tool_guidance"]["strict_powershell"] = False

    diff_res = slots.diff_harness_slots(base_data, head_data)
    assert diff_res["changed_slots_count"] == 2
    assert "guardrails" in diff_res["changed_slots"]
    assert "tool_guidance" in diff_res["changed_slots"]

    # Test report formatting
    report = slots.format_slots_report(base_data)
    assert "# 8-Slot Harness Scaffolding" in report
    assert "SYSTEM_PROMPT" in report
    assert "GUARDRAILS" in report
    assert "MEMORY" in report
    assert "SKILLS" in report


def test_propose_variants_and_scoring(tmp_path):
    base_data = slots.inspect_harness_slots(tmp_path)
    variants = probe.propose_variants(base_data, task_prompt="Optimize memory indexing", k=3)

    assert len(variants) == 3
    v1, v2, v3 = variants[0], variants[1], variants[2]
    assert v1["id"] == "variant_1"
    assert v1["name"] == "Strict-Guardrail-Verifier"
    assert "guardrails" in v1["slots_delta"]

    assert v2["id"] == "variant_2"
    assert v2["name"] == "Token-Frugal-AST-Specialist"
    assert "tool_guidance" in v2["slots_delta"]

    assert v3["id"] == "variant_3"
    assert v3["name"] == "Memory-Augmented-Ruling-Specialist"

    # Test contrast scoring
    t_good = probe.ProbeTrajectory(
        variant_id="v_good",
        name="Good",
        steps_count=4,
        tool_errors=0,
        loops_detected=0,
        verification_passed=True,
        artifacts=["result.py"],
    )
    score_good = probe.calculate_contrast_score(t_good)
    assert score_good > 60.0

    t_bad = probe.ProbeTrajectory(
        variant_id="v_bad",
        name="Bad",
        steps_count=15,
        tool_errors=2,
        loops_detected=1,
        verification_passed=False,
    )
    score_bad = probe.calculate_contrast_score(t_bad)
    assert score_bad < 20.0


def test_propose_probe_compose_cycle(tmp_path):
    base_data = slots.inspect_harness_slots(tmp_path)
    result = probe.run_propose_probe_compose(base_data, task_prompt="Refactor database access", k=2)

    assert result["k"] == 2
    assert result["variants_count"] == 2
    composed = result["composed_result"]
    assert composed["winner_id"] in ("variant_1", "variant_2")
    assert composed["winner_score"] > 0
    assert "contrastive_justification" in composed
    assert len(composed["trajectories"]) == 2

    report = probe.format_probe_report(result)
    assert "# Propose–Probe–Compose Contrastive Report" in report
    assert "Behavioral Contrasts Matrix" in report
    assert "Adopted Harness Improvements" in report
    assert "Contrastive Justification (Without Ground Truth)" in report


def test_cli_slot_commands(tmp_path, capsys):
    class FakeArgs:
        action = "inspect"
        json = False
        target = None

    class FakeProject:
        root = tmp_path
        def task(self):
            return None

    # 1. CLI slot inspect (Markdown)
    ret = cli.cmd_slot(FakeArgs(), FakeProject(), None)
    assert ret == 0
    out = capsys.readouterr().out
    assert "# 8-Slot Harness Scaffolding" in out

    # 2. CLI slot inspect (--json)
    FakeArgs.json = True
    ret = cli.cmd_slot(FakeArgs(), FakeProject(), None)
    assert ret == 0
    out_json = capsys.readouterr().out
    data = json.loads(out_json)
    assert data["status"] == "ok"
    assert "slots" in data

    # 3. CLI slot export
    FakeArgs.action = "export"
    ret = cli.cmd_slot(FakeArgs(), FakeProject(), None)
    assert ret == 0
    out_exp = capsys.readouterr().out
    exp_data = json.loads(out_exp)
    assert exp_data["status"] == "ok"

    # 4. CLI slot diff
    target_file = tmp_path / "modified_slots.json"
    mod_slots = exp_data["slots"]
    mod_slots["slots"]["guardrails"]["extra_rule"] = "check"
    target_file.write_text(json.dumps(mod_slots), encoding="utf-8")

    FakeArgs.action = "diff"
    FakeArgs.target = str(target_file)
    ret = cli.cmd_slot(FakeArgs(), FakeProject(), None)
    assert ret == 0
    out_diff = capsys.readouterr().out
    d_data = json.loads(out_diff)
    assert d_data["status"] == "ok"
    assert d_data["diff"]["changed_slots_count"] >= 1


def test_cli_probe_commands(tmp_path, capsys):
    class FakeArgs:
        action = "run"
        k = 2
        prompt = "Run diagnostic task"
        json = False

    class FakeProject:
        root = tmp_path
        def task(self):
            return None

    # 1. CLI probe (Markdown report)
    ret = cli.cmd_probe(FakeArgs(), FakeProject(), None)
    assert ret == 0
    out = capsys.readouterr().out
    assert "# Propose–Probe–Compose Contrastive Report" in out

    # 2. CLI probe (--json)
    FakeArgs.json = True
    ret = cli.cmd_probe(FakeArgs(), FakeProject(), None)
    assert ret == 0
    out_json = capsys.readouterr().out
    data = json.loads(out_json)
    assert data["status"] == "ok"
    assert "probe" in data


def test_gateway_harness_slots_tool(tmp_path):
    repo = tmp_path / "gw_slots_repo"
    repo.mkdir()

    gw = gateway.Gateway(repo, write=lambda s: None, log=lambda s: None)

    # 1. Inspect Markdown
    res_inspect = gw._call({
        "name": "harness_slots",
        "arguments": {"action": "inspect"}
    })
    assert "# 8-Slot Harness Scaffolding" in res_inspect["content"][0]["text"]

    # 2. Inspect raw_json
    res_json = gw._call({
        "name": "harness_slots",
        "arguments": {"action": "inspect", "raw_json": True}
    })
    data = json.loads(res_json["content"][0]["text"])
    assert data["slots_version"] == "harness2-8slot-v1"
    assert "guardrails" in data["slots"]

    # 3. Export
    res_export = gw._call({
        "name": "harness_slots",
        "arguments": {"action": "export"}
    })
    exp = json.loads(res_export["content"][0]["text"])
    assert "slots" in exp

    # 4. Probe
    res_probe = gw._call({
        "name": "harness_slots",
        "arguments": {"action": "probe", "k": 2}
    })
    assert "# Propose–Probe–Compose Contrastive Report" in res_probe["content"][0]["text"]

    # 5. Diff with malformed JSON
    bad_file = repo / "bad.json"
    bad_file.write_text("{malformed_json: true", encoding="utf-8")
    res_diff_bad = gw._call({
        "name": "harness_slots",
        "arguments": {"action": "diff", "target": str(bad_file)}
    })
    assert res_diff_bad.get("isError") is True
    assert "Failed to parse target slots file" in res_diff_bad["content"][0]["text"]

