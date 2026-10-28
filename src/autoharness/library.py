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
# a skill that has the agent change a file a later run reads, with no word on how: written in place, a crash
# leaves it half-written or empty (R1 of evidence.CODE_CHECKS). Judged sentence by sentence: one that names a
# data file (or saved state) and tells the agent to change it. Not a script or page the agent produces, not a
# file the sentence leaves alone or hands to the user, not what a tool does ("it updates the lock file").
# R7: the name is bounded and starts at a name boundary, so a long run of word characters stays linear
DATA_FILE = re.compile(r"(?<![\w./~-])[\w./~-]{0,200}?[\w-]\.(?:json|jsonl|ya?ml|toml|db|sqlite3?|csv|ini|lock|pickle|pkl)\b"
                       r"(?![`'\"]?\s+(?:unchanged|untouched|as\s+is|for\s+the\s+user))", re.IGNORECASE)
SAVED_STATE = re.compile(r"\b(?:state|cache|index|progress|settings|results?|history|checkpoint)\s+files?\b|"
                         r"\b(?:sav(?:e|ing)|persist(?:ing)?|stor(?:e|ing)|writ(?:e|ing)|record(?:ing)?|keep(?:ing)?)\s+"
                         r"(?:the\s+|its\s+|their\s+|your\s+)?(?:[\w-]+\s+)?state\b(?!\s+(?:of|machine))|"
                         r"\bto\s+disk\b|\b(?:next|later|following)\s+(?:run|session)\b|\bbetween\s+(?:runs|sessions)\b|"
                         r"состояни|кэш|прогресс", re.IGNORECASE)
# a change the sentence asks for: the verb's base or "-ing" form anywhere in it, so "you must write", "please
# save" and "make sure to update" count; English imperatives drop the subject and are negated with "don't"
# (https://en.wikipedia.org/wiki/Imperative_mood), while "it updates" or "the CLI stores" describe someone else
CHANGE = re.compile(r"(?<![\w.-])(?P<verb>"
                    r"write|writing|overwrite|overwriting|rewrite|rewriting|save|saving|store|storing|persist|"
                    r"persisting|dump|update|updating|append|appending|edit|editing|modify|change|"
                    r"add|insert|log|track|put|mark|record|set|increment|bump|keep|serialize|serialise|"
                    r"запис\w*|запиш\w*|перезапи\w*|пиши|сохран\w*|обнов\w*|добав\w*|измени\w*|отмет\w*|"
                    r"внес\w*|внос\w*|полож\w*)(?![\w-]|\.\w)", re.IGNORECASE)
# a negation reaches right only to the end of its clause: "the scope of a trigger term extends to the right of the
# trigger term and ends at a termination term" (ConText, https://pmc.ncbi.nlm.nih.gov/articles/PMC2757457/), so
# "if the user does not answer, save the state" still asks for the write; "don't forget to write" is no negation
NEG_TRIGGER = re.compile(r"\b(?:do\s+not|does\s+not|don't|doesn't|never|not|no|без|не|нельзя)\b(?!\s+(?:forget|забы|забуд))",
                         re.IGNORECASE)
CLAUSE_END = re.compile(r"[,:;()]|\s[-–—]\s|\b(?:then|and|but|so|after|before|once|потом|затем|и|но|после)\b",
                        re.IGNORECASE)
# the verb used as a noun ("at write time", "the change log", "a record") or as a plan ("about to change")
NOUN_USE = re.compile(r"\b(?:at|on|per|each|every|the|a|an|this|that|its|their|your|about\s+to|going\s+to)\s+$", re.IGNORECASE)
# what gets written is code or a page that uses the data file, not the file: "write a test (a script) that parses
# settings.json"
CODE_OBJECT = re.compile(r"\s+(?:(?:a|an|the|new|small|unit|more|python|bash|shell|node|js|helper)\s+){0,3}"
                         r"(?:tests?|scripts?|modules?|functions?|class(?:es)?|programs?|parsers?|pages?|components?|"
                         r"code|tools?|helpers?|cli)\b", re.IGNORECASE)
# a project's own manifests are edited by hand or by an editor tool, not kept as state between runs
MANIFESTS = {"package.json", "package-lock.json", "tsconfig.json", "jsconfig.json", "vercel.json", "plugin.json",
             "manifest.json", "pyproject.toml", "cargo.toml", "cargo.lock", "composer.json", "poetry.lock",
             "uv.lock", "yarn.lock", "pnpm-lock.yaml", ".mcp.json", "mcp.json", "app.json", "deno.json"}
SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n")
# a way of writing, said of a write; it covers the writes of its own paragraph, and the whole skill only when
# what it says is written that way is state or files ("state files are written atomically"): "write the report
# atomically" says nothing about queue.json elsewhere
ATOMIC_WRITE = re.compile(r"\b(?:writ|sav|stor|persist)\w*\b[^.\n]{0,60}\batomically\b|"
                          r"\batomic(?:ally)?\s+(?:write|replace|rename|save|swap)|атомарн", re.IGNORECASE)
ATOMIC_BLANKET = re.compile(r"\b(?:state|data|json|cache|all|every|each|any)\b[^.\n]{0,30}\bfiles?\b[^.\n]{0,40}"
                            r"\batomic|\batomic\w*\b[^.\n]{0,40}\b(?:state|data|every|all)\b[^.\n]{0,20}\bfiles?\b|"
                            r"\b(?:state|data)\s+(?:is|are)\s+(?:always\s+)?(?:written|saved|stored)\s+atomically|"
                            r"(?:все|любые)\s+файлы[^.\n]{0,40}атомарн", re.IGNORECASE)
TEMP_FILE = re.compile(r"temp(?:orary)?\s+file|tempfile|временн\w*\s+файл", re.IGNORECASE)
MOVED_OVER = re.compile(r"os\.replace|\b(?:rename|move|mv|replace)\w*\b|переимен\w*|замени\w*", re.IGNORECASE)
# read, then write back: the in-place update R1 is about, whatever the file is called
READ_THEN_WRITE = re.compile(r"\bread\w*\b[^\n]{0,60}\b(?:then|and)\s+(?:write|save|update|overwrite)\w*\s+(?:it|the\s+"
                             r"file)\s+back\b|\bread\w*\b[^\n]{0,60}\bthen\s+(?:write|save|update|overwrite)\b|"
                             r"\bчита\w*\b[^\n]{0,60}\b(?:потом|затем)\s+(?:пиш|запис|сохран)", re.IGNORECASE)


def _negated(before: str) -> bool:
    """Whether a negation stands in the verb's own clause, before it."""
    return bool(NEG_TRIGGER.search(CLAUSE_END.split(before)[-1]))


def _asks_change(sentence: str, data) -> bool:
    for verb in CHANGE.finditer(sentence):
        word, before = verb["verb"].lower(), sentence[:verb.start()]
        if CODE_OBJECT.match(sentence, verb.end()) or _negated(before) or NOUN_USE.search(before):
            continue
        # "keep the session alive", "keep x.json lean": keep writes only into a file ("keep it in state.json")
        if word.startswith("keep") and not (data and re.search(r"\bin\s+[`'\"]?" + re.escape(data.group(0)), sentence)):
            continue
        return True
    return False


def _covered(sentence: str, near: list[str], data) -> bool:
    """A way of writing named beside this write: in the sentence itself, or in its paragraph as an atomic write,
    a temp file renamed over, or a rename over this very file."""
    if ATOMIC_WRITE.search(sentence) or TEMP_FILE.search(sentence) or "os.replace" in sentence:
        return True
    for other in near:
        if ATOMIC_WRITE.search(other) or (TEMP_FILE.search(other) and MOVED_OVER.search(other)):
            return True
        if data and data.group(0) in other and MOVED_OVER.search(other):
            return True
    return False


def writes_saved_state(skill_md: str) -> bool:
    """Whether the skill tells the agent to change a file a later run reads without saying how to write it."""
    body = re.sub(r"\A---\n.*?\n---\n", "", skill_md, flags=re.S)  # the frontmatter describes
    if ATOMIC_BLANKET.search(body):
        return False
    for paragraph in re.split(r"\n\s*\n", body):
        sentences = [s.strip() for s in SENTENCE.split(paragraph) if s.strip()]
        for sentence in sentences:
            if sentence.startswith("#"):  # a heading names a topic ("## queue.json update")
                continue
            data = next((m for m in DATA_FILE.finditer(sentence)  # not a manifest, not a URL
                         if m.group(0).rsplit("/", 1)[-1].lower() not in MANIFESTS and sentence[m.start() - 1:m.start()] != ":"),
                        None)
            asked = (READ_THEN_WRITE.search(sentence) or re.search(r"\bjson\.dump\b", sentence)
                     or ((data or SAVED_STATE.search(sentence)) and _asks_change(sentence, data)))
            if asked and not _covered(sentence, sentences, data):
                return True
    return False


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
    if writes_saved_state(skill_md):
        found.append("the skill has the agent write a file or saved state but does not say how: say to write it "
                     "atomically (a temp file in the same directory, then os.replace), or a crash can leave it "
                     "half-written (R1)")
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
