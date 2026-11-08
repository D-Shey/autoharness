---
name: codemode-gateway
description: Code Mode sandbox execution and RPC dispatch in AutoHarness gateway
---

# codemode-gateway

## When to use
Use when implementing or debugging Code Mode execution in AutoHarness Gateway:
- Connecting isolated script execution environments (Node.js/QuickJS subprocesses) with Gateway's internal MCP tool registry.
- Proxying tool calls from sandbox scripts to MCP client instances (`client.call_tool`) without leaking host environment secrets or filesystem access.
- Batching multi-step tool calls, loops, and data transformations into a single model round-trip.

## Steps
1. **Define the RPC protocol over stdio**:
   - Establish a line-delimited JSON-RPC channel on stdio between the Gateway host and the sandbox runner.
   - Deliver initial script payload via the first line of stdin rather than argv to avoid OS command-line length limits (Windows 32KB limit).
   - Reference: Node.js stdio stream handling (https://nodejs.org/api/process.html#processstdout).
2. **Robust async timeout and cancellation**:
   - `node:vm` timeout only interrupts synchronous execution loops; async promises require an explicit JS-side `setTimeout` watchdog timer.
   - The host Python process must implement an independent watchdog timer (`threading.Timer`) to forcefully terminate `proc.kill()` if stdio reads block on hanging async promises.
   - Reference: Node.js VM context isolation (https://nodejs.org/api/vm.html).
3. **Sanitized identifier mapping**:
   - When generating TypeScript definitions, server names with hyphens or dots are converted to valid TS identifiers (e.g., `git-hub` -> `git_hub`).
   - The Gateway dispatch layer must maintain a reverse resolution map (`_resolve_server`) so `mcp.git_hub.call()` resolves back to target server `git-hub`.
4. **Environment isolation**:
   - Run the sandbox subprocess with `child_env({})` (restricted to safe system vars like PATH, SystemRoot) to prevent secret exfiltration from host environment variables.
   - Reference: AutoHarness child process isolation (https://github.com/D-Shey/autoharness).
5. **Resource cleanup**:
   - In Python `finally` blocks, ensure `proc.stdin.close()`, `proc.stdout.close()`, `proc.stderr.close()` and `proc.wait()` are executed on all exit paths.

## How to verify
1. Test standalone execution without tool calls: `return 1 + 1` yields `2`.
2. Test async MCP dispatch: call a mock tool through `mcp.mock_server.test_tool({ count: 5 })` and verify correct RPC dispatch and argument forwarding.
3. Test hanging async promise: verify that an unresolved Promise times out cleanly without blocking the gateway.
4. Test hyphenated server name resolution: verify that server `git-hub` is callable via `mcp.git_hub`.

## Facts found here
- Standard MCP gateway metadata tools in AutoHarness are defined in `src/autoharness/gateway.py`.
- MCP clients implement `call_tool(tool_name, arguments)` returning standard MCP tool call results.
