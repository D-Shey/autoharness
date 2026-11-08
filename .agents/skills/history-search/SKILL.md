---
name: history-search
description: Search and retrieval across task history, runs, and reviews
---

# history-search

## When to use
Use when querying previous execution details in long-running tasks without loading massive logs into active context:
- Searching past test logs (`runs/*.log`) for specific assertion failures or stack traces.
- Querying earlier critic reviews and arbitrator decisions.
- Retrieving previous tool call outputs on demand.

## Steps
1. **Targeted query dispatch**:
   - Call `harness_history_search(query, kind)` where `kind` is `runs`, `reviews`, or `all`.
   - The search engine inspects task log files on disk and returns only matching lines with file coordinates and line numbers.
   - Reference: Reversible context retrieval (https://github.com/ranxianglei/billion-context).
2. **Context decompression**:
   - If a specific test error needs full inspection, fetch only that run's snippet rather than the entire 500-line pytest dump.
3. **Bounded token return**:
   - Limit search results to the top matching snippets with context windows (e.g., 3 lines before/after).

## How to verify
1. Run `harness_history_search` with an assertion query and verify matched log snippets are returned.
2. Verify filtering by kind (`runs` vs `reviews`) returns only relevant records.

## Facts found here
- `runs/` directory in task state holds output of every `autoharness verify run`.
- `answers/` directory holds exact responses of critic and quality reviewers.
