"""Unit and integration tests for Ontology 2.0 (`.autoharness/ontology.md`)."""
from __future__ import annotations

from pathlib import Path

from autoharness import ontology


def test_ontology_init_add_and_query(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()

    init_res = ontology.init_ontology(proj)
    assert init_res["created"] is True
    assert (proj / ".autoharness" / "ontology.md").is_file()

    # Second init without overwrite is a no-op
    again = ontology.init_ontology(proj)
    assert again["created"] is False

    # Add entries across sections
    r1 = ontology.add_ontology_entry(
        proj,
        "glossary",
        "A check run made before the latest file edit; cannot satisfy teardown.",
        term="Stale Run",
    )
    assert r1["status"] == "added"

    # Idempotent duplicate add
    r1_dup = ontology.add_ontology_entry(
        proj,
        "glossary",
        "A check run made before the latest file edit; cannot satisfy teardown.",
        term="Stale Run",
    )
    assert r1_dup["status"] == "unchanged"

    ontology.add_ontology_entry(
        proj,
        "sources",
        "Authoritative state lives in `src/core.py`, never in legacy `src/old_core.py`.",
        term="Core Engine",
    )
    ontology.add_ontology_entry(
        proj,
        "judgments",
        "Prefer explicit errors over silent fallback defaults when parsing user config.",
        term="Config Parsing",
    )

    loaded = ontology.load_ontology(proj)
    assert loaded["exists"] is True
    assert loaded["entry_count"] == 3
    assert len(loaded["sections"]["glossary"]) == 1
    assert len(loaded["sections"]["sources_of_truth"]) == 1
    assert len(loaded["sections"]["judgment_templates"]) == 1

    q = ontology.query_ontology(proj, "stale teardown")
    assert q["exists"] is True
    assert len(q["matches"]) == 1
    assert q["matches"][0]["term"] == "Stale Run"

    ctx = ontology.format_ontology_context(proj, "config")
    assert "Config Parsing" in ctx
    assert "explicit errors" in ctx


def test_ontology_validate_detects_drift_and_conflicts(tmp_path: Path):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "engine.py").write_text(
        "class ActiveEngine:\n    def run(self):\n        return 1\n",
        encoding="utf-8",
    )

    ontology.init_ontology(proj)
    ontology.add_ontology_entry(
        proj,
        "sources",
        "Canonical runner is `src/engine.py` def `run`.",
        term="Runner",
    )
    ontology.add_ontology_entry(
        proj,
        "entities",
        "Legacy reference in `src/deleted_mod.py` and missing def `ghost_fn` in `src/engine.py`.",
        term="Legacy",
    )
    ontology.add_ontology_entry(proj, "glossary", "First definition.", term="Ambiguous")
    ontology.add_ontology_entry(proj, "glossary", "Contradicting definition.", term="Ambiguous")

    report = ontology.validate_ontology(proj)
    assert report["status"] == "drifted"
    assert report["total_entries"] == 4
    assert report["valid_entries"] == 2
    assert any(bp["path"] == "src/deleted_mod.py" for bp in report["broken_paths"])
    assert any(ms["symbol"] == "ghost_fn" for ms in report["missing_symbols"])
    assert any(cf["term"] == "Ambiguous" for cf in report["conflicts"])

    md = ontology.format_ontology_report(report)
    assert "`DRIFTED`" in md
    assert "src/deleted_mod.py" in md
    assert "ghost_fn" in md


def test_ontology_canonical_defaults_aliases_memory_prune_and_gateway(tmp_path: Path, capsys):
    import json
    from autoharness import cli, gateway, memory

    proj = tmp_path / "proj"
    proj.mkdir()

    # CLI default add puts entry in canonical Glossary and validates JSON error on newline injection
    assert cli.main(["--project", str(proj), "ontology", "init"]) == 0
    capsys.readouterr()
    assert cli.main(["--project", str(proj), "ontology", "add", "Canonical term def", "--term", "Token"]) == 0
    add_res = json.loads(capsys.readouterr().out)
    assert add_res["section"] == "Glossary"

    assert cli.main(["--project", str(proj), "ontology", "add", "bad\n## Hack", "--term", "Bad"]) == 1
    err_res = json.loads(capsys.readouterr().out)
    assert err_res["status"] == "error"

    assert ontology.query_ontology(proj, query="", limit=-1)["matches"] == []

    # Alias heading `## Project Glossary` merges cleanly without creating duplicate heading & records 1-based line
    md_path = ontology.ontology_path(proj)
    md_path.write_text("# Ontology\n\n## Project Glossary\n- **Alpha**: first\n", encoding="utf-8")
    ontology.add_ontology_entry(proj, "glossary", "second", term="Beta")
    text_after = md_path.read_text("utf-8")
    assert text_after.count("## ") == 1
    loaded = ontology.load_ontology(proj)
    assert all(e["line"] > 0 for e in loaded["entries"])

    # Memory indexing stores real line numbers and prunes stale ontology docs + vectors when removed
    counts1 = memory.index_project_memory(proj)
    assert counts1["ontology"] == 2
    md_path.write_text("# Ontology\n\n## Project Glossary\n- **Alpha**: first\n", encoding="utf-8")
    counts2 = memory.index_project_memory(proj)
    assert counts2["ontology"] == 1
    con = memory.get_memory_db(proj)
    rows = con.execute("SELECT id, metadata FROM memory_docs WHERE kind = 'ontology'").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["metadata"])["line"] > 0
    orphan_vecs = con.execute(
        "SELECT doc_id FROM vector_embeddings WHERE doc_id LIKE 'ontology_%' AND doc_id NOT IN (SELECT id FROM memory_docs)"
    ).fetchall()
    con.close()
    assert orphan_vecs == []

    # Alias key `"sources"` must resolve cleanly to existing `## Sources of Truth` without duplicate headings
    ontology.init_ontology(proj, overwrite=True)
    ontology.add_ontology_entry(proj, "sources", "Primary store is `src/store.py`.", term="Store")
    assert ontology.add_ontology_entry(proj, "sources", "Primary store is `src/store.py`.", term="Store")["status"] == "unchanged"
    assert ontology.ontology_path(proj).read_text("utf-8").count("## Sources of Truth") == 1
    import pytest
    with pytest.raises(ValueError):
        ontology.add_ontology_entry(proj, "Bad\n## Header", "x")

    # Gateway harness_ontology uses Glossary default & rejects invalid action/limit
    gw = gateway.Gateway(proj, lambda _m: None, lambda _l: None)
    assert gw._call({"name": "harness_ontology", "arguments": {"action": "bogus"}})["isError"] is True
    assert gw._call({"name": "harness_ontology", "arguments": {"action": "query", "limit": "nan"}})["isError"] is True
    gw_add = gw._call({"name": "harness_ontology", "arguments": {"action": "add", "entry": "GW def", "term": "Gamma", "raw_json": True}})
    assert not gw_add.get("isError")
    assert json.loads(gw_add["content"][0]["text"])["section"] == "Glossary"
