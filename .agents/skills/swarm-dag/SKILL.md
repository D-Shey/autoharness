---
name: swarm-dag
description: Topological DAG scheduling and dependency resolution for multi-agent workflows
---

# swarm-dag

## When to use
Use when a complex task or epic consists of multiple interrelated subtasks where some subtasks can run in parallel, while others require predecessor outputs or verification gates before starting.

## Steps
1. Parse the workflow plan (JSON/YAML) containing nodes with `id`, `name`, `dependencies` (or `depends_on`), `role`, and `command` or `action`.
2. Construct adjacency list and in-degree maps.
3. Validate acyclicity using Kahn's algorithm or depth-first search cycle detection. Reject execution if circular dependencies exist.
4. Compute topological execution tiers (generations):
   - Tier 0: nodes with in-degree 0 (no prerequisites).
   - Tier N+1: nodes whose prerequisites are strictly satisfied by Tier <= N.
5. Execute nodes within the current tier concurrently using a worker pool (`concurrent.futures.ThreadPoolExecutor` or `asyncio`).
6. Enforce barrier synchronization: wait for all nodes in the current tier to reach terminal status (`completed` or `failed`) before dispatching dependent nodes.
7. If any prerequisite node fails, mark downstream dependent nodes as `blocked`/`skipped`.
8. Persist graph execution state and per-node output artifacts to `.autoharness/swarm/<plan_id>.json`.

## How to verify
1. Test acyclic linear graph (A -> B -> C): verify sequential execution order.
2. Test branching parallel graph (A -> [B, C] -> D): verify B and C run concurrently after A finishes, and D waits for both B and C.
3. Test cycle detection (A -> B -> A): ensure ValueError or CycleDetectedError is raised with cycle path.
4. Verify graceful handling of failure: if B fails, D is blocked while independent tasks finish.

## Facts found here
- Standard library `graphlib.TopologicalSorter` is built into Python 3.9+, enabling zero-dependency topological sorting.
