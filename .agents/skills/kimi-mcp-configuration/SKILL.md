---
name: kimi-mcp-configuration
description: Use when inspecting, editing, validating, or reloading Kimi Code MCP mcp.json configuration, especially when configured or imported servers do not appear in /mcp
---

# Kimi MCP configuration

## When to use

Use for missing MCP servers, invalid or misplaced `mcp.json` files, scope conflicts, and server additions that are not visible in an open session.

## Steps

1. Resolve the user data root from `KIMI_CODE_HOME`, falling back to `~/.kimi-code`; do not assume the fallback when the variable is set.
2. Inspect the user-global `mcp.json`, the project-root `.mcp.json`, and the current-working-directory `.kimi-code/mcp.json` as applicable. Record which file declares each server so edits happen at the actual source.
3. Preserve the top-level `mcpServers` object and every unrelated entry. A missing file can be initialized with an empty `mcpServers` object; malformed JSON must be reported rather than overwritten.
4. Use `command` for stdio and `url` for HTTP; add `transport: "sse"` only for legacy SSE. Keep timeouts as integer milliseconds in the documented range and use environment-variable references instead of new literal secrets.
5. Show the target path and intended change before writing. Project stdio entries execute at session start, so they belong only in trusted workspaces.
6. Start a new session or restart Kimi after direct file edits. Servers added mid-session are not registered in that session; use `/mcp` to inspect connection status.

Reference: https://www.kimi.com/code/docs/en/kimi-code-cli/customization/mcp.html

## How to verify

Parse the final JSON and inspect the merged server names by source and precedence. Compare the written entry with the requested command, URL, arguments, environment overlays, and timeouts. In a new session, check `/mcp` for the server and its connection status; do not treat JSON parsing alone as proof that the server connected.

## Facts found here

- `kimi doctor` validated `config.toml` and `tui.toml` during this task but did not report `mcp.json`; parse and compare MCP JSON separately.
- When six imported servers exceeded the provider's tool-schema size limit, `[mcp] default_deferred = true` together with `[experimental] tool-select = true` allowed a new session to start and expose the MCP manifests on demand.
