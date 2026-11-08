---
name: ast-codemap
description: AST Code Map generation and structural code search using Python standard library ast
---

# ast-codemap

## When to use
Use when an agent needs to inspect, navigate or search the structure of a Python codebase without reading full raw files into the context window, reducing token usage by 80-90%.

## Steps
1. Parse the target Python files or directory recursively using `ast.parse`.
2. Extract definitions: top-level classes, methods, functions, async functions, decorators, parameters, docstrings, and module-level imports.
3. Compute summary statistics: lines of code, symbol hierarchy, and call dependencies.
4. Format into compact indented tree or JSON output for LLM consumption.
5. In Gateway, expose through `harness_codemap` or Code Mode scripting.

## How to verify
Run pytest on `tests/test_memory_codemap.py` and verify `generate_codemap` correctly extracts class and function hierarchies.
