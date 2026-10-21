"""Local library of skills the agent wrote itself, and the checks they pass before saving.

    <AUTOHARNESS_HOME or ~/.autoharness>/skills/<name>/          the skill
    <AUTOHARNESS_HOME or ~/.autoharness>/skills/<name>.meta.json  where it came from

A saved skill is installed elsewhere as `local:<name>`. Checks here guard against
leaking things out of a project (secrets, personal paths); trust.scan_skill still
guards the agent against dangerous instructions.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from autoharness import text
from autoharness.env import RUNTIME_JUNK, HarnessError, _hash_dir
from autoharness.recipes import home
from autoharness.trust import TrustReport, scan_skill

FILL_MARKER = "<!-- autoharness: fill in -->"
NAME_RE = re.compile(r"^(?!(?:con|prn|aux|nul|com\d|lpt\d)$)[a-z0-9][a-z0-9-]{1,63}$")  # no Windows device names
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


# procedure travels to the next task; a value measured in one project (a threshold, a mapping, a colour)
# is only a hypothesis there, so a skill keeps the two apart
FACTS_NOTE = ("<!-- optional: values verified in the project this skill came from (a threshold, a mapping, a colour), "
              "each with where it was measured. Another task re-checks them before relying on them; procedure "
              "goes in Steps. -->")


def template(name: str, description: str) -> str:
    return (f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n"
            f"## When to use\n{FILL_MARKER}\n\n## Steps\n{FILL_MARKER}\n\n## How to verify\n{FILL_MARKER}\n\n"
            f"## Facts found here\n{FACTS_NOTE}\n")


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
    return CheckResult("ok", description, [], warnings + craft(description, skill_md.read_text("utf-8")))


MAX_LINES = 500
RETOLD_STEPS = re.compile(r"\bthen\b|->|→|\bstep \d", re.IGNORECASE)
SHOUTING = re.compile(r"\b(?:MUST|NEVER|ALWAYS|CRITICAL|IMPORTANT)\b")


def craft(description: str, skill_md: str) -> list[str]:
    """What tends to make a skill go unused or unread; warnings, the author decides."""
    found = []
    if len(description.split()) < 4:
        found.append("the description is too short for an agent to tell when to open the skill: name the "
                     "situation and the words a request would use")
    if RETOLD_STEPS.search(description):
        found.append("the description seems to retell the steps: say when to use the skill instead, or an agent "
                     "may follow the summary and skip the body")
    if (n := len(skill_md.splitlines())) > MAX_LINES:
        found.append(f"SKILL.md has {n} lines: move reference material into references/ and say when to read it")
    if len(SHOUTING.findall(skill_md)) >= 5:
        found.append("many MUST/NEVER/ALWAYS: say why a step matters, so an agent can handle the case the skill "
                     "did not foresee")
    return found


def root() -> Path:
    return home() / "skills"


def skill_dir(name: str) -> Path:
    return root() / name


def meta_path(name: str) -> Path:
    return root() / f"{name}.meta.json"


def exists(name: str) -> bool:
    return skill_dir(name).is_dir()


def save(src: Path, meta: dict) -> Path:
    """Copy `src` into the library, replacing any earlier version, and record where it came from."""
    validate_name(src.name)
    dest = skill_dir(src.name)
    if dest.exists():
        shutil.rmtree(dest)
    root().mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*RUNTIME_JUNK))
    record = meta | {"name": src.name, "saved": time.strftime("%Y-%m-%dT%H:%M:%S"), "sha256": _hash_dir(dest)}
    meta_path(src.name).write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return dest


def list_skills() -> tuple[list[dict], list[str]]:
    entries, warnings = [], []
    if not root().exists():
        return entries, warnings
    for path in sorted(root().glob("*.meta.json")):
        try:
            m = json.loads(path.read_text("utf-8"))
            entries.append({"name": m["name"], "description": m.get("description", ""),
                            "capabilities": m.get("capabilities", []), "saved": m.get("saved")})
        except (ValueError, KeyError):
            warnings.append(f"unreadable metadata {path.name}; skipped")
    return entries, warnings


def delete(name: str) -> None:
    validate_name(name)
    if not exists(name):
        raise HarnessError(f"no skill {name!r} in the library")
    shutil.rmtree(skill_dir(name))
    meta_path(name).unlink(missing_ok=True)


def with_capability(capability: str) -> list[dict]:
    return [{"name": e["name"], "description": e["description"]}
            for e in list_skills()[0] if capability in e["capabilities"]]


MATCH_STEMS = 2  # shared content-word stems between a query and a skill's name and description


def matching(capabilities: list[str], query: str) -> list[dict]:
    """Saved skills that fit: tagged with one of `capabilities` (the capability they were saved for), named
    `query`, or sharing MATCH_STEMS content words with it in their name and description. The tag is what
    carries across languages: a Russian task finds an English description through its capability."""
    wanted = text.stems(query.replace("-", " "))
    out = []
    for e in list_skills()[0]:
        own = text.stems(f"{e['name'].replace('-', ' ')} {e['description']}")
        if (set(e["capabilities"]) & set(capabilities) or e["name"] == query.strip()
                or len(wanted & own) >= MATCH_STEMS):
            out.append({"name": e["name"], "description": e["description"]})
    return out


def stage_from_library(repo: str, skill: str, stage_dir: Path) -> Path:
    """Stager for `local:<name>`: the library copy is the staged skill (add_skill scans, then copies it)."""
    if not exists(skill):
        raise HarnessError(f"no skill {skill!r} in the library; see `autoharness skill list`")
    return skill_dir(skill)


def trust_report(spec: str) -> dict:
    return TrustReport(spec, 100, "high", ["saved to the local library with the user's approval",
                                           "skills run with full agent permissions; content is statically scanned before install"],
                       False, None, {"runs_with": "full agent permissions"}).to_dict()
