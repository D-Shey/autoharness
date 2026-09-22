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
        data = _read_json_file(self.path)
        data.setdefault("mcpServers", {})[name] = native
        _write_text(self.path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

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

    def has(self, name: str) -> bool:
        key = re.escape(name) + "|" + re.escape(json.dumps(name))
        return re.search(rf"^\s*\[mcp_servers\.(?:{key})\s*[\].]", self._text(), re.M) is not None

    def add(self, name: str, native: dict) -> None:
        body = "\n".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in native.items())
        block = (f"# >>> autoharness:{name}\n[mcp_servers.{_toml_key(name)}]\n{body}\n"
                 f"# <<< autoharness:{name}\n")
        text = self._block_re(name).sub("", self._text())
        if text and not text.endswith("\n"):
            text += "\n"
        _write_text(self.path, text + ("\n" if text.strip() else "") + block)

    def remove(self, name: str, *, created_file: bool, created_dir: bool) -> None:
        if not self.path.exists():
            return
        text = self._block_re(name).sub("", self._text()).rstrip("\n")
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

    def mcp_path(self, root: Path) -> Path:
        return root / self.mcp_file

    def mcp_store(self, root: Path):
        path = self.mcp_path(root)
        return TomlMcpStore(path) if path.suffix == ".toml" else JsonMcpStore(path)

    def skills_path(self, root: Path) -> Path:
        return root / self.skills_dir

    def user_skills_path(self) -> Path:
        return Path.home() / self.user_skills_dir


_GEMINI_HINT = "restart {title} (or run /mcp refresh) so it picks up the new server"

AGENTS: dict[str, Agent] = {a.name: a for a in [
    Agent("claude", "Claude Code", ".mcp.json", ".claude/skills", ".claude/skills", to_claude,
          "restart Claude Code (or run /mcp) and approve the project server"),
    Agent("codex", "Codex CLI", ".codex/config.toml", ".agents/skills", ".agents/skills", to_codex,
          "restart Codex; project .codex/config.toml is only loaded when the project is trusted"),
    Agent("gemini", "Gemini CLI", ".gemini/settings.json", ".gemini/skills", ".gemini/skills", to_gemini,
          _GEMINI_HINT.format(title="Gemini CLI")),
    Agent("qwen", "Qwen Code", ".qwen/settings.json", ".qwen/skills", ".qwen/skills", to_gemini,
          _GEMINI_HINT.format(title="Qwen Code")),
    Agent("gigacode", "GigaCode CLI", ".gigacode/settings.json", ".gigacode/skills", ".gigacode/skills", to_gemini,
          _GEMINI_HINT.format(title="GigaCode CLI")),
    Agent("strands", "Strands harness", ".agent/mcp.json", ".agent/skills", ".agent/skills", to_claude,
          "restart the agent; pass mcp_servers=\".agent/mcp.json\" to create_harness() so it loads the server"),
]}

DEFAULT_AGENT = "claude"

# Env vars the agents set for the shell commands they run.
_DETECT = [("CLAUDECODE", "claude"), ("GEMINI_CLI", "gemini"), ("QWEN_CODE", "qwen"),
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


def get(name: str | None) -> Agent:
    name = name or detect() or DEFAULT_AGENT
    if name not in AGENTS:
        raise AgentError(f"unknown agent {name!r}; choose one of {', '.join(AGENTS)}")
    return AGENTS[name]
