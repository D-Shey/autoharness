---
name: context-pruning
description: Layered review folding and active context pruning in AutoHarness
---

# context-pruning

## When to use
Use when managing context window usage in multi-round agent sessions:
- Folding resolved review rounds into compact summaries instead of keeping entire prompt histories in context.
- Organizing session history into tiered LSM layers: Capture (full log on disk), Distill (key verified facts), and Condense (final decisions).
- Keeping prefix prompt caching intact while pruning intermediate noisy steps.

## Steps
1. **Tiered context organization**:
   - Save full stdout/stderr of runs in `.autoharness/tasks/<id>/runs/` and reviews in `answers/` (Capture tier).
   - In active conversation memory, maintain only the latest diff and the current round's checklist (Distill tier).
   - Citing methodology: Active Context Pruning (https://github.com/ranxianglei/billion-context).
2. **Review round folding**:
   - When a critic finding is resolved in a subsequent round, collapse previous round points into a one-line summary:
     `Round N settled: <issue> resolved, verified by run #M`.
   - Keep log file references (`runs/*.log`, `answers/*.txt`) on disk so any detail remains reversible.
3. **Prefix caching preservation**:
   - Keep tool definitions, static instructions, and system configuration at the start of the prompt.
   - Place dynamic data (latest diff, latest review) in the suffix so the prefix remains cacheable by model providers.

## How to verify
1. Verify that folded reviews maintain accurate references to log files on disk.
2. Verify that token consumption remains bounded across multiple rounds.

## Facts found here
- AutoHarness stores task state in `.autoharness/tasks/<id>/`.
- Reviews are signed with one-time ANSWER-IDs in `answers/`.
