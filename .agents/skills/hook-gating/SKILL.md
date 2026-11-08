---
name: hook-gating
description: Hook gating and pre-tool validation for coding agents and MCP server recognition
---

# hook-gating

## When to use
Use when validating agent session hooks, managing pre-tool gates, and recognizing already-installed user-level or project-level MCP servers.

## Steps
1. Discover active and configured MCP servers:
   - Query project-level MCP config (`project.agent.mcp_store(root)`).
   - Query user-level global MCP config (`project.agent.user_mcp_store()`), including `.claude.json` and `.gemini/config/mcp_config.json`.
   - Aggregate with kept MCP servers and builtin servers.
2. In domain coverage checks (`_check_domains`):
   - Check if an MCP server named in `--domains` is already in the installed server set. If so, treat it as valid coverage without demanding external registry searches.
3. In post-brief pre-tool gate (`_skills_not_installed`):
   - Check against all installed servers so that existing user MCP servers (e.g. `github`, `playwright`) do not trigger spurious installation blockers.
4. In pending brief hook gate:
   - Permit writing arguments files (`args.json`, `brief.json`, `*.args.json`, `*.brief.json`) so agents can record briefs using `--args-file` without being deadlocked.
   - Exempt args files from `_writes_records` denial.

## How to verify
1. Run pytest test cases verifying that writing `args.json` during pending state is permitted.
2. Verify that an already configured MCP server in user/global settings satisfies domain requirements.

## Facts found here
- Claude Code user MCP configuration resides in `~/.claude.json` under `mcpServers` (source: https://modelcontextprotocol.io/).
- Antigravity CLI user MCP configuration resides in `~/.gemini/config/mcp_config.json` under `mcpServers`.
