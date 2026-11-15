---
name: rollback-guard
description: Automated git and filesystem rollback guard with atomic state recovery. Use when executing risky modifications, self-refinement iterations, or autonomous driver loops to automatically snapshot clean baselines and restore them on test or verification failure.
---

# Rollback Guard

## When to use
Use before initiating risky automated code changes, multi-agent autonomous iterations, or self-refinement loops to snapshot clean workspace state and restore it immediately if post-flight tests, AST checks, or syntax validations fail.

## Steps
1. **Pre-flight Snapshot**:
   - Verify git status / clean tree state.
   - Record HEAD commit hash and working tree state SHA256 (`evidence.tree_state`).
   - Create lightweight filesystem or stash snapshot in `.autoharness/snapshots/<id>/`.
2. **Execute Changes**:
   - Perform code modifications, agent tasks, or refactoring.
3. **Post-flight Verification**:
   - Run test suite (e.g. pytest).
   - Run AST Anti-Shim sentinel to ensure no empty stubs or mocks were introduced.
   - Check blast radius and syntax validity.
4. **Automated Rollback on Failure**:
   - If tests or AST checks fail, immediately invoke `rollback()`.
   - Restore modified tracked files (`git restore .`) and remove untracked artifacts (`git clean -fd` or snapshot overlay).
   - Log the regression failure and revert status to `.autoharness/sentinel.json`.
5. **Success Confirmation**:
   - If verification passes, finalize snapshot as verified baseline.

## How to verify
1. Test clean snapshot capture: verify snapshot file is created with valid tree hash.
2. Test rollback recovery: introduce broken code or stub in workspace, trigger rollback, verify file is restored to pre-flight snapshot.

## Facts found here
- Combining `git restore` with untracked artifact cleaning provides deterministic zero-leakage rollback across Windows and Unix platforms.
- Pre-flight test baselines prevent attributing pre-existing broken tests to current task changes.
