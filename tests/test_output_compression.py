"""Tests for Intelligent Output Compression & Token Cascading (dean0x/skim model)."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from autoharness import compression, cli, gateway


def test_compress_pytest_output():
    # Simulate a long verbose pytest run with 50 passing dots and 2 failures
    lines = [
        "============================= test session starts =============================",
        "platform win32 -- Python 3.14.3, pytest-9.0.3, pluggy-1.6.0",
        "rootdir: D:\\Projects\\autoharness",
        "configfile: pyproject.toml",
        "collected 60 items",
        "",
        "tests\\test_module.py " + ("." * 40) + "F" + ("." * 10) + "F" + ("." * 8),
        "",
        "================================== FAILURES ===================================",
        "_________________________________ test_crash_one ______________________________",
        "    def test_crash_one():",
        ">       assert 1 == 2",
        "E       assert 1 == 2",
        "tests\\test_module.py:42: AssertionError",
        "_________________________________ test_crash_two ______________________________",
        "    def test_crash_two():",
        ">       raise ValueError('Invalid token syntax')",
        "E       ValueError: Invalid token syntax",
        "tests\\test_module.py:88: ValueError",
        "=========================== short test summary info ===========================",
        "FAILED tests/test_module.py::test_crash_one - assert 1 == 2",
        "FAILED tests/test_module.py::test_crash_two - ValueError: Invalid token syntax",
        "========================= 2 failed, 58 passed in 1.45s =========================",
    ]
    # Add 200 lines of extra noisy output
    noisy_output = "\n".join(lines[:7] + ["random debug info line " + str(i) for i in range(150)] + lines[7:])

    res = compression.compress_output(noisy_output, kind="pytest")
    assert res["kind"] == "pytest"
    assert res["compression_ratio"] >= 0.50
    assert "test_crash_one" in res["compressed_text"]
    assert "test_crash_two" in res["compressed_text"]
    assert "AssertionError" in res["compressed_text"]
    assert "ValueError: Invalid token syntax" in res["compressed_text"]
    assert "Passing test progress folded" in res["compressed_text"]

    report = compression.format_compressed_report(res)
    assert "# Output Compression Report" in report
    assert "pytest" in report


def test_compress_git_diff():
    # Long git diff with lots of unchanged lines
    diff_text = """diff --git a/src/core.py b/src/core.py
index 1234567..89abcdef 100644
--- a/src/core.py
+++ b/src/core.py
@@ -10,15 +10,15 @@ def existing():
 context line 1
 context line 2
 context line 3
-old_value = 100
+new_value = 200
 context line 4
 context line 5
""" + "\n".join([f" context line {i}" for i in range(100)])

    res = compression.compress_output(diff_text, kind="diff")
    assert res["kind"] == "diff"
    assert res["compression_ratio"] >= 0.60
    assert "src/core.py" in res["compressed_text"]
    assert "+new_value = 200" in res["compressed_text"]
    assert "Unchanged context folded" in res["compressed_text"]


def test_compress_traceback():
    log_text = """[INFO] 2026-11-15 Server starting up...
[INFO] Loading configurations from disk...
[INFO] Connecting to database cluster...
[DEBUG] Worker pool initialized with 8 threads
Traceback (most recent call last):
  File "app/main.py", line 45, in run
    service.start()
  File "app/service.py", line 120, in start
    raise ConnectionRefusedError("Database port 5432 unreachable")
ConnectionRefusedError: Database port 5432 unreachable
[INFO] Teardown hook finished
"""
    # Prepend 200 noisy log lines
    full_log = "\n".join([f"[DEBUG] background ping {i}" for i in range(100)]) + "\n" + log_text

    res = compression.compress_output(full_log, kind="traceback")
    assert res["kind"] == "traceback"
    assert res["compression_ratio"] >= 0.70
    assert "ConnectionRefusedError: Database port 5432 unreachable" in res["compressed_text"]
    assert "Preceding logs folded" in res["compressed_text"]


def test_compress_deep_traceback_preserves_root_cause():
    frames = ["Traceback (most recent call last):"]
    for i in range(100):
        frames.append(f'  File "module_{i}.py", line {i}, in step_{i}\n    do_work()')
    frames.append("ZeroDivisionError: integer division by zero")
    deep_traceback = "\n".join(frames)

    res = compression.compress_traceback(deep_traceback, max_lines=20)
    compressed, summary = res
    assert "Traceback root cause: ZeroDivisionError: integer division by zero" in summary
    assert "Traceback (most recent call last):" in compressed
    assert "traceback frames folded" in compressed
    # Crucial: root cause exception must NOT be chopped off by max_lines
    assert "ZeroDivisionError: integer division by zero" in compressed


def test_compress_pytest_errors_and_raw_persistence(tmp_path):
    error_output = (
        "============================= test session starts =============================\n"
        "collected 0 items / 1 error\n"
        "\n"
        "==================================== ERRORS ====================================\n"
        "__________________________ ERROR collecting test_foo.py _________________________\n"
        "ImportError: cannot import name 'UnknownSymbol' from 'pkg'\n"
        "=========================== short test summary info ===========================\n"
        "ERROR test_foo.py\n"
        "============================== 1 error in 0.05s ===============================\n"
    )
    res = compression.compress_output(
        error_output,
        kind="pytest",
        save_raw=True,
        save_dir=str(tmp_path)
    )
    assert res["kind"] == "pytest"
    assert "ImportError: cannot import name 'UnknownSymbol'" in res["compressed_text"]
    assert "raw_log_path" in res
    assert Path(res["raw_log_path"]).exists()
    assert Path(res["raw_log_path"]).read_text(encoding="utf-8") == error_output


def test_cli_skim(tmp_path, capsys):
    sample_file = tmp_path / "long_log.txt"
    sample_file.write_text(
        "Traceback (most recent call last):\n"
        "  File 'run.py', line 10, in <module>\n"
        "ZeroDivisionError: division by zero\n",
        encoding="utf-8"
    )

    class FakeArgs:
        target = str(sample_file)
        kind = "auto"
        max_lines = 60
        no_save = False
        json = True

    class FakeProject:
        root = tmp_path

    ret = cli.cmd_skim(FakeArgs(), FakeProject(), None)
    assert ret == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["exit_code"] == 0
    assert out["kind"] == "traceback"
    assert "ZeroDivisionError" in out["compressed_text"]
    assert "raw_log_path" in out
    assert Path(out["raw_log_path"]).exists()


def test_gateway_compress_output(tmp_path):
    repo = tmp_path / "gw_repo"
    repo.mkdir()

    gw = gateway.Gateway(repo, write=lambda s: None, log=lambda s: None)

    # 1. Markdown report
    res = gw._call({
        "name": "harness_compress_output",
        "arguments": {
            "text": "error: cannot find symbol Foo in module bar.cc:42\n" + "\n".join([f"compiling {i}.cc" for i in range(50)])
        }
    })
    text = res["content"][0]["text"]
    assert "# Output Compression Report" in text
    assert "compiler" in text

    # 2. Raw JSON
    res_json = gw._call({
        "name": "harness_compress_output",
        "arguments": {
            "text": "Traceback (most recent call last):\n  File 'a.py', line 1\nKeyError: 'secret'",
            "raw_json": True
        }
    })
    data = json.loads(res_json["content"][0]["text"])
    assert data["kind"] == "traceback"
    assert data["compression_ratio"] >= 0.0
    assert "KeyError" in data["compressed_text"]
