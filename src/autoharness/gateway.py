"""The autoharness gateway: one MCP server the agent registers once, which runs the servers
`autoharness add mcp` puts in .autoharness/gateway.json, so they work without restarting the agent.

Two fixed meta-tools (harness_tools, harness_call) work in every agent. Native `<server>__<tool>`
tools and notifications/tools/list_changed are offered too, for agents that handle them.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from autoharness import __version__, approvals, egress
from autoharness.mcp_client import PROTOCOL_VERSIONS, McpError, make_client, withheld_env

INSTRUCTIONS = ("Servers added with `autoharness add mcp` appear here without a restart. Call harness_tools to "
                "see them and their tools, then harness_call(server, tool, arguments). If your client shows "
                "tools named <server>__<tool>, you can call those directly.")
META_TOOLS = [
    {"name": "harness_tools",
     "description": "List the MCP servers autoharness added for this task: each server's status (running, or the "
                    "error that stopped it) and its tools with their input schemas and, where the client shows "
                    "them, their native <server>__<tool> names. Call it after `autoharness add mcp`, and when "
                    "harness_call reports an unknown server or tool. It lists only servers added through "
                    "autoharness, not other MCP servers the client already has.",
     "inputSchema": {"type": "object", "properties": {
         "server": {"type": "string", "description": "Only this server; omit to list all of them."}}}},
    {"name": "harness_call",
     "description": "Call one tool on an MCP server autoharness added and return that tool's own result unchanged. "
                    "Take `server` and `tool` from harness_tools and pass `arguments` that match the tool's input "
                    "schema. A server still starting is waited for up to 30 seconds and one whose process exited is "
                    "restarted once; an unknown server, a server that "
                    "could not start, or the tool's own failure comes back as an error result that names the "
                    "cause. Calling <server>__<tool> directly, where the client shows it, does the same.",
     "inputSchema": {"type": "object", "properties": {
         "server": {"type": "string", "description": "Server name as harness_tools lists it."},
         "tool": {"type": "string", "description": "Tool name on that server (not the <server>__<tool> name)."},
         "arguments": {"type": "object", "description": "The tool's arguments, per its input schema."}},
                     "required": ["server", "tool"]}},
]
STARTUP_WAIT = 30.0  # seconds a call waits for a server that is still starting
LIST_CHANGED = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}


def find_project(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        if (d / ".autoharness" / "gateway.json").exists():
            return d
    return None


def _text(t: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": t}], **({"isError": True} if error else {})}


def native_name(server: str, tool: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"{server}__{tool}")[:64]


class Gateway:
    def __init__(self, project: Path | None, write: Callable[[dict], None], log: Callable[[str], None],
                 stderr=None, client_factory=make_client, start: Path | None = None):
        self.project, self.write, self.log, self.stderr = project, write, log, stderr
        self.start = start  # where to look for the project until it has one (a gateway in the agent's own config)
        self.client_factory = client_factory
        self.configs: dict[str, dict] = {}
        self.clients: dict[str, object] = {}
        self.status: dict[str, dict] = {}
        self.tools: dict[str, list[dict]] = {}
        self.registry_error: str | None = None
        self.initialized = False
        self._mtime: int | None = None
        self._allow: list[str] = []
        self._lock = threading.RLock()

    # --- registry and children -------------------------------------------------

    def _registry_file(self) -> Path | None:
        if self.project is None and self.start is not None:
            self.project = find_project(self.start)   # the task adds its first server after the agent started
            if self.project:
                self.log(f"serving {self.project}")
        return self.project / ".autoharness" / "gateway.json" if self.project else None

    def _enforce_hosts(self) -> bool:
        """Follow allowed_hosts: cut off running remote servers it no longer lets through, and boot again
        the ones it blocked once it lets them through."""
        allow = self._allowed_hosts()
        if allow == self._allow:
            return False
        self._allow, cut, back = allow, [], []
        with self._lock:
            for name, cfg in self.configs.items():
                reach = egress.check(cfg, allow)
                blocked = reach["enforced"] and not reach["ok"]
                if blocked and name in self.clients:
                    client = self.clients.pop(name)
                    self.tools[name] = []
                    self.status[name] = {"status": "error", "error": f"blocked: {reach['reason']}"}
                    threading.Thread(target=client.close, daemon=True).start()
                    cut.append(name)
                elif not blocked and self.status.get(name, {}).get("error", "").startswith("blocked:"):
                    self.status[name] = {"status": "starting"}
                    back.append(name)
        for name in back:
            threading.Thread(target=self._boot, args=(name, self.configs[name]), daemon=True).start()
        for name in cut:
            self.log(f"{name}: blocked: allowed_hosts changed")
        if cut:
            self._changed()
        return bool(cut or back)

    def sync(self) -> bool:
        """Bring running servers in line with gateway.json and allowed_hosts. Returns whether anything changed."""
        enforced = self._enforce_hosts()
        enforced = self._retry_approved() or enforced
        path = self._registry_file()
        if path is None or not path.exists():
            if not self.configs:
                return enforced
            self._mtime, wanted = None, {}
        else:
            mtime = path.stat().st_mtime_ns
            if mtime == self._mtime:
                return enforced
            self._mtime = mtime
            try:
                wanted = json.loads(path.read_text("utf-8")).get("servers", {})
                self.registry_error = None
            except ValueError as e:
                self.registry_error = f"gateway.json is not valid JSON ({e}); keeping the previous servers"
                self.log(self.registry_error)
                return enforced
        changed = False
        with self._lock:
            for name in list(self.configs):
                if wanted.get(name) != self.configs[name]:
                    self._stop(name)
                    changed = True
            for name, cfg in wanted.items():
                if name not in self.configs:
                    self.configs[name] = cfg
                    self.status[name] = {"status": "starting"}
                    self.tools[name] = []
                    threading.Thread(target=self._boot, args=(name, cfg), daemon=True).start()
                    changed = True
        if changed:
            self._changed()
        return changed or enforced

    def _retry_approved(self) -> bool:
        """Start the servers that waited for `autoharness gateway approve` once the user approved them."""
        with self._lock:
            ready = [(name, cfg) for name, cfg in self.configs.items()
                     if self.status.get(name, {}).get("error", "").startswith("not approved")
                     and approvals.approved(self.project, name, cfg)]
            for name, _ in ready:
                self.status[name] = {"status": "starting"}
        for name, cfg in ready:
            threading.Thread(target=self._boot, args=(name, cfg), daemon=True).start()
        return bool(ready)

    def _allowed_hosts(self) -> list[str]:
        if self.project is None:
            return []
        from autoharness import config  # lazy: config pulls in the task environment
        return config.allowed_hosts(self.project)

    def _boot(self, name: str, cfg: dict) -> None:
        client = None
        reach = egress.check(cfg, self._allowed_hosts())
        if reach["enforced"] and not reach["ok"]:
            # the user approved the server when it was added, but allowed_hosts changed since
            with self._lock:
                if self.configs.get(name) is cfg:
                    self.status[name] = {"status": "error", "error": f"blocked: {reach['reason']}"}
            self.log(f"{name}: blocked: {reach['reason']}")
            self._changed()
            return
        if not approvals.approved(self.project, name, cfg):
            # gateway.json may come from a pull: run only what the user approved on this machine
            error = (f"not approved on this machine: check its config in .autoharness/gateway.json, then run "
                     f"`autoharness gateway approve {name}`")
            with self._lock:
                if self.configs.get(name) is cfg:
                    self.status[name] = {"status": "error", "error": error}
            self.log(f"{name}: {error}")
            self._changed()
            return
        try:
            client = self.client_factory(name, cfg, log=self.log, on_tools_changed=self._refresh, stderr=self.stderr)
            client.start()
            tools = client.list_tools()
        except (McpError, OSError) as e:
            if client is not None:
                client.close()
            withheld = withheld_env(cfg.get("env", {})) if "command" in cfg else []
            hint = (f" (not passed from the agent's environment: {', '.join(withheld)}; a server that needs one "
                    f"takes it through its `env` as ${{VAR}})" if withheld else "")
            with self._lock:
                if self.configs.get(name) is cfg:
                    self.status[name] = {"status": "error", "error": str(e) + hint}
            self.log(f"{name}: {e}{hint}")
            self._changed()
            return
        with self._lock:
            if self.configs.get(name) is not cfg:
                client.close()
                return
            self.clients[name], self.tools[name], self.status[name] = client, tools, {"status": "running"}
        self._changed()

    def _stop(self, name: str) -> None:
        client = self.clients.pop(name, None)
        for d in (self.configs, self.status, self.tools):
            d.pop(name, None)
        if client is not None:
            threading.Thread(target=client.close, daemon=True).start()

    def _restart(self, name: str):
        with self._lock:
            cfg = self.configs.get(name)
            old = self.clients.pop(name, None)
            if cfg is None:
                return None
            self.status[name] = {"status": "starting"}
        if old is not None:
            old.close()
        self._boot(name, cfg)
        return self.clients.get(name)

    def _refresh(self, name: str) -> None:
        client = self.clients.get(name)
        if client is None:
            return
        try:
            tools = client.list_tools()
        except McpError as e:
            self.log(f"{name}: refreshing tools failed: {e}")
            return
        with self._lock:
            self.tools[name] = tools
        self._changed()

    def _changed(self) -> None:
        self._write_state()
        if self.initialized:
            self.write(LIST_CHANGED)

    def _write_state(self) -> None:
        if not self.project:
            return
        with self._lock:
            servers = {n: {**s, "tools": len(self.tools.get(n, []))} for n, s in self.status.items()}
        path = self.project / ".autoharness" / "gateway.state.json"
        try:
            path.write_text(json.dumps({"pid": os.getpid(), "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "servers": servers}, indent=2, ensure_ascii=False) + "\n", "utf-8")
        except OSError as e:
            self.log(f"could not write {path}: {e}")

    def close_all(self) -> None:
        with self._lock:
            clients = list(self.clients.values())
            self.clients.clear()
        for c in clients:
            c.close()

    # --- tools -----------------------------------------------------------------

    def native_map(self) -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        with self._lock:
            for server in sorted(self.tools):
                for t in self.tools[server]:
                    out.setdefault(native_name(server, t["name"]), (server, t["name"]))
        return out

    def _tool_list(self) -> list[dict]:
        listed = list(META_TOOLS)
        with self._lock:
            by_key = {(s, t["name"]): t for s, ts in self.tools.items() for t in ts}
        for native, key in self.native_map().items():
            t = by_key[key]
            listed.append({"name": native, "description": f"[{key[0]}] {t.get('description', '')}".strip(),
                           "inputSchema": t.get("inputSchema") or {"type": "object"}})
        return listed

    def _describe(self, only: str | None) -> dict:
        reverse = {v: k for k, v in self.native_map().items()}
        servers = {}
        with self._lock:
            for name in sorted(self.status):
                if only and name != only:
                    continue
                st = dict(self.status[name])
                client = self.clients.get(name)
                if st["status"] == "running" and client is not None and not client.alive():
                    st = {"status": "error", "error": "the server process exited; the next call restarts it once"}
                servers[name] = {**st, "transport": "http" if self.configs.get(name, {}).get("type") == "http" else "stdio",
                                 "tools": [{"name": t["name"], "native_name": reverse.get((name, t["name"])),
                                            "description": t.get("description", ""),
                                            "inputSchema": t.get("inputSchema") or {"type": "object"}}
                                           for t in self.tools.get(name, [])]}
        out = {"project": str(self.project) if self.project else None, "servers": servers}
        if self.registry_error:
            out["registry_error"] = self.registry_error
        if not self.project:
            out["note"] = "no .autoharness/gateway.json found from the gateway's working directory upwards"
        return out

    def _forward(self, server: str | None, tool: str | None, arguments: dict) -> dict:
        # a server added a moment ago is still starting in the background: the agent's first call waits
        # for it instead of failing, since `add` told the agent it is available now
        deadline = time.monotonic() + STARTUP_WAIT
        while True:
            with self._lock:
                client, st = self.clients.get(server), self.status.get(server)
            if client is not None or not st or st["status"] != "starting" or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        if client is None:
            if st:
                return _text(f"server {server!r} is {st['status']}" + (f": {st['error']}" if st.get("error") else ""), True)
            return _text(f"unknown server {server!r}; call harness_tools to see what is available", True)
        if not client.alive():
            client = self._restart(server)
            if client is None:
                return _text(f"server {server!r} stopped and did not restart: {self.status.get(server, {}).get('error')}", True)
        try:
            return client.call_tool(tool, arguments)
        except McpError as e:
            return _text(str(e), True)

    def _call(self, params: dict) -> dict:
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "harness_tools":
            return _text(json.dumps(self._describe(args.get("server")), ensure_ascii=False, indent=2))
        if name == "harness_call":
            return self._forward(args.get("server"), args.get("tool"), args.get("arguments") or {})
        target = self.native_map().get(name)
        if target is None:
            return _text(f"unknown tool {name!r}; call harness_tools to see what is available", True)
        return self._forward(target[0], target[1], args)

    # --- JSON-RPC --------------------------------------------------------------

    def handle(self, msg: dict) -> dict | None:
        method, rid = msg.get("method"), msg.get("id")
        if method is None:
            return None
        if rid is None:
            if method == "notifications/initialized":
                self.initialized = True
            return None
        try:
            if method == "initialize":
                asked = msg.get("params", {}).get("protocolVersion")
                result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                          "capabilities": {"tools": {"listChanged": True}},
                          "serverInfo": {"name": "autoharness", "version": __version__},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self._tool_list()}
            elif method == "tools/call":
                result = self._call(msg.get("params", {}))
            else:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as e:  # never let one request kill the gateway
            self.log(f"{method} failed: {e!r}")
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}


def serve(start: Path | None = None) -> int:
    """Run the gateway on stdin/stdout until the agent closes stdin."""
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="\n")
    project = find_project((start or Path.cwd()).resolve())
    log_file = open(project / ".autoharness" / "gateway.log", "a", encoding="utf-8") if project else None
    out_lock = threading.Lock()

    def write(msg: dict) -> None:
        with out_lock:
            sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def log(line: str) -> None:
        stamped = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}"
        print(stamped, file=sys.stderr, flush=True)
        if log_file:
            log_file.write(stamped + "\n")
            log_file.flush()

    gw = Gateway(project, write, log, stderr=log_file, start=(start or Path.cwd()).resolve())
    gw.sync()
    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(1.0):
            try:
                gw.sync()
            except Exception as e:
                log(f"sync failed: {e!r}")

    threading.Thread(target=watch, daemon=True).start()
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            if msg.get("method") == "tools/call":      # calls can be slow: do not block pings and lists
                threading.Thread(target=lambda m=msg: write(gw.handle(m)), daemon=True).start()
                continue
            resp = gw.handle(msg)
            if resp is not None:
                write(resp)
    finally:
        stop.set()
        gw.close_all()
        if log_file:
            log_file.close()
    return 0
