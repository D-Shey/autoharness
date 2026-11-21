"""Tests for Aval-style TDD Phase Enforcement and Discipline."""
from __future__ import annotations

import sys
from pathlib import Path
from autoharness import sentinel


def test_tdd_red_phase_rejects_passing_test(tmp_path: Path):
    # python -c "sys.exit(0)" exits 0 -> should be rejected in red phase
    res = sentinel.record_tdd_red_phase(tmp_path, test_command=f'"{sys.executable}" -c "import sys; sys.exit(0)"')
    assert res["status"] == "rejected"
    assert res["phase"] == "red"
    assert "violation" in res["error"].lower()


def test_tdd_red_phase_accepts_failing_test(tmp_path: Path):
    # python -c "sys.exit(1)" exits 1 -> accepted as red phase baseline
    res = sentinel.record_tdd_red_phase(tmp_path, test_command=f'"{sys.executable}" -c "import sys; sys.exit(1)"')
    assert res["status"] == "ok"
    assert res["phase"] == "red"
    assert res["fail_exit_code"] == 1

    status = sentinel.get_tdd_status(tmp_path)
    assert status["active"] is True
    assert status["phase"] == "red"


def test_tdd_green_phase_flow(tmp_path: Path):
    # 1. Green verification rejected without prior red baseline
    early_green = sentinel.verify_tdd_green_phase(tmp_path)
    assert early_green["status"] == "rejected"
    assert "no red phase baseline" in early_green["error"].lower()

    # 2. Establish red baseline
    red_cmd = f'"{sys.executable}" -c "import sys; sys.exit(1)"'
    sentinel.record_tdd_red_phase(tmp_path, test_command=red_cmd)

    # 3. If test is still failing, green phase rejects
    still_fail = sentinel.verify_tdd_green_phase(tmp_path, test_command=red_cmd)
    assert still_fail["status"] == "failed"

    # 4. If test now passes (green), green phase succeeds and completes
    pass_cmd = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
    green_res = sentinel.verify_tdd_green_phase(tmp_path, test_command=pass_cmd)
    assert green_res["status"] == "ok"
    assert green_res["phase"] == "green"

    final_status = sentinel.get_tdd_status(tmp_path)
    assert final_status["phase"] == "green"
    assert final_status["status"] == "completed"
