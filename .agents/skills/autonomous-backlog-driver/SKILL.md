---
name: autonomous-backlog-driver
description: Continuous task execution loop over followups with regression rollback guards and safety budgets
---

# autonomous-backlog-driver

## When to use
Use when running unattended or semi-autonomous development cycles that iteratively work down technical debt, open followups, and bug backlogs without manual task-by-task prompting.

## Steps
1. Load pending followups from `.autoharness/followups.json` where `status == "open"`.
2. Prioritize items based on explicit order or creation timestamp.
3. Establish a safety budget:
   - Maximum tasks per drive session (default: 3).
   - Maximum execution time per task (timeout guard).
   - Test regression sentinel: run baseline verification before task execution.
4. For each selected item:
   - Create or assign task plan.
   - Execute task implementation.
   - Run verification checks.
   - If tests fail, invoke rollback to baseline tree state and record a ruling.
   - If tests pass and critic accepts, mark followup as done, record lesson, and commit changes.
5. Persist driver session state in `.autoharness/drive_state.json`.

## How to verify
1. Run driver in `--dry-run` mode: verify candidate selection order without touching files.
2. Run driver with `--max-tasks 1`: verify single task processing, state update, and graceful termination.
3. Verify regression guard: simulate a failing test, confirm immediate rollback of changes.

## Facts found here
- Outer loop automation requires atomic state persistence and hard budget limits to prevent runaway resource consumption.
