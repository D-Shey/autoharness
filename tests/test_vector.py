"""Unit tests for LEANN-style selective recomputation vector index and hybrid RRF search."""
import json
import math
import pytest
from pathlib import Path

from autoharness import vector, memory, cli, gateway


def test_vector_embedding_properties():
    # 1. Dimension and normalization
    vec1 = vector.compute_embedding("Distributed multi-agent swarm orchestration")
    assert len(vec1) == vector.VECTOR_DIM
    norm = math.sqrt(sum(x * x for x in vec1))
    assert abs(norm - 1.0) < 1e-5

    # 2. Self similarity is 1.0
    sim_self = vector.cosine_similarity(vec1, vec1)
    assert abs(sim_self - 1.0) < 1e-5

    # 3. Morphological subword similarity
    vec_orchestrate = vector.compute_embedding("orchestrate")
    vec_orchestration = vector.compute_embedding("orchestration")
    sim_morph = vector.cosine_similarity(vec_orchestrate, vec_orchestration)
    assert sim_morph > 0.5, f"Expected strong subword similarity, got {sim_morph}"

    # 4. Pack and unpack binary compactness
    packed = vector.pack_vector(vec1)
    assert len(packed) == vector.VECTOR_DIM * 4  # 1024 bytes (97% storage savings)
    unpacked = vector.unpack_vector(packed)
    assert len(unpacked) == vector.VECTOR_DIM
    assert abs(unpacked[0] - vec1[0]) < 1e-5


def test_vector_indexing_and_cosine_search(tmp_path: Path):
    project_root = tmp_path / "proj"
    project_root.mkdir()

    con = memory.get_memory_db(project_root)
    try:
        # Store test documents
        memory.reconcile_and_store(
            con,
            kind="ruling",
            source="rulings.json",
            title="Ruling #1: Atomic Writes",
            content="Always write files atomically using temporary files and os.replace under lock.",
            doc_id="r1_atomic",
        )
        memory.reconcile_and_store(
            con,
            kind="ruling",
            source="rulings.json",
            title="Ruling #2: Task Graph Scheduling",
            content="Topological sorting and acyclic graph scheduling for parallel worker pool.",
            doc_id="r2_graph",
        )

        # Vector search for conceptual paraphrase
        results = vector.search_vectors(con, "atomic file replacement", limit=2)
        assert len(results) >= 1
        assert results[0]["id"] == "r1_atomic"
        assert results[0]["vector_score"] > 0.3

        # Search for graph paraphrase
        results_graph = vector.search_vectors(con, "dag worker order", limit=2)
        assert len(results_graph) >= 1
        assert results_graph[0]["id"] == "r2_graph"
    finally:
        con.close()


def test_hybrid_rrf_search(tmp_path: Path):
    project_root = tmp_path / "proj_hybrid"
    project_root.mkdir()

    con = memory.get_memory_db(project_root)
    try:
        # Populate documents
        memory.reconcile_and_store(
            con,
            kind="lesson",
            source="lesson1",
            title="SQLite Connection Lifetimes",
            content="Always guard database connections with try finally con.close() to avoid leaks.",
            doc_id="doc_sqlite",
        )
        memory.reconcile_and_store(
            con,
            kind="lesson",
            source="lesson2",
            title="Subprocess Pipe Buffering",
            content="Use python -u line buffering for Windows stdio child processes.",
            doc_id="doc_pipe",
        )

        # Hybrid search: matches both BM25 keywords and vector concepts
        hybrid_res = vector.hybrid_search(con, "SQLite database connection leak", limit=2)
        assert len(hybrid_res) >= 1
        assert hybrid_res[0]["id"] == "doc_sqlite"
        assert "rrf_score" in hybrid_res[0]
        assert hybrid_res[0]["rrf_score"] > 0.0
    finally:
        con.close()


def test_cli_and_gateway_hybrid_vector_search(tmp_path: Path, monkeypatch, capsys):
    project_root = tmp_path / "proj_cli"
    project_root.mkdir()
    (project_root / ".autoharness").mkdir()

    con = memory.get_memory_db(project_root)
    try:
        memory.reconcile_and_store(
            con,
            kind="ruling",
            source="rulings.json",
            title="Zero Runtime Dependencies",
            content="AutoHarness must use only the Python standard library.",
            doc_id="ruling_zero_dep",
        )
    finally:
        con.close()

    monkeypatch.chdir(project_root)

    # 1. CLI search in hybrid mode (default)
    code = cli.main(["--project", str(project_root), "memory", "search", "standard library", "--mode", "hybrid"])
    assert code == 0
    out1 = capsys.readouterr().out
    data1 = json.loads(out1)
    assert data1["matches_count"] >= 1
    assert data1["results"][0]["id"] == "ruling_zero_dep"

    # 2. CLI search in vector mode
    code = cli.main(["--project", str(project_root), "memory", "search", "standard library", "--mode", "vector"])
    assert code == 0
    out2 = capsys.readouterr().out
    data2 = json.loads(out2)
    assert data2["matches_count"] >= 1

    # 3. Gateway tool call
    written = []
    gw = gateway.Gateway(project_root, write=written.append, log=lambda s: None)
    res = gw._call({"name": "harness_memory_search", "arguments": {"query": "standard library", "mode": "hybrid"}})
    assert not res.get("isError")
    assert "ZERO RUNTIME DEPENDENCIES" in res["content"][0]["text"].upper()
