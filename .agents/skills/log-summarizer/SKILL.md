---
name: log-summarizer
description: Extract failure tracebacks, error summaries and folded log skeletons
---

# log-summarizer

## When to use
Use to extract concise actionable error summaries and tracebacks from long logs or diagnostic dumps, isolating the root error message, source line, and exception type.

## Steps
1. Parse log stream line by line.
2. Locate error anchors: `Traceback (most recent call last):`, `FAILURES`, `AssertionError`, `Error:`, `Exception:`.
3. Extract source context (file, line number, offending expression).
4. Collapse repetitive progress markers, timestamp prefixes, and verbose info logging.
5. Provide a compact 5-15 line diagnostic skeleton.

## How to verify
1. Run `pytest tests/test_output_compression.py -k test_traceback_extraction`.
2. Verify that non-error context lines are collapsed and root exceptions are preserved.

## Facts found here
- Extracting only traceback anchors and test failure blocks reduces typical 1,000+ line logs to under 30 lines (97% reduction).
