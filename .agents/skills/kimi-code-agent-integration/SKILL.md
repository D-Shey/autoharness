---
name: kimi-code-agent-integration
description: Use when integrating Kimi Code CLI with external agent tooling (harnesses, adapters, CI wrappers): where its MCP config and skills live, how to run it headless, and what cannot be detected from the environment
---

# Kimi Code integration for agent tooling

## When to use

Use when a tool must drive, configure, or detect Kimi Code CLI the way it drives other coding agents
(Claude Code, Codex, Cursor): writing MCP server entries, installing skills, running a headless
review, or deciding how to detect that it runs inside Kimi.

## Steps

1. Resolve the data root from `KIMI_CODE_HOME`, falling back to `~/.kimi-code`; do not hardcode
   the fallback, the variable relocates config, sessions and OAuth data together.
2. MCP config is `{"mcpServers": {...}}` JSON: user-global at `<data root>/mcp.json`, project at
   `.kimi-code/mcp.json` (cwd-level; project root is the nearest `.git` upward). Some builds also
   read a project-root `.mcp.json` (Claude-compatible scope). Entries: stdio `command` (+ `args`,
   `env`, `cwd`), HTTP `url` (+ `headers`, `bearerTokenEnvVar`), legacy SSE needs
   `transport: "sse"`. An `Authorization: Bearer ${VAR}` header becomes `bearerTokenEnvVar: "VAR"`.
3. Skills: Kimi scans `.kimi-code/skills/` and `.agents/skills/` at project level,
   `<data root>/skills/` and `~/.agents/skills/` at user level (directory form `<name>/SKILL.md`
   with `name` and `description` frontmatter required, or flat `<name>.md`). Writing to
   `.agents/skills` serves Codex/agy/Kimi from one copy.
4. Headless run: `kimi -p "<prompt>"` (auto permission, text to stdout, `--output-format
   stream-json` for JSONL). It cannot combine with `--yolo`/`--auto`/`--plan`, so a read-only
   reviewer run is instruction-based: point it at a prompt file and forbid edits. Project-level
   MCP servers stay disabled in headless runs unless the workspace is trusted
   (`KIMI_CODE_TRUST_WORKSPACE=1` for that process, or `[workspace] require_trust = false`).
5. Detection: Kimi Code sets no documented env marker for subprocesses and no session-id env var
   (hooks receive `session_id` via stdin JSON only) — do not invent a `KIMI_*` detection variable;
   rely on an explicit override (e.g. `AUTOHARNESS_AGENT=kimi`).

## How to verify

Confirm a written `.kimi-code/mcp.json` is accepted by the real CLI: a headless
`KIMI_CODE_TRUST_WORKSPACE=1 kimi -p "..."` in that directory can see and call the server's
`mcp__<server>__*` tools. Validate `config.toml`/`tui.toml` with `kimi doctor`; it does not check
`mcp.json` — parse that JSON separately.

## Facts found here

- Measured 2026-11-05 with a stdio probe server: Kimi Code passes the literal `${VAR}` in `mcp.json`
  `env` (no expansion), and a stdio server inherits the CLI's environment (a var exported to `kimi`
  reached the server with no `env` entry at all).
- `kimi -p` loads `.kimi-code/mcp.json` of the cwd and exposes `mcp__<server>__*` tools when run with
  `KIMI_CODE_TRUST_WORKSPACE=1` (project servers stay disabled in headless runs in untrusted folders).
- `kimi -p` output prefixes transcript lines with `• `; tool progress goes to stderr.
- `kimi doctor` validates only `config.toml` and `tui.toml`, not `mcp.json`.
- Reference: https://www.kimi.com/code/docs/en/kimi-code-cli/customization/mcp.html ,
  .../customization/skills.html , .../reference/kimi-command.html

Docs fetched 2026-11-05 from kimi.com/code/docs/en.
