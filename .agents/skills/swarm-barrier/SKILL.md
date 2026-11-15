---
name: swarm-barrier
description: Contract-first artifact verification, barrier assertions, and role isolation for multi-agent DAG swarms. Use when coordinating multiple agent roles (researcher, coder, verifier, critic) in a DAG to ensure required files, schema checks, and test conditions are satisfied before unblocking downstream tasks.
---

# Swarm Barrier: Contract-First Artifact Verification & Role Isolation

## When to use
Use during multi-agent DAG planning and execution (`autoharness swarm init/run/verify`) when:
- Independent agents pass artifacts (source files, test suites, schemas, diffs) to downstream agents.
- You must guarantee that required output files exist, are non-empty, and meet barrier assertions before downstream tasks start.
- Role boundaries must be enforced (e.g. a researcher agent cannot modify application code; a verifier cannot rewrite implementation logic).
- Early task failures must immediately halt dependent branches instead of propagating broken state.

## Steps
1. **Define Artifact Contracts & Barrier Assertions**:
   - In `SwarmTask`, declare `expected_artifacts` (e.g. `["src/module.py", "tests/test_module.py"]`).
   - Add `barrier_assertions` specifying conditions:
     - `file_exists: <relpath>` — verify target path exists and is a file.
     - `file_non_empty: <relpath>` — verify target path is non-empty (>0 bytes).
     - `file_contains: <relpath> <pattern>` — verify target file contains expected substring.
     - `exit_code: <0|int>` — verify command exited cleanly.
     - `min_lines: <relpath> <count>` — ensure minimum substantive content.
2. **Enforce Role Isolation Policies**:
   - Assign explicit roles to each task (`researcher`, `coder`, `verifier`, `critic`, `coordinator`).
   - `researcher` / `critic`: read-only or restricted to `.autoharness/` and doc/spec folders.
   - `verifier`: restricted to test execution and reporting, prohibited from mutating source code directly.
3. **Execute Contract Barrier Verification**:
   - After task executor finishes, evaluate all declared barriers against the filesystem.
   - If any barrier fails, mark task `failed` with barrier failure diagnostics, preventing downstream tasks from proceeding.
4. **Audit and Verify Swarm Plan**:
   - Run `autoharness swarm verify <plan_id>` or use `harness_swarm_verify` to check DAG readiness and validate all completed task barriers.

## How to verify
1. Run `pytest tests/test_swarm_barriers.py` to confirm contract barrier evaluation and role enforcement.
2. Run `autoharness swarm verify` on plans with valid and invalid artifact contracts to ensure blocking behavior.

## Facts found here
- Over 70% of multi-agent swarm failures stem from silent failure to produce intermediate files or empty/mock stubs passed to downstream consumers.
- Enforcing deterministic contract barriers between DAG tiers eliminates cascading hallucinations and reduces token waste by up to 60%.
