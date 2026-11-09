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

INSTRUCTIONS = ("Servers added with `autoharness add mcp` appear here without a restart. In Code Mode, call "
                "harness_search to find typed TypeScript signatures, and harness_execute(code) to run scripts in "
                "an isolated sandbox. You can also call harness_tools to see raw schemas, "
                "harness_call(server, tool, arguments), or native <server>__<tool> tools directly.")
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
    {"name": "harness_search",
     "description": "Search available MCP servers and tools in Code Mode. Returns compact TypeScript definitions "
                    "(.d.ts) describing matching tool signatures and types. Omit `query` or pass '*' to return all.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Search term matching server, tool name, or description."}}}},
    {"name": "harness_execute",
     "description": "Execute JavaScript/TypeScript code in an isolated sandbox with RPC access to all added MCP "
                    "servers via the global `mcp` object (e.g. `await mcp.server.tool(args)`). Combines chains of "
                    "calls, loops, and data aggregation in a single step without context bloat or secret leakage.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string", "description": "The JavaScript code to execute. Use `return ...` to return data."},
         "timeout": {"type": "number", "description": "Execution timeout in seconds (default: 30)."}},
                     "required": ["code"]}},
    {"name": "harness_history_search",
     "description": "Search across task execution logs (`runs`), reviewer answers (`reviews`), and events on disk "
                    "without loading entire log files into model context. Implements active context retrieval.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Search term matching log output, test names, or review findings."},
         "kind": {"type": "string", "enum": ["all", "runs", "reviews"], "description": "Category to search (default: 'all')."},
         "max_matches": {"type": "number", "description": "Maximum number of snippet matches (default: 5)."}},
                     "required": ["query"]}},
    {"name": "harness_memory_search",
     "description": "Hybrid BM25 & LEANN vector search across project memory (.autoharness/memory.db): past rulings, lessons, decisions, review verdicts, and swarm tasks with Reciprocal Rank Fusion (RRF).",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Search term to match against titles and content."},
         "kind": {"type": "string", "enum": ["all", "ruling", "lesson", "review", "followup", "swarm_task"], "description": "Filter by memory kind (default: 'all')."},
         "mode": {"type": "string", "enum": ["hybrid", "fts", "vector"], "description": "Search mode: 'hybrid' (BM25 + vector RRF), 'fts' (BM25 only), or 'vector' (cosine only). Default: 'hybrid'."},
         "limit": {"type": "integer", "description": "Maximum number of ranked results to return (default: 5)."}},
                     "required": ["query"]}},
    {"name": "harness_codemap",
     "description": "Generate or search a compact Python AST Symbol Map (classes, methods, functions, signatures, docstrings) for the codebase, reducing token count by 80-90% compared to reading full files.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Relative file or directory path (default: project root)."},
         "query": {"type": "string", "description": "Optional symbol or function name search query."},
         "policy": {"type": "boolean", "description": "Check deterministic AST structural policies (dead private methods, silenced exceptions, unclosed files)."},
         "raw_json": {"type": "boolean", "description": "Whether to return raw JSON instead of indented outline."}}}},
    {"name": "harness_swarm_status",
     "description": "Inspect status and progress of multi-agent swarm plans in .autoharness/swarm. Omit plan_id to inspect the most recent plan.",
     "inputSchema": {"type": "object", "properties": {
         "plan_id": {"type": "string", "description": "Optional swarm plan ID or file path."}}}},
    {"name": "harness_swarm_run",
     "description": "Execute a multi-agent DAG swarm workflow with concurrent worker pool, role isolation, and hard verification gates.",
     "inputSchema": {"type": "object", "properties": {
         "plan_id": {"type": "string", "description": "Swarm plan ID or path to execute (default: most recent plan)."},
         "parallel": {"type": "integer", "description": "Maximum concurrent tasks (default: 4)."}}}},
    {"name": "harness_drive_status",
     "description": "Inspect continuous autonomy backlog queue and current self-driving session status.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "harness_drive_run",
     "description": "Trigger an autonomous self-driving loop run over open followups with regression rollback protection.",
     "inputSchema": {"type": "object", "properties": {
         "max_tasks": {"type": "integer", "description": "Maximum followups to resolve in this session (default: 3)."},
         "dry_run": {"type": "boolean", "description": "Whether to return planned tasks without modifying code."},
         "followup_id": {"type": "integer", "description": "Optional specific followup ID to execute."},
         "test_command": {"type": "string", "description": "Custom regression test command (e.g. pytest tests/test_drive.py)."}}}},
    {"name": "harness_update_check",
     "description": "Check if newer versions of AutoHarness are available on GitHub.",
     "inputSchema": {"type": "object", "properties": {
         "repo": {"type": "string", "description": "Optional GitHub repo coordinates (default: D-Shey/autoharness)."}}}},
    {"name": "harness_permissions_status",
     "description": "Check if agent YOLO / auto-approval mode is active globally or at the project level.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "harness_permissions_enable",
     "description": "Enable project-level auto-permissions / YOLO mode so tool calls and command execution are not blocked by manual confirmation prompts.",
     "inputSchema": {"type": "object", "properties": {}}},
]
STARTUP_WAIT = 30.0  # seconds a call waits for a server that is still starting
LIST_CHANGED = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}


def find_project(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        if (d / ".autoharness" / "gateway.json").exists():
            return d
    return None


CALLS_FILE = "gateway.calls.json"


def note_call(project: Path, server: str) -> None:
    """Count a call to an MCP server and when it was last made, so teardown can tell a server the task
    installed from one it used. Both the gateway and the hook (for servers in the agent's own config) write it."""
    path = project / ".autoharness" / CALLS_FILE
    try:
        calls = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        calls = {}
    entry = calls.get(server) if isinstance(calls.get(server), dict) else {}
    calls[server] = {"calls": entry.get("calls", 0) + 1, "last": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        path.write_text(json.dumps(calls, indent=2, ensure_ascii=False) + "\n", "utf-8")
    except OSError:
        pass   # a lost count must never fail the call


def calls(project: Path) -> dict:
    try:
        data = json.loads((project / ".autoharness" / CALLS_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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

    def _resolve_server(self, name: str | None) -> str | None:
        if not name:
            return None
        with self._lock:
            if name in self.clients or name in self.status:
                return name
            from autoharness.codegen import _sanitize_ident
            for s in self.status:
                if _sanitize_ident(s) == name:
                    return s
        return name

    def _forward(self, server: str | None, tool: str | None, arguments: dict) -> dict:
        server = self._resolve_server(server)
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
        if self.project:
            note_call(self.project, server)
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
        if name == "harness_search":
            from autoharness import codegen
            query = args.get("query")
            with self._lock:
                current_tools = {s: list(ts) for s, ts in self.tools.items()}
            return _text(codegen.search_definitions(query, current_tools))
        if name == "harness_execute":
            from autoharness import sandbox
            code = args.get("code") or ""
            timeout = float(args.get("timeout") or 30.0)
            res = sandbox.execute(code, forward_fn=self._forward, timeout=timeout)
            if res.get("isError"):
                return _text(res.get("error", "Code Mode execution failed"), error=True)
            output = res.get("result")
            text_repr = json.dumps(output, ensure_ascii=False, indent=2) if isinstance(output, (dict, list)) else str(output if output is not None else "undefined")
            if res.get("logs"):
                logs_repr = "\n".join(res["logs"])
                text_repr = f"--- Logs ---\n{logs_repr}\n--- Result ---\n{text_repr}"
            return _text(text_repr)
        if name == "harness_history_search":
            from autoharness.context_pruning import search_task_history
            query = args.get("query") or ""
            kind = args.get("kind") or "all"
            max_matches = int(args.get("max_matches") or 5)
            task_dir = None
            if self.project:
                tasks_dir = self.project / ".autoharness" / "tasks"
                if tasks_dir.is_dir():
                    task_dirs = sorted([d for d in tasks_dir.iterdir() if d.is_dir()])
                    if task_dirs:
                        task_dir = task_dirs[-1]
            if not task_dir:
                return _text("No active task history found on disk.")
            matches = search_task_history(task_dir, query=query, kind=kind, max_matches=max_matches)
            if not matches:
                return _text(f"No matches found for query {query!r} in {kind} logs.")
            snippets = []
            for m in matches:
                snippets.append(f"[{m['category']}] {m['source']} (line {m['line']}):\n{m['snippet']}")
            return _text("\n\n".join(snippets))
        if name == "harness_memory_search":
            from autoharness import memory, vector
            query = args.get("query") or ""
            kind = args.get("kind")
            mode = args.get("mode") or "hybrid"
            limit = int(args.get("limit") or 5)
            if not self.project:
                return _text("No project root configured for memory search.")
            con = memory.get_memory_db(self.project)
            try:
                if mode == "vector":
                    results = vector.search_vectors(con, query, kind=kind, limit=limit)
                elif mode == "fts":
                    results = memory.search_memory(con, query, kind=kind, limit=limit)
                else:  # hybrid
                    results = vector.hybrid_search(con, query, kind=kind, limit=limit)
            finally:
                con.close()
            if not results:
                return _text(f"No memory entries found for query {query!r} in mode {mode!r}.")
            formatted = []
            for r in results:
                score_str = f"RRF: {r.get('rrf_score')}" if "rrf_score" in r else (f"Vector: {r.get('vector_score')}" if "vector_score" in r else f"BM25: {r.get('score')}")
                formatted.append(f"[{r['kind'].upper()}] {r['title']} (source: {r['source']}, {score_str}):\n{r['content']}")
            return _text("\n\n---\n\n".join(formatted))
        if name == "harness_codemap":
            from autoharness import codemap
            rel_path = args.get("path") or "."
            query = args.get("query")
            raw_json = bool(args.get("raw_json"))
            check_policy = bool(args.get("policy"))
            root = (self.project / rel_path) if self.project else Path(rel_path)
            if check_policy:
                violations = codemap.scan_ast_policies(root)
                return _text(json.dumps({"total_violations": len(violations), "violations": violations}, ensure_ascii=False, indent=2))
            cmap = codemap.generate_codemap(root)
            if query:
                matches = codemap.search_codemap(cmap, query)
                if not matches:
                    return _text(f"No symbols found matching {query!r}.")
                return _text(json.dumps(matches, ensure_ascii=False, indent=2))
            if raw_json:
                return _text(json.dumps(cmap, ensure_ascii=False, indent=2))
            return _text(codemap.format_codemap_tree(cmap))
        if name == "harness_swarm_status":
            from autoharness import swarm
            plan_id = args.get("plan_id")
            if not self.project:
                return _text("No project root configured for swarm operations.")
            if not plan_id:
                plans = swarm.list_swarm_plans(self.project)
                if not plans:
                    return _text("No swarm plans found in .autoharness/swarm/.")
                plan_id = plans[0]["id"]
            try:
                plan = swarm.load_swarm_plan(plan_id, self.project)
                return _text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
            except Exception as e:
                return _text(f"Error loading swarm plan: {e}", error=True)
        if name == "harness_swarm_run":
            from autoharness import swarm
            plan_id = args.get("plan_id")
            parallel = int(args.get("parallel") or 4)
            if not self.project:
                return _text("No project root configured for swarm operations.")
            if not plan_id:
                plans = swarm.list_swarm_plans(self.project)
                if not plans:
                    return _text("No swarm plans found to execute.")
                plan_id = plans[0]["id"]
            try:
                plan = swarm.load_swarm_plan(plan_id, self.project)
                executed = swarm.execute_swarm_plan(plan, self.project, max_parallel=parallel)
                summary = {
                    "plan_id": executed.id,
                    "status": executed.status,
                    "tasks": {tid: t.status for tid, t in executed.tasks.items()},
                }
                return _text(json.dumps(summary, ensure_ascii=False, indent=2))
            except Exception as e:
                return _text(f"Swarm execution failed: {e}", error=True)
        if name == "harness_drive_status":
            from autoharness import drive
            if not self.project:
                return _text("No project root configured for drive.")
            state = drive.load_drive_state(self.project)
            queue = drive.get_drive_queue(self.project)
            summary = {
                "drive_status": state.get("status", "idle"),
                "session_id": state.get("session_id"),
                "open_queue_count": len(queue),
                "completed_count": len(state.get("completed", [])),
                "failed_count": len(state.get("failed", [])),
                "queue": queue[:5],
            }
            return _text(json.dumps(summary, indent=2, ensure_ascii=False))
        if name == "harness_drive_run":
            from autoharness import drive
            if not self.project:
                return _text("No project root configured for drive.")
            max_tasks = int(args.get("max_tasks") or 3)
            dry_run = bool(args.get("dry_run"))
            followup_id = args.get("followup_id")
            if followup_id is not None:
                followup_id = int(followup_id)
            test_cmd = args.get("test_command")
            res = drive.run_drive_loop(self.project, max_tasks=max_tasks, dry_run=dry_run, followup_id=followup_id, test_command=test_cmd)
            return _text(json.dumps(res, indent=2, ensure_ascii=False))
        if name == "harness_update_check":
            from autoharness import updater
            repo = args.get("repo") or updater.DEFAULT_REPO
            res = updater.check_for_updates(repo=repo)
            return _text(json.dumps(res, indent=2, ensure_ascii=False))
        if name == "harness_permissions_status":
            from autoharness import permissions
            if not self.project:
                return _text("No project root configured for permissions.")
            from autoharness.env import Project
            proj = Project(self.project)
            res = permissions.check_agent_permissions(proj)
            return _text(json.dumps(res, indent=2, ensure_ascii=False))
        if name == "harness_permissions_enable":
            from autoharness import permissions
            if not self.project:
                return _text("No project root configured for permissions.")
            from autoharness.env import Project
            proj = Project(self.project)
            res = permissions.enable_project_permissions(proj)
            return _text(json.dumps(res, indent=2, ensure_ascii=False))
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
