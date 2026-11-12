---
name: code-frugality
description: Code minimalism, radical YAGNI, and anti-bloat practices based on the 7-step decision ladder. Use when writing new code, refactoring, fixing bugs, or reviewing changes to keep diffs minimal, eliminate unnecessary abstractions and dependencies, and ensure tests cover risky logic.
---

# code-frugality

## When to use
Use this skill whenever:
- Designing or implementing any code change (feature, bugfix, refactoring).
- The temptation arises to introduce new dependencies, wrapper classes, generic abstractions, or speculative configuration options.
- Reviewing code for bloat, unnecessary complexity, or over-engineering.
- The user requests a "simple solution", "minimal change", "YAGNI", or complains about bloated diffs.

## Steps

### 1. Pre-Implementation Scoping
- Inspect the callers, tests, fixtures, and configuration touched by the change before writing code.
- Understand the exact contract required by the task; identify what data or behaviors cannot be broken.
- Separate true scope from speculative additions: extra options, premature generalizations, and configurable flags nobody asked for are out of scope.

### 2. The 7-Step Resolution Ladder
Before adding new classes or functions, stop at the first rung that fully satisfies the task:
1. **Does it need to exist?** If a feature or parameter was not requested, omit it. Vague requests get the simplest implementation fulfilling the core requirement.
2. **Already in this codebase?** Search existing utilities, helpers, and patterns across the repository. Reuse existing code conventions and abstractions rather than inventing new ones.
3. **Standard library or runtime native?** Check standard library modules and platform primitives. Standard library constructs beat external libraries.
4. **Platform/environment capability?** Leverage native platform features (e.g. native browser controls, OS signals, standard stream piping).
5. **Already installed dependency?** If standard library does not suffice, check existing dependencies in `pyproject.toml` or `package.json`. Never add a new dependency for a few lines of logic.
6. **Can it be a clear single line?** If a straightforward, readable one-liner solves the problem, use it without extra helper wrappers.
7. **Minimum viable implementation:** Write the smallest possible correct diff. Non-trivial logic (branches, parsers, security, money) must leave at least one focused test or assertion.

### 3. Deliberate Compromises and Debt Marking
- When an intentional simplification or ceiling is chosen, document it directly in code:
  `# shortcut: <the limit/ceiling>, <when/trigger to upgrade>`
- Never leave a shortcut without an explicit upgrade trigger (prevent silent rot).

### 4. Non-Negotiable Invariants
Never sacrifice the following for the sake of smaller diffs:
- Validation at trust boundaries.
- Error handling that prevents data loss or silent failures.
- Security checks and cryptographic sanity.
- Accessibility standards.

## How to verify
1. **Line Count & Lean Check:** Run code review focusing on deletion over addition. Ask: *Can any line in this diff be deleted without breaking tests or the spec?*
2. **No Unrequested Dependencies:** Verify `git diff` contains no newly added package dependencies unless explicitly requested.
3. **Edge Case Test:** Ensure any new non-trivial branch or parsing logic has a passing test that fails if the behavior is removed.
4. **Shortcut Verification:** Scan for `# shortcut:` comments and confirm every instance specifies both its operational ceiling and an upgrade condition.

## Facts found here
- Ponytail benchmark evidence: applying the decision ladder reduces code volume by ~50% and output tokens by ~45% while increasing test coverage on risky logic to 98%.
- Python standard library (`pathlib`, `re`, `json`, `dataclasses`, `urllib`) eliminates the need for 90% of auxiliary utility libraries in CLI tools.
