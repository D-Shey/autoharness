---
name: sqlite-memory
description: In-process SQLite FTS5 persistent memory with BM25 ranking and reconcile-on-write
---

# sqlite-memory

## When to use
Use when persisting, indexing, or querying task memory, past rulings, lessons, and review feedback across sessions without external vector databases or remote services.

## Steps
1. Maintain an SQLite database (`.autoharness/memory.db`) with an FTS5 virtual table.
2. Store documents categorized by kind: `ruling`, `lesson`, `review`, `followup`, `decision`.
3. Apply `reconcile-on-write`: deduplicate incoming entries against existing keys/content before inserting or updating.
4. Provide BM25 ranked full-text queries matching stemmed tokens and exact phrases.
5. In Gateway, expose through `harness_memory_search` and Code Mode `mcp.memory` tool calls.

## How to verify
Run pytest on `tests/test_memory_codemap.py` and verify `search_memory` returns ranked results with BM25 scores.
