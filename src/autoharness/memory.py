"""In-process SQLite FTS5 persistent memory with BM25 ranking and reconcile-on-write.

Pure Python standard library implementation (zero runtime dependencies).
Maintains project memory in .autoharness/memory.db.
Provides ranked full-text search across past rulings, lessons, followups, decisions, and review verdicts.
"""
from __future__ import annotations

import ast
import json
import keyword
import re
import sqlite3
import time
import uuid
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

    was_in_transaction = con.in_transaction
    sp_name = f"sp_mem_{uuid.uuid4().hex[:12]}"
    con.execute(f"SAVEPOINT {sp_name}")
    try:
        cur = con.cursor()
        if not doc_id:
            row = cur.execute(
                "SELECT id, content FROM memory_docs WHERE kind = ? AND source = ? AND title = ?",
                (kind, source, title),
            ).fetchone()
            if row:
                existing_id, existing_content = row["id"], row["content"]
                if existing_content.strip() == content.strip():
                    con.execute(f"RELEASE SAVEPOINT {sp_name}")
                    return existing_id
                doc_id = existing_id

        if not doc_id:
            clean_source = source.replace("\\", "/").strip("/").replace("/", "_").replace(".", "_") or "doc"
            doc_id = f"{kind}_{clean_source}_{uuid.uuid4().hex}"

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

        from autoharness import vector
        vector.index_document_vector(con, doc_id, f"{title or ''} {content}", manage_transaction=False)

        con.execute(f"RELEASE SAVEPOINT {sp_name}")
        return doc_id
    except Exception:
        try:
            con.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
            con.execute(f"RELEASE SAVEPOINT {sp_name}")
        except sqlite3.Error:
            pass
        if not was_in_transaction and con.in_transaction:
            try:
                con.rollback()
            except sqlite3.Error:
                pass
        raise


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
    counts = {"rulings": 0, "followups": 0, "reviews": 0, "decisions": 0, "ontology": 0}
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
        except (OSError, json.JSONDecodeError, sqlite3.Error, ValueError, TypeError) as e:
            counts.setdefault("errors", 0)
            counts["errors"] += 1

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
        except (OSError, json.JSONDecodeError, sqlite3.Error, ValueError, TypeError):
            counts.setdefault("errors", 0)
            counts["errors"] += 1

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
                    except (OSError, sqlite3.Error, ValueError):
                        counts.setdefault("errors", 0)
                        counts["errors"] += 1

    # 4. Index .autoharness/ontology.md (Ontology 2.0)
    try:
        from autoharness import ontology as _ontology
        onto = _ontology.load_ontology(project_root)
        with con:
            try:
                con.execute("DELETE FROM vector_embeddings WHERE doc_id IN (SELECT id FROM memory_docs WHERE kind = 'ontology')")
            except sqlite3.OperationalError:
                pass
            con.execute("DELETE FROM memory_docs WHERE kind = 'ontology'")
        if onto.get("exists"):
            idx = 0
            for sec_name, entries in onto.get("sections", {}).items():
                for item in entries:
                    idx += 1
                    reconcile_and_store(
                        con,
                        kind="ontology",
                        source=".autoharness/ontology.md",
                        content=f"[{sec_name}] {item.get('raw', '')}",
                        title=f"Ontology: {item.get('term') or sec_name}",
                        metadata={"section": sec_name, "term": item.get("term", ""), "line": item.get("line", 0)},
                        doc_id=f"ontology_{idx}",
                    )
                    counts["ontology"] += 1
    except (OSError, sqlite3.Error, ValueError, ImportError):
        counts.setdefault("errors", 0)
        counts["errors"] += 1

    con.close()

    try:
        from autoharness import vector
        counts["vectors"] = vector.sync_memory_vectors(project_root)
    except (sqlite3.Error, ImportError, ValueError):
        pass

    return counts


PATH_PATTERN = re.compile(
    r'(?<![/\w\.-])(?:[a-zA-Z0-9_.-]+[/\\])+[a-zA-Z0-9_.-]+\.(?:py|json|md|yaml|yml|toml|txt|sh|bat|ps1|ts|js|html|css|sql)(?![/\w\.-])'
)

POSITIVE_KEYWORDS = {
    "always", "allow", "allowed", "enable", "enabled", "require", "required",
    "permit", "permitted", "всегда", "разрешено", "используй", "обязательно",
    "подтверждай", "подтверждать",
}
NEGATIVE_KEYWORDS = {
    "never", "prohibit", "prohibited", "forbid", "forbidden", "disable",
    "disabled", "deny", "denied", "никогда", "запрещено", "не используй",
    "нельзя", "блокируй", "блокировать",
}
COMMON_STOPWORDS = {
    "always", "never", "allow", "prohibit", "rule", "task", "code", "file",
    "test", "should", "must", "with", "from", "that", "this", "when", "then",
    "into", "onto", "before", "after", "while", "using", "make", "take",
    "does", "have", "more", "also", "each", "every", "other", "some", "such",
    "only", "true", "false", "none", "null", "self", "error", "status",
    "agent", "already", "accept", "verdict", "answer", "critic", "review",
    "summary", "finding", "findings", "check", "checks", "checked", "issue",
    "issues", "result", "results", "tested", "passed", "failed", "verified",
    "running", "written", "first", "second", "third", "which", "there", "their",
}


PROSE_SYMBOLS = set(keyword.kwlist) | {
    "has", "have", "had", "was", "were", "been", "be", "do", "does", "did",
    "used", "removed", "added", "deleted", "fixed", "called", "defined",
    "created", "modified", "passed", "failed", "named", "found", "seen",
    "taken", "given", "run", "runs", "set", "sets", "like", "such", "well",
    "here", "then", "when", "what", "which", "where", "with", "both", "each",
    "methods", "functions", "classes", "definitions", "symbols", "modules",
}


def extract_code_references(
    content: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[set[str], set[str]]:
    """Extract referenced file paths and code symbols from memory content and metadata."""
    paths: set[str] = set()
    symbols: set[str] = set()

    meta = metadata or {}
    for key in ("file", "path"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            clean = val.strip().replace("\\", "/").strip("`'\"()[],;:")
            # Do not treat review artifact names as workspace files
            if clean and not clean.startswith("critic-"):
                paths.add(clean)

    for key in ("symbol", "symbols", "function", "class", "method"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            symbols.add(val.strip().strip("`'\"()[],;:"))
        elif isinstance(val, list):
            for s in val:
                if isinstance(s, str) and s.strip():
                    symbols.add(s.strip().strip("`'\"()[],;:"))

    # Extract paths from content
    for m in PATH_PATTERN.finditer(content):
        raw = m.group(0).strip("`'\"()[],;:")
        if not raw.startswith(("http:", "https:", "ftp:", "file:")):
            paths.add(raw.replace("\\", "/"))

    # Extract explicit def / class / function references from content (strictly backticked identifiers)
    for m in re.finditer(r'(?:def|class|function|method)\s+`([a-zA-Z_]\w*)`', content):
        sym = m.group(1)
        if len(sym) >= 2 and sym.lower() not in PROSE_SYMBOLS:
            symbols.add(sym)

    return paths, symbols


def collect_file_ast_symbols(file_path: Path) -> set[str]:
    """Parse a Python source file and extract function, class, and assignment symbol names."""
    if not file_path.is_file() or file_path.suffix != ".py":
        return set()
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, OSError):
        return set()

    symbols = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbols.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                symbols.add(node.target.id)
    return symbols


def detect_statement_polarity(text: str) -> str:
    """Return 'positive', 'negative', or 'neutral' based on prescriptive policy keywords."""
    lower = text.lower()
    has_neg = any(re.search(r'\b' + re.escape(w) + r'\b', lower) for w in NEGATIVE_KEYWORDS)
    if has_neg:
        return "negative"
    has_pos = any(re.search(r'\b' + re.escape(w) + r'\b', lower) for w in POSITIVE_KEYWORDS)
    if has_pos:
        return "positive"
    return "neutral"


def reconcile_memory_drift(
    project_root: Path,
    prune_orphans: bool = False,
    kind: str | None = None,
) -> dict[str, Any]:
    """Audit persistent SQLite memory (.autoharness/memory.db) against ground-truth workspace files and AST symbols.

    Google Research Harness² model:
    - Identifies orphaned documents (referenced files or source tasks deleted).
    - Identifies drifted documents (missing AST symbols, conflicting prescriptive rulings).
    - Computes drift metrics and returns structured reconciliation report.
    - Optionally permanently prunes orphaned documents when prune_orphans=True.
    """
    root = Path(project_root).resolve()
    con = get_memory_db(root)
    try:
        cur = con.cursor()
        if kind and kind != "all":
            rows = cur.execute("SELECT * FROM memory_docs WHERE kind = ?", (kind,)).fetchall()
        else:
            rows = cur.execute("SELECT * FROM memory_docs").fetchall()

        ast_cache: dict[Path, set[str]] = {}
        docs: list[dict[str, Any]] = []
        for r in rows:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
            docs.append({
                "id": r["id"],
                "kind": r["kind"],
                "source": r["source"],
                "title": r["title"] or "",
                "content": r["content"],
                "metadata": meta,
                "updated_at": r["updated_at"],
            })

        drifted_items: list[dict[str, Any]] = []
        orphaned_ids: list[str] = []
        valid_count = 0

        # Build polarity map for conflict detection
        polarities: list[tuple[dict[str, Any], str, set[str]]] = []
        for d in docs:
            pol = detect_statement_polarity(d["title"] + " " + d["content"])
            words = set(re.findall(r'[a-zA-Z_]\w{3,}', (d["title"] + " " + d["content"]).lower()))
            subject_words = words - COMMON_STOPWORDS
            polarities.append((d, pol, subject_words))

        for idx, d in enumerate(docs):
            missing_files: list[str] = []
            missing_symbols: list[str] = []
            conflicts: list[str] = []
            reasons: list[str] = []

            # 1. Source check for reviews
            primary_source_missing = False
            if d["kind"] == "review":
                source_rel = d["source"].replace("\\", "/")
                task_dir_name = source_rel.split("/")[0]
                task_dir_path = root / ".autoharness" / "tasks" / task_dir_name
                if not task_dir_path.exists():
                    missing_files.append(source_rel)
                    reasons.append(f"Review source task directory '.autoharness/tasks/{task_dir_name}' does not exist")
                    primary_source_missing = True
                # Historical review documents record past critiques; do not parse content for code drift
                ref_paths, ref_symbols = set(), set()
            else:
                # 2. Extract code and file references for rulings, lessons, and followups
                ref_paths, ref_symbols = extract_code_references(d["content"], d["metadata"])

            primary_file_missing = False
            existing_py_paths: list[Path] = []
            IGNORE_PATH_PREFIXES = (
                ".cursor/", ".gemini/", ".claude/", ".kimi-code/", "KIMI_CODE_HOME/",
                "evidence/", "answers/", "prompts/", "runs/", "tmp/", "temp/",
            )

            for p_str in ref_paths:
                p_norm = p_str.replace("\\", "/").strip("`'\"()[],;:")
                if p_norm.startswith(("http:", "https:", "ftp:", "#")) or any(p_norm.startswith(pfx) for pfx in IGNORE_PATH_PREFIXES):
                    continue
                # Handle line numbers e.g. foo.py:42
                if ":" in p_norm:
                    parts = p_norm.split(":")
                    if not (len(parts[0]) == 1 and parts[0].isalpha()):  # Not Windows drive letter
                        p_norm = parts[0]

                # Bare filenames: skip unless known workspace root configuration files
                if "/" not in p_norm and "\\" not in p_norm:
                    if p_norm not in {"package.json", "setup.py", "setup.cfg", "pyproject.toml", "tox.ini", "Makefile"}:
                        continue

                try:
                    p_path = (root / p_norm).resolve()
                    if not p_path.is_relative_to(root):
                        continue
                except (ValueError, OSError):
                    continue

                if not p_path.exists():
                    missing_files.append(p_norm)
                    reasons.append(f"Referenced path '{p_norm}' does not exist on disk")
                    if d["metadata"].get("file") == p_str or d["metadata"].get("path") == p_str:
                        primary_file_missing = True
                else:
                    if p_path.suffix == ".py":
                        existing_py_paths.append(p_path)
                        if p_path not in ast_cache:
                            ast_cache[p_path] = collect_file_ast_symbols(p_path)

            # Check symbols against AST of existing Python files
            explicit_symbols = set()
            for k in ("symbol", "symbols", "function", "class", "method"):
                v = d["metadata"].get(k)
                if isinstance(v, str) and v.strip():
                    explicit_symbols.add(v.strip())
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, str) and item.strip():
                            explicit_symbols.add(item.strip())

            symbols_to_verify = explicit_symbols or (ref_symbols if existing_py_paths else set())

            if existing_py_paths and symbols_to_verify:
                all_py_symbols = set()
                for py_path in existing_py_paths:
                    all_py_symbols.update(ast_cache.get(py_path, set()))

                for s in symbols_to_verify:
                    if s not in all_py_symbols and not s.startswith("_"):
                        missing_symbols.append(s)
                        reasons.append(f"Symbol '{s}' not found in AST of referenced Python files")
            elif explicit_symbols and not existing_py_paths:
                for s in explicit_symbols:
                    missing_symbols.append(s)
                    reasons.append(f"Symbol '{s}' specified in metadata but referenced file is missing")

            # 3. Check conflicts with other docs (prescriptive kinds only)
            PRESCRIPTIVE_KINDS = {"ruling", "lesson", "policy", "rule"}
            curr_d, curr_pol, curr_subs = polarities[idx]
            if curr_d["kind"] in PRESCRIPTIVE_KINDS and curr_pol != "neutral":
                for other_d, other_pol, other_subs in polarities:
                    if other_d["id"] == curr_d["id"] or other_d["kind"] not in PRESCRIPTIVE_KINDS:
                        continue
                    if other_pol != "neutral" and other_pol != curr_pol:
                        shared = (curr_subs & other_subs)
                        curr_title_words = set(re.findall(r'[a-zA-Z_]\w{3,}', curr_d["title"].lower())) - COMMON_STOPWORDS
                        other_title_words = set(re.findall(r'[a-zA-Z_]\w{3,}', other_d["title"].lower())) - COMMON_STOPWORDS
                        if len(shared) >= 2 or (curr_title_words & other_title_words):
                            subj_sample = ", ".join(sorted(shared)[:2])
                            conflict_desc = f"Opposing directive with {other_d['id']} on topic '{subj_sample}'"
                            if conflict_desc not in conflicts:
                                conflicts.append(conflict_desc)
                                reasons.append(f"Contradicts {other_d['id']} ({other_pol} vs {curr_pol}) on '{subj_sample}'")

            if missing_files or missing_symbols or conflicts:
                if d["kind"] == "review":
                    is_orphaned = primary_source_missing
                else:
                    is_orphaned = primary_file_missing or (bool(ref_paths) and len(missing_files) == len(ref_paths) and not existing_py_paths)

                status = "orphaned" if is_orphaned else "drifted"
                drifted_item = {
                    "id": d["id"],
                    "kind": d["kind"],
                    "title": d["title"],
                    "source": d["source"],
                    "status": status,
                    "missing_files": sorted(set(missing_files)),
                    "missing_symbols": sorted(set(missing_symbols)),
                    "conflicts": sorted(set(conflicts)),
                    "reasons": sorted(set(reasons)),
                }
                drifted_items.append(drifted_item)
                if status == "orphaned":
                    orphaned_ids.append(d["id"])
            else:
                valid_count += 1

        pruned_count = 0
        if prune_orphans and orphaned_ids:
            was_in_tx = con.in_transaction
            sp_name = f"sp_reconcile_{uuid.uuid4().hex[:12]}"
            con.execute(f"SAVEPOINT {sp_name}")
            try:
                for oid in orphaned_ids:
                    cur.execute("DELETE FROM memory_docs WHERE id = ?", (oid,))
                    try:
                        cur.execute("DELETE FROM vector_embeddings WHERE doc_id = ?", (oid,))
                    except sqlite3.OperationalError:
                        pass
                con.execute(f"RELEASE SAVEPOINT {sp_name}")
                if not was_in_tx:
                    con.commit()
                pruned_count = len(orphaned_ids)
            except Exception:
                con.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                con.execute(f"RELEASE SAVEPOINT {sp_name}")
                raise

        total_scanned = len(docs)
        drifted_count = sum(1 for item in drifted_items if item["status"] == "drifted")
        orphaned_count = len(orphaned_ids)
        drift_rate = round((drifted_count + orphaned_count) / max(1, total_scanned), 4)

        onto_check = None
        try:
            from autoharness import ontology as _ontology
            if _ontology.ontology_path(project_root).is_file():
                onto_check = _ontology.validate_ontology(project_root)
        except (OSError, ValueError, ImportError):
            onto_check = None

        res = {
            "status": "ok",
            "scanned_docs": total_scanned,
            "valid_count": valid_count,
            "drifted_count": drifted_count,
            "orphaned_count": orphaned_count,
            "pruned_count": pruned_count,
            "drift_rate": drift_rate,
            "drifted_items": drifted_items,
        }
        if onto_check is not None:
            res["ontology"] = {
                "status": onto_check["status"],
                "valid": onto_check["status"] == "healthy",
                "broken_paths": len(onto_check["broken_paths"]),
                "missing_symbols": len(onto_check["missing_symbols"]),
                "conflicts": len(onto_check["conflicts"]),
            }
        return res
    finally:
        con.close()


def format_memory_reconcile_report(report: dict[str, Any]) -> str:
    """Format memory drift reconciliation results into an agent/human-readable Markdown report."""
    total = report.get("scanned_docs", 0)
    valid = report.get("valid_count", 0)
    drifted = report.get("drifted_count", 0)
    orphaned = report.get("orphaned_count", 0)
    pruned = report.get("pruned_count", 0)
    rate_pct = round(report.get("drift_rate", 0.0) * 100, 1)

    overall = "DRIFT_DETECTED" if (drifted + orphaned > 0) else "HEALTHY"
    badge = f"`{overall}`"

    lines = [
        "# AutoHarness Knowledge Reconciliation & Memory Drift Report",
        "",
        f"- **Audit Status:** {badge}",
        f"- **Scanned Documents:** {total}",
        f"- **Valid Memories:** {valid}",
        f"- **Drifted / Stale:** {drifted}",
        f"- **Orphaned:** {orphaned}",
        f"- **Pruned Records:** {pruned}",
        f"- **Drift Rate:** {rate_pct}%",
        "",
    ]

    items = report.get("drifted_items", [])
    if not items:
        lines.append("All persistent memory records are consistent with ground-truth workspace files and AST symbols. No knowledge drift detected.")
        return "\n".join(lines)

    lines.append("## Drifted and Orphaned Memory Records")
    lines.append("| ID | Kind | Title | Status | Issues |")
    lines.append("|---|---|---|---|---|")
    for item in items:
        status_badge = f"`{item['status'].upper()}`"
        reasons_summary = "; ".join(item.get("reasons", [])) or "Stale reference"
        clean_title = (item.get("title") or item["id"]).replace("|", "\\|")
        lines.append(f"| `{item['id']}` | `{item['kind']}` | {clean_title} | {status_badge} | {reasons_summary} |")

    lines.append("")
    lines.append("## Remediation Guidance")
    if orphaned > 0 and pruned == 0:
        lines.append("- Run `autoharness memory reconcile --prune-orphans` to purge broken orphaned references.")
    if drifted > 0:
        lines.append("- Review conflicting rulings or update stale symbol references to match current repository codebase.")

    return "\n".join(lines)

