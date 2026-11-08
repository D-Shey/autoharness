"""In-process SQLite FTS5 persistent memory with BM25 ranking and reconcile-on-write.

Pure Python standard library implementation (zero runtime dependencies).
Maintains project memory in .autoharness/memory.db.
Provides ranked full-text search across past rulings, lessons, followups, decisions, and review verdicts.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_docs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT,
    content TEXT NOT NULL,
    metadata TEXT,
    updated_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    title,
    content,
    content='memory_docs',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS memory_docs_ai AFTER INSERT ON memory_docs BEGIN
    INSERT INTO memory_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memory_docs_ad AFTER DELETE ON memory_docs BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, title, content) VALUES('delete', old.rowid, old.title, old.content);
END;

CREATE TRIGGER IF NOT EXISTS memory_docs_au AFTER UPDATE ON memory_docs BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, title, content) VALUES('delete', old.rowid, old.title, old.content);
    INSERT INTO memory_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;
"""


def get_memory_db(project_root: Path) -> sqlite3.Connection:
    """Connect to or initialize the SQLite memory database in .autoharness/memory.db."""
    state_dir = project_root / ".autoharness"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "memory.db"
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    with con:
        con.executescript(SCHEMA)
    return con


def reconcile_and_store(
    con: sqlite3.Connection,
    kind: str,
    source: str,
    content: str,
    title: str = "",
    metadata: dict[str, Any] | None = None,
    doc_id: str | None = None,
) -> str:
    """Store or update a memory document using reconcile-on-write (deduplication).

    If doc_id is given and exists, updates it.
    If no doc_id given, checks if identical (kind, source, title) exists:
      - If exact content matches, skips write (idempotent).
      - If different content, updates existing doc.
      - If not found, generates a clean deterministic ID and inserts.
    """
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)
    now = time.time()

    with con:
        cur = con.cursor()
        if not doc_id:
            row = cur.execute(
                "SELECT id, content FROM memory_docs WHERE kind = ? AND source = ? AND title = ?",
                (kind, source, title),
            ).fetchone()
            if row:
                existing_id, existing_content = row["id"], row["content"]
                if existing_content.strip() == content.strip():
                    return existing_id
                doc_id = existing_id

        if not doc_id:
            clean_source = source.replace("\\", "/").rsplit("/", 1)[-1].replace(".", "_")
            doc_id = f"{kind}_{clean_source}_{int(now * 1000) % 1000000}"

        cur.execute(
            """
            INSERT INTO memory_docs(id, kind, source, title, content, metadata, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind = excluded.kind,
                source = excluded.source,
                title = excluded.title,
                content = excluded.content,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (doc_id, kind, source, title, content, meta_json, now),
        )

        try:
            from autoharness import vector
            vector.index_document_vector(con, doc_id, f"{title or ''} {content}")
        except (sqlite3.Error, ImportError, ValueError):
            pass
    return doc_id


def search_memory(
    con: sqlite3.Connection,
    query: str,
    kind: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search memory using FTS5 with BM25 ranking.

    Returns list of dicts with match details, BM25 score, and contextual snippets.
    """
    if not query or not query.strip():
        return []

    # Format tokens for FTS5 prefix/match queries safely
    tokens = re.findall(r'[\w\u0400-\u04FF]+', query.strip())
    if not tokens:
        return []
    fts_query = " ".join(f'"{t}"*' for t in tokens)

    with con:
        cur = con.cursor()
        if kind and kind != "all":
            sql = """
                SELECT d.id, d.kind, d.source, d.title, d.content, d.metadata, d.updated_at,
                       bm25(memory_fts) as rank
                FROM memory_fts f
                JOIN memory_docs d ON f.rowid = d.rowid
                WHERE memory_fts MATCH ? AND d.kind = ?
                ORDER BY rank
                LIMIT ?
            """
            params = (fts_query, kind, limit)
        else:
            sql = """
                SELECT d.id, d.kind, d.source, d.title, d.content, d.metadata, d.updated_at,
                       bm25(memory_fts) as rank
                FROM memory_fts f
                JOIN memory_docs d ON f.rowid = d.rowid
                WHERE memory_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            params = (fts_query, limit)

        try:
            rows = cur.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Fallback to simple LIKE if FTS expression has syntax peculiarities
            fallback_sql = """
                SELECT id, kind, source, title, content, metadata, updated_at, 0.0 as rank
                FROM memory_docs
                WHERE (lower(title) LIKE ? OR lower(content) LIKE ?)
            """
            param_str = f"%{query.lower()}%"
            if kind and kind != "all":
                fallback_sql += " AND kind = ? LIMIT ?"
                rows = cur.execute(fallback_sql, (param_str, param_str, kind, limit)).fetchall()
            else:
                fallback_sql += " LIMIT ?"
                rows = cur.execute(fallback_sql, (param_str, param_str, limit)).fetchall()

    results = []
    for r in rows:
        meta = json.loads(r["metadata"]) if r["metadata"] else {}
        results.append({
            "id": r["id"],
            "kind": r["kind"],
            "source": r["source"],
            "title": r["title"] or "",
            "content": r["content"],
            "metadata": meta,
            "updated_at": r["updated_at"],
            "score": round(float(r["rank"]), 4),
        })
    return results


def index_project_memory(project_root: Path) -> dict[str, int]:
    """Scan and index all task artifacts and knowledge into .autoharness/memory.db."""
    con = get_memory_db(project_root)
    counts = {"rulings": 0, "followups": 0, "reviews": 0, "decisions": 0}
    state_dir = project_root / ".autoharness"

    # 1. Index rulings.json
    rulings_file = state_dir / "rulings.json"
    if rulings_file.is_file():
        try:
            data = json.loads(rulings_file.read_text("utf-8"))
            items = data.get("rulings", []) if isinstance(data, dict) else data
            for idx, item in enumerate(items):
                text = item.get("text") or item.get("ruling") or str(item)
                reconcile_and_store(
                    con,
                    kind="ruling",
                    source="rulings.json",
                    content=text,
                    title=f"Ruling #{idx + 1}",
                    metadata=item if isinstance(item, dict) else {},
                    doc_id=f"ruling_{idx + 1}",
                )
                counts["rulings"] += 1
        except Exception:
            pass

    # 2. Index followups.json
    followups_file = state_dir / "followups.json"
    if followups_file.is_file():
        try:
            data = json.loads(followups_file.read_text("utf-8"))
            items = data.get("items", []) if isinstance(data, dict) else data
            for item in items:
                fid = item.get("id")
                text = item.get("text", "")
                status = item.get("status", "open")
                reconcile_and_store(
                    con,
                    kind="followup",
                    source="followups.json",
                    content=text,
                    title=f"Followup #{fid} [{status}]",
                    metadata=item,
                    doc_id=f"followup_{fid}",
                )
                counts["followups"] += 1
        except Exception:
            pass

    # 3. Index task answers/reviews
    tasks_dir = state_dir / "tasks"
    if tasks_dir.is_dir():
        for task_dir in tasks_dir.iterdir():
            if not task_dir.is_dir():
                continue
            answers_dir = task_dir / "answers"
            if answers_dir.is_dir():
                for af in answers_dir.glob("*.txt"):
                    try:
                        content = af.read_text("utf-8", errors="replace")
                        reconcile_and_store(
                            con,
                            kind="review",
                            source=f"{task_dir.name}/{af.name}",
                            content=content,
                            title=f"Task {task_dir.name} Critic Review",
                            metadata={"task_id": task_dir.name, "file": af.name},
                            doc_id=f"review_{task_dir.name}_{af.stem}",
                        )
                        counts["reviews"] += 1
                    except Exception:
                        pass

    con.close()

    try:
        from autoharness import vector
        counts["vectors"] = vector.sync_memory_vectors(project_root)
    except (sqlite3.Error, ImportError, ValueError):
        pass

    return counts
