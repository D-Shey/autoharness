"""Tests for AutoHarness Code Mode: TypeScript code generation and isolated sandbox execution."""

import json
import sys
import time
from pathlib import Path
import pytest
from autoharness import approvals, codegen, gateway, mcp_client, sandbox
from autoharness.env import Project


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOHARNESS_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    root.mkdir()
    return Project(root, "claude")


def test_codegen_primitive_and_object():
    schema = {
        "type": "object",
        "description": "User profile schema",
        "properties": {
            "name": {"type": "string", "description": "The user's full name"},
            "age": {"type": "integer"},
            "is_active": {"type": "boolean"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "role": {"type": "string", "enum": ["admin", "editor", "viewer"]},
        },
        "required": ["name", "role"],
    }
    ts = codegen.json_schema_to_ts(schema)
    assert "name: string;" in ts
    assert 'role: "admin" | "editor" | "viewer";' in ts
    assert "age?: number;" in ts
    assert "is_active?: boolean;" in ts
    assert "tags?: string[];" in ts
    assert "/** The user's full name */" in ts


def test_codegen_generate_and_search_definitions():
    tools_by_server = {
        "github": [
            {
                "name": "list_issues",
                "description": "List issues for a repository",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository in owner/repo format"}
                    },
                    "required": ["repo"]
                }
            },
            {
                "name": "get_issue",
                "description": "Fetch a specific issue",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "issue_number": {"type": "number"}
                    },
                    "required": ["issue_number"]
                }
            }
        ],
        "database": [
            {
                "name": "query",
                "description": "Run a SQL query",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "sql": {"type": "string"}
                    },
                    "required": ["sql"]
                }
            }
        ]
    }

    full_defs = codegen.generate_definitions(tools_by_server)
    assert "export interface GithubTools" in full_defs
    assert "export interface DatabaseTools" in full_defs
    assert "list_issues(args: {" in full_defs
    assert "get_issue(args: {" in full_defs
    assert "query(args: {" in full_defs
    assert "const mcp: McpRegistry;" in full_defs

    # Search for "sql"
    sql_defs = codegen.search_definitions("sql", tools_by_server)
    assert "DatabaseTools" in sql_defs
    assert "GithubTools" not in sql_defs

    # Search for "github"
    gh_defs = codegen.search_definitions("github", tools_by_server)
    assert "GithubTools" in gh_defs
    assert "DatabaseTools" not in gh_defs

    # Search for non-existent
    empty_defs = codegen.search_definitions("nonexistent", tools_by_server)
    assert "No MCP tools matching" in empty_defs


def test_sandbox_simple_eval():
    res = sandbox.execute("return 40 + 2;", forward_fn=lambda s, t, a: {})
    assert res.get("isError") is False
    assert res.get("result") == 42


def test_sandbox_async_and_logs():
    code = """
    console.log("step 1");
    const val = await Promise.resolve("async success");
    console.log("step 2", val);
    return val;
    """
    res = sandbox.execute(code, forward_fn=lambda s, t, a: {})
    assert res.get("isError") is False
    assert res.get("result") == "async success"
    assert "step 1" in res.get("logs", [])
    assert "step 2 async success" in res.get("logs", [])


def test_sandbox_mcp_forwarding():
    calls = []

    def mock_forward(server: str, tool: str, args: dict) -> dict:
        calls.append((server, tool, args))
        if tool == "echo":
            return {"echoed": args.get("msg")}
        if tool == "add":
            return {"sum": args.get("a", 0) + args.get("b", 0)}
        return {"error": "unknown tool"}

    code = """
    const res1 = await mcp.test_server.echo({ msg: "hello code mode" });
    const res2 = await mcp.test_server.add({ a: 15, b: 25 });
    return {
        echo: res1.echoed,
        sum: res2.sum
    };
    """
    res = sandbox.execute(code, forward_fn=mock_forward)
    assert res.get("isError") is False
    assert res.get("result") == {"echo": "hello code mode", "sum": 40}
    assert len(calls) == 2
    assert calls[0] == ("test_server", "echo", {"msg": "hello code mode"})
    assert calls[1] == ("test_server", "add", {"a": 15, "b": 25})


def test_sandbox_isolation_and_error():
    # Attempting to access process or require should fail or be undefined
    code = """
    if (typeof process !== 'undefined') {
        throw new Error('process is defined!');
    }
    if (typeof require !== 'undefined') {
        throw new Error('require is defined!');
    }
    return "isolated";
    """
    res = sandbox.execute(code, forward_fn=lambda s, t, a: {})
    assert res.get("isError") is False
    assert res.get("result") == "isolated"

    # Syntax error handling
    res_err = sandbox.execute("return 1 +++ ;", forward_fn=lambda s, t, a: {})
    assert res_err.get("isError") is True
    assert "error" in res_err


def test_gateway_codemode_integration(project):
    fake_path = Path(__file__).parent / "fake_mcp_server.py"
    fake_cfg = {"command": sys.executable, "args": [str(fake_path)]}
    for name, cfg in {"fake": fake_cfg}.items():
        approvals.approve(project.root, name, cfg)
    project.save_gateway_registry({"installed": ["claude"], "servers": {"fake": fake_cfg}})

    sent = []
    gw = gateway.Gateway(project.root, sent.append, lambda line: None)
    gw.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    gw.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert gw.sync()

    # Wait until fake server is running
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and gw.status.get("fake", {}).get("status") != "running":
        time.sleep(0.1)
    assert gw.status.get("fake", {}).get("status") == "running"

    def rpc(method, params=None, rid=1):
        return gw.handle({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})

    def call(name, args=None):
        return rpc("tools/call", {"name": name, "arguments": args or {}})["result"]

    # 1. Test harness_search
    search_res = call("harness_search", {"query": "echo"})
    search_text = search_res["content"][0]["text"]
    assert "FakeTools" in search_text
    assert "echo" in search_text

    # 2. Test harness_execute single call
    exec_code = """
    const res = await mcp.fake.echo({ text: "hello from code mode" });
    return res.content[0].text;
    """
    exec_res = call("harness_execute", {"code": exec_code})
    assert not exec_res.get("isError")
    assert exec_res["content"][0]["text"] == "hello from code mode"

    # 3. Test harness_execute batch multi-step call with filtering
    batch_code = """
    const items = ["alpha", "beta", "gamma"];
    const results = [];
    for (const item of items) {
        const resp = await mcp.fake.echo({ text: "processed-" + item });
        results.push(resp.content[0].text);
    }
    return results.filter(r => r.includes("a"));
    """
    batch_res = call("harness_execute", {"code": batch_code})
    assert not batch_res.get("isError")
    parsed = json.loads(batch_res["content"][0]["text"])
    assert parsed == ["processed-alpha", "processed-beta", "processed-gamma"]


def test_sandbox_async_timeout_hang():
    # An unresolved promise should be aborted by the watchdog timer
    code = "return new Promise(() => {});"
    res = sandbox.execute(code, forward_fn=lambda s, t, a: {}, timeout=1.0)
    assert res.get("isError") is True
    assert "timed out" in res.get("error", "").lower()


def test_gateway_hyphenated_server_resolution(project):
    fake_path = Path(__file__).parent / "fake_mcp_server.py"
    fake_cfg = {"command": sys.executable, "args": [str(fake_path)]}
    for name, cfg in {"fake-hyphen": fake_cfg}.items():
        approvals.approve(project.root, name, cfg)
    project.save_gateway_registry({"installed": ["claude"], "servers": {"fake-hyphen": fake_cfg}})

    sent = []
    gw = gateway.Gateway(project.root, sent.append, lambda line: None)
    gw.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    gw.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert gw.sync()

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and gw.status.get("fake-hyphen", {}).get("status") != "running":
        time.sleep(0.1)
    assert gw.status.get("fake-hyphen", {}).get("status") == "running"

    def rpc(method, params=None, rid=1):
        return gw.handle({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})

    def call(name, args=None):
        return rpc("tools/call", {"name": name, "arguments": args or {}})["result"]

    exec_code = """
    const res = await mcp.fake_hyphen.echo({ text: "hyphen resolved" });
    return res.content[0].text;
    """
    exec_res = call("harness_execute", {"code": exec_code})
    assert not exec_res.get("isError")
    assert exec_res["content"][0]["text"] == "hyphen resolved"


def test_codegen_recursion_depth():
    cyclic_schema = {"type": "object", "properties": {}}
    cyclic_schema["properties"]["self"] = cyclic_schema
    ts = codegen.json_schema_to_ts(cyclic_schema)
    assert "Record<string, any>" in ts or "self?:" in ts


