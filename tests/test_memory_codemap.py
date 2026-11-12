"""Tests for AutoHarness in-tree SQLite FTS5 persistent memory and AST Code Map."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from autoharness import approvals, codemap, gateway, memory
from autoharness.env import Project


def test_memory_store_and_bm25_search(tmp_path):
    con = memory.get_memory_db(tmp_path)

    # 1. Store memory documents
    id1 = memory.reconcile_and_store(
        con,
        kind="ruling",
        source="rulings.json",
        content="Always verify subprocess exit codes before asserting completion",
        title="Subprocess Exit Code Rule",
        doc_id="ruling_1",
    )
    assert id1 == "ruling_1"

    id2 = memory.reconcile_and_store(
        con,
        kind="lesson",
        source="lessons.md",
        content="Watchdog timers must terminate hanging async promises in Node.js VM isolates",
        title="Node.js Sandbox Isolation",
        doc_id="lesson_1",
    )
    assert id2 == "lesson_1"

    # 2. Test reconcile-on-write deduplication (idempotency)
    id1_again = memory.reconcile_and_store(
        con,
        kind="ruling",
        source="rulings.json",
        content="Always verify subprocess exit codes before asserting completion",
        title="Subprocess Exit Code Rule",
        doc_id="ruling_1",
    )
    assert id1_again == "ruling_1"

    # 3. Test FTS5 BM25 search
    results = memory.search_memory(con, "subprocess exit codes")
    assert len(results) >= 1
    assert results[0]["id"] == "ruling_1"
    assert "Always verify subprocess" in results[0]["content"]

    # Search for isolate / sandbox
    results_sandbox = memory.search_memory(con, "sandbox async promises")
    assert len(results_sandbox) >= 1
    assert results_sandbox[0]["id"] == "lesson_1"

    # Filter by kind
    filtered = memory.search_memory(con, "subprocess", kind="lesson")
    assert len(filtered) == 0

    con.close()


def test_memory_index_project_artifacts(tmp_path):
    state_dir = tmp_path / ".autoharness"
    state_dir.mkdir(parents=True)

    # Fake rulings.json
    (state_dir / "rulings.json").write_text(
        json.dumps([{"text": "Never bypass sandbox boundaries", "author": "critic"}]),
        encoding="utf-8"
    )

    # Fake followups.json
    (state_dir / "followups.json").write_text(
        json.dumps({"items": [{"id": 101, "text": "Fix hanging promises in V8 isolate", "status": "open"}]}),
        encoding="utf-8"
    )

    # Fake task review
    task_dir = state_dir / "tasks" / "task-01" / "answers"
    task_dir.mkdir(parents=True)
    (task_dir / "critic-01.txt").write_text(
        "B1: met - Code Mode sandbox implemented with strict environment cleanup.\nVERDICT: none",
        encoding="utf-8"
    )

    counts = memory.index_project_memory(tmp_path)
    assert counts["rulings"] == 1
    assert counts["followups"] == 1
    assert counts["reviews"] == 1

    con = memory.get_memory_db(tmp_path)
    res = memory.search_memory(con, "hanging promises")
    assert len(res) == 1
    assert res[0]["kind"] == "followup"

    res_review = memory.search_memory(con, "Code Mode sandbox strict")
    assert len(res_review) == 1
    assert res_review[0]["kind"] == "review"
    con.close()


def test_codemap_parse_python_file(tmp_path):
    code = '''"""Sample module docstring for testing."""
import os
from typing import List, Optional

class BaseEngine:
    """Base class for engines."""
    def start(self) -> bool:
        """Start engine."""
        return True

class DataProcessor(BaseEngine):
    """Processes datasets."""
    def __init__(self, name: str, batch_size: int = 100):
        self.name = name

    async def fetch_records(self, limit: int = 10) -> List[dict]:
        """Fetch remote records asynchronously."""
        return []

def standalone_helper(x: int, y: int) -> int:
    """Helper addition function."""
    return x + y
'''
    py_file = tmp_path / "sample.py"
    py_file.write_text(code, encoding="utf-8")

    parsed = codemap.parse_python_file(py_file)
    assert parsed["exists"] is True
    assert parsed["doc"] == "Sample module docstring for testing."
    assert len(parsed["classes"]) == 2

    # Check BaseEngine
    base = parsed["classes"][0]
    assert base["name"] == "BaseEngine"
    assert len(base["methods"]) == 1
    assert base["methods"][0]["name"] == "start"
    assert base["methods"][0]["returns"] == " -> bool"

    # Check DataProcessor
    dp = parsed["classes"][1]
    assert dp["name"] == "DataProcessor"
    assert dp["bases"] == ["BaseEngine"]
    assert len(dp["methods"]) == 2
    async_m = dp["methods"][1]
    assert async_m["name"] == "fetch_records"
    assert async_m["is_async"] is True
    assert "limit: int" in async_m["args"]

    # Check standalone_helper
    assert len(parsed["functions"]) == 1
    fn = parsed["functions"][0]
    assert fn["name"] == "standalone_helper"
    assert fn["returns"] == " -> int"
    assert "x: int, y: int" in fn["args"]


def test_codemap_tree_and_symbol_search(tmp_path):
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "mod_a.py").write_text("class Alpha:\n    def run_alpha(self): pass\n", encoding="utf-8")
    (sub / "mod_b.py").write_text("def execute_pipeline(data: dict) -> None:\n    '''Run the pipeline.'''\n    pass\n", encoding="utf-8")

    cmap = codemap.generate_codemap(tmp_path)
    assert cmap["modules_count"] == 2

    tree = codemap.format_codemap_tree(cmap)
    assert "pkg/mod_a.py" in tree or "pkg\\mod_a.py" in tree
    assert "class Alpha:" in tree
    assert "def run_alpha" in tree
    assert "def execute_pipeline" in tree

    # Search symbol
    matches = codemap.search_codemap(cmap, "execute_pipeline")
    assert len(matches) == 1
    assert matches[0]["name"] == "execute_pipeline"
    assert matches[0]["type"] == "function"
    assert matches[0]["doc"] == "Run the pipeline."

    matches_cls = codemap.search_codemap(cmap, "Alpha")
    assert len(matches_cls) >= 1


def test_gateway_memory_and_codemap_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOHARNESS_HOME", str(tmp_path / "home"))
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    project = Project(proj_dir, "claude")

    # Seed memory
    con = memory.get_memory_db(proj_dir)
    memory.reconcile_and_store(
        con,
        kind="ruling",
        source="test",
        content="Hot-reload MCP servers without agent restart",
        title="Gateway Architecture"
    )
    con.close()

    # Seed Python file
    (proj_dir / "worker.py").write_text("class TaskWorker:\n    def process(self): pass\n", encoding="utf-8")

    gw = gateway.Gateway(proj_dir, lambda msg: None, lambda line: None)

    # 1. Test harness_memory_search via Gateway
    mem_call = gw.handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "harness_memory_search", "arguments": {"query": "Hot-reload"}}
    })
    mem_text = mem_call["result"]["content"][0]["text"]
    assert "Gateway Architecture" in mem_text
    assert "Hot-reload MCP servers" in mem_text

    # 2. Test harness_codemap via Gateway
    cmap_call = gw.handle({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "harness_codemap", "arguments": {"query": "TaskWorker"}}
    })
    cmap_text = cmap_call["result"]["content"][0]["text"]
    assert "TaskWorker" in cmap_text

    # 3. Test harness_codemap tree outline
    tree_call = gw.handle({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "harness_codemap", "arguments": {}}
    })
    tree_text = tree_call["result"]["content"][0]["text"]
    assert "worker.py" in tree_text
    assert "class TaskWorker:" in tree_text

    # 4. Test harness_codemap policy check via Gateway
    (proj_dir / "flawed.py").write_text(
        "class Engine:\n"
        "    def _unused_method(self):\n"
        "        pass\n"
        "    def run(self):\n"
        "        try:\n"
        "            pass\n"
        "        except Exception:\n"
        "            pass\n",
        encoding="utf-8"
    )
    policy_call = gw.handle({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "harness_codemap", "arguments": {"policy": True}}
    })
    policy_res = json.loads(policy_call["result"]["content"][0]["text"])
    assert policy_res["total_violations"] >= 2
    rules = {v["rule"] for v in policy_res["violations"]}
    assert "DEAD_PRIVATE_METHOD" in rules
    assert "SILENCED_EXCEPTION" in rules


def test_ast_policy_dead_private_methods():
    code = """
class OrderService:
    def __init__(self):
        self._used_helper()

    def _used_helper(self):
        return 42

    def _dead_helper(self):
        return 99

    @property
    def _dynamic_prop(self):
        return 1
"""
    violations = codemap.check_ast_policies(code, filename="test.py")
    rules = [v["rule"] for v in violations]
    symbols = [v["symbol"] for v in violations]
    assert "DEAD_PRIVATE_METHOD" in rules
    assert "OrderService._dead_helper" in symbols
    assert "OrderService._used_helper" not in symbols
    assert "OrderService._dynamic_prop" not in symbols


def test_ast_policy_silenced_exceptions():
    code = """
def process():
    try:
        do_work()
    except Exception:
        pass

    try:
        do_work()
    except BaseException:
        '''ignore'''
        continue

    try:
        do_work()
    except (ValueError, Exception):
        pass

    try:
        do_work()
    except ValueError:
        # specific exception allowed
        pass

    try:
        do_work()
    except Exception as e:
        log_error(e)
        raise
"""
    violations = codemap.check_ast_policies(code, filename="test.py")
    silenced = [v for v in violations if v["rule"] == "SILENCED_EXCEPTION"]
    assert len(silenced) == 3
    # Check lines: lines 5, 11, 17 are flagged, but ValueError and log_error+raise are not
    lines = [v["line"] for v in silenced]
    assert len(lines) == 3


def test_ast_policy_unbounded_open(tmp_path):
    code = """
def bad_reader():
    f = open('data.txt')
    return f.read()

def good_reader():
    with open('data.txt') as f:
        return f.read()

def safe_finally():
    f = open('data.txt')
    try:
        return f.read()
    finally:
        f.close()
"""
    violations = codemap.check_ast_policies(code, filename="test.py")
    open_violations = [v for v in violations if v["rule"] == "UNBOUNDED_RESOURCE_OPEN"]
    assert len(open_violations) == 1
    assert open_violations[0]["line"] == 3


def test_codemap_parse_javascript_file(tmp_path):
    if codemap.get_tree_sitter_parser("javascript") is None:
        pytest.skip("tree-sitter-javascript unavailable")
    js_file = tmp_path / "service.js"
    js_file.write_text("""import express from "express";
const { helper } = require("./utils");

/**
 * Main Controller service
 */
class ApiController extends BaseController {
    constructor(db) { this.db = db; }
    async handleRequest(req, res) { return res.json({}); }
}

function computeStats(items) { return items.length; }
const transform = async (data) => data.toUpperCase();
""", encoding="utf-8")

    parsed = codemap.parse_source_file(js_file)
    assert parsed["exists"] is True
    assert len(parsed["classes"]) == 1
    cls = parsed["classes"][0]
    assert cls["name"] == "ApiController"
    assert "BaseController" in cls["bases"]
    assert "Main Controller service" in cls["doc"]
    assert len(cls["methods"]) == 2
    method_names = [m["name"] for m in cls["methods"]]
    assert "constructor" in method_names and "handleRequest" in method_names
    handle_m = next(m for m in cls["methods"] if m["name"] == "handleRequest")
    assert handle_m["is_async"] is True

    fn_names = [f["name"] for f in parsed["functions"]]
    assert "computeStats" in fn_names and "transform" in fn_names
    transform_fn = next(f for f in parsed["functions"] if f["name"] == "transform")
    assert transform_fn["is_async"] is True


def test_codemap_parse_typescript_and_tsx_file(tmp_path):
    if codemap.get_tree_sitter_parser("typescript") is None:
        pytest.skip("tree-sitter-typescript unavailable")
    ts_file = tmp_path / "user.ts"
    ts_file.write_text("""import { Observable } from "rxjs";

export interface IUser {
    getUser(id: string): Promise<UserDTO>;
}

/**
 * User manager handles user authentication and lookup.
 */
export class UserManager implements IUser {
    private repo: Repo;
    async getUser(id: string): Promise<UserDTO> { return null; }
}

/**
 * Hashes raw user password with salt.
 */
export const hashPassword = (raw: string): string => raw;
""", encoding="utf-8")

    parsed = codemap.parse_source_file(ts_file)
    assert parsed["exists"] is True
    class_names = [c["name"] for c in parsed["classes"]]
    assert "IUser" in class_names and "UserManager" in class_names

    user_mgr = next(c for c in parsed["classes"] if c["name"] == "UserManager")
    assert "IUser" in user_mgr["bases"]
    assert "User manager handles user authentication" in user_mgr["doc"]
    assert len(user_mgr["methods"]) == 1
    m = user_mgr["methods"][0]
    assert m["name"] == "getUser"
    assert "Promise<UserDTO>" in m["returns"]
    assert m["is_async"] is True

    fn = next(f for f in parsed["functions"] if f["name"] == "hashPassword")
    assert "string" in fn["returns"]
    assert "Hashes raw user password with salt" in fn["doc"]

    # Test TSX
    tsx_file = tmp_path / "component.tsx"
    tsx_file.write_text("""export const Header = (props: Props) => <div className="head">{props.title}</div>;""", encoding="utf-8")
    parsed_tsx = codemap.parse_source_file(tsx_file)
    assert any(f["name"] == "Header" for f in parsed_tsx["functions"])


def test_codemap_parse_java_file(tmp_path):
    if codemap.get_tree_sitter_parser("java") is None:
        pytest.skip("tree-sitter-java unavailable")
    java_file = tmp_path / "OrderService.java"
    java_file.write_text("""package com.example.orders;
import java.util.List;
import org.springframework.stereotype.Service;

@Service
public class OrderService extends BaseService implements IOrderService {
    public OrderService() {}

    @Transactional
    public List<Order> getActiveOrders(Long userId) { return List.of(); }
}

public interface IOrderService {
    List<Order> getActiveOrders(Long userId);
}
""", encoding="utf-8")

    parsed = codemap.parse_source_file(java_file)
    assert parsed["exists"] is True
    assert "package com.example.orders;" in parsed["imports"]
    class_names = [c["name"] for c in parsed["classes"]]
    assert "OrderService" in class_names and "IOrderService" in class_names

    order_svc = next(c for c in parsed["classes"] if c["name"] == "OrderService")
    assert "BaseService" in order_svc["bases"]
    assert "IOrderService" in order_svc["bases"]
    assert any("@Service" in d for d in order_svc["decorators"])

    method_names = [m["name"] for m in order_svc["methods"]]
    assert "OrderService" in method_names and "getActiveOrders" in method_names
    get_orders = next(m for m in order_svc["methods"] if m["name"] == "getActiveOrders")
    assert "List<Order>" in get_orders["returns"]
    assert "Long userId" in get_orders["args"]
    assert any("@Transactional" in d for d in get_orders["decorators"])


def test_codemap_tree_renders_error_modules():
    fake_cmap = {
        "modules": {
            "broken.js": {"error": "Tree-sitter parser for javascript unavailable"}
        }
    }
    tree = codemap.format_codemap_tree(fake_cmap)
    assert "[ERROR: Tree-sitter parser for javascript unavailable]" in tree


def test_codemap_graceful_fallback_when_parser_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(codemap, "get_tree_sitter_parser", lambda lang: None)
    js_file = tmp_path / "index.js"
    js_file.write_text("class Server {};", encoding="utf-8")
    parsed = codemap.parse_source_file(js_file)
    assert parsed["exists"] is True
    assert "unavailable" in parsed.get("error", "")
    assert parsed["classes"] == []


def test_codemap_excludes_node_modules_and_venv(tmp_path):
    repo = tmp_path / "test_repo"
    repo.mkdir()
    (repo / "index.ts").write_text("export const main = () => {};", encoding="utf-8")
    nm = repo / "node_modules" / "some-lib"
    nm.mkdir(parents=True)
    (nm / "lib.js").write_text("module.exports = {};", encoding="utf-8")
    venv = repo / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "site.py").write_text("x = 1", encoding="utf-8")

    cmap = codemap.generate_codemap(repo)
    assert cmap["modules_count"] == 1
    assert "index.ts" in cmap["modules"]
    assert not any("node_modules" in k for k in cmap["modules"])
    assert not any(".venv" in k for k in cmap["modules"])


def test_codemap_parsing_benchmark(tmp_path):
    if codemap.get_tree_sitter_parser("javascript") is None:
        pytest.skip("tree-sitter unavailable")
    import time
    repo = tmp_path / "bench_repo"
    repo.mkdir()
    for i in range(15):
        (repo / f"mod_{i}.py").write_text(
            f"class Worker{i}:\n    def do_work(self, item: int) -> bool:\n        return True\n",
            encoding="utf-8"
        )
        (repo / f"service_{i}.ts").write_text(
            f"export class Service{i} {{\n    process(data: string): boolean {{ return true; }}\n}}\n",
            encoding="utf-8"
        )
        (repo / f"handler_{i}.js").write_text(
            f"function handle{i}(req, res) {{ return req.id; }}\nconst fn{i} = () => 42;\n",
            encoding="utf-8"
        )
        (repo / f"Entity{i}.java").write_text(
            f"public class Entity{i} {{\n    public int getId() {{ return {i}; }}\n}}\n",
            encoding="utf-8"
        )

    t0 = time.perf_counter()
    cmap = codemap.generate_codemap(repo)
    elapsed = time.perf_counter() - t0

    assert cmap["modules_count"] == 60
    assert elapsed < 2.0
    matches = codemap.search_codemap(cmap, "Worker10")
    assert len(matches) == 1
    assert matches[0]["name"] == "Worker10"


def test_codemap_polyglot_repository_scan_and_tree(tmp_path):
    if any(codemap.get_tree_sitter_parser(l) is None for l in ("javascript", "typescript", "java")):
        pytest.skip("tree-sitter grammars unavailable")
    repo = tmp_path / "polyglot_repo"
    repo.mkdir()
    (repo / "app.py").write_text("class PyApp:\n    def start(self): pass\n", encoding="utf-8")
    (repo / "client.ts").write_text("export class TsClient { fetch() {} }\n", encoding="utf-8")
    (repo / "worker.js").write_text("const work = () => {};\n", encoding="utf-8")
    (repo / "Server.java").write_text("public class Server { public void listen() {} }\n", encoding="utf-8")

    cmap = codemap.generate_codemap(repo)
    assert cmap["modules_count"] == 4
    assert any(k.endswith("app.py") for k in cmap["modules"])
    assert any(k.endswith("client.ts") for k in cmap["modules"])
    assert any(k.endswith("worker.js") for k in cmap["modules"])
    assert any(k.endswith("Server.java") for k in cmap["modules"])

    tree = codemap.format_codemap_tree(cmap)
    assert "PyApp" in tree
    assert "TsClient" in tree
    assert "Server" in tree
    assert "work" in tree

    # Symbol search across polyglot files
    matches_server = codemap.search_codemap(cmap, "Server")
    assert any(m["name"] == "Server" and m["file"].endswith("Server.java") for m in matches_server)

    matches_ts = codemap.search_codemap(cmap, "TsClient")
    assert any(m["name"] == "TsClient" and m["file"].endswith("client.ts") for m in matches_ts)


