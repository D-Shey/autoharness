"""Tests for AutoHarness Dynamic Secret Redaction and Shield (inspired by keyshroud)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from autoharness import runs, secrets
from autoharness.env import Project


def test_redact_openai_and_anthropic_tokens():
    fake_openai = "sk-" + "abcdef1234567890abcdef123456"
    fake_proj = "sk-proj-" + "123456789012345678901234567890"
    fake_ant = "sk-ant-api03-" + "abcdef12345678901234567890"
    text = (
        f"Config loaded: key={fake_openai}\n"
        f"Project key: {fake_proj}\n"
        f"Anthropic key: {fake_ant}"
    )
    redacted = secrets.redact_secrets(text)
    assert "sk-abcdef" not in redacted
    assert "sk-proj-" not in redacted
    assert "sk-ant-" not in redacted
    assert "[REDACTED:OPENAI_KEY]" in redacted
    assert "[REDACTED:OPENAI_PROJ_KEY]" in redacted
    assert "[REDACTED:ANTHROPIC_KEY]" in redacted


def test_redact_github_and_huggingface_tokens():
    fake_ghp = "ghp_" + "123456789012345678901234567890123456"
    fake_pat = "github_pat_" + "11AAAAAAA_0123456789abcdefghijklmnopqrstuvwxyz0123456789"
    fake_hf = "hf_" + "abcdefghijklmnopqrstuvwxyz01234567"
    text = (
        f"Cloning repo with token {fake_ghp}\n"
        f"Fine-grained: {fake_pat}\n"
        f"Model download using {fake_hf}"
    )
    redacted = secrets.redact_secrets(text)
    assert "ghp_" not in redacted
    assert "github_pat_" not in redacted
    assert "hf_" not in redacted
    assert "[REDACTED:GITHUB_PAT]" in redacted
    assert "[REDACTED:GITHUB_FINE_GRAINED_PAT]" in redacted
    assert "[REDACTED:HF_TOKEN]" in redacted


def test_redact_jwt_and_bearer_and_aws():
    fake_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    fake_slack = "xoxb" + "-1234567890-123456789012-abcdefghijklmnopqrstuvwx"
    text = (
        f"Authorization: Bearer {fake_jwt}\n"
        "AWS credentials: AKIAIOSFODNN7EXAMPLE\n"
        f"Slack webhook: {fake_slack}"
    )
    redacted = secrets.redact_secrets(text)
    assert fake_jwt not in redacted
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "xoxb-" not in redacted
    assert "[REDACTED:AUTH_HEADER]" in redacted
    assert "[REDACTED:AWS_ACCESS_KEY]" in redacted
    assert "[REDACTED:SLACK_TOKEN]" in redacted


def test_redact_private_key_and_url_credentials():
    priv_key = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0Yv+Z4B8...fake...fake...\n"
        "-----END RSA PRIVATE KEY-----"
    )
    text = (
        f"Server identity key:\n{priv_key}\n"
        "Connecting to postgres://dbuser:super_secret_pass_123@db.internal:5432/prod"
    )
    redacted = secrets.redact_secrets(text)
    assert "BEGIN RSA PRIVATE KEY" not in redacted
    assert "super_secret_pass_123" not in redacted
    assert "[REDACTED:PRIVATE_KEY]" in redacted
    assert "postgres://dbuser:[REDACTED:PASSWORD]@db.internal:5432/prod" in redacted


def test_redact_environment_secrets(monkeypatch):
    monkeypatch.setenv("AUTOHARNESS_TEST_API_TOKEN", "classified_token_xyz_987654")
    monkeypatch.setenv("DB_PASSWORD", "my_secret_database_password_2026")

    text = "Failed connecting with classified_token_xyz_987654 (using database password value my_secret_database_password_2026)"
    redacted = secrets.redact_secrets(text)

    assert "classified_token_xyz_987654" not in redacted
    assert "my_secret_database_password_2026" not in redacted
    assert "[REDACTED:ENV_AUTOHARNESS_TEST_API_TOKEN]" in redacted
    assert "[REDACTED:ENV_DB_PASSWORD]" in redacted


def test_has_secrets_detection(monkeypatch):
    assert secrets.has_secrets("Clean line without any tokens") is False
    assert secrets.has_secrets("Found token: ghp_123456789012345678901234567890123456") is True

    monkeypatch.setenv("SECRET_AUTH_KEY", "env_secret_val_123456")
    assert secrets.has_secrets("Value is env_secret_val_123456 in logs") is True


def test_runs_redaction_integration(tmp_path):
    project = Project(tmp_path)
    (tmp_path / ".autoharness" / "tasks" / "task_test_sec").mkdir(parents=True, exist_ok=True)
    task = project.task("task_test_sec")
    (task.dir / "task.json").write_text(json.dumps({"runs": [], "mode": "light"}), encoding="utf-8")

    fake_token = "sk-proj-987654321098765432109876543210"
    cmd = f'python -c "print(\'Logging token: {fake_token}\')"'

    # 1. Run with redaction (default)
    item = runs.run(task, cmd, name="pytest", purpose="B1", redact=True)
    assert item["passed"] is True
    assert fake_token not in item["skim"]
    assert "[REDACTED:OPENAI_PROJ_KEY]" in item["skim"]

    log_path = Path(item["log"])
    log_content = log_path.read_text(encoding="utf-8")
    assert fake_token not in log_content
    assert "[REDACTED:OPENAI_PROJ_KEY]" in log_content


def test_runs_no_redact_flag(tmp_path):
    project = Project(tmp_path)
    (tmp_path / ".autoharness" / "tasks" / "task_test_no_sec").mkdir(parents=True, exist_ok=True)
    task = project.task("task_test_no_sec")
    (task.dir / "task.json").write_text(json.dumps({"runs": [], "mode": "light"}), encoding="utf-8")

    fake_token = "sk-proj-unredactedtoken123456789012345"
    cmd = f'python -c "print(\'Raw token: {fake_token}\')"'

    # 2. Run with redaction disabled (redact=False)
    item = runs.run(task, cmd, name="pytest", purpose="B1", redact=False)
    assert item["passed"] is True
    assert fake_token in item["skim"]
    log_content = Path(item["log"]).read_text(encoding="utf-8")
    assert fake_token in log_content


def test_redact_google_api_key_and_git_url():
    fake_google = "AIzaSy" + "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q7"
    fake_git_url = "https://oauth2:ghp_123456789012345678901234567890123456@github.com/org/repo.git"
    text = f"Google Gemini key: {fake_google}\nGit clone url: {fake_git_url}"
    redacted = secrets.redact_secrets(text)
    assert fake_google not in redacted
    assert "[REDACTED:GOOGLE_API_KEY]" in redacted
    assert "ghp_" not in redacted


def test_mask_dict_recursive():
    fake_key = "sk-proj-123456789012345678901234567890"
    payload = {
        "service": "openai",
        "nested": {
            "token": fake_key,
            "items": [1, 2, f"key={fake_key}"]
        }
    }
    masked = secrets.mask_dict(payload)
    assert fake_key not in json.dumps(masked)
    assert "[REDACTED:OPENAI_PROJ_KEY]" in masked["nested"]["token"]
    assert "[REDACTED:OPENAI_PROJ_KEY]" in masked["nested"]["items"][2]

