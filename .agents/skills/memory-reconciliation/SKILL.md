---
name: memory-reconciliation
description: Audit and reconcile persistent agent memory against ground-truth AST and workspace files
---

# memory-reconciliation

## When to use
Use when inspecting, auditing, or pruning persistent SQLite agent memory (`.autoharness/memory.db`) to detect knowledge drift, orphaned file/symbol references, or conflicting lessons after refactorings, deletions, or codebase evolution.

## Steps
1. Open persistent SQLite database (`.autoharness/memory.db`) via `memory.get_memory_db(project_root)`.
2. Scan active documents across kinds (`ruling`, `lesson`, `review`, `followup`, `swarm_task`).
3. Differentiate between prescriptive policies and historical review records:
   - For `ruling`, `lesson`, and `followup`: extract referenced file paths and code symbols.
   - For `review`: verify only that the source task directory (`.autoharness/tasks/{task_id}`) still exists. Never flag historical critique prose quoting transient tests or external configurations as code drift or orphans.
4. Extract code references with strict token discipline:
   - Match path tokens while filtering out external URLs and transient tool directories (`answers/`, `runs/`, `evidence/`, `.cursor/`, `.gemini/`, `.claude/`, etc.).
   - Extract code symbols only when explicitly declared in document metadata or formatted in strict backticked identifier syntax (e.g. `def `name``, `class `Name``), filtering out Python keywords and prose verbs.
5. Verify referenced symbols against Python AST symbols (`ast.parse`) collected from existing workspace files.
6. Detect prescriptive contradictions by evaluating policy polarity (`positive` vs `negative`) on documents sharing multi-token topic overlap or title matches.
7. Compute drift metrics (`valid_count`, `drifted_count`, `orphaned_count`, `drift_rate`).
8. If `--prune-orphans` is specified, wrap deletions in an atomic SQLite transaction (`SAVEPOINT`) to safeguard database consistency without data loss.

## How to verify
Run `pytest tests/test_memory_reconciliation.py` and execute `autoharness memory reconcile --json`.

## Facts found here
- Research reference: Google Research Harness² (arXiv:2610.14224, https://arxiv.org/abs/2610.14224) and https://github.com/google-research/harness2.
- Transient directory filters: `answers/`, `prompts/`, `runs/`, `evidence/`, `.cursor/`, `.gemini/`, `.claude/`, `.kimi-code/`, `KIMI_CODE_HOME/`.
- Strict symbol backtick syntax eliminates false positive drift from ordinary English sentences.
