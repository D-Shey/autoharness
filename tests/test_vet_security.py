"""Tests for Clawvet-style Offline AST Security Gatekeeper for Skills and MCP configs."""
from __future__ import annotations

from pathlib import Path
from autoharness import vet


def test_scan_python_ast_dynamic_exec():
    code = """
def run_something(cmd):
    eval(cmd)
    exec("import os")
"""
    findings = vet.scan_python_ast_security(code, filename="malicious.py")
    rules = [f["rule"] for f in findings]
    assert "DYNAMIC_EXECUTION" in rules
    assert any(f["severity"] == "high" for f in findings)


def test_scan_python_ast_shell_injection_and_subprocess():
    code = """
import os
import subprocess

def do_work():
    os.system("rm -rf /")
    subprocess.Popen(["ls"], shell=True)
"""
    findings = vet.scan_python_ast_security(code, filename="shell_danger.py")
    rules = [f["rule"] for f in findings]
    assert "SHELL_INJECTION" in rules
    assert "DANGEROUS_SUBPROCESS" in rules


def test_scan_python_ast_sensitive_file_access():
    code = """
from pathlib import Path

def steal_keys():
    with open("~/.ssh/id_rsa", "r") as f:
        data = f.read()
    env_data = Path(".env").read_text()
"""
    findings = vet.scan_python_ast_security(code, filename="harvest.py")
    rules = [f["rule"] for f in findings]
    assert rules.count("SENSITIVE_FILE_ACCESS") >= 2


def test_scan_python_ast_env_harvesting():
    code = """
import os

def dump():
    all_env = dict(os.environ)
    copy_env = os.environ.copy()
"""
    findings = vet.scan_python_ast_security(code, filename="env_dump.py")
    rules = [f["rule"] for f in findings]
    assert "ENVIRONMENT_HARVESTING" in rules


def test_scan_python_ast_clean_code():
    code = """
def add(a: int, b: int) -> int:
    \"\"\"Pure function with no side effects.\"\"\"
    return a + b
"""
    findings = vet.scan_python_ast_security(code, filename="clean.py")
    assert len(findings) == 0


def test_scan_skill_security_ast(tmp_path: Path):
    clean_dir = tmp_path / "clean_skill"
    clean_dir.mkdir()
    (clean_dir / "tool.py").write_text("def run():\n    return 42\n", "utf-8")
    report = vet.scan_skill_security_ast(clean_dir)
    assert report["clean"] is True
    assert report["risk_level"] == "clean"

    bad_dir = tmp_path / "bad_skill"
    bad_dir.mkdir()
    (bad_dir / "attack.py").write_text("import os\nos.system('whoami')\n", "utf-8")
    bad_report = vet.scan_skill_security_ast(bad_dir)
    assert bad_report["clean"] is False
    assert bad_report["risk_level"] == "critical"
    assert bad_report["high_severity"] >= 1

    formatted = vet.format_ast_security_report(bad_report)
    assert "CRITICAL" in formatted
    assert "attack.py" in formatted


def test_scan_mcp_security():
    bad_config = {
        "command": "cmd.exe",
        "args": ["/c", "echo pwned", "--dangerously-skip-permissions"],
        "env": {"API_KEY": "sk-proj-123456789012345678901234567890"}
    }
    sec = vet.scan_mcp_security(bad_config)
    assert sec["clean"] is False
    assert sec["risk_level"] == "critical"
    rules = [f["rule"] for f in sec["findings"]]
    assert "SHELL_WRAPPER" in rules
    assert "PERMISSION_BYPASS" in rules
    assert "HARDCODED_SECRET_ENV" in rules

    clean_config = {
        "command": "node",
        "args": ["build/index.js"],
        "env": {"DEBUG": "false"}
    }
    clean_sec = vet.scan_mcp_security(clean_config)
    assert clean_sec["clean"] is True
    assert clean_sec["risk_level"] == "clean"
