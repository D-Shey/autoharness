"""Local library of skills the agent wrote itself, and the checks they pass before saving.

    <AUTOHARNESS_HOME or ~/.autoharness>/skills/<name>/          the skill
    <AUTOHARNESS_HOME or ~/.autoharness>/skills/<name>.meta.json  where it came from

A saved skill is installed elsewhere as `local:<name>`. Checks here guard against
leaking things out of a project (secrets, personal paths); trust.scan_skill still
guards the agent against dangerous instructions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from autoharness.env import HarnessError
from autoharness.trust import scan_skill

FILL_MARKER = "<!-- autoharness: fill in -->"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
TEXT_SUFFIXES = {".md", ".txt", ".py", ".js", ".ts", ".mjs", ".cjs", ".sh", ".ps1", ".json", ".yaml", ".yml", ".toml", ""}

SECRETS = [
    ("GitHub token", re.compile(r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]+")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("credential assignment", re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"$<{]{8,}")),
]
PRIVACY = [
    ("absolute user path", re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+|/home/[^/\s]+/|/Users/[^/\s]+/")),
    ("email address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
]


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise HarnessError(f"skill name must match {NAME_RE.pattern}, got {name!r}")


def template(name: str, description: str) -> str:
    return (f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n"
            f"## When to use\n{FILL_MARKER}\n\n## Steps\n{FILL_MARKER}\n\n## How to verify\n{FILL_MARKER}\n")


@dataclass
class CheckResult:
    status: str                      # ok | invalid | blocked
    description: str = ""
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _frontmatter(text: str) -> dict[str, str] | None:
    if not text.startswith("---\n") or "\n---" not in text[4:]:
        return None
    head = text[4:text.index("\n---", 4)]
    return {k.strip(): v.strip() for k, _, v in (line.partition(":") for line in head.splitlines()) if k.strip()}


def _text_files(src: Path):
    for path in sorted(src.rglob("*")):
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES and path.stat().st_size <= 1_000_000:
            try:
                yield path, path.read_text("utf-8")
            except UnicodeDecodeError:
                continue


def check(src: Path) -> CheckResult:
    """Format, then secrets, then the dangerous-pattern scan; privacy findings only warn."""
    skill_md = src / "SKILL.md"
    if not skill_md.is_file():
        return CheckResult("invalid", problems=["SKILL.md is missing"])
    fm = _frontmatter(skill_md.read_text("utf-8"))
    problems = []
    if fm is None:
        problems.append("SKILL.md has no frontmatter (--- name/description ---)")
    else:
        if fm.get("name") != src.name:
            problems.append(f"frontmatter name {fm.get('name')!r} must equal the folder name {src.name!r}")
        if not fm.get("description"):
            problems.append("frontmatter description is empty")
    files = list(_text_files(src))
    for path, text in files:
        if FILL_MARKER in text:
            problems.append(f"{path.relative_to(src)} still has '{FILL_MARKER}' — fill in every section")
    description = (fm or {}).get("description", "")
    if problems:
        return CheckResult("invalid", description, problems)

    for path, text in files:
        for i, line in enumerate(text.splitlines(), 1):
            for label, pattern in SECRETS:
                if pattern.search(line):
                    problems.append(f"possible secret ({label}) at {path.relative_to(src)}:{i}")
    findings = scan_skill(src)
    problems += [f"{f.message} at {f.file}:{f.line}" for f in findings if f.severity == "high"]
    warnings = [f"{f.message} at {f.file}:{f.line}" for f in findings if f.severity != "high"]
    if problems:
        return CheckResult("blocked", description, problems, warnings)

    for path, text in files:
        for i, line in enumerate(text.splitlines(), 1):
            for label, pattern in PRIVACY:
                if pattern.search(line):
                    warnings.append(f"{label} at {path.relative_to(src)}:{i}")
    return CheckResult("ok", description, [], warnings)
