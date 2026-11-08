---
name: hybrid-semantic-search
description: Reciprocal Rank Fusion hybrid retrieval combining BM25 keyword matching and dense vector similarity
---

# hybrid-semantic-search

## When to use
Use when neither keyword search (BM25) nor pure semantic vector search alone is sufficient: BM25 excels at exact symbol and variable names, while vector search excels at conceptual intent and paraphrased queries.

## Steps
1. Execute BM25 search on SQLite FTS5 index (`memory_fts MATCH ... ORDER BY bm25(...)`).
2. Execute vector cosine similarity search on the compact vector index (`vector_docs`).
3. Apply Reciprocal Rank Fusion (RRF) to merge both rankings:
   - For each document appearing in rank list $R$, compute:
     $RRF\_score(doc) = \sum_{r \in R} \frac{1}{k + rank(r)}$
   - Constant $k = 60$ balances influence between top-ranked items and longer tails.
4. Normalize and sort merged results by combined RRF score.
5. Return top-K blended results with source attribution (BM25 rank, vector cosine score, combined RRF score).

## How to verify
1. Test exact symbol match: querying `TopologicalSorter` ranks highest via BM25 boost.
2. Test conceptual paraphrase: querying `order tasks before running them` ranks highest via vector boost.
3. Test combined RRF ordering: documents present in both top lists receive the highest merged priority.

## Facts found here
- Reciprocal Rank Fusion with $k=60$ is proven by information retrieval research to outperform either sparse (BM25) or dense (vector) alone without requiring hyperparameter fine-tuning.
