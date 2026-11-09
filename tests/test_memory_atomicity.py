"""Tests for SQLite transaction atomicity, savepoint isolation, and UUID uniqueness in AutoHarness memory."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from autoharness import memory, vector


def test_automatic_ids_preserve_distinct_documents_in_same_millisecond(tmp_path: Path):
    con = memory.get_memory_db(tmp_path)

    # 1. Store 100 documents with fixed mocked time to simulate concurrent generation in the same millisecond
    fixed_time = 1700000000.123456
    with patch("time.time", return_value=fixed_time):
        doc_ids = []
        for i in range(100):
            d_id = memory.reconcile_and_store(
                con,
                kind="lesson",
                source="lessons.md",
                title=f"Lesson #{i}",
                content=f"Detailed lesson content {i} regarding system design and verification.",
            )
            doc_ids.append(d_id)

    # All generated IDs must be unique
    assert len(set(doc_ids)) == 100, f"Expected 100 unique IDs, got {len(set(doc_ids))}"

    # Exactly 100 rows must exist in memory_docs
    row_count = con.execute("SELECT COUNT(*) FROM memory_docs").fetchone()[0]
    assert row_count == 100, f"Expected 100 rows in memory_docs, got {row_count}"

    # Verify that updating an existing document with the same (kind, source, title) updates it in place
    first_id = doc_ids[0]
    updated_id = memory.reconcile_and_store(
        con,
        kind="lesson",
        source="lessons.md",
        title="Lesson #0",
        content="Updated lesson 0 content.",
    )
    assert updated_id == first_id
    total_count = con.execute("SELECT COUNT(*) FROM memory_docs").fetchone()[0]
    assert total_count == 100

    row = con.execute("SELECT content FROM memory_docs WHERE id = ?", (first_id,)).fetchone()
    assert row["content"] == "Updated lesson 0 content."


def test_automatic_ids_distinguish_full_source_paths(tmp_path: Path):
    con = memory.get_memory_db(tmp_path)

    id_first = memory.reconcile_and_store(
        con,
        kind="lesson",
        source="first/lessons.md",
        title="Path Test",
        content="Content from first directory.",
    )
    id_second = memory.reconcile_and_store(
        con,
        kind="lesson",
        source="second/lessons.md",
        title="Path Test",
        content="Content from second directory.",
    )

    assert id_first != id_second
    assert "first_lessons_md" in id_first
    assert "second_lessons_md" in id_second

    rows = con.execute("SELECT id, source FROM memory_docs WHERE title = 'Path Test'").fetchall()
    assert len(rows) == 2
    sources = {r["source"] for r in rows}
    assert sources == {"first/lessons.md", "second/lessons.md"}


def test_savepoint_atomic_rollback_on_vector_failure(tmp_path: Path):
    db_path = tmp_path / ".autoharness" / "memory.db"
    con = memory.get_memory_db(tmp_path)

    # Simulate vector failure during reconcile_and_store
    with patch("autoharness.vector.index_document_vector", side_effect=RuntimeError("Vector embedding crashed")):
        with pytest.raises(RuntimeError, match="Vector embedding crashed"):
            memory.reconcile_and_store(
                con,
                kind="ruling",
                source="rulings.json",
                title="Faulty Ruling",
                content="This document must not be partially stored.",
            )

    # Verify memory_docs is empty in current connection
    assert con.execute("SELECT COUNT(*) FROM memory_docs").fetchone()[0] == 0
    con.close()

    # Reopen connection from disk to ensure nothing was committed
    con_fresh = sqlite3.connect(str(db_path))
    con_fresh.row_factory = sqlite3.Row
    assert con_fresh.execute("SELECT COUNT(*) FROM memory_docs").fetchone()[0] == 0
    con_fresh.close()


def test_search_memory_does_not_commit_or_destroy_caller_savepoint(tmp_path: Path):
    con = memory.get_memory_db(tmp_path)

    # Caller starts an outer savepoint
    con.execute("SAVEPOINT caller_sp")

    # Caller inserts a pending row
    con.execute(
        """
        INSERT INTO memory_docs (id, kind, source, title, content, updated_at)
        VALUES ('pending_1', 'note', 'scratch.txt', 'Pending', 'Caller pending content', 1000.0)
        """
    )

    # Calling search_memory must NOT commit the caller's transaction or invalidate caller_sp
    results = memory.search_memory(con, "pending")
    # Result may or may not be returned depending on trigger/fts state, but transaction must remain active

    # Caller should be able to rollback to their own savepoint without error
    con.execute("ROLLBACK TO SAVEPOINT caller_sp")
    con.execute("RELEASE SAVEPOINT caller_sp")

    # The pending row must be gone
    row = con.execute("SELECT id FROM memory_docs WHERE id = 'pending_1'").fetchone()
    assert row is None


def test_search_vectors_does_not_commit_caller_transaction(tmp_path: Path):
    con = memory.get_memory_db(tmp_path)
    vector.init_vector_table(con)

    # Caller starts a transaction
    con.execute("SAVEPOINT caller_vec_sp")
    con.execute(
        """
        INSERT INTO memory_docs (id, kind, source, title, content, updated_at)
        VALUES ('uncommitted_vec', 'note', 'v.txt', 'Uncommitted', 'Vector search should not commit me', 1000.0)
        """
    )

    # Run vector search
    _ = vector.search_vectors(con, "search query")

    # Roll back caller savepoint
    con.execute("ROLLBACK TO SAVEPOINT caller_vec_sp")
    con.execute("RELEASE SAVEPOINT caller_vec_sp")

    # Row must not exist
    row = con.execute("SELECT id FROM memory_docs WHERE id = 'uncommitted_vec'").fetchone()
    assert row is None


def test_reconcile_and_store_standalone_commits_on_success(tmp_path: Path):
    db_path = tmp_path / ".autoharness" / "memory.db"
    con = memory.get_memory_db(tmp_path)

    doc_id = memory.reconcile_and_store(
        con,
        kind="ruling",
        source="rulings.json",
        title="Persistent Ruling",
        content="This document must be persistently committed to disk.",
    )
    con.close()

    # Reopen fresh connection from disk
    con_fresh = sqlite3.connect(str(db_path))
    con_fresh.row_factory = sqlite3.Row
    row = con_fresh.execute("SELECT id, title, content FROM memory_docs WHERE id = ?", (doc_id,)).fetchone()
    assert row is not None
    assert row["title"] == "Persistent Ruling"

    # Verify vector table also has the embedding
    vec_row = con_fresh.execute("SELECT doc_id, vector FROM vector_embeddings WHERE doc_id = ?", (doc_id,)).fetchone()
    assert vec_row is not None
    assert len(vec_row["vector"]) > 0
    con_fresh.close()


def test_atomic_rollback_on_sqlite_trigger_failure(tmp_path: Path):
    con = memory.get_memory_db(tmp_path)
    vector.init_vector_table(con)

    # Install trigger that fails insertion on vector_embeddings
    con.execute(
        """
        CREATE TRIGGER abort_vector_insert
        BEFORE INSERT ON vector_embeddings
        BEGIN
            SELECT RAISE(ABORT, 'forced abort in vector_embeddings trigger');
        END;
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced abort"):
        memory.reconcile_and_store(
            con,
            kind="ruling",
            source="rules.md",
            title="Trigger Failure Doc",
            content="Document body that should be rolled back completely.",
        )

    # Ensure document row was rolled back completely
    count = con.execute("SELECT COUNT(*) FROM memory_docs").fetchone()[0]
    assert count == 0
