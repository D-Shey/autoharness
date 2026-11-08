---
name: regression-sentinel
description: Pre-flight and post-flight test suite baseline verification with automatic rollback on test failures
---

# regression-sentinel

## When to use
Use during autonomous or batch task execution loops to protect codebase integrity by catching test regressions immediately and reverting destructive modifications.

## Steps
1. Capture pre-flight baseline:
   - Check clean git working tree state or save SHA/stash.
   - Run baseline test suite command (or fast smoke test).
2. During task execution:
   - Allow modifications within task workspace boundary.
3. Post-flight verification:
   - Run full regression test suite.
   - Compare exit code and failure logs.
4. If regression is detected:
   - Perform atomic rollback (`git restore` or tree restoration).
   - Log the failure reason into `.autoharness/drive_state.json`.
   - Prevent downstream corrupted state from propagating.
5. If verification succeeds:
   - Approve state for commit and milestone completion.

## How to verify
1. Test clean state approval: run verification on passing code, verify status passes.
2. Test regression rejection: introduce an intentional syntax error or failing assertion, verify sentinel triggers rollback and returns non-zero.

## Facts found here
- Automated safety rollbacks eliminate the need for manual intervention during autonomous overnight runs.
