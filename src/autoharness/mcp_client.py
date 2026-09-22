"""Clients for the MCP servers the gateway runs on the agent's behalf: stdio processes and Streamable HTTP."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Callable, Mapping

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class McpError(Exception):
    pass


def timeout_s() -> float:
    return float(os.environ.get("AUTOHARNESS_GATEWAY_TIMEOUT", "120"))


# A server gets a secret only through its own `env`, as `${VAR}`, never by inheriting the agent's environment.
# Secrets come in too many shapes to list (DB_PASS, BEARER, a connection string, a webhook URL), so a server
# inherits only what locates the system, the user, the locale and the network, and nothing else.
PASS_ENV = {
    "PATH", "PATHEXT", "HOME", "USER", "USERNAME", "LOGNAME", "SHELL", "TERM", "LANG", "LANGUAGE", "TZ",
    "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS", "USERPROFILE", "APPDATA",
    "LOCALAPPDATA", "PROGRAMDATA", "ALLUSERSPROFILE", "HOMEDRIVE", "HOMEPATH", "PUBLIC", "COMPUTERNAME",
    "NUMBER_OF_PROCESSORS", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
    "SSH_AUTH_SOCK", "NO_PROXY", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "PYTHONIOENCODING", "PYTHONUTF8", "JAVA_HOME",
    # which config, profile or daemon to use: without them a server silently talks to another account
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "KUBECONFIG", "AWS_PROFILE", "AWS_REGION",
    "AWS_DEFAULT_REGION", "AWS_CONFIG_FILE", "CLOUDSDK_CONFIG", "AZURE_CONFIG_DIR", "VIRTUAL_ENV",
    "PYTHONPATH", "NODE_OPTIONS", "NODE_PATH", "GOPATH", "GOROOT", "CARGO_HOME", "RUSTUP_HOME",
    "GH_HOST", "GITHUB_HOST", "GOOGLE_CLOUD_PROJECT", "CLOUDSDK_CORE_PROJECT", "AZURE_SUBSCRIPTION_ID",
    "OLLAMA_HOST", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "NODE_ENV", "PSMODULEPATH", "JAVA_OPTS",
    "GH_CONFIG_DIR", "CLOUDSDK_ACTIVE_CONFIG_NAME", "AWS_CA_BUNDLE", "GIT_CONFIG_GLOBAL", "GNUPGHOME",
    "GIT_SSH", "GIT_SSH_COMMAND", "JAVA_TOOL_OPTIONS", "GRADLE_USER_HOME", "DENO_DIR", "BUN_INSTALL", "USERDOMAIN",
}
# Selectors of a config (KUBECONFIG, DOCKER_CONFIG, GH_CONFIG_DIR, AWS_CONFIG_FILE, AWS_PROFILE) pass: they
# point a server at the user's own default setup, which it can read anyway, running as the user; without them
# it would silently use another one. Names containing CREDENTIAL, KEY or TOKEN are withheld whatever they hold,
# as before, so a non-default AWS_SHARED_CREDENTIALS_FILE or GOOGLE_APPLICATION_CREDENTIALS must come through
# the server's `env`. The SSH agent socket passes, as before: git over SSH needs it.
PATH_POINTERS = {"SSH_AUTH_SOCK"}
PROXY_ENV = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "NO_PROXY"}
PASS_ENV_PREFIXES = ("LC_", "XDG_", "PROCESSOR_", "PROGRAMFILES", "PROGRAMW6432", "COMMONPROGRAMFILES",
                     "COMMONPROGRAMW6432", "UV_", "NPM_CONFIG_", "NVM_", "VOLTA_", "PNPM_", "PIP_", "CONDA_",
                     "DOCKER_", "PLAYWRIGHT_", "PUPPETEER_", "PYENV_", "ASDF_", "MISE_")
# even among those, no credential-shaped name (npm_config__authToken) ...
SENSITIVE_ENV = re.compile(r"KEY|PASSWORD|PASSWD|SECRET|TOKEN|CREDENTIAL"             # anywhere in the name
                           r"|(?:^|_)(?:AUTH|COOKIE|DSN|PAT|PRIVATE)(?:_|$)", re.IGNORECASE)  # as a whole word
# ... and no URL with a password in it (UV_INDEX_URL=https://user:pw@host), except a proxy (PROXY_ENV): the
# server's traffic has to go through it, login and all
CREDENTIAL_URL = re.compile(r"://[^/\s@]*:[^/\s@]+@")


def _passes(name: str, value: str) -> bool:
    upper = name.upper()
    if upper in PROXY_ENV:
        return True
    if upper in PATH_POINTERS:
        return True
    if SENSITIVE_ENV.search(upper):
        return False
    return (upper in PASS_ENV or upper.startswith(PASS_ENV_PREFIXES)) and not CREDENTIAL_URL.search(value)


def child_env(extra: Mapping[str, str], parent: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a server process: the harmless part of the parent's (PASS_ENV), plus `extra`."""
    parent = os.environ if parent is None else parent
    return {k: v for k, v in parent.items() if _passes(k, v)} | dict(extra)


def withheld_env(extra: Mapping[str, str], parent: Mapping[str, str] | None = None) -> list[str]:
    """Names (never values) of the parent's variables a server does not get: shown when it fails to start."""
    parent = os.environ if parent is None else parent
    return sorted(k for k, v in parent.items() if k not in extra and not _passes(k, v))


def expand_secrets(value, env: Mapping[str, str] | None = None):
    """Replace ${VAR} with its value from `env` (default: os.environ), recursively."""
    env = os.environ if env is None else env

    def sub(m: re.Match) -> str:
        if m.group(1) not in env:
            raise McpError(f"set {m.group(1)} in the environment the agent is started from")
        return env[m.group(1)]

    if isinstance(value, str):
        return VAR.sub(sub, value)
    if isinstance(value, list):
        return [expand_secrets(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand_secrets(v, env) for k, v in value.items()}
    return value


def _nothing(*_):
    return None


class _Client:
    transport = ""

    def __init__(self, name: str, config: dict, log: Callable[[str], None] = _nothing,
                 on_tools_changed: Callable[[str], None] = _nothing, stderr=None):
        self.name, self.config, self.log, self.on_tools_changed = name, config, log, on_tools_changed
        self.stderr = stderr
        self.protocol_version = PROTOCOL_VERSIONS[0]
        self._id_lock = threading.Lock()
        self._next_id = 0

    def _new_id(self) -> int:
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _handshake(self) -> None:
        res = self.request("initialize", {"protocolVersion": PROTOCOL_VERSIONS[0], "capabilities": {},
                                          "clientInfo": {"name": "autoharness-gateway", "version": "0.1"}})
        self.protocol_version = res.get("protocolVersion", self.protocol_version)
        self.notify("notifications/initialized")

    def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        while True:
            res = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, tool: str, arguments: dict | None) -> dict:
        return self.request("tools/call", {"name": tool, "arguments": arguments or {}})

    def _raise_for(self, method: str, msg: dict) -> dict:
        if "error" in msg:
            err = msg["error"]
            raise McpError(f"{self.name}: {err.get('message', err) if isinstance(err, dict) else err}")
        return msg.get("result", {})


class StdioClient(_Client):
    transport = "stdio"

    def start(self) -> None:
        cfg = expand_secrets(self.config)
        exe = shutil.which(cfg["command"])
        if not exe:
            raise McpError(f"command not found: {cfg['command']}")
        self._pending: dict[int, dict] = {}
        self._write_lock = threading.Lock()
        self._eof = False
        self.proc = subprocess.Popen([exe, *cfg.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.stderr if self.stderr is not None else subprocess.DEVNULL,
                                     env=child_env(cfg.get("env", {})))
        threading.Thread(target=self._read, daemon=True).start()
        self._handshake()

    def alive(self) -> bool:
        # stdout reaching EOF is the first sign of a dead server; poll() can lag behind it
        return getattr(self, "proc", None) is not None and not self._eof and self.proc.poll() is None

    def _send(self, msg: dict) -> None:
        data = (json.dumps(msg) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                raise McpError(f"{self.name}: server is not running") from e

    def _read(self) -> None:
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                self.log(f"{self.name}: non-JSON output: {raw[:200]!r}")
                continue
            if "method" not in msg and msg.get("id") in self._pending:
                slot = self._pending.pop(msg["id"])
                slot["msg"] = msg
                slot["event"].set()
            elif "method" in msg and "id" in msg:
                try:
                    self._send({"jsonrpc": "2.0", "id": msg["id"],
                                "error": {"code": -32601, "message": f"autoharness gateway does not support {msg['method']}"}})
                except McpError:
                    pass
            elif msg.get("method") == "notifications/tools/list_changed":
                threading.Thread(target=self.on_tools_changed, args=(self.name,), daemon=True).start()
        self._eof = True
        for rid in list(self._pending):
            slot = self._pending.pop(rid, None)
            if slot:
                slot["msg"] = {"error": {"message": "server exited"}}
                slot["event"].set()

    def request(self, method: str, params: dict | None = None) -> dict:
        if not self.alive():
            raise McpError(f"{self.name}: server is not running")
        rid = self._new_id()
        slot = {"event": threading.Event(), "msg": None}
        self._pending[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        if not slot["event"].wait(timeout_s()):
            self._pending.pop(rid, None)
            raise McpError(f"{self.name}: {method} timed out after {timeout_s():g} s")
        return self._raise_for(method, slot["msg"])

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def close(self) -> None:
        proc = getattr(self, "proc", None)
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


class HttpClient(_Client):
    transport = "http"

    def start(self) -> None:
        self._cfg = expand_secrets(self.config)
        self.session: str | None = None
        self._handshake()

    def alive(self) -> bool:
        return True

    def _post(self, msg: dict) -> dict | None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   **self._cfg.get("headers", {})}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if msg.get("method") != "initialize":
            headers["MCP-Protocol-Version"] = self.protocol_version
        req = urllib.request.Request(self._cfg["url"], data=json.dumps(msg).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout_s())
        except urllib.error.HTTPError as e:
            raise McpError(f"{self.name}: HTTP {e.code} {e.reason}") from e
        except OSError as e:
            raise McpError(f"{self.name}: {e}") from e
        with resp:
            self.session = resp.headers.get("Mcp-Session-Id") or self.session
            if "id" not in msg or resp.status == 202:
                return None
            if resp.headers.get("Content-Type", "").startswith("text/event-stream"):
                data: list[str] = []
                for raw in resp:
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        event = json.loads("\n".join(data))
                        data = []
                        if event.get("id") == msg["id"] and ("result" in event or "error" in event):
                            return event
                raise McpError(f"{self.name}: event stream ended without a response")
            return json.loads(resp.read().decode("utf-8"))

    def request(self, method: str, params: dict | None = None) -> dict:
        msg = self._post({"jsonrpc": "2.0", "id": self._new_id(), "method": method, "params": params or {}})
        return self._raise_for(method, msg or {})

    def notify(self, method: str, params: dict | None = None) -> None:
        self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def close(self) -> None:
        if getattr(self, "session", None):
            req = urllib.request.Request(self._cfg["url"], method="DELETE",
                                         headers={"Mcp-Session-Id": self.session, **self._cfg.get("headers", {})})
            try:
                urllib.request.urlopen(req, timeout=5).close()
            except OSError:
                pass


def make_client(name: str, config: dict, log: Callable[[str], None] = _nothing,
                on_tools_changed: Callable[[str], None] = _nothing, stderr=None) -> _Client:
    if "command" in config:
        return StdioClient(name, config, log, on_tools_changed, stderr)
    if config.get("type") == "http":
        return HttpClient(name, config, log, on_tools_changed, stderr)
    raise McpError(f"{name}: unsupported transport; only stdio and http servers go through the gateway")
