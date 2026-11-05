---
name: claude-kimi-mcp-normalization
description: Use when importing or normalizing Claude Code MCP server declarations into Kimi Code without overwriting existing entries, especially after an import appears to succeed but servers are absent from Kimi
---

# Claude-to-Kimi MCP normalization

## When to use

Use for Claude Code MCP imports, source-to-target scope decisions, field conversion, disabled source servers, and unresolved environment placeholders.

## Steps

1. Resolve `CLAUDE_CONFIG_DIR` before choosing the Claude home. Read Claude's user configuration only for an MCP import and inspect both user-scope `mcpServers` and the resolved project-root or current-directory entry under `projects`.
2. Skip source-project names listed in `disabledMcpServers`; do not silently re-enable them or convert that project choice into a global disabled entry.
3. Keep user declarations user-global and project-local declarations project-local. Preserve every existing Kimi entry and keep the existing Kimi entry on a name collision.
4. Map stdio, HTTP, and SSE to Kimi's `mcpServers` shape. Map `timeout` to `toolTimeoutMs` and `alwaysLoad: true` to `deferred: false`; omit inferred transport except explicit SSE.
5. Drop Claude-only policy fields rather than pretending they work in Kimi. Report unsupported transports and entries blocked by unresolved placeholders.
6. Convert an `Authorization: Bearer ${VAR}` header to `bearerTokenEnvVar: "VAR"`. Drop pass-through environment declarations because Kimi inherits the parent environment; never substitute a secret's current value into the preview or target.
7. Warn about apparent literal credentials and trusted-workspace requirements for project stdio servers. Direct file changes require a new session before `/mcp` can show them.

Reference: https://www.kimi.com/code/docs/en/kimi-code-cli/customization/mcp.html

## How to verify

Compare source and normalized server names, scopes, launch commands or URLs, arguments, environment overlays, and timeouts. Confirm every skipped entry has a reason and every pre-existing Kimi entry is unchanged. Parse the final target JSON, then check `/mcp` in a new session for actual loading and connection status.

## Facts found here

- Context7 accepts `CONTEXT7_API_KEY` instead of the `--api-key` argument, so the literal key can be removed from imported arguments and supplied through the inherited environment. Source: https://www.npmjs.com/package/@upstash/context7-mcp
