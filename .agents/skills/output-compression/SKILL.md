---
name: output-compression
description: Intelligent cascading output compression for test, diff, build and log streams
---

# output-compression

## When to use
Use whenever tool or command execution output (pytest, compiler, test runner, git diff, npm/cargo) produces hundreds or thousands of lines that would pollute the agent's context window. Grounded in dean0x/skim, this skill compresses raw logs by 70-95% while isolating critical failures.

## Steps
1. **Identify Log Stream Format**:
   Detect whether the input stream is `pytest`, `unittest`, `git_diff`, `traceback`, or `compiler`.
2. **Skeleton & Failure Extraction**:
   - For test suites: fold passing dots (`...`), retain failed test names, exception types, and traceback lines.
   - For git diffs: summarize file changes, fold long context lines, keep active diff hunks.
   - For compiler/build logs: filter for `error:` and `warning:` lines with file coordinates.
3. **Compute Compression Metrics**:
   - Measure original characters/lines vs compressed characters/lines.
   - Calculate compression ratio: `1.0 - (compressed_len / raw_len)`.
4. **Deliver Cascading Representation**:
   - Save full uncompressed log to persistent storage (`.autoharness/runs/...` or scratch).
   - Return clean folded skeleton to agent / user with compression stats.

## How to verify
1. Run `pytest tests/test_output_compression.py`.
2. Execute `autoharness skim <file_or_command>` and check compression ratio.
3. Verify `harness_compress_output` in Gateway MCP.

## Facts found here
- Compressing test and build output typically achieves 75-92% token reduction, preserving critical context without blowing up model prompt limits.
