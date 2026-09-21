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

from autoharness import __version__
from autoharness.mcp_client import PROTOCOL_VERSIONS, McpError, make_client

INSTRUCTIONS = ("Servers added with `autoharness add mcp` appear here without a restart. Call harness_tools to "
                "see them and their tools, then harness_call(server, tool, arguments). If your client shows "
                "tools named <server>__<tool>, you can call those directly.")
META_TOOLS = [
    {"name": "harness_tools",
     "description": "List MCP servers autoharness added for this task and their tools (with input schemas). "
                    "Call this after `autoharness add mcp`.",
     "inputSchema": {"type": "object", "properties": {"server": {"type": "string"}}}},
    {"name": "harness_call",
     "description": "Call a tool of an MCP server autoharness added: harness_call(server, tool, arguments).",
     "inputSchema": {"type": "object", "properties": {"server": {"type": "string"}, "tool": {"type": "string"},
                                                      "arguments": {"type": "object"}},
                     "required": ["server", "tool"]}},
]
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
                 stderr=None, client_factory=make_client):
        self.project, self.write, self.log, self.stderr = project, write, log, stderr
        self.client_factory = client_factory
        self.configs: dict[str, dict] = {}
        self.clients: dict[str, object] = {}
        self.status: dict[str, dict] = {}
        self.tools: dict[str, list[dict]] = {}
        self.registry_error: str | None = None
        self.initialized = False
        self._mtime: int | None = None
        self._lock = threading.RLock()

    # --- registry and children -------------------------------------------------

    def _registry_file(self) -> Path | None:
        return self.project / ".autoharness" / "gateway.json" if self.project else None

    def sync(self) -> bool:
        """Bring running servers in line with gateway.json. Returns whether anything changed."""
        path = self._registry_file()
        if path is None or not path.exists():
            if not self.configs:
                return False
            self._mtime, wanted = None, {}
        else:
            mtime = path.stat().st_mtime_ns
            if mtime == self._mtime:
                return False
            self._mtime = mtime
            try:
                wanted = json.loads(path.read_text("utf-8")).get("servers", {})
                self.registry_error = None
            except ValueError as e:
                self.registry_error = f"gateway.json is not valid JSON ({e}); keeping the previous servers"
                self.log(self.registry_error)
                return False
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
        return changed

    def _boot(self, name: str, cfg: dict) -> None:
        client = None
        try:
            client = self.client_factory(name, cfg, log=self.log, on_tools_changed=self._refresh, stderr=self.stderr)
            client.start()
            tools = client.list_tools()
        except (McpError, OSError) as e:
            if client is not None:
                client.close()
            with self._lock:
                if self.configs.get(name) is cfg:
                    self.status[name] = {"status": "error", "error": str(e)}
            self.log(f"{name}: {e}")
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
        with self._lock:
            client, st = self.clients.get(server), self.status.get(server)
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
