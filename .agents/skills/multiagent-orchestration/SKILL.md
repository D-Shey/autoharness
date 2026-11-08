---
name: multiagent-orchestration
description: Role-based multi-agent coordination with isolated subagents and gated milestone checks
---

# multiagent-orchestration

## When to use
Use when dividing work among multiple specialized agent roles (e.g. Architect, Researcher, Worker/Coder, Verifier, Critic) to prevent single-context bloat and enforce independent review gates.

## Steps
1. Define specialized roles for subtasks:
   - `researcher`: reads codebase and external docs, generates specification or evidence.
   - `coder`: implements modifications according to specification.
   - `verifier`: executes tests and checks invariants.
   - `critic`: independent evaluation without author bias.
2. Isolate agent workspace context:
   - Run subagents with independent context windows to prevent token exhaustion and distraction.
   - Communicate between agents strictly via structured inputs/outputs (JSON artifacts or memory records).
3. Connect subagents to shared Gateway memory:
   - Index intermediate results in `.autoharness/memory.db` so subsequent agents can retrieve them using BM25 query.
4. Enforce Hard Verification Gates:
   - Never mark a subagent task complete without automated verification pass (`autoharness verify`).
   - Escalate to arbitrator if a subtask fails verification multiple times.
5. Aggregate final results into an executive summary.

## How to verify
1. Run a multi-step task involving 2+ distinct roles.
2. Confirm each role receives only necessary inputs, not the entire conversation history.
3. Confirm that verification gate prevents invalid intermediate state from propagating to downstream workers.
4. Verify all role actions and artifacts are logged in `.autoharness/swarm/` and searchable in memory.

## Facts found here
- Role specialization prevents context degradation in multi-step workflows (as demonstrated by ByteDance Deer-Flow and Ruflo).
