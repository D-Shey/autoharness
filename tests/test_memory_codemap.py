"""Tests for AutoHarness in-tree SQLite FTS5 persistent memory and AST Code Map."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from autoharness import approvals, cli, codemap, gateway, memory
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


def test_arch_dependency_graph_and_cycles(tmp_path):
    repo = tmp_path / "cyclic_repo"
    repo.mkdir()
    (repo / "alpha.py").write_text("import beta\nclass Alpha: pass\n", encoding="utf-8")
    (repo / "beta.py").write_text("import gamma\nclass Beta: pass\n", encoding="utf-8")
    (repo / "gamma.py").write_text("import alpha\nclass Gamma: pass\n", encoding="utf-8")
    (repo / "independent.py").write_text("x = 42\n", encoding="utf-8")

    cmap = codemap.generate_codemap(repo)
    dep_graph = codemap.build_dependency_graph(cmap)

    assert len(dep_graph["modules"]) == 4
    assert len(dep_graph["edges"]) == 3
    assert "alpha.py" in dep_graph["adjacency"]

    cycles = codemap.detect_architecture_cycles(dep_graph)
    assert len(cycles) == 1
    cycle = cycles[0]
    assert sorted(cycle) == ["alpha.py", "beta.py", "gamma.py"]


def test_arch_boundary_violations():
    fake_graph = {
        "modules": ["src/autoharness/evidence.py", "src/autoharness/cli.py", "src/autoharness/core.py", "tests/test_cli.py"],
        "edges": [
            {"source": "src/autoharness/evidence.py", "target": "src/autoharness/cli.py"},
            {"source": "src/autoharness/core.py", "target": "tests/test_cli.py"},
        ],
        "adjacency": {
            "src/autoharness/evidence.py": ["src/autoharness/cli.py"],
            "src/autoharness/core.py": ["tests/test_cli.py"],
        }
    }
    violations = codemap.detect_boundary_violations(fake_graph)
    assert len(violations) == 2
    rules = {v["rule"] for v in violations}
    assert "CORE_CANNOT_IMPORT_PRESENTATION" in rules
    assert "PROD_CANNOT_IMPORT_TESTS" in rules


def test_arch_diff_calculation():
    base_graph = {
        "modules": ["a.py", "b.py"],
        "edges": [{"source": "a.py", "target": "b.py"}],
        "adjacency": {"a.py": ["b.py"], "b.py": []}
    }
    head_graph = {
        "modules": ["a.py", "b.py", "c.py"],
        "edges": [
            {"source": "a.py", "target": "b.py"},
            {"source": "b.py", "target": "a.py"},
            {"source": "b.py", "target": "c.py"},
        ],
        "adjacency": {
            "a.py": ["b.py"],
            "b.py": ["a.py", "c.py"],
            "c.py": []
        }
    }

    diff = codemap.architecture_diff(base_graph, head_graph)
    assert diff["verdict"] == "violation"
    assert diff["added_modules"] == ["c.py"]
    assert len(diff["new_cycles"]) == 1
    assert sorted(diff["new_cycles"][0]) == ["a.py", "b.py"]

    # Now verify clean diff when cycle is removed
    clean_head = {
        "modules": ["a.py", "b.py", "c.py"],
        "edges": [
            {"source": "a.py", "target": "b.py"},
            {"source": "b.py", "target": "c.py"},
        ],
        "adjacency": {
            "a.py": ["b.py"],
            "b.py": ["c.py"],
            "c.py": []
        }
    }
    clean_diff = codemap.architecture_diff(base_graph, clean_head)
    assert clean_diff["verdict"] == "clean"
    assert clean_diff["new_cycles"] == []
    assert clean_diff["new_violations"] == []


def test_arch_report_formatting():
    arch_data = {
        "modules_count": 2,
        "edges_count": 1,
        "cycles": [],
        "violations": [],
        "dep_graph": {
            "modules": ["mod_a.py", "mod_b.py"],
            "edges": [{"source": "mod_a.py", "target": "mod_b.py"}],
            "in_degree": {"mod_b.py": 1, "mod_a.py": 0}
        }
    }
    report = codemap.format_architecture_report(arch_data)
    assert "# Architecture Dependency Graph (2 modules, 1 dependencies)" in report
    assert "Cycles Detected: 0" in report
    assert "None detected" in report
    assert "`mod_b.py`: 1 incoming references" in report


def test_cli_codemap_arch(tmp_path, capsys):
    from autoharness import cli
    repo = tmp_path / "cli_arch_repo"
    repo.mkdir()
    (repo / "m1.py").write_text("import m2\n", encoding="utf-8")
    (repo / "m2.py").write_text("import m1\n", encoding="utf-8")

    class FakeArgs:
        path = str(repo)
        arch = True
        json = False
        fail_on_cycle = False
        query = None

    class FakeProject:
        root = Path(tmp_path)

    # 1. Run without fail_on_cycle (should return 0)
    ret = cli.cmd_codemap(FakeArgs(), FakeProject(), None)
    assert ret == 0
    captured = capsys.readouterr().out
    assert "# Architecture Dependency Graph" in captured
    assert "Cycles Detected: 1" in captured

    # 2. Run with fail_on_cycle (should return 1 due to m1 <-> m2 cycle)
    FakeArgs.fail_on_cycle = True
    ret_fail = cli.cmd_codemap(FakeArgs(), FakeProject(), None)
    assert ret_fail == 1


def test_gateway_codemap_arch(tmp_path):
    repo = tmp_path / "gw_repo"
    repo.mkdir()
    (repo / "worker.py").write_text("x = 1\n", encoding="utf-8")

    gw = gateway.Gateway(repo, write=lambda s: None, log=lambda s: None)
    # Test JSON output
    res_json = gw._call({
        "name": "harness_codemap",
        "arguments": {"arch": True, "raw_json": True}
    })
    data = json.loads(res_json["content"][0]["text"])
    assert data["modules_count"] == 1
    assert data["cycles"] == []
    assert data["violations"] == []

    # Test report text output
    res_report = gw._call({
        "name": "harness_codemap",
        "arguments": {"arch": True, "raw_json": False}
    })
    assert "# Architecture Dependency Graph" in res_report["content"][0]["text"]


def test_call_graph_and_impact_analysis(tmp_path):
    repo = tmp_path / "impact_repo"
    repo.mkdir()

    (repo / "core.py").write_text(
        "def low_level_calc():\n"
        "    return 42\n"
        "\n"
        "class Engine:\n"
        "    def _internal(self):\n"
        "        return 1\n"
        "    def compute(self):\n"
        "        self._internal()\n"
        "        return low_level_calc()\n",
        encoding="utf-8"
    )

    (repo / "service.py").write_text(
        "from core import low_level_calc, Engine\n"
        "\n"
        "def execute_job():\n"
        "    eng = Engine()\n"
        "    eng.compute()\n"
        "    return low_level_calc()\n",
        encoding="utf-8"
    )

    (repo / "test_service.py").write_text(
        "from service import execute_job\n"
        "\n"
        "def test_execution():\n"
        "    assert execute_job() == 42\n",
        encoding="utf-8"
    )

    cmap = codemap.generate_codemap(repo)
    cgraph = codemap.build_call_graph(cmap)

    assert cgraph["nodes_count"] >= 5
    assert cgraph["edges_count"] >= 4

    # Direct callers of low_level_calc
    callers = codemap.find_callers(cgraph, "low_level_calc")
    caller_names = {c["name"] for c in callers}
    assert "Engine.compute" in caller_names
    assert "execute_job" in caller_names

    # Direct callees of compute
    callees = codemap.find_callees(cgraph, "compute")
    callee_names = {c["name"] for c in callees}
    assert "Engine._internal" in callee_names
    assert "low_level_calc" in callee_names

    # Impact analysis of low_level_calc
    impact = codemap.analyze_impact(cgraph, "low_level_calc", max_depth=5)
    assert impact["impact_radius"] >= 3
    impacted_names = {s["name"] for s in impact["impacted_symbols"]}
    assert "Engine.compute" in impacted_names
    assert "execute_job" in impacted_names
    assert "test_execution" in impacted_names

    # Check impacted tests
    test_names = {t["name"] for t in impact["impacted_tests"]}
    assert "test_execution" in test_names

    # Check format report
    report = codemap.format_impact_report(impact)
    assert "# Symbol Impact Analysis for" in report
    assert "Direct Callers:" in report
    assert "Direct Callees:" in report
    assert "Transitive Impact Radius:" in report
    assert "Impacted Tests" in report
    assert "test_execution" in report


def test_cli_codemap_impact_and_callers(tmp_path, capsys):
    repo = tmp_path / "cli_repo"
    repo.mkdir()
    (repo / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "def run():\n"
        "    return add(1, 2)\n",
        encoding="utf-8"
    )

    class FakeArgs:
        path = "."
        arch = False
        query = None
        json = False
        impact = "add"
        callers = None

    class FakeProject:
        root = repo

    # 1. Test CLI --impact report
    ret = cli.cmd_codemap(FakeArgs(), FakeProject(), None)
    assert ret == 0
    out = capsys.readouterr().out
    assert "# Symbol Impact Analysis for" in out
    assert "run" in out

    # 2. Test CLI --impact --json
    FakeArgs.json = True
    ret_json = cli.cmd_codemap(FakeArgs(), FakeProject(), None)
    assert ret_json == 0
    out_json = capsys.readouterr().out
    data = json.loads(out_json)
    assert data["status"] == "ok"
    assert data["impact"]["impact_radius"] == 1

    # 3. Test CLI --callers
    FakeArgs.impact = None
    FakeArgs.callers = "add"
    FakeArgs.json = False
    ret_callers = cli.cmd_codemap(FakeArgs(), FakeProject(), None)
    assert ret_callers == 0
    out_callers = capsys.readouterr().out
    c_data = json.loads(out_callers)
    assert c_data["status"] == "ok"
    assert c_data["callers_count"] == 1
    assert c_data["callers"][0]["name"] == "run"


def test_gateway_codemap_impact_and_callers(tmp_path):
    repo = tmp_path / "gw_impact_repo"
    repo.mkdir()
    (repo / "math_util.py").write_text(
        "def square(x):\n"
        "    return x * x\n"
        "def sum_squares(a, b):\n"
        "    return square(a) + square(b)\n",
        encoding="utf-8"
    )

    gw = gateway.Gateway(repo, write=lambda s: None, log=lambda s: None)

    # 1. Call gateway with impact markdown report
    res = gw._call({
        "name": "harness_codemap",
        "arguments": {"impact": "square"}
    })
    text = res["content"][0]["text"]
    assert "# Symbol Impact Analysis for" in text
    assert "sum_squares" in text

    # 2. Call gateway with impact raw_json
    res_json = gw._call({
        "name": "harness_codemap",
        "arguments": {"impact": "square", "raw_json": True}
    })
    data = json.loads(res_json["content"][0]["text"])
    assert data["impact_radius"] == 1
    assert data["target"]["name"] == "square"

    # 3. Call gateway with callers
    res_callers = gw._call({
        "name": "harness_codemap",
        "arguments": {"callers": "square"}
    })
    c_data = json.loads(res_callers["content"][0]["text"])
    assert c_data["callers_count"] == 1
    assert c_data["callers"][0]["name"] == "sum_squares"


def test_detect_anti_shims_types(tmp_path):
    repo = tmp_path / "shim_repo"
    repo.mkdir()
    sample = repo / "sample_shims.py"
    sample.write_text(
        "import abc\n\n"
        "def empty_func():\n"
        "    pass\n\n"
        "def ellipsis_func():\n"
        "    '''Just a docstring'''\n"
        "    ...\n\n"
        "def not_impl():\n"
        "    raise NotImplementedError('TODO: implement')\n\n"
        "def fake_result():\n"
        "    return True\n\n"
        "def legit_calc(x):\n"
        "    val = x * 2\n"
        "    return val > 10\n\n"
        "class Base(abc.ABC):\n"
        "    @abc.abstractmethod\n"
        "    def abstract_meth(self):\n"
        "        pass\n\n"
        "def test_cheat_one():\n"
        "    assert True\n\n"
        "def test_cheat_tautology():\n"
        "    x = 42\n"
        "    assert x == x\n\n"
        "def test_legit():\n"
        "    x = 40 + 2\n"
        "    assert x == 42\n",
        encoding="utf-8"
    )

    shims = codemap.detect_anti_shims(sample)
    rules = [s["rule"] for s in shims]

    assert "EMPTY_STUB_BODY" in rules
    assert "NOT_IMPLEMENTED_STUB" in rules
    assert "TRIVIAL_RETURN_STUB" in rules
    assert "TRIVIAL_ASSERTION_CHEAT" in rules

    # Verify abstract methods and legitimate calculations are not flagged as shims
    symbols_flagged = [s["symbol"] for s in shims]
    assert "legit_calc" not in symbols_flagged
    assert "abstract_meth" not in symbols_flagged
    assert "test_legit" not in symbols_flagged

    report = codemap.format_anti_shim_report(shims)
    assert "# Anti-Shim Sentinel Report" in report
    assert "EMPTY_STUB_BODY" in report


def test_cli_codemap_check_shims(tmp_path, capsys):
    repo = tmp_path / "cli_shim_repo"
    repo.mkdir()
    (repo / "shim.py").write_text("def cheat():\n    return False\n", encoding="utf-8")

    class FakeArgs:
        path = "."
        query = None
        json = True
        raw_json = True
        policy = False
        check_shims = True
        arch = False
        impact = None
        callers = None
        fail_on_cycle = True

    class FakeProject:
        root = repo

    ret = cli.cmd_codemap(FakeArgs(), FakeProject(), None)
    assert ret == 1  # non-zero because shims detected
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["status"] == "warning"
    assert data["total_shims"] == 1
    assert data["shims"][0]["rule"] == "TRIVIAL_RETURN_STUB"


def test_gateway_codemap_check_shims(tmp_path):
    repo = tmp_path / "gw_shim_repo"
    repo.mkdir()
    (repo / "fake.py").write_text(
        "def stub():\n"
        "    ...\n\n"
        "def test_fake():\n"
        "    assert 1 == 1\n",
        encoding="utf-8"
    )

    gw = gateway.Gateway(repo, write=lambda s: None, log=lambda s: None)

    # 1. Markdown report
    res = gw._call({
        "name": "harness_codemap",
        "arguments": {"check_shims": True}
    })
    text = res["content"][0]["text"]
    assert "# Anti-Shim Sentinel Report" in text
    assert "EMPTY_STUB_BODY" in text
    assert "TRIVIAL_ASSERTION_CHEAT" in text

    # 2. Raw JSON
    res_json = gw._call({
        "name": "harness_codemap",
        "arguments": {"check_shims": True, "raw_json": True}
    })
    data = json.loads(res_json["content"][0]["text"])
    assert data["total_shims"] == 2





