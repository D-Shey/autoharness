# Gateway MCP: design

Date: 2026-09-21. Status: in review.

## Why

Agents read MCP servers from their config only when a session starts, so `autoharness add mcp` needs a
restart. The gateway removes that limit: autoharness itself becomes an MCP server that is registered
in the agent once and from then on runs the added servers inside itself, without restarting the
agent.

### Why not just `tools/list_changed`

The MCP protocol lets a server tell the agent that its tool list changed. As of September 2026 this
cannot be relied on:

| Agent | State |
|---|---|
| Claude Code | the docs claim support since 2.1.0, but new tools do not reach deferred tool search: issue anthropics/claude-code#77314 (2.1.198), closed as not planned |
| Codex | does not request `tools/list` again after the notification: openai/codex#33266 (0.144.1), open |
| Gemini CLI | the code is merged but does not work: google-gemini/gemini-cli#19509, open, P1 |
| Qwen Code, GigaCode | no data; Qwen Code is a fork of Gemini CLI |

So the main path is **a fixed set of meta-tools** that work in any agent. Native tools with the
notification are an addition where the agent handles it.

## Out of scope

- OAuth sign-in for remote servers (such servers go into the agent's config, as now).
- The legacy SSE transport (`type: "sse"`): also straight into the agent's config.
- Notifications from HTTP servers (the Streamable HTTP GET stream): the gateway does not listen for
  them; `list_changed` is accepted from stdio servers only.
- Requests from child servers to the client (`sampling`, `roots/list`, `elicitation`): the answer is
  `-32601 Method not found`.
- Call cancellation and progress.

## Installing the gateway

`autoharness gateway install` (for the project's agent, `--agent` as usual):

1. Writes a server named `autoharness` into the agent's MCP config:
   - `{"command": "autoharness", "args": ["gateway", "serve"]}` if `autoharness` is on `PATH`;
   - otherwise `{"command": "<sys.executable>", "args": ["-m", "autoharness", "gateway", "serve"]}`.
   If an `autoharness` server is already in the config and autoharness did not write it, it is an
   error, as for any foreign server.
2. Creates or updates `.autoharness/gateway.json`: `installed` gets the agent's name.
3. The answer: `next` asks the user to restart the agent (one last time) and approve the project
   server.

`gateway uninstall` is the reverse; the servers in `gateway.json` stay recorded, but while the gateway
is not installed, `add mcp` writes into the agent's config again.
`gateway status`: whether the gateway is installed for the agent, which servers are in
`gateway.json`, errors from the last run (if the gateway wrote `gateway.state.json`, see below).

`.autoharness/.gitignore` lets `gateway.json` through as well (like `config.json` and `kept.json`).

## `.autoharness/gateway.json`

```json
{
  "installed": ["claude"],
  "servers": {
    "context7": {"command": "npx", "args": ["-y", "@upstash/context7-mcp"]},
    "github": {"type": "http", "url": "https://api.githubcopilot.com/mcp/",
               "headers": {"Authorization": "Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}"}}
  }
}
```

Configs are in the canonical form (the `.mcp.json` format, secrets as `${VAR}`), the same as in the
lockfile. Who owns an entry is still known by the task's lockfile and `kept.json`; `gateway.json` only
says "what should be running".

## The `add mcp` route

A server goes through the gateway when all of these hold:

- `.autoharness/gateway.json` has the current agent in `installed` **and** the agent's config has the
  `autoharness` server;
- the config can be proxied: it has `command` (stdio) **or** `type == "http"` with non-empty `headers`
  (a remote server without headers may need OAuth and goes direct);
- `--direct` was not passed.

Then:

- the entry is added to `gateway.json["servers"]`, nothing is written to the agent's config;
- the lockfile entry gets `"via": "gateway"`;
- `add` answers `"via": "gateway"`, and `next`: the server is available now; call `harness_tools`,
  then `harness_call` (or the native `<server>__<tool>` if the agent shows them); no restart is
  needed. If secrets are needed, it names the variables and says the agent's process must see them.

Otherwise the behaviour is as before, and if the gateway is not installed, `next` adds a hint: offer
the user `autoharness gateway install` once, so later servers need no restart.

`remove_mcp`, `teardown` (`remove`), `Project.remove_kept` and `prune` remove a server from
`gateway.json` instead of the agent's config for entries with `via: gateway`. `keep` leaves it in
`gateway.json`. The "already there and not ours" check for a name takes `gateway.json` into account
too.

## `gateway serve`

An MCP server on stdio (line-delimited JSON-RPC 2.0, one message per line, UTF-8).

**Finding the project.** From the current directory up to the first one that has
`.autoharness/gateway.json`; if there is none, it runs with an empty set and says so in
`harness_tools`.

**`initialize`.** The answer: `protocolVersion` is the one the agent asked for if it is in
`SUPPORTED = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")`, otherwise the first in the
list; `capabilities: {"tools": {"listChanged": true}}`; `serverInfo: {"name": "autoharness", "version"}`;
`instructions`: a short note on `harness_tools` / `harness_call`.

**`tools/list`.** The meta-tools, then the native tools of every running server.

```json
{"name": "harness_tools",
 "description": "List MCP servers autoharness added for this task and their tools (with input schemas). Call this after `autoharness add mcp`.",
 "inputSchema": {"type": "object", "properties": {"server": {"type": "string"}}}}
{"name": "harness_call",
 "description": "Call a tool of an MCP server autoharness added: harness_call(server, tool, arguments).",
 "inputSchema": {"type": "object", "properties": {"server": {"type": "string"}, "tool": {"type": "string"},
                 "arguments": {"type": "object"}}, "required": ["server", "tool"]}}
```

The native name: `re.sub(r"[^a-zA-Z0-9_-]", "_", f"{server}__{tool}")[:64]`; when names collide, the
first server alphabetically gets it, the rest are reachable only through `harness_call`.

**`tools/call`.** `harness_tools` → text with the JSON
`{"servers": {name: {"status": "running|starting|error", "error"?, "transport": "stdio|http",
"tools": [{"name", "native_name"|null, "description", "inputSchema"}]}}, "project": path|null}`.
`harness_call` and native names → `tools/call` of the child server; the result (`content`, `isError`,
`structuredContent`) is returned unchanged. An unknown server or tool → a result with
`isError: true` and text saying what to do (`harness_tools`).

**`ping`** → `{}`. Other methods → `-32601`. Notifications from the agent
(`notifications/initialized`, `notifications/cancelled`) are accepted silently.

**Watching.** A thread checks the `mtime` of `gateway.json` once a second. On a change: new servers
start (in the background, status `starting`), removed ones stop, changed ones restart; then
`notifications/tools/list_changed` goes to the agent. The same happens when a stdio server sends
`list_changed` itself (the gateway reads its `tools/list` again). The notification is sent only after
the agent has sent `notifications/initialized`.

**State for `gateway status`.** After every change the gateway writes
`.autoharness/gateway.state.json`: `{"pid", "updated", "servers": {name: {"status", "error"?, "tools": n}}}`.

## Child servers (`mcp_client.py`)

The common interface: `start()`, `list_tools() -> list[dict]`, `call_tool(name, arguments) -> dict`,
`close()`; at start: `initialize` (our latest version from `SUPPORTED`, `capabilities: {}`,
`clientInfo: {"name": "autoharness-gateway"}`) → `notifications/initialized` → `tools/list` (paged by
`nextCursor`).

**Filling in secrets.** `${VAR}` in `command`, `args`, `env`, `url`, `headers` is replaced with values
from the gateway's environment. A missing variable → the server gets the status
`error: set <VAR> in the environment the agent is started from`, and the process does not start.

**stdio.** `subprocess.Popen` with `shutil.which(command)` (on Windows it finds `npx.cmd`); the
environment is the gateway's plus the config's `env`; `stdin/stdout` are pipes, `stderr` goes to
`.autoharness/gateway.log`. A reader thread parses lines: answers are matched with waiting requests
by `id`, `notifications/tools/list_changed` triggers a re-read, requests from the server get
`-32601`. Non-line output on the child's stdout is written to the log and skipped.

**HTTP (Streamable HTTP).** `urllib.request`, `POST url`, headers
`Content-Type: application/json`, `Accept: application/json, text/event-stream`, the config's headers,
and after `initialize`, `MCP-Protocol-Version` and `Mcp-Session-Id` (if the server issued one). An
`application/json` answer is the whole message; with `text/event-stream`, `data:` events are read
until the answer with our `id`; `202` to a notification is fine. `close()` sends `DELETE` with
`Mcp-Session-Id` and ignores errors.

**Timeouts and failures.** A request to a child server waits `AUTOHARNESS_GATEWAY_TIMEOUT` seconds
(120 by default) → a result with `isError: true` "timed out after N s". A crashed stdio process →
status `error`; the next call tries to restart it once, and on failure returns `isError: true` with the
last lines of the log. On exit (EOF on stdin, a signal) the gateway closes all children.

**The gateway's stdout** carries JSON-RPC only. Everything else goes to stderr and
`.autoharness/gateway.log`.

## Instructions to the agent (`SKILL.md`)

- Step 3: if `add mcp` answered `via: gateway`, the server is available right away: `harness_tools`,
  then `harness_call`, or the native `<server>__<tool>` if they appeared. No restart is needed.
- If the gateway is not installed and `add mcp` suggested `gateway install`, offer it to the user
  once, explaining that it is the last restart.
- The paragraph "New MCP servers load at session start" stays for the case without the gateway.

README: a "Gateway" section, a row in the command table, and the item in "MVP limitations" becomes the
gateway's limits (OAuth and SSE go direct; native tools depend on `list_changed` in the agent).

## Where it goes in the code

| File | What |
|---|---|
| `src/autoharness/mcp_client.py` (new) | `expand_secrets`, `StdioClient`, `HttpClient`, `make_client(name, config, log)` |
| `src/autoharness/gateway.py` (new) | the `gateway.json` registry (`load`, `save`, `is_installed`, `proxyable`), `Gateway` (the JSON-RPC server), `serve()` |
| `src/autoharness/env.py` | the route in `Task.add_mcp` / `remove_mcp` / `_keep_all` / `Project.remove_kept`, `STATE_GITIGNORE` |
| `src/autoharness/cli.py` | `gateway install|uninstall|status|serve`, `add mcp --direct`, the `add` answers |
| `tests/fake_mcp_server.py` (new) | a fake stdio MCP server for tests |

## Errors

| Situation | Behaviour |
|---|---|
| `gateway install` while an `autoharness` server already exists and is foreign | the error "not managed by autoharness" |
| `gateway.json` does not parse | the gateway keeps the previous set; the error goes to the log and to `harness_tools` (`registry_error`) |
| no environment variable for a secret | status `error` with the variable's name |
| command not found | status `error: command not found: <cmd>` |
| a child server did not answer `initialize` within the timeout | status `error`, the process is stopped |

## Tests

No network and no Node: a fake stdio server `tests/fake_mcp_server.py` (tools `echo` and `slow`; on
the `add_tool` command it sends `list_changed` itself) and a fake HTTP server on `http.server` in a
thread (JSON and SSE answers, `Mcp-Session-Id`).

1. `StdioClient`: initialize, list_tools, call_tool, `-32601` to a request from the server,
   `list_changed` from the server triggers a re-read.
2. `HttpClient`: a JSON answer, an SSE answer, passing `Mcp-Session-Id`, `${VAR}` in headers.
3. `expand_secrets`: substitution and the error for a missing variable.
4. End to end: `gateway serve` as a process; `initialize` → `tools/list` (two meta-tools) → a fake
   server written into `gateway.json` → `list_changed` arrives → `tools/list` contains `fake__echo` →
   `harness_call` and a direct `fake__echo` return the result → removal from `gateway.json` →
   `list_changed`, the tool is gone.
5. A timeout (`slow`) → `isError: true`; a crashed server → `error` in `harness_tools`.
6. The `add mcp` route: with the gateway installed (stdio → gateway, http with headers → gateway,
   http without headers → direct, `--direct` → direct); without the gateway → direct, with a hint.
7. `teardown remove` and `prune` remove from `gateway.json`; `keep` leaves it.
8. `gateway install` / `uninstall` / `status`; refusal when a foreign `autoharness` server exists.

Manual check after implementation: `gateway install` in a test project, restart Claude Code,
`add mcp fetch` without a restart; record whether Claude Code sees the native tools or only the
meta-tools work.
