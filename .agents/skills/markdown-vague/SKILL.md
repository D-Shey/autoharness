---
name: markdown-vague
description: Markdown parsing skipping fenced code blocks in step validation
---

# markdown-vague

## When to use
Use when extracting lists, steps, and procedures from Markdown documents while ignoring nested or illustrative code snippets.

## Steps
1. Detect fenced code blocks in Markdown:
   - Identify triple backtick (` ``` `) or triple tilde (` ~~~ `) delimiters with optional language tags.
   - Maintain a boolean `in_fence` state across line iterations.
2. In list item extraction (`vague.items`):
   - When encountering a fence delimiter, toggle `in_fence` and reset any current item accumulator.
   - While `in_fence` is true, ignore list item markers (`-`, `*`, `+`, `1.`) so code lines are never treated as procedural steps.
3. Validate steps outside code blocks:
   - Run vague step heuristics (`vague_step`) only on genuine procedural items.

## How to verify
1. Run pytest testing `vague.vague_steps` on Markdown containing fenced code blocks with dash lines (e.g. `- do it properly`).
2. Verify that no false positive warnings or trailing fence markers are produced.

## Facts found here
- Standard Markdown fenced code blocks start and end with at least three consecutive backticks or tildes at line start (source: https://spec.commonmark.org/0.31.2/#fenced-code-blocks).
- Lines inside code blocks frequently use dash prefixes for CLI flags, bullet comments, or diffs that must not be parsed as CommonMark list items.
