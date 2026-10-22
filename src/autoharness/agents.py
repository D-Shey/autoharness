"""Agent adapters: where each coding agent keeps project MCP servers and skills.

AutoHarness stores MCP configs in one canonical shape (Claude Code's
`.mcp.json` server entry, `${VAR}` for secrets) in lockfiles and recipes, and
each adapter translates it into the agent's native config on write.

    agent     MCP config (project)       skills (project)
    claude    .mcp.json                  .claude/skills
    codex     .codex/config.toml         .agents/skills
    gemini    .gemini/settings.json      .gemini/skills
    qwen      .qwen/settings.json        .qwen/skills
    gigacode  .gigacode/settings.json    .gigacode/skills
    strands   .agent/mcp.json            .agent/skills
    agy       .agents/mcp_config.json    .agents/skills

Antigravity CLI (agy) shares `.agents/skills` with Codex, so the autoharness skill
installed there speaks to both. Its user-level skills live in `~/.gemini/config/skills`:
agy 1.4.0 suggests `~/.gemini/antigravity-cli/skills` in /skills but does not load from it.

Strands harness has no fixed MCP config file: it takes `create_harness(mcp_servers=<path>)`,
so autoharness writes an mcpServers JSON at `.agent/mcp.json` for the user to pass in.

Adapters only ever touch the server entries they are asked to write or remove;
everything else in the file is preserved.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import tomllib


class AgentError(Exception):
    pass


ENV_REF = re.compile(r"\$\{(\w+)\}")
WHOLE_ENV_REF = re.compile(r"^\$\{(\w+)\}$")
BEARER_REF = re.compile(r"^Bearer \$\{(\w+)\}$")


# --- config translation -----------------------------------------------------

def to_claude(cfg: dict) -> tuple[dict, list[str]]:
    return dict(cfg), []


def to_gemini(cfg: dict) -> tuple[dict, list[str]]:
    """Gemini CLI and its forks (Qwen Code, GigaCode): `httpUrl` for streamable HTTP, `url` for SSE."""
    if "url" not in cfg:
        return {k: v for k, v in cfg.items() if k != "type"}, []
    out = {"url" if cfg.get("type") == "sse" else "httpUrl": cfg["url"]}
    if cfg.get("headers"):
        out["headers"] = dict(cfg["headers"])
    return out, []


def to_codex(cfg: dict) -> tuple[dict, list[str]]:
    """Codex does not expand `${VAR}`: secrets are forwarded by name instead."""
    warnings: list[str] = []
    out: dict = {}
    if "url" in cfg:
        out["url"] = cfg["url"]
        static, from_env = {}, {}
        for header, value in cfg.get("headers", {}).items():
            if header.lower() == "authorization" and (m := BEARER_REF.match(value)):
                out["bearer_token_env_var"] = m.group(1)
            elif m := WHOLE_ENV_REF.match(value):
                from_env[header] = m.group(1)
            elif ENV_REF.search(value):
                warnings.append(f"header {header!r} mixes text and env vars; Codex cannot expand it, set it by hand")
            else:
                static[header] = value
        if static:
            out["http_headers"] = static
        if from_env:
            out["env_http_headers"] = from_env
        return out, warnings

    out["command"] = cfg["command"]
    if cfg.get("args"):
        out["args"] = list(cfg["args"])
        refs = sorted({v for a in cfg["args"] for v in ENV_REF.findall(a)})
        if refs:
            warnings.append(f"Codex does not expand env vars in args ({', '.join(refs)}); "
                            "replace them in .codex/config.toml or pass the value another way")
    env, forward = {}, []
    for key, value in cfg.get("env", {}).items():
        m = WHOLE_ENV_REF.match(value)
        if m and m.group(1) == key:
            forward.append(key)
        elif ENV_REF.search(value):
            forward.extend(ENV_REF.findall(value))
            warnings.append(f"env {key}={value!r} renames a variable; Codex only forwards variables by their own name")
        else:
            env[key] = value
    if env:
        out["env"] = env
    if forward:
        out["env_vars"] = sorted(set(forward))
    return out, warnings


def to_agy(cfg: dict) -> tuple[dict, list[str]]:
    """Antigravity CLI: `serverUrl` for remote servers. It does not expand `${VAR}`,
    but servers inherit agy's environment, so `"K": "${K}"` is simply left out."""
    warnings: list[str] = []
    if "url" in cfg:
        out: dict = {"serverUrl": cfg["url"]}
        if cfg.get("headers"):
            out["headers"] = dict(cfg["headers"])
            for header, value in cfg["headers"].items():
                if ENV_REF.search(value):
                    warnings.append(f"header {header!r} uses env vars; Antigravity CLI does not expand them, "
                                    "put the value in .agents/mcp_config.json by hand")
        return out, warnings

    out = {"command": cfg["command"]}
    if cfg.get("args"):
        out["args"] = list(cfg["args"])
        refs = sorted({v for a in cfg["args"] for v in ENV_REF.findall(a)})
        if refs:
            warnings.append(f"Antigravity CLI does not expand env vars in args ({', '.join(refs)}); "
                            "replace them in .agents/mcp_config.json or pass the value another way")
    env = {}
    for key, value in cfg.get("env", {}).items():
        m = WHOLE_ENV_REF.match(value)
        if m and m.group(1) == key:
            continue
        if ENV_REF.search(value):
            warnings.append(f"env {key}={value!r} needs expansion Antigravity CLI does not do; "
                            f"set {key} in the environment agy starts from")
        else:
            env[key] = value
    if env:
        out["env"] = env
    return out, warnings


# --- MCP config stores ------------------------------------------------------

def _read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text("utf-8") or "{}")
    except json.JSONDecodeError as e:
        raise AgentError(f"{path} is not plain JSON ({e}); add the server by hand or remove comments") from e


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, "utf-8")
    tmp.replace(path)


def _cleanup(path: Path, created_dir: bool) -> None:
    path.unlink(missing_ok=True)
    if created_dir and path.parent.exists() and not any(path.parent.iterdir()):
        path.parent.rmdir()


class JsonMcpStore:
    """`{"mcpServers": {...}}` inside a JSON file that may hold other settings."""

    def __init__(self, path: Path):
        self.path = path

    def has(self, name: str) -> bool:
        return name in _read_json_file(self.path).get("mcpServers", {})

    def add(self, name: str, native: dict) -> None:
        _write_text(self.path, self.preview_add(name, native))

    def preview_add(self, name: str, native: dict) -> str:
        data = _read_json_file(self.path)
        data.setdefault("mcpServers", {})[name] = native
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    def remove(self, name: str, *, created_file: bool, created_dir: bool) -> None:
        data = _read_json_file(self.path)
        data.get("mcpServers", {}).pop(name, None)
        if created_file and not data.get("mcpServers") and set(data) <= {"mcpServers"}:
            _cleanup(self.path, created_dir)
        elif self.path.exists():
            _write_text(self.path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)  # JSON string escapes are valid TOML basic-string escapes
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise AgentError(f"cannot write {value!r} as TOML")


class TomlMcpStore:
    """`[mcp_servers.<name>]` tables in a Codex config.toml.

    The file is edited as text so user comments and formatting survive: each
    server AutoHarness adds is wrapped in marker comments and removed by them.
    """

    def __init__(self, path: Path):
        self.path = path

    def _text(self) -> str:
        return self.path.read_text("utf-8") if self.path.exists() else ""

    @staticmethod
    def _block_re(name: str) -> re.Pattern:
        return re.compile(rf"^# >>> autoharness:{re.escape(name)}\n.*?^# <<< autoharness:{re.escape(name)}\n?",
                          re.M | re.S)

    def _without_block(self, text: str, name: str) -> str:
        def remove(match: re.Match) -> str:
            # A real block starts between complete TOML statements. A marker
            # inside a multiline string (or unfinished array) is user content.
            try:
                tomllib.loads(text[:match.start()])
            except tomllib.TOMLDecodeError:
                return match.group(0)
            return ""
        return self._block_re(name).sub(remove, text)

    def has(self, name: str) -> bool:
        return name in self._parse(self._text()).get("mcp_servers", {})

    def _parse(self, text: str) -> dict:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise AgentError(f"{self.path} is not valid TOML ({e}); not changing it") from e
        if not isinstance(data.get("mcp_servers", {}), dict):
            raise AgentError(f"{self.path}: mcp_servers must be a TOML table")
        return data

    def add(self, name: str, native: dict) -> None:
        _write_text(self.path, self.preview_add(name, native))

    def preview_add(self, name: str, native: dict) -> str:
        original = self._text()
        self._parse(original)
        text = self._without_block(original, name)
        if name in self._parse(text).get("mcp_servers", {}):
            raise AgentError(f"MCP server {name!r} already exists outside an autoharness block")
        body = "\n".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in native.items())
        block = (f"# >>> autoharness:{name}\n[mcp_servers.{_toml_key(name)}]\n{body}\n"
                 f"# <<< autoharness:{name}\n")
        if text and not text.endswith("\n"):
            text += "\n"
        result = text + ("\n" if text.strip() else "") + block
        self._parse(result)
        return result

    def remove(self, name: str, *, created_file: bool, created_dir: bool) -> None:
        if not self.path.exists():
            return
        original = self._text()
        self._parse(original)
        text = self._without_block(original, name).rstrip("\n")
        self._parse(text)
        if created_file and not text.strip():
            _cleanup(self.path, created_dir)
        else:
            _write_text(self.path, text + "\n" if text else "")


# --- agents -----------------------------------------------------------------

@dataclass(frozen=True)
class Agent:
    name: str
    title: str
    mcp_file: str                 # relative to the project root
    skills_dir: str               # relative to the project root
    user_skills_dir: str          # relative to the home directory
    translate: Callable[[dict], tuple[dict, list[str]]]
    reload_hint: str
    user_mcp_file: str = ""       # relative to the home directory: servers every project gets ("" unknown)

    def mcp_path(self, root: Path) -> Path:
        return root / self.mcp_file

    def mcp_store(self, root: Path):
        path = self.mcp_path(root)
        return TomlMcpStore(path) if path.suffix == ".toml" else JsonMcpStore(path)

    def skills_path(self, root: Path) -> Path:
        return root / self.skills_dir

    def user_skills_path(self) -> Path:
        return Path.home() / self.user_skills_dir

    def user_mcp_store(self):
        """The agent's own MCP file, which every project loads; None when autoharness does not know it."""
        if not self.user_mcp_file:
            return None
        path = Path.home() / self.user_mcp_file
        return TomlMcpStore(path) if path.suffix == ".toml" else JsonMcpStore(path)


_GEMINI_HINT = "restart {title} (or run /mcp refresh) so it picks up the new server"

AGENTS: dict[str, Agent] = {a.name: a for a in [
    Agent("claude", "Claude Code", ".mcp.json", ".claude/skills", ".claude/skills", to_claude,
          "restart Claude Code (or run /mcp) and approve the project server"),
    Agent("codex", "Codex CLI", ".codex/config.toml", ".agents/skills", ".agents/skills", to_codex,
          "restart Codex; project .codex/config.toml is only loaded when the project is trusted"),
    Agent("gemini", "Gemini CLI", ".gemini/settings.json", ".gemini/skills", ".gemini/skills", to_gemini,
          _GEMINI_HINT.format(title="Gemini CLI"), ".gemini/settings.json"),
    Agent("qwen", "Qwen Code", ".qwen/settings.json", ".qwen/skills", ".qwen/skills", to_gemini,
          _GEMINI_HINT.format(title="Qwen Code"), ".qwen/settings.json"),
    Agent("gigacode", "GigaCode CLI", ".gigacode/settings.json", ".gigacode/skills", ".gigacode/skills", to_gemini,
          _GEMINI_HINT.format(title="GigaCode CLI"), ".gigacode/settings.json"),
    Agent("strands", "Strands harness", ".agent/mcp.json", ".agent/skills", ".agent/skills", to_claude,
          "restart the agent; pass mcp_servers=\".agent/mcp.json\" to create_harness() so it loads the server"),
    Agent("agy", "Antigravity CLI", ".agents/mcp_config.json", ".agents/skills", ".gemini/config/skills", to_agy,
          "restart agy so it picks up the new server", ".gemini/config/mcp_config.json"),
]}

DEFAULT_AGENT = "claude"


# --- servers built into an agent --------------------------------------------
# Some agents bring an MCP server of their own, in no config file autoharness writes and in no registry it
# searches: Claude Code's Claude in Chrome drives the user's own Chrome, signed in to their sites, which a fresh
# Playwright browser is not. Where one is there, it is the first choice for what it covers.

CLAUDE_IN_CHROME_EXTENSION = "fcoeoabgfenejglbffodgkkbkcdhcgfn"


def _chrome_user_data() -> list[Path]:
    """Where Chrome keeps its profiles: Windows, macOS, Linux."""
    home = Path.home()
    return [home / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
            home / "Library" / "Application Support" / "Google" / "Chrome",
            home / ".config" / "google-chrome"]


def claude_in_chrome_installed() -> bool:
    """The Claude extension is in some Chrome profile of this user (Default, Profile 1, ...)."""
    for data in _chrome_user_data():
        try:
            if any((p / "Extensions" / CLAUDE_IN_CHROME_EXTENSION).is_dir() for p in data.iterdir() if p.is_dir()):
                return True
        except OSError:
            continue
    return False


@dataclass(frozen=True)
class BuiltinMcp:
    name: str
    agent: str
    description: str
    capabilities: tuple[str, ...]   # catalog capabilities it covers
    installed: Callable[[], bool]


BUILTIN_MCP = [
    BuiltinMcp("claude-in-chrome", "claude",
               "Claude in Chrome, built into Claude Code: drives the user's own Chrome with their sign-ins "
               "(tools mcp__claude-in-chrome__*). Nothing to install.",
               ("browser-automation", "browser-devtools"), claude_in_chrome_installed),
]


def builtin_mcp(agent: str) -> list[BuiltinMcp]:
    """The servers built into `agent` that are there on this machine."""
    return [b for b in BUILTIN_MCP if b.agent == agent and b.installed()]


def sharing_skills_dir(agent: Agent, user: bool = False) -> list[Agent]:
    """Agents that read their skills from the same directory as `agent` (Codex and agy share `.agents/skills`)."""
    key = (lambda a: a.user_skills_dir) if user else (lambda a: a.skills_dir)
    return [a for a in AGENTS.values() if key(a) == key(agent)]


# Env vars the agents set for the shell commands they run. agy sets ANTIGRAVITY_AGENT
# per command and comes first: it inherits CLAUDECODE when started from Claude Code.
_DETECT = [("ANTIGRAVITY_AGENT", "agy"), ("CLAUDECODE", "claude"), ("GEMINI_CLI", "gemini"), ("QWEN_CODE", "qwen"),
           ("GIGACODE_CLI", "gigacode"), ("CODEX_SANDBOX", "codex"), ("CODEX_THREAD_ID", "codex")]


def detect(environ: dict | None = None) -> str | None:
    environ = os.environ if environ is None else environ
    if (explicit := environ.get("AUTOHARNESS_AGENT")) in AGENTS:
        return explicit
    for var, agent in _DETECT:
        if environ.get(var):
            return agent
    ai_agent = environ.get("AI_AGENT", "").lower()
    return next((name for name in AGENTS if ai_agent.startswith(name)), None)


# The id of the agent session a shell command runs in, the same id its hooks receive as session_id
_SESSION = {"claude": "CLAUDE_CODE_SESSION_ID", "agy": "ANTIGRAVITY_CONVERSATION_ID", "codex": "CODEX_THREAD_ID"}


def session_id(environ: dict | None = None, agent: str | None = None) -> str:
    """The agent session running this command, or "" outside one (the user's own terminal). An agent started
    from another agent's shell inherits that one's id as well: the agent named for the command comes first."""
    environ = os.environ if environ is None else environ
    agent = agent or detect(environ)
    if agent in _SESSION and environ.get(_SESSION[agent]):
        return environ[_SESSION[agent]]
    return next((environ[v] for v in _SESSION.values() if environ.get(v)), "")


def get(name: str | None) -> Agent:
    name = name or detect() or DEFAULT_AGENT
    if name not in AGENTS:
        raise AgentError(f"unknown agent {name!r}; choose one of {', '.join(AGENTS)}")
    return AGENTS[name]
