"""Trust scoring for discovered MCP servers and static scanning of downloaded skills.

The goal is not to prove something safe, but to make the risky cases loud:
anything that needs secrets, runs arbitrary containers, or contains
exfiltration-looking code must go through explicit human approval.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

LEVELS = ((70, "high"), (45, "medium"), (0, "low"))


def _level(score: int) -> str:
    return next(name for threshold, name in LEVELS if score >= threshold)


@dataclass
class TrustReport:
    name: str
    score: int
    level: str
    reasons: list[str]
    requires_approval: bool
    config: dict | None
    permissions: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def curated_report(name: str, entry: dict) -> TrustReport:
    perms = entry["permissions"]
    secrets = perms.get("secrets", [])
    reasons = ["curated catalog entry"]
    if secrets:
        reasons.append(f"needs secrets: {', '.join(secrets)}")
    return TrustReport(name, 90, "high", reasons, bool(secrets), entry["config"], perms)


def _github_owner(url: str) -> str | None:
    m = re.match(r"https?://github\.com/([^/]+)/", url or "")
    return m.group(1).lower() if m else None


def _config_from_package(pkg: dict) -> tuple[dict | None, list[str]]:
    """Translate a registry `packages[]` entry into a Claude Code .mcp.json server config."""
    kind, ident, version = pkg.get("registryType"), pkg.get("identifier"), pkg.get("version")
    if pkg.get("transport", {}).get("type", "stdio") != "stdio":
        return None, []
    passed = [v["name"] for v in pkg.get("environmentVariables", []) if v.get("isRequired") or v.get("isSecret")]
    env = {name: f"${{{name}}}" for name in passed}
    if kind == "npm":
        cfg = {"command": "npx", "args": ["-y", f"{ident}@{version}" if version else ident]}
    elif kind == "pypi":
        cfg = {"command": "uvx", "args": [f"{ident}=={version}" if version else ident]}
    elif kind == "oci":
        cfg = {"command": "docker", "args": ["run", "-i", "--rm", ident]}
    else:
        return None, passed
    if env:
        cfg["env"] = env
    return cfg, passed


def _config_from_remote(remote: dict) -> tuple[dict, list[str]]:
    rtype = "sse" if remote.get("type") == "sse" else "http"
    cfg: dict = {"type": rtype, "url": remote["url"]}
    secrets = []
    headers = {}
    for h in remote.get("headers", []):
        var = re.sub(r"[^A-Z0-9]", "_", h["name"].upper()) + "_TOKEN"
        headers[h["name"]] = f"${{{var}}}"
        secrets.append(var)
    if headers:
        cfg["headers"] = headers
    return cfg, secrets


def _dns_publisher(ns: str, trusted_namespaces: dict[str, str]) -> str | None:
    """The trusted publisher a DNS namespace belongs to, if it is listed. Exactly as listed: subdomains are
    not the publisher's (anyone gets `<bucket>.s3.amazonaws.com`)."""
    return trusted_namespaces.get(ns)


def score_registry_entry(entry: dict, trusted_publishers: set[str] = frozenset(),
                         trusted_namespaces: dict[str, str] | None = None) -> TrustReport:
    """Score a registry entry. A verified namespace only proves who published it;
    reaching "high" also requires a publisher on the trusted list."""
    server = entry.get("server", {})
    meta = entry.get("_meta", {}).get("io.modelcontextprotocol.registry/official", {})
    name = server.get("name", "?")
    score, reasons = 25, []

    if meta.get("status") == "active":
        score += 5
    else:
        score -= 20
        reasons.append(f"registry status: {meta.get('status', 'unknown')}")

    repo_owner = _github_owner(server.get("repository", {}).get("url", ""))
    if repo_owner:
        score += 5
        reasons.append(f"source repo on GitHub ({repo_owner})")
    else:
        reasons.append("no public source repository")
        score -= 10
    # who published it is what the registry verified: the GitHub owner of an io.github.<owner> namespace, or
    # the owner of a DNS namespace on the curated list. The repository URL is whatever the publisher wrote.
    ns = name.split("/")[0].lower()
    if repo_owner and ns == f"io.github.{repo_owner}":
        publisher = repo_owner
        score += 15
        reasons.append("namespace verified against GitHub owner")
    elif publisher := _dns_publisher(ns, trusted_namespaces or {}):
        score += 15
        reasons.append(f"namespace {ns} is a domain of {publisher}, verified by the registry")
    if publisher in trusted_publishers:
        score += 25
        reasons.append(f"publisher {publisher} is on the trusted list")
    elif repo_owner in trusted_publishers:
        reasons.append(f"claims a repository of {repo_owner}, but namespace {ns} is not verified against it")

    config, secrets, env_names = None, [], []
    packages = server.get("packages", [])
    remotes = server.get("remotes", [])
    for pkg in packages:
        config, env_names = _config_from_package(pkg)
        secrets = [v["name"] for v in pkg.get("environmentVariables", []) if v.get("isSecret") and v["name"] in env_names]
        if config:
            if pkg.get("registryType") == "oci":
                reasons.append("runs a container image")
                score -= 5
            else:
                score += 10
                reasons.append(f"{pkg.get('registryType')} package, pinned version {pkg.get('version')}")
            break
    if config is None and remotes:
        config, secrets = _config_from_remote(remotes[0])
        env_names = secrets
        reasons.append(f"remote server at {remotes[0]['url']} (your data leaves the machine)")
    if config is None:
        reasons.append("no installable package or remote found")
        score -= 30

    if secrets:
        reasons.append(f"needs secrets: {', '.join(secrets)}")
    # a value is read from the user's environment whatever the publisher calls it: a key marked merely
    # "required" leaks just the same, so every variable passed on is shown and needs approval
    if others := [n for n in env_names if n not in secrets]:
        reasons.append(f"reads from your environment (not marked secret, but may be): {', '.join(others)}")

    score = max(0, min(100, score))
    level = _level(score)
    perms = {"network": "any", "filesystem": "unknown", "shell": "unknown", "secrets": secrets, "env": env_names}
    requires_approval = bool(env_names) or level != "high" or bool(remotes and config and "url" in config)
    return TrustReport(name, score, level, reasons, requires_approval, config, perms)


# --- skill scanning ---------------------------------------------------------

SKILL_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("high", "pipes a download into a shell", re.compile(r"(curl|wget|iwr|Invoke-WebRequest)[^\n|]*\|\s*(ba|z)?sh|\|\s*iex", re.I)),
    ("high", "reads credential stores", re.compile(r"~/\.ssh|id_rsa|\.aws/credentials|\.netrc|\.git-credentials|Login Data|\.keychain(?:-db)?\b"
                                            r"|security\s+(?:find-(?:generic|internet)-password|dump-keychain)", re.I)),
    ("high", "destructive recursive delete of home/root", re.compile(r"rm\s+-rf\s+(/|~|\$HOME)(\s|$)", re.I)),
    ("high", "instructs the agent to hide actions", re.compile(r"(do not|don't|never) (tell|inform|mention to) the user|without (telling|asking) the user", re.I)),
    ("medium", "decodes obfuscated payloads", re.compile(r"base64\s+(-d|--decode)|FromBase64String|atob\(", re.I)),
    ("medium", "posts data to a remote endpoint", re.compile(r"curl[^\n]*(-d|--data|-F|-X\s*POST)|requests\.post\(|fetch\([^)]*method:\s*['\"]POST", re.I)),
    # Whole-environment dumps only: copying the env for a subprocess or reading one variable is ordinary.
    ("medium", "dumps environment variables", re.compile(
        r"\bprintenv\b|\b(Get-ChildItem|gci|dir|ls)\s+env:|os\.environ\.items\(\)|Object\.(entries|keys)\(process\.env\)"
        r"|\b(print|pprint|dumps?|repr|str|write|log|stringify|send|post)\s*\([^)\n]*\b(os\.environ|process\.env)\b(?!\s*[.\[])",
        re.I)),
    ("low", "disables safety checks", re.compile(r"--no-verify|--dangerously|bypassPermissions", re.I)),
]

TEXT_SUFFIXES = {".md", ".txt", ".py", ".js", ".ts", ".mjs", ".cjs", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1",
                 ".psd1", ".bat", ".cmd", ".rb", ".pl", ".php", ".lua", ".go", ".rs", ".json", ".yaml", ".yml",
                 ".toml", ""}


CONTINUATION = {".ps1": "`", ".psm1": "`", ".psd1": "`", ".bat": "^", ".cmd": "^"}  # everything else: `\`


def _logical_lines(lines: list[str], suffix: str) -> list[tuple[int, str]]:
    """(first line number, text) with continued lines joined, so a pipe on the next line is still seen
    with the download before it."""
    mark = CONTINUATION.get(suffix, "\\")
    joined: list[tuple[int, str]] = []
    start, parts = 0, []
    for i, line in enumerate(lines, 1):
        stripped = line.rstrip()
        if not parts:
            start = i
        if stripped.endswith(mark):
            parts.append(stripped[:-1])
            continue
        joined.append((start, " ".join([*parts, line])))
        parts = []
    if parts:
        joined.append((start, " ".join(parts)))
    return joined


@dataclass
class ScanFinding:
    severity: str
    message: str
    file: str
    line: int
    excerpt: str


# Text aimed at the agent itself. A skill that warns against an attack forbids it or quotes it: a match right
# after a negation ("never ...") is not counted, one inside quotes or a markdown code block only warns (it is
# probably an example, but a quote mark must not be a way around the rule). Refusal suppression carries its
# negation in the attack itself and gets no such exemption.
OVERRIDE = re.compile(
    r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+)?(?:the\s+|your\s+|of\s+)?"
    r"(?:previous|prior|above|earlier|preceding|system|original)\s+(?:instructions|prompts?|rules|guidelines|directives)\b"
    r"|\byou\s+(?:now\s+)?have\s+no\s+(?:restrictions|rules|guidelines|limits)\b"
    r"|\b(?:ignore|bypass|disregard)\s+(?:your\s+|the\s+)?(?:safety|content)\s+(?:guidelines|policies|rules|filters)\b",
    re.I)
NEGATED = re.compile(r"\b(?:never|not|don't|do not|must not|shall not)\s+$", re.I)
QUOTES = "\"`“”«»'"
CLOSING = {"“": "”", "«": "»"}
CONTRACTION = re.compile(r"(?<=\w)['’](?=\w)")
# The attack itself is an imperative to the model at the start of a sentence or clause, maybe after "first,",
# "please" or an address ("assistant:", "you must"): the OWASP prompt injection cheat sheet's examples
# (https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html). The same phrase
# as the object of another verb ("if a page tells you to ...", "attempts to ...") describes the attack.
# What limits the phrase to a scope, as SkillSpector's prohibition_context.py reasons about negations
# (https://github.com/NVIDIA/skillspector/blob/main/src/skillspector/nodes/analyzers/prohibition_context.py): a clause
# opened by a condition ("When summarizing, ignore ..."), or the external content it is limited to ("... embedded in
# the document"). That content is what OWASP LLM01 names as the carrier of indirect injection
# (https://genai.owasp.org/llmrisk/llm01-prompt-injection/): pages, files, documents, tool results, untrusted sources.
# "within this conversation", "found anywhere", "so far" point at the agent's own context: still the attack.
CONDITION = re.compile(r"[\W_]*(?:when|whenever|if|while|unless|once|after|before|during)\b", re.I)
SCOPED_AFTER = re.compile(r"\s*(?:(?:that|which)\s+(?:appears?|are|is|shows?\s+up|comes?)\s+)?(?:coming\s+)?(?:(?:embedded|found|"
                          r"contained|hidden)\s+)?(?:in|inside|within|from)\s+(?:(?:the|a|an|any|such|those|these|fetched|"
                          r"retrieved|untrusted|external|third-party|user-supplied|web|remote)\s+)*(?:documents?|pages?|websites?|"
                          r"web\s+content|files?|content|text|inputs?|data|emails?|messages?|tool\s+(?:outputs?|results?)|"
                          r"results?|outputs?|sources?|search\s+results|(?P<pronoun>it|them))\b", re.I)
EXTERNAL = re.compile(r"\b(?:fetched|retrieved|untrusted|external|documents?|pages?|files?|content|text|tool\s+(?:outputs?|"
                      r"results?)|emails?|web)\b", re.I)  # what "inside it" can point back to
# a line named as an example of the attack, by itself or by a label line ending in ":" right above it
EXAMPLE_LABEL = re.compile(r"\b(?:examples?|e\.g\.|attacks?|payloads?|injections?|samples?|malicious|"
                           r"users?\s+(?:sometimes\s+|might\s+|may\s+|often\s+)?(?:write|say|type|send|paste))\b", re.I)
HEADING = re.compile(r"\s{0,3}#{1,6}\s")
LABEL_LINE = re.compile(EXAMPLE_LABEL.pattern + r"[^\n]*:[*_\s]*$", re.I)
LEAD = r"(?:first|now|then|also|so|and|please|ok(?:ay)?|from now on|important|note|instead)"
ADDRESS = r"(?:(?:assistant|claude|agent|model|ai|system)\s*[:,]|you\s+(?:must|should|will|shall|need\s+to|have\s+to|are\s+to)(?:\s+now)?)"
DIRECTIVE = re.compile(r"(?:(?:^[\W_]*|[.!?;:,]\s+)(?:" + LEAD + r"\b[\s,:!*_-]*|" + ADDRESS + r"\s*)*"
                       r"|\s(?:and|then|but|so|also)\s+)[\"'`“«*_>#-]*\s*$", re.I)
FENCE = re.compile(r"^\s*(?:```|~~~)")
REFUSAL = re.compile(r"\b(?:never|do not|don't)\s+refuse\b(?!\s+to\s+\w+\s+(?:questions\s+)?(?:about|on|regarding)\b)"
                     r"|\balways\s+comply\b(?!\s+with\b)"
                     r"|\b(?:no|without|omit(?:ting)?)\s+(?:any\s+)?(?:disclaimers|moralizing|ethical\s+commentary)\b", re.I)
# Shell start-up files, from MITRE ATT&CK T1546.004 (https://attack.mitre.org/techniques/T1546/004/).
SHELL_RC = r"(?:bashrc|bash_profile|bash_login|bash_logout|profile|zshrc|zshenv|zprofile|zlogin|zlogout)"
ARG = r"""["',\s]+"""  # what separates a command from its argument, in a shell line or a Python argv list
PERSISTENCE = re.compile(
    r"\|\s*crontab\b|\bcrontab\s+(?:-e\b|(?!-)[\w~$.-]*/[\w./-]*|[\w~$.-]+\.(?:txt|cron|tab|crontab)\b)"
    r"""|\bcrontab["']\s*,(?!\s*["']-[lr]["'])"""
    r"|\bschtasks(?:\.exe)?" + ARG + r"/create\b|\b(?:Register|New)-ScheduledTask\b"
    r"|Start Menu\\{1,2}Programs\\{1,2}Startup|CurrentVersion\\{1,2}Run(?:Once)?\b"
    r"|\blaunchctl" + ARG + r"(?:load|bootstrap)\b|\bsystemctl" + ARG + r"(?:--user" + ARG + r")?enable\b"
    r"|(?:>>|\btee\s+-a)\s*(?:\S*\." + SHELL_RC + r"\b|/etc/(?:" + SHELL_RC + r"\b|profile\.d/))"
    r"|\." + SHELL_RC + r"""\b["')\s]*,\s*["']a\+?["']"""  # open(home / ".bashrc", "a")
    r"|Add-Content\s[^\n]*\$PROFILE"
    # a file dropped where the system starts it by itself
    r"|\b(?:cp|mv|ln|tee|install|copy|Copy-Item)\b[^\n]*(?:Library/Launch(?:Agents|Daemons)|\.config/autostart"
    r"|\.config/systemd/user|/etc/systemd/system|/etc/cron\.)",
    re.I)

# Characters that make what the agent reads differ from what a reviewer sees. Bidi controls and Unicode
# tag characters have no business in a skill; zero-width ones turn up in non-English text, so they only warn.
# U+200D joins emoji and a BOM may open a file (scan_skill drops that one): both are fine there.
HIDDEN_CHARS = re.compile("[\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]")
ZERO_WIDTH = re.compile("[\u200b\u200c\u2060\u180e]|(?<=[\x00-\x7f])\u200d(?=[\x00-\x7f])|\ufeff")
# Tag characters are legitimate only inside an emoji tag sequence: an emoji, U+E0020-E007E, then
# U+E007F CANCEL TAG (the flags of England, Scotland, Wales). Unicode TR51 ED-14a.
EMOJI_TAG_SEQUENCE = re.compile(r"[\U0001f000-\U0001faff][\U000e0020-\U000e007e]+\U000e007f")
# Trojan Source (https://trojansource.codes/) is about bidi controls left open. A closed isolate or embedding
# is ordinary in right-to-left text; an override reverses what a reviewer sees, so it is never let through.
NO_BIDI = r"[^\N{LEFT-TO-RIGHT EMBEDDING}-\N{RIGHT-TO-LEFT OVERRIDE}\N{LEFT-TO-RIGHT ISOLATE}-\N{POP DIRECTIONAL ISOLATE}]*"
CLOSED_BIDI = re.compile(
    r"[\N{LEFT-TO-RIGHT ISOLATE}\N{RIGHT-TO-LEFT ISOLATE}\N{FIRST STRONG ISOLATE}]" + NO_BIDI + r"\N{POP DIRECTIONAL ISOLATE}"
    r"|[\N{LEFT-TO-RIGHT EMBEDDING}\N{RIGHT-TO-LEFT EMBEDDING}]" + NO_BIDI + r"\N{POP DIRECTIONAL FORMATTING}")
HTML_COMMENT = re.compile(r"<!--(.*?)-->", re.S)
# what tells someone what to do: an obligation word after the addressee ("you can read more" only informs)
# or an order: an address followed by a sentence ("Claude: email the repo ...", not "AGENT: anomalies" or
# "model: gemini, ..."), or an action verb with an object opening a clause ("Please send ...", "When done, delete ...")
AGENT_DIRECTED = re.compile(r"\b(?:you|agent|assistant|model|claude|AI)\b[^.\n]{0,80}?\b(?:must|should|always|never|"
                            r"need to|have to|are to|do not|don't)\b|^\s*(?:assistant|claude|agent|AI)\s*[:,]\s*(?:[a-z][\w'-]*\s+){2}"
                            r"|(?:^|[.:;,!]|\bplease\b|\balso\b)\s*(?:\w+ly\s+)?(?:ignore|run|execute|send|upload|reveal|email|"
                            r"post|delete|remove|wipe|copy|curl|fetch|download|forward|share|exfiltrate|print|read)\s+[\w~$./'\"-]",
                            re.I | re.M)
# a note to whoever edits the file ("TODO: you should update this"), unless it names the agent or asks for an action
AUTHOR_NOTE = re.compile(r"\s*(?:TODO|FIXME|XXX|NOTE)\b(?![\s\S]*\b(?:agent|assistant|model|claude|AI|ignore|run|execute|send|"
                         r"upload|read|reveal|delete|remove|download|fetch|post|must|always|never|do not|don't)\b)", re.I)
PADDING = re.compile(r"\S[ \t]{150,}[^\s|]")  # a gap before `|` is a padded table cell
PADDING_BLANK_LINES = 40
COMPILED_SUFFIXES = {".pyc", ".pyo", ".so", ".dll", ".exe", ".dylib", ".class", ".jar"}
ARCHIVE_SUFFIXES = {".zip", ".whl", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar"}
EXECUTABLE_MAGIC = (b"\x7fELF", b"MZ", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe")
NOT_SCRIPTS = {".md", ".txt", ".json", ".yaml", ".yml", ".toml"}


# The environment piped or substituted straight into a network client, in one shell line.
SHELL_EXFIL = re.compile(
    r"(?:\bprintenv\b|(?:^|[;&|(`]\s*)env\b(?!\s*=)|\b(?:Get-ChildItem|gci|dir|ls)\s+env:)[^\n]*"
    r"\|\s*(?:curl|wget|nc|ncat|Invoke-WebRequest|Invoke-RestMethod|iwr|irm)\b"
    r"|\b(?:curl|wget)\b[^\n]*\$\((?:printenv|env)\)", re.I)
EXFIL = "sends environment or credentials over the network"


def _without_closed_bidi(line: str) -> str:
    while (inner := CLOSED_BIDI.sub("", line)) != line:  # nested pairs close from the inside out
        line = inner
    return line


def _quoted(before: str, after: str) -> bool:
    """Inside a quote that opens before the phrase and closes after it: a lone quote mark proves nothing."""
    before, after = CONTRACTION.sub("", before), CONTRACTION.sub("", after)
    for q in QUOTES:
        close = CLOSING.get(q, q)
        opened = before.count(q) > before.count(close) if close != q else before.count(q) % 2
        if opened and close in after:
            return True
    return False


WINDOW = 300  # bounded work per match: a line of repeated phrases must not take quadratic time


def _override_severity(line: str, in_code_block: bool, labelled: bool = False) -> str | None:
    severity = None
    for m in OVERRIDE.finditer(line):
        start = max(0, m.start() - WINDOW)
        # a cut-off window does not open the line, so it never counts as the start of a sentence
        before, after = ("x " if start else "") + line[start:m.start()], line[m.end():m.end() + WINDOW]
        if NEGATED.search(before):
            continue
        clause = re.split(r"[.!?;]\s", before)[-1]
        scoped = SCOPED_AFTER.match(after)
        if scoped and scoped.group("pronoun") and not EXTERNAL.search(before):
            scoped = None
        if in_code_block or labelled or _quoted(before, after) or not DIRECTIVE.search(before) \
                or EXAMPLE_LABEL.search(clause) or HEADING.match(line) or scoped or CONDITION.match(clause):
            severity = severity or "medium"
        else:
            return "high"
    return severity


def _instruction_findings(line: str, in_code_block: bool = False, labelled: bool = False) -> list[tuple[str, str]]:
    """`labelled`: the label line right above names what follows as examples."""
    found = []
    if severity := _override_severity(line, in_code_block, labelled):
        found.append((severity, "tells the agent to drop its instructions"))
    if SHELL_EXFIL.search(line):
        found.append(("high", EXFIL))
    elif ENV_TO_FILE.search(line):
        found.append(("medium", "dumps environment variables"))
    if PS_SEND.search(line):
        found.append(("medium", "posts data to a remote endpoint"))
    if REFUSAL.search(line):
        found.append(("medium", "tells the agent never to refuse"))
    if PERSISTENCE.search(line):
        found.append(("medium", "sets itself up to run again later"))
    if HIDDEN_CHARS.search(_without_closed_bidi(EMOJI_TAG_SEQUENCE.sub("", line))):
        found.append(("high", "hides text with bidi or tag characters"))
    if ZERO_WIDTH.search(line):
        found.append(("medium", "contains invisible zero-width characters"))
    if PADDING.search(line):
        found.append(("medium", "hides text behind long whitespace"))
    return found


# --- python: taint from a source to the exact argument where it does harm ---------------------------------
# After Semgrep's taint mode (https://docs.semgrep.dev/writing-rules/data-flow/taint-mode): within one function
# (plus the module-level names it reads), only from assignments before the sink, and the sink is the exact
# argument that becomes code or leaves the machine, not any part of the call.

DECODE_OR_FETCH = {"b64decode", "b32decode", "b85decode", "a85decode", "decodebytes", "fromhex", "decompress",
                   "urlopen", "urlretrieve"}
HTTP_CLIENTS = {"requests", "httpx", "urllib3", "aiohttp"}
HTTP_CALLS = {"get", "post", "put", "patch", "delete", "request"}
SOCKET_SENDS = {"send", "sendall", "sendto"}
CODE_RUNNERS = {("exec",), ("eval",), ("compile",)}
SHELL_RUNNERS = {("os", "system"), ("os", "popen"), ("subprocess", "run"), ("subprocess", "call"),
                 ("subprocess", "Popen"), ("subprocess", "check_output"), ("subprocess", "check_call")}
# programs that run code given to them: after a code flag, or as their first positional (the script)
INTERPRETERS = {"python", "python3", "py", "pythonw", "sh", "bash", "zsh", "node", "deno", "perl", "ruby",
                "powershell", "pwsh", "cmd"}
CODE_FLAGS = {"-c", "-e", "--eval", "-command", "-encodedcommand", "/c"}
ENV_LOOKUPS = {"get", "setdefault", "pop"}  # one variable, not the whole environment
CREDENTIAL_FILES = next(p for _, message, p in SKILL_PATTERNS if message == "reads credential stores")
ENV_DUMP = next(p for _, message, p in SKILL_PATTERNS if message == "dumps environment variables")
ONE_VARIABLE = re.compile(r"\bprintenv\s+[A-Za-z_]\w*")
# a request with a body, per the Invoke-RestMethod docs (-Body, -Form, -InFile; -Method Post/Put/Patch), or wget's post
PS_SEND = re.compile(r"\b(?:Invoke-RestMethod|Invoke-WebRequest|irm|iwr)\b[^\n]*\s-(?:Body|Form|InFile|Method\s+['\"]?(?:Post|Put|Patch))\b"
                     r"|\bwget\b[^\n]*--post-(?:data|file)\b", re.I)
ENV_TO_FILE = re.compile(r"(?:^|[;&|(`]\s*)env\s*(?:>>?|\|\s*tee\b)")  # `env > /tmp/e`: the whole environment saved


def _dotted(node: ast.AST) -> tuple[str, ...]:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


def _with_imports_resolved(tree: ast.Module) -> ast.Module:
    """Spell every name an import bound as what it is bound to (`sp.run` -> `subprocess.run`, `system` ->
    `os.system`), per the import statement reference
    (https://docs.python.org/3/reference/simple_stmts.html#the-import-statement). A name the file also assigns
    or takes as a parameter is left alone."""
    bound = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            bound |= {a.asname: a.name for a in n.names if a.asname}
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            bound |= {a.asname or a.name: f"{n.module}.{a.name}" for a in n.names if a.name != "*"}
    assigned = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load)}
    assigned |= {a.arg for a in ast.walk(tree) if isinstance(a, ast.arg)}
    assigned |= {d.name for d in ast.walk(tree) if isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    bound = {k: v for k, v in bound.items() if k not in assigned}

    def spelled(dotted: str, at: ast.AST) -> ast.AST:
        first, *rest = dotted.split(".")
        node = ast.copy_location(ast.Name(first, ast.Load()), at)
        for attr in rest:
            node = ast.copy_location(ast.Attribute(node, attr, ast.Load()), at)
        return node

    def text(n: ast.AST) -> str | None:
        return n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None

    def resolved(n):
        """What `n` stands for, or None: an imported name, `__import__("os")`, `getattr(os, "system")`."""
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in bound:
            return spelled(bound[n.id], n)
        if isinstance(n, ast.Call) and n.args and (module := text(n.args[0])):
            name = _dotted(n.func)
            if name == ("__import__",):
                return spelled(module.split(".")[0], n)  # __import__("a.b") returns package a
            if name == ("importlib", "import_module"):
                return spelled(module, n)
        if isinstance(n, ast.Call) and _dotted(n.func) == ("getattr",) and len(n.args) >= 2 and (attr := text(n.args[1])):
            return ast.copy_location(ast.Attribute(n.args[0], attr, ast.Load()), n)
        return None

    # in place, deepest first so `getattr(__import__("os"), "system")` resolves inside out; no recursion
    for parent in reversed(list(ast.walk(tree))):
        for field, value in ast.iter_fields(parent):
            if isinstance(value, ast.AST) and (new := resolved(value)) is not None:
                setattr(parent, field, new)
            elif isinstance(value, list):
                value[:] = [new if isinstance(v, ast.AST) and (new := resolved(v)) is not None else v for v in value]
    return tree


def _fetched_or_decoded(node: ast.AST, clients: frozenset[str] = frozenset()) -> bool:
    """A call that downloads or decodes: by name, or `.get()` and friends on an HTTP client object (`s =
    requests.Session()`, `requests.Session().get(...)`)."""
    if not isinstance(node, ast.Call):
        return False
    name = _dotted(node.func)
    if isinstance(node.func, ast.Attribute) and node.func.attr in HTTP_CALLS and (
            (len(name) == 2 and name[0] in clients) or (isinstance(node.func.value, ast.Call)
                                                        and _dotted(node.func.value.func)[:1] in {(c,) for c in HTTP_CLIENTS})):
        return True
    return bool(name) and (name[-1] in DECODE_OR_FETCH
                           or (len(name) > 1 and name[0] in HTTP_CLIENTS and name[-1] in HTTP_CALLS)
                           or name[-2:] == ("codecs", "decode") or name[-2:] == ("marshal", "loads"))


def _whole_environment(tree: ast.AST) -> set[int]:
    """ids of the `os.environ` nodes that take the whole environment, not one variable looked up in it."""
    lookups = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Subscript):
            lookups.add(id(n.value))
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ENV_LOOKUPS:
            lookups.add(id(n.func.value))
        elif isinstance(n, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in n.ops):
            lookups |= {id(c) for c in n.comparators}
    return {id(n) for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and _dotted(n) == ("os", "environ") and id(n) not in lookups}


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _root(path: str) -> str:
    return re.split(r"[.\[]", path, maxsplit=1)[0]


def _own_nodes(scope: ast.AST):
    """The nodes of one function (or the module) without those of the functions defined inside it."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, FUNCTIONS):
            stack.extend(ast.iter_child_nodes(node))


def _flows(node: ast.AST):
    """ast.walk without what a call hands a child process as its environment: `env=` goes to the child, the
    call's result (its output) does not carry it (https://docs.python.org/3/library/subprocess.html)."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(c for c in ast.iter_child_nodes(n) if not (isinstance(c, ast.keyword) and c.arg == "env"))


def _pos(node: ast.AST) -> float:
    """Where a node starts, comparable across statements on one line (`x = decode(b); exec(x)`)."""
    return node.lineno + node.col_offset / 10_000


DEPTH = 50  # how far the recursive helpers below follow nesting: 1500 `+` terms parse, then overflow the stack


def _path(node: ast.AST, depth: int = 0) -> str | None:
    """`name`, `obj.attr` or `obj["key"]`: what an assignment binds. Assigning to `self.latest` taints that,
    not all of `self`."""
    if isinstance(node, ast.Name):
        return node.id
    if depth > DEPTH:
        return None
    if isinstance(node, ast.Attribute):
        base = _path(node.value, depth + 1)
        return base and f"{base}.{node.attr}"
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        base = _path(node.value, depth + 1)
        return base and f"{base}[{node.slice.value!r}]"
    return None


def _target_paths(target: ast.AST, depth: int = 0):
    if isinstance(target, (ast.Tuple, ast.List)) and depth <= DEPTH:
        for t in target.elts:
            yield from _target_paths(t, depth + 1)
    elif isinstance(target, ast.Starred) and depth <= DEPTH:
        yield from _target_paths(target.value, depth + 1)
    elif path := _path(target):
        yield path


def _tainted(node: ast.AST, source, since: dict[str, float], line: float) -> bool:
    """Whether `node` holds tainted data at `line`: a source in it, or a name it got from one earlier in the
    same function."""
    return any(source(n) or (isinstance(n, (ast.Name, ast.Attribute, ast.Subscript)) and (p := _path(n)) is not None
                             and since.get(p, line) < line) for n in _flows(node))


def _is_interpreter(program: ast.AST) -> bool:
    return _dotted(program) == ("sys", "executable") or (
        isinstance(program, ast.Constant) and isinstance(program.value, str)
        and Path(program.value).stem.lower() in INTERPRETERS)


def _argv_runs(elts: list[ast.AST], source, since: dict[str, float], line: float) -> bool:
    """An argv list runs tainted code when its program is tainted or, for an interpreter, the code after a
    code flag or the script it is given; other arguments are only data (`python -m pip install x==ver`)."""
    if not elts:
        return False
    program, rest = elts[0], iter(elts[1:])
    if _tainted(program, source, since, line):
        return True
    if not _is_interpreter(program):
        return False
    for arg in rest:
        flag = arg.value.lower() if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None
        if flag in CODE_FLAGS:
            code = next(rest, None)
            return code is not None and _tainted(code, source, since, line)
        if flag == "-m":
            return False
        if not (flag and flag.startswith("-")):
            return _tainted(arg, source, since, line)
    return False


def _resolve(node: ast.AST, values: dict[str, ast.AST]) -> ast.AST:
    """A name assigned once in the scope stands for the value it was given."""
    for _ in range(5):
        if not (isinstance(node, ast.Name) and node.id in values):
            break
        node = values[node.id]
    return node


def _argv_elts(node: ast.AST, values: dict[str, ast.AST], depth: int = 0) -> list[ast.AST] | None:
    """The items of an argv written as a list, a name bound to one, or lists added together; None when the
    items cannot be told (`make_argv(ver)`, `self.cmd + [...]`)."""
    node = _resolve(node, values)
    if isinstance(node, (ast.List, ast.Tuple)):
        return list(node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and depth <= DEPTH:
        left, right = _argv_elts(node.left, values, depth + 1), _argv_elts(node.right, values, depth + 1)
        return None if left is None or right is None else left + right
    return None


PLACEHOLDER = re.compile(r"\{[^{}]*\}|%(?:\([^)]*\))?[-#0 +]*\d*(?:\.\d+)?[a-z]")


def _shell_parts(node: ast.AST, values: dict[str, ast.AST], depth: int = 0) -> list | None:
    """A command string as fixed text and the expressions put into it, in order (f-string, `+`, `%`,
    `.format`); None when it is not built from a string the code shows."""
    node = _resolve(node, values)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [v.value for v in node.values]  # a Constant's text, a FormattedValue's expression
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and depth <= DEPTH:
        left, right = _shell_parts(node.left, values, depth + 1), _shell_parts(node.right, values, depth + 1)
        if left is None and right is None:
            return None
        return (left if left is not None else [node.left]) + (right if right is not None else [node.right])
    template, args = None, []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        template, args = node.left, node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        template, args = node.func.value, [*node.args, *(k.value for k in node.keywords)]
    if not (isinstance(template, ast.Constant) and isinstance(template.value, str)):
        return None
    texts = PLACEHOLDER.split(template.value)
    parts = [texts[0]]
    for i, text in enumerate(texts[1:]):
        parts += [args[i] if i < len(args) else ast.Constant("x"), text]
    return parts + args[len(texts) - 1:]


# Per POSIX 2.2 (https://pubs.opengroup.org/onlinepubs/9799919799/utilities/V3_chap02.html) these start another
# command wherever they stand, so the word after one is a program again.
SHELL_OPERATORS = re.compile(r"(;|&|\||`|\$\(|\(|\n)")
SANITIZERS = {("shlex", "quote"), ("pipes", "quote")}


def _shell_string_runs(parts: list, source, since: dict[str, float], line: float) -> bool | None:
    """The shell parses the whole string, interpolated text included (`/bin/sh -c`): tainted text that lands
    where the shell expects a program, or code for an interpreter, runs (True); anywhere else it may still
    smuggle an operator in (False); None when nothing tainted goes in."""
    runs, command = None, []
    for part in parts:
        if isinstance(part, str):
            for piece in SHELL_OPERATORS.split(part):
                if SHELL_OPERATORS.fullmatch(piece):
                    command = []
                else:
                    command += [ast.Constant(w.strip("\"'")) for w in piece.split() if w.strip("\"'")]
        elif isinstance(part, ast.Call) and _dotted(part.func) in SANITIZERS:
            command.append(ast.Constant("x"))
        else:
            if _tainted(part, source, since, line):
                runs = runs or _argv_runs([*command, part], source, since, line)
            command.append(part)
    return runs


def _command_severity(command: ast.AST, values: dict[str, ast.AST], source, since: dict[str, float],
                      line: float) -> str | None:
    """high when downloaded or decoded data becomes the code a shell runner runs, medium when it goes into a
    command where it cannot be told apart from code."""
    if (elts := _argv_elts(command, values)) is not None:
        return "high" if _argv_runs(elts, source, since, line) else None
    if (parts := _shell_parts(command, values)) is not None:
        runs = _shell_string_runs(parts, source, since, line)
        return None if runs is None else "high" if runs else "medium"
    if not _tainted(command, source, since, line):
        return None
    while True:  # the downloaded value itself is the command (`r.text`, `r.read().decode()`)
        if source(command) or ((p := _path(command)) is not None and since.get(p, line) < line):
            return "high"
        if isinstance(command, ast.Call) and isinstance(command.func, ast.Attribute):
            command = command.func.value
        elif isinstance(command, (ast.Attribute, ast.Subscript)):
            command = command.value
        else:
            return "medium"


def _stdin_runs(name: tuple[str, ...], call: ast.Call, source, since: dict[str, float]) -> bool:
    """Text given on stdin to an interpreter (`run(["bash"], input=...)`, `Popen([python, "-"]).communicate(...)`)
    runs as code: `curl | bash` in Python."""
    if name in SHELL_RUNNERS and call.args:
        argv, data = call.args[0], [k.value for k in call.keywords if k.arg == "input"]
    elif name[-1:] == ("communicate",) and isinstance(call.func.value, ast.Call) and call.func.value.args \
            and _dotted(call.func.value.func) in SHELL_RUNNERS:
        argv, data = call.func.value.args[0], [*call.args, *(k.value for k in call.keywords if k.arg == "input")]
    else:
        return False
    elts = argv.elts if isinstance(argv, (ast.List, ast.Tuple)) and argv.elts else [argv]
    return _is_interpreter(elts[0]) and _stdin_is_program(elts[1:]) \
        and any(_tainted(d, source, since, _pos(call)) for d in data)


def _stdin_is_program(args: list[ast.AST]) -> bool:
    """An interpreter runs stdin only when given no program (`python`, `python -`, `bash -s`); after `-c`, `-m` or a
    script stdin is that program's input (https://docs.python.org/3/using/cmdline.html)."""
    for arg in args:
        flag = arg.value.lower() if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None
        if flag in ("-", "-s"):
            return True
        if flag is None or flag in CODE_FLAGS or flag == "-m" or not flag.startswith("-"):
            return False
    return True


def _bindings(nodes: list[ast.AST]) -> list[tuple[float, ast.AST, list[ast.AST]]]:
    """(line, value, targets) for every way a name gets a value: `=`, `:=`, `with ... as`, `for`, comprehensions.
    A header's binding counts half a line early, so a body on the same line (`with x as r: exec(r)`) sees it."""
    found = []
    for n in nodes:
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and n.value:
            found.append((_pos(n), n.value, n.targets if isinstance(n, ast.Assign) else [n.target]))
        elif isinstance(n, ast.withitem) and n.optional_vars:
            found.append((n.context_expr.lineno - 0.5, n.context_expr, [n.optional_vars]))
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            found.append((n.iter.lineno - 0.5, n.iter, [n.target]))
    return found


def _taint(nodes: list[ast.AST], source, seed: dict[str, float], argv_roles: bool) -> dict[str, float]:
    """name -> first line it holds tainted data, from the bindings among `nodes`."""
    since = dict(seed)
    bindings = _bindings(nodes)
    for _ in range(3):  # a few rounds carry taint through `a = decode(x); b = a.decode()`
        for line, value, targets in bindings:
            if argv_roles and isinstance(value, (ast.List, ast.Tuple)):
                carries = _argv_runs(value.elts, source, since, line)
            else:
                carries = _tainted(value, source, since, line)
            if carries:
                for path in (p for target in targets for p in _target_paths(target)):
                    since[path] = min(since.get(path, line), line)
    return since


def _single_values(nodes: list[ast.AST]) -> dict[str, ast.AST]:
    """Names bound exactly once among `nodes`, by a plain `name = value`, to that value."""
    bindings = _bindings(nodes)
    counts = Counter(p for _, _, targets in bindings for t in targets for p in _target_paths(t))
    return {targets[0].id: value for _, value, targets in bindings
            if len(targets) == 1 and isinstance(targets[0], ast.Name) and counts[targets[0].id] == 1}


def _http_clients(tree: ast.AST) -> set[str]:
    """Names bound to a client object of an HTTP library (`s = requests.Session()`, `async with
    aiohttp.ClientSession() as session`)."""
    return {t.id for line, value, targets in _bindings(list(ast.walk(tree)))
            if isinstance(value, ast.Call) and _dotted(value.func)[:1] and _dotted(value.func)[0] in HTTP_CLIENTS
            for target in targets for t in ast.walk(target) if isinstance(t, ast.Name)}


def _sent(name: tuple[str, ...], call: ast.Call, clients: set[str]) -> list[ast.AST]:
    """The arguments of a call that leave the machine."""
    chained = isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Call) \
        and _dotted(call.func.value.func)[:1] and _dotted(call.func.value.func)[0] in HTTP_CLIENTS
    http = (len(name) > 1 and (name[0] in HTTP_CLIENTS or name[0] in clients)) or chained
    if name in SHELL_RUNNERS and call.args and isinstance(call.args[0], (ast.List, ast.Tuple)) and call.args[0].elts \
            and isinstance(program := call.args[0].elts[0], ast.Constant) and str(program.value) in ("curl", "wget"):
        return list(call.args[0].elts[1:])  # a command-line client: what it is given is what it sends
    if (http and call.func.attr in HTTP_CALLS) or (name and name[-1] in SOCKET_SENDS) \
            or (name and name[-1] == "urlopen"):
        return [*call.args, *(k.value for k in call.keywords)]
    return []


def _python_findings(text: str) -> list[tuple[str, str, int]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):  # too deeply nested for the parser (ast docs)
        return []
    tree = _with_imports_resolved(tree)
    whole_env = _whole_environment(tree)

    def secret(n: ast.AST) -> bool:
        return id(n) in whole_env or (isinstance(n, ast.Constant) and isinstance(n.value, str)
                                      and bool(CREDENTIAL_FILES.search(n.value)))

    clients = _http_clients(tree)

    def fetched(n: ast.AST) -> bool:
        return _fetched_or_decoded(n, frozenset(clients))

    module = list(_own_nodes(tree))
    module_code = _taint(module, fetched, {}, True)
    module_secret = _taint(module, secret, {}, False)
    module_values = _single_values(module)
    found = set()
    for scope in [tree, *(n for n in ast.walk(tree) if isinstance(n, FUNCTIONS))]:
        if scope is tree:
            nodes, code_since, secret_since, values = module, module_code, module_secret, module_values
        else:
            nodes = list(_own_nodes(scope))
            # a module-level name a function reads (and neither assigns nor takes as a parameter) holds what it
            # held there
            local = {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            local |= {a.arg for a in ast.walk(scope.args) if isinstance(a, ast.arg)}
            code_since = _taint(nodes, fetched, {k: 0 for k in module_code if _root(k) not in local}, True)
            secret_since = _taint(nodes, secret, {k: 0 for k in module_secret if _root(k) not in local}, False)
            values = {k: v for k, v in module_values.items() if k not in local} | _single_values(nodes)
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            name, line = _dotted(node.func), _pos(node)
            if any(_tainted(arg, secret, secret_since, line) for arg in _sent(name, node, clients)):
                found.add(("high", EXFIL, node.lineno))
            if _stdin_runs(name, node, fetched, code_since):
                found.add(("high", "runs code it downloads or decodes", node.lineno))
            if not node.args or (name not in CODE_RUNNERS and name not in SHELL_RUNNERS):
                continue
            first = node.args[0]
            if name in SHELL_RUNNERS:
                severity = _command_severity(first, values, fetched, code_since, line)
            elif _tainted(first, fetched, code_since, line):
                severity = "high"
            else:
                severity = "medium" if name != ("compile",) and not isinstance(first, ast.Constant) else None
            if severity == "high":
                found.add(("high", "runs code it downloads or decodes", node.lineno))
            elif severity:
                found.add(("medium", "runs dynamically built code", node.lineno))
    return sorted(found, key=lambda f: f[2])


def _file_findings(path: Path, text: str) -> list[tuple[str, str, int]]:
    """Findings that need the whole file rather than one line."""
    found = []
    if path.suffix.lower() == ".md":
        for m in HTML_COMMENT.finditer(text):
            if (AGENT_DIRECTED.search(m.group(1)) and not AUTHOR_NOTE.match(m.group(1))) or OVERRIDE.search(m.group(1)):
                found.append(("medium", "hides instructions in an HTML comment", text.count("\n", 0, m.start()) + 1))
    blank = 0
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip():
            if blank >= PADDING_BLANK_LINES:
                found.append(("medium", "hides text behind long whitespace", i))
            blank = 0
        else:
            blank += 1
    if path.suffix.lower() == ".py":
        found += _python_findings(text)
    return found


def _compiled(path: Path) -> bool:
    if path.suffix.lower() in COMPILED_SUFFIXES:
        return True
    try:
        with path.open("rb") as f:
            head = f.read(1024)
    except OSError:
        return False
    return head.startswith(EXECUTABLE_MAGIC) and b"\0" in head  # real headers hold NUL bytes; "MZ is ..." text does not


def scan_skill(root: Path) -> list[ScanFinding]:
    findings = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        if _compiled(path):
            findings.append(ScanFinding("medium", "ships compiled code that cannot be reviewed", rel, 1, "(binary file)"))
        elif path.suffix.lower() in ARCHIVE_SUFFIXES:
            findings.append(ScanFinding("medium", "ships an archive that cannot be reviewed", rel, 1, "(archive)"))
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        # a text file the scanner cannot read is reported, as ClamAV's AlertExceedsMax does
        # (https://raw.githubusercontent.com/Cisco-Talos/clamav/main/etc/clamd.conf.sample): padding past the limit or one
        # invalid byte must not hide a file
        try:
            if path.stat().st_size > 1_000_000:
                raise ValueError("too large")
            text = path.read_text("utf-8").removeprefix("\ufeff")
        except (ValueError, OSError):  # UnicodeDecodeError is a ValueError
            if not _compiled(path):
                findings.append(ScanFinding("medium", "has a text file too large or malformed to review", rel, 1, "(unread)"))
            continue
        lines = text.splitlines()
        own = []
        in_code_block, previous = False, ""
        for i, line in _logical_lines(lines, path.suffix.lower()):
            if path.suffix.lower() == ".md" and FENCE.match(line):
                in_code_block = not in_code_block
            for severity, message, pattern in SKILL_PATTERNS:
                if pattern.search(line):
                    own.append(ScanFinding(severity, message, rel, i, line.strip()[:160]))
            own += [ScanFinding(severity, message, rel, i, line.strip()[:160])
                    for severity, message in _instruction_findings(line, in_code_block, bool(LABEL_LINE.search(previous)))]
            previous = line if line.strip() else previous
        own += [ScanFinding(severity, message, rel, i, lines[i - 1].strip()[:160] if 0 < i <= len(lines) else "")
                for severity, message, i in _file_findings(path, text)]
        if chained := _exfil_chain(path, own):
            own.append(chained)
        findings += own
    return findings


def _exfil_chain(path: Path, own: list[ScanFinding]) -> ScanFinding | None:
    """Reading the whole environment and posting data are each ordinary; both in one script is the exfiltration
    chain. Python follows the data itself instead (_python_findings)."""
    # `printenv NAME` prints one variable, like os.environ["NAME"]: not the whole environment
    messages = {f.message for f in own if f.message != "dumps environment variables"
                or ENV_DUMP.search(ONE_VARIABLE.sub("", f.excerpt)) or ENV_TO_FILE.search(f.excerpt)}
    if path.suffix.lower() in NOT_SCRIPTS | {".py"} or EXFIL in messages or "posts data to a remote endpoint" \
            not in messages or not messages & {"dumps environment variables", "reads credential stores"}:
        return None
    post = next(f for f in own if f.message == "posts data to a remote endpoint")
    return ScanFinding("high", EXFIL, post.file, post.line, post.excerpt)


def is_blocking(findings: list[ScanFinding]) -> bool:
    return any(f.severity == "high" for f in findings)


def score_skill(spec: str, installs: int, trusted_owners: set[str]) -> TrustReport:
    owner = spec.split("/", 1)[0].lower()
    score, reasons = 30, []
    if owner in trusted_owners:
        score += 45
        reasons.append(f"publisher {owner} is on the trusted list")
    if installs >= 10_000:
        score += 15
        reasons.append(f"{installs} installs")
    elif installs >= 500:
        score += 5
        reasons.append(f"{installs} installs")
    else:
        reasons.append(f"few installs ({installs})")
    score = min(100, score)
    level = _level(score)
    reasons.append("skills run with full agent permissions; content is statically scanned before install")
    return TrustReport(spec, score, level, reasons, level != "high", None, {"runs_with": "full agent permissions"})


def score_marketplace(name: str, plugin: dict, servers: dict[str, dict]) -> TrustReport:
    """A plugin of Anthropic's curated marketplace: reviewed before it was listed, so high trust; still, a
    remote server sends data off the machine and a value read from the environment may be a secret, and
    both need the user's approval."""
    reasons = ["listed in Anthropic's curated plugin marketplace (claude-plugins-official)"]
    if not servers:
        return TrustReport(name, 0, "low", reasons + ["the plugin brings no MCP server"], True, None,
                           {"network": "any", "filesystem": "unknown", "shell": "unknown", "secrets": [], "env": []})
    server_name, config = next(iter(servers.items()))
    config = {k: v for k, v in config.items() if k in ("type", "command", "args", "env", "url", "headers")}
    if len(servers) > 1:
        reasons.append(f"brings {len(servers)} servers; installs `{server_name}`")
    env_names = sorted({m for v in [*config.get("env", {}).values(), *config.get("headers", {}).values()]
                        for m in re.findall(r"\$\{(\w+)", str(v))})
    remote = "url" in config
    if remote:
        reasons.append(f"remote server at {config['url']} (your data leaves the machine)")
    else:
        reasons.append(f"runs `{config.get('command')} {' '.join(map(str, config.get('args', [])))}` locally")
    if env_names:
        reasons.append(f"reads from your environment: {', '.join(env_names)}")
    if plugin.get("homepage"):
        reasons.append(f"homepage: {plugin['homepage']}")
    perms = {"network": "any", "filesystem": "unknown", "shell": "unknown", "secrets": env_names, "env": env_names}
    return TrustReport(name, 90, "high", reasons, bool(env_names) or remote, config, perms)
