"""Trust scoring for discovered MCP servers and static scanning of downloaded skills.

The goal is not to prove something safe, but to make the risky cases loud:
anything that needs secrets, runs arbitrary containers, or contains
exfiltration-looking code must go through explicit human approval.
"""

from __future__ import annotations

import re
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
    ("high", "reads credential stores", re.compile(r"~/\.ssh|id_rsa|\.aws/credentials|\.netrc|\.git-credentials|Login Data|keychain", re.I)),
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


def scan_skill(root: Path) -> list[ScanFinding]:
    findings = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > 1_000_000:
            continue
        try:
            lines = path.read_text("utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for i, line in _logical_lines(lines, path.suffix.lower()):
            for severity, message, pattern in SKILL_PATTERNS:
                if pattern.search(line):
                    findings.append(ScanFinding(severity, message, str(path.relative_to(root)), i, line.strip()[:160]))
    return findings


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
