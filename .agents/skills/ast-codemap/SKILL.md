---
name: ast-codemap
description: AST Code Map generation and structural code search across Python, JavaScript, TypeScript, and Java using Tree-sitter and stdlib ast
---

# ast-codemap

## When to use
Use when an agent needs to inspect, navigate or search the structure of a Python, JavaScript, TypeScript, or Java codebase without reading full raw files into the context window, reducing token usage by 80-90%.

## Steps
1. Parse the target source files or directory recursively using `parse_source_file` (Python `ast` for `.py`, Tree-sitter for `.js`, `.ts`, `.tsx`, `.java`).
2. Extract definitions: top-level classes, interfaces, methods, functions, async functions, decorators, parameters, return types, docstrings, and imports.
3. Compute summary statistics: lines of code, symbol hierarchy, and module count.
4. Format into compact indented tree or JSON output for LLM consumption via `format_codemap_tree` or `search_codemap`.
5. In Gateway, expose through `harness_codemap` or Code Mode scripting.

## How to verify
Run pytest on `tests/test_memory_codemap.py` and verify `generate_codemap` correctly extracts class and function hierarchies across Python, JS, TS, and Java.
