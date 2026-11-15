"""Dynamic Secret Redaction and Shield for Agent Execution (inspired by keyshroud).

Pure Python standard library implementation (zero runtime dependencies).
Detects and redacts sensitive credentials, API tokens, private keys, authorization headers,
and known environment secrets from subprocess outputs, logs, and agent contexts.
Preserves error messages, stack traces, and line numbers while eliminating secret leakage.
"""
from __future__ import annotations

import os
import re
from typing import Pattern

# Environment variable substrings that denote secret values
SENSITIVE_ENV_KEYWORDS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "AUTH",
    "CREDENTIAL",
    "APIKEY",
    "PRIVATE",
)

# Standard non-secret environment variables to ignore
IGNORED_ENV_VARS = {
    "PATH", "PATHEXT", "PYTHONPATH", "PROMPT", "PS1", "SHELL", "TERM", "OS",
    "WINDIR", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "USERNAME", "TEMP", "TMP",
    "SYSTEMROOT", "SYSTEMDRIVE", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES",
    "HOMEPATH", "HOMEDRIVE", "COMPUTERNAME", "COMSPEC", "LANG", "LC_ALL", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL", "PROCESSOR_REVISION",
}

# Compiled regex patterns for well-known API tokens and credentials
SECRET_PATTERNS: list[tuple[str, Pattern[str]]] = [
    # 1. Private cryptographic keys (multiline)
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z0-9_ ]+PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9_ ]+PRIVATE KEY-----")),

    # 2. HTTP Authorization headers (Bearer, Basic, Token)
    ("AUTH_HEADER", re.compile(r"(?i)\b(Authorization\s*:\s*(?:Bearer|Basic|Token)\s+)[A-Za-z0-9_\-\.=+/]{12,}\b")),

    # 3. URL embedded passwords: http(s)://user:password@host
    ("URL_CREDENTIAL", re.compile(r"((?:https?|ftp|postgres(?:ql)?|mysql|mongodb)://[^:\s/]+:)([^@\s/]+)(@)")),

    # 4. OpenAI keys (standard and project-scoped)
    ("OPENAI_PROJ_KEY", re.compile(r"\b(sk-proj-[A-Za-z0-9_\-]{20,})\b")),
    ("OPENAI_KEY", re.compile(r"\b(sk-[A-Za-z0-9]{20,})\b")),

    # 5. Anthropic API keys
    ("ANTHROPIC_KEY", re.compile(r"\b(sk-ant-[A-Za-z0-9_\-]{20,})\b")),

    # 6. GitHub Personal Access Tokens and OAuth tokens
    ("GITHUB_FINE_GRAINED_PAT", re.compile(r"\b(github_pat_[A-Za-z0-9_]{50,})\b")),
    ("GITHUB_PAT", re.compile(r"\b(ghp_[A-Za-z0-9]{36})\b")),
    ("GITHUB_OAUTH", re.compile(r"\b(gho_[A-Za-z0-9]{36})\b")),
    ("GITHUB_USER", re.compile(r"\b(ghu_[A-Za-z0-9]{36})\b")),
    ("GITHUB_SERVER", re.compile(r"\b(ghs_[A-Za-z0-9]{36})\b")),
    ("GITHUB_REFRESH", re.compile(r"\b(ghr_[A-Za-z0-9]{36})\b")),

    # 7. HuggingFace tokens
    ("HF_TOKEN", re.compile(r"\b(hf_[A-Za-z0-9]{34})\b")),

    # 8. AWS Access Keys
    ("AWS_ACCESS_KEY", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),

    # 9. Slack tokens
    ("SLACK_TOKEN", re.compile(r"\b(xox[baprs]-[0-9A-Za-z]{10,48})\b")),

    # 10. JSON Web Tokens (JWT)
    ("JWT_TOKEN", re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_\-\.]+)\b")),
]


def scan_environment_secrets(min_length: int = 6) -> list[tuple[str, str]]:
    """Scan current environment variables for sensitive tokens and passwords.

    Returns a list of (var_name, secret_value) sorted by secret length descending.
    """
    found: list[tuple[str, str]] = []
    for k, v in os.environ.items():
        k_upper = k.upper()
        if k_upper in IGNORED_ENV_VARS:
            continue
        if any(kw in k_upper for kw in SENSITIVE_ENV_KEYWORDS):
            val = v.strip()
            # Ignore short values or common booleans/integers
            if len(val) >= min_length and val.lower() not in ("true", "false", "null", "none", "0", "1"):
                found.append((k, val))

    # Sort longest values first to ensure greedy matching
    found.sort(key=lambda kv: len(kv[1]), reverse=True)
    return found


def redact_secrets(
    text: str,
    custom_secrets: list[str] | None = None,
    redact_env: bool = True,
    placeholder_template: str = "[REDACTED:{kind}]",
) -> str:
    """Redact sensitive credentials, tokens, and environment secrets from text.

    Preserves text structure, exit codes, and stack traces while replacing
    sensitive values with deterministic redaction markers.
    """
    if not text:
        return ""

    result = text

    # 1. Redact multiline private keys first
    for kind, pattern in SECRET_PATTERNS:
        if kind == "PRIVATE_KEY":
            result = pattern.sub(placeholder_template.format(kind="PRIVATE_KEY"), result)
        elif kind == "AUTH_HEADER":
            result = pattern.sub(rf"\g<1>{placeholder_template.format(kind='AUTH_HEADER')}", result)
        elif kind == "URL_CREDENTIAL":
            result = pattern.sub(rf"\g<1>{placeholder_template.format(kind='PASSWORD')}\g<3>", result)
        else:
            result = pattern.sub(placeholder_template.format(kind=kind), result)

    # 2. Redact environment variables if requested
    if redact_env:
        for var_name, secret_val in scan_environment_secrets():
            if secret_val in result:
                result = result.replace(secret_val, placeholder_template.format(kind=f"ENV_{var_name}"))

    # 3. Redact any custom explicit secrets provided
    if custom_secrets:
        sorted_custom = sorted((s for s in custom_secrets if len(s) >= 4), key=len, reverse=True)
        for s in sorted_custom:
            if s in result:
                result = result.replace(s, placeholder_template.format(kind="CUSTOM"))

    return result


def has_secrets(text: str) -> bool:
    """Return True if text contains unredacted known secret signatures or sensitive env values."""
    if not text:
        return False

    for _, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return True

    for _, secret_val in scan_environment_secrets():
        if secret_val in text:
            return True

    return False
