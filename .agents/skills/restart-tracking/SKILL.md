---
name: restart-tracking
description: Track task requested level and past lightening raises across runs
---

# Task restart level tracking

## When to use

Use when determining the minimum restart mode for a repeated task name, ensuring chores started light do not permanently escalate to full mode.

## Steps

1. Iterate historical task records matching the same task text.
2. For each historical task, inspect its `requested` level and whether its `mode` was raised solely by the lightening rule (`light raised to full` in `harness.md`).
3. If an earlier task's level was raised solely by the lightening rule and was not requested as `full` or `reviewed`, its effective level for restarting is `light`.
4. Allow subsequent runs of routine chores to start in light mode when the lightening rule no longer applies or when routine skills are available.
5. If the earlier task was explicitly requested as `full` or `reviewed`, preserve that level as the restart floor.

## How to verify

Run unit tests confirming that an earlier task that was lightened to full can be restarted in light mode if requested.
