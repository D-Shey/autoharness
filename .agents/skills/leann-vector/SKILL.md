---
name: leann-vector
description: Compact selective-recomputation vector indexing for codebases and memory
---

# leann-vector

## When to use
Use when semantic search across code or project memory requires vector similarity without the disk/memory overhead of traditional vector databases (Chroma, Pinecone) or external daemon requirements.

## Steps
1. Parse text into semantic documents with metadata (source file, doc kind, symbols, updated timestamp).
2. Tokenize and extract subword character n-grams and normalized terms to capture morphological variations.
3. Compute compact sparse term-frequency vectors or hashing embeddings:
   - Zero runtime dependencies: pure standard library Python vectorizer with hash-trick projection into compact dimensionality (e.g. 256 or 512 dimensions).
   - L2 normalization for fast cosine similarity via dot product: `dot(v1, v2) / (|v1| * |v2|)`.
4. Store compact vectors in `.autoharness/vector.db` (SQLite) or alongside `.autoharness/memory.db`.
5. Implement selective recomputation: store lightweight text references and compute vectors on-the-fly or cache compact quantized hashes, saving up to 97% storage compared to dense float matrices.
6. Provide fast top-K nearest neighbor search by cosine distance.

## How to verify
1. Test semantic similarity: queries with synonym or paraphrased intent (e.g. "remove cycles from task graph") must rank topological sorter documents highly even without exact keyword matches.
2. Test vector normalization: verify dot product of identical vectors equals 1.0, orthogonal vectors equals 0.0.
3. Test storage footprint: verify database size remains compact (< 100 KB for typical projects).

## Facts found here
- LEANN architecture (MLsys 2026 Best Paper: arxiv.org/abs/2506.08276) achieves 97% savings by storing graph representations and selective recomputation rather than dense uncompressed matrices.
