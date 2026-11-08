"""Code Mode sandbox runner for AutoHarness.

Executes JavaScript/TypeScript code within an isolated Node.js VM context,
bridging tool calls back to AutoHarness Gateway via stdio JSON-RPC.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from autoharness.mcp_client import child_env

DEFAULT_TIMEOUT = 30.0

RUNNER_SCRIPT = r"""
const readline = require('node:readline');
const vm = require('node:vm');

const pending = new Map();
let nextId = 1;
const logs = [];

const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    terminal: false
});

let initialized = false;
let watchdogTimer = null;

rl.on('line', (line) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    if (!initialized) {
        initialized = true;
        initAndRun(trimmed);
        return;
    }
    handleRpc(trimmed);
});

function handleRpc(line) {
    try {
        const msg = JSON.parse(line);
        if (msg.id && pending.has(msg.id)) {
            const { resolve, reject } = pending.get(msg.id);
            pending.delete(msg.id);
            if (msg.error) {
                reject(new Error(msg.error.message || JSON.stringify(msg.error)));
            } else {
                resolve(msg.result);
            }
        }
    } catch (e) {
        // ignore JSON parse error
    }
}

function callMcpTool(server, tool, args) {
    return new Promise((resolve, reject) => {
        const id = nextId++;
        pending.set(id, { resolve, reject });
        const req = {
            jsonrpc: "2.0",
            id: id,
            method: "call_tool",
            params: { server, tool, arguments: args || {} }
        };
        process.stdout.write(JSON.stringify(req) + "\n");
    });
}

const mcpProxy = new Proxy({}, {
    get(target, server) {
        if (typeof server !== 'string') return undefined;
        return new Proxy({}, {
            get(subTarget, tool) {
                if (typeof tool !== 'string') return undefined;
                return (args) => callMcpTool(server, tool, args);
            }
        });
    }
});

const sandboxGlobals = {
    mcp: mcpProxy,
    console: {
        log: (...args) => logs.push(args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ')),
        error: (...args) => logs.push("[ERROR] " + args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ')),
        warn: (...args) => logs.push("[WARN] " + args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' '))
    },
    JSON,
    Math,
    Date,
    Array,
    Object,
    String,
    Number,
    Boolean,
    RegExp,
    Map,
    Set,
    Promise,
    parseInt,
    parseFloat,
    isNaN,
    isFinite,
    encodeURI,
    decodeURI,
    encodeURIComponent,
    decodeURIComponent
};

function initAndRun(payloadStr) {
    let payload;
    try {
        payload = JSON.parse(payloadStr);
    } catch (err) {
        process.stdout.write(JSON.stringify({ jsonrpc: "2.0", method: "error", error: "Invalid initialization payload: " + err.message, logs: [] }) + "\n");
        process.exit(1);
    }

    const userScript = payload.code || "";
    const scriptTimeoutMs = Math.max(1000, (payload.timeout || 30) * 1000);

    // Watchdog for asynchronous promise hang
    watchdogTimer = setTimeout(() => {
        process.stdout.write(JSON.stringify({
            jsonrpc: "2.0",
            method: "error",
            error: `Async execution timed out after ${scriptTimeoutMs / 1000} seconds`,
            logs: logs
        }) + "\n");
        process.exit(1);
    }, scriptTimeoutMs);
    if (watchdogTimer.unref) {
        watchdogTimer.unref();
    }

    const context = vm.createContext(sandboxGlobals);

    // Normalize user code: wrap bare single expression with return if needed
    let wrappedCode = userScript.trim();
    if (!wrappedCode.includes("return ") && !wrappedCode.startsWith("return")) {
        if (!wrappedCode.includes(";") && !wrappedCode.includes("\n")) {
            wrappedCode = "return (" + wrappedCode + ");";
        }
    }

    const fullScript = `(async () => {\n${wrappedCode}\n})()`;

    try {
        const promise = vm.runInContext(fullScript, context, {
            timeout: scriptTimeoutMs,
            displayErrors: true
        });

        Promise.resolve(promise).then((result) => {
            if (watchdogTimer) clearTimeout(watchdogTimer);
            const resp = {
                jsonrpc: "2.0",
                method: "completed",
                result: result !== undefined ? result : null,
                logs: logs
            };
            process.stdout.write(JSON.stringify(resp) + "\n");
            process.exit(0);
        }).catch((err) => {
            if (watchdogTimer) clearTimeout(watchdogTimer);
            const resp = {
                jsonrpc: "2.0",
                method: "error",
                error: err && err.message ? err.message : String(err),
                logs: logs
            };
            process.stdout.write(JSON.stringify(resp) + "\n");
            process.exit(1);
        });
    } catch (err) {
        if (watchdogTimer) clearTimeout(watchdogTimer);
        const resp = {
            jsonrpc: "2.0",
            method: "error",
            error: err && err.message ? err.message : String(err),
            logs: logs
        };
        process.stdout.write(JSON.stringify(resp) + "\n");
        process.exit(1);
    }
}
"""


class SandboxError(Exception):
    """Raised when sandbox execution fails."""


def execute(
    code: str,
    forward_fn: Callable[[str, str, dict], dict],
    timeout: float = DEFAULT_TIMEOUT,
    node_binary: str | None = None,
) -> dict[str, Any]:
    """Execute a JavaScript snippet in the Node.js sandbox.

    Args:
        code: JavaScript/TypeScript code string.
        forward_fn: Callback function `forward(server, tool, args) -> dict` implementing tool calls.
        timeout: Execution timeout in seconds.
        node_binary: Custom path to Node.js executable.

    Returns:
        Dict with execution results: `{"result": Any, "logs": list[str]}` or error structure.
    """
    node = node_binary or shutil.which("node")
    if not node:
        return {
            "isError": True,
            "error": "Node.js is required for Code Mode execution but was not found on PATH.",
        }

    clean_env = child_env({})

    cmd = [node, "-e", RUNNER_SCRIPT]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=clean_env,
            text=True,
            encoding="utf-8",
        )
    except OSError as e:
        return {"isError": True, "error": f"Failed to start sandbox runner: {e}"}

    # Python watchdog to guarantee proc is killed if stdin/stdout hang
    watchdog = threading.Timer(timeout + 2.0, proc.kill)
    watchdog.daemon = True
    watchdog.start()

    final_result: dict[str, Any] | None = None

    try:
        # Send initialization payload on the first line of stdin
        init_payload = json.dumps({"code": code, "timeout": timeout}, ensure_ascii=False)
        if proc.stdin:
            proc.stdin.write(init_payload + "\n")
            proc.stdin.flush()

        while True:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.01)
                continue

            trimmed = line.strip()
            if not trimmed:
                continue

            try:
                msg = json.loads(trimmed)
            except ValueError:
                continue

            method = msg.get("method")
            if method == "call_tool":
                req_id = msg.get("id")
                params = msg.get("params") or {}
                server = params.get("server")
                tool = params.get("tool")
                args = params.get("arguments") or {}

                # Execute forward call in Gateway
                try:
                    tool_res = forward_fn(server, tool, args)
                    resp_payload = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": tool_res,
                    }
                except Exception as e:  # noqa: BLE001
                    resp_payload = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32000, "message": str(e)},
                    }

                if proc.stdin:
                    proc.stdin.write(json.dumps(resp_payload, ensure_ascii=False) + "\n")
                    proc.stdin.flush()

            elif method == "completed":
                final_result = {
                    "isError": False,
                    "result": msg.get("result"),
                    "logs": msg.get("logs", []),
                }
                break

            elif method == "error":
                final_result = {
                    "isError": True,
                    "error": msg.get("error", "Unknown script error"),
                    "logs": msg.get("logs", []),
                }
                break

        proc.wait(timeout=2.0)
    except Exception as e:  # noqa: BLE001
        proc.kill()
        return {"isError": True, "error": f"Sandbox execution error: {e}"}
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
        try:
            if proc.stdin:
                proc.stdin.close()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        except Exception:  # noqa: BLE001
            pass
        proc.wait()

    if final_result is None:
        return {
            "isError": True,
            "error": f"Execution timed out or runner exited unexpectedly after {timeout} seconds",
        }

    return final_result
