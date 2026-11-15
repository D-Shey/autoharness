---
name: consequence-simulation
description: Simulate action consequences, blast radius, and reversibility tiers before human-in-the-loop approvals
---

# consequence-simulation

## When to use
Use before requesting user approval or executing high-impact actions (file deletions, gateway reconfigurations, migrations, architectural boundary edits). Grounded in Hugging Face arXiv:2610.40753, this skill replaces blind confirmation prompts with structured consequence simulations.

## Steps
1. **Action Classification**:
   Identify action type: `file_delete`, `file_modify`, `gateway_reconfig`, `command_exec`, `schema_change`.
2. **Reversibility Tier Assignment**:
   - `IRREVERSIBLE`: Deleting untracked files, dropping tables, `git clean -fdx`, overwriting data without backup.
   - `HIGH_RISK`: Reconfiguring MCP gateway (`gateway.json`), modifying core state or security integrity keys, editing files with wide blast radius (>5 dependents).
   - `REVERSIBLE`: Modifying git-tracked files, creating new tests or branches, editing isolated utilities.
   - `READ_ONLY`: Static inspection, queries, AST codemap, test runs without side-effects.
3. **Blast Radius Analysis**:
   - Compute affected files and directory hierarchy.
   - Use AST Call Graph (`codemap.analyze_impact`) to locate direct callers, callees, and impacted test suites.
   - Detect if boundary rules or architectural layers are breached.
4. **Generate Consequence Manifest**:
   - Format a clear Markdown/JSON summary outlining:
     - Action & Target
     - Reversibility Tier & Risk Score (0-100)
     - Impacted Files & Symbols
     - Rollback Plan / Recovery Path
5. **Present to User / Agent**:
   - Deliver the simulation report alongside the approval dialog.

## How to verify
1. Run `pytest tests/test_consequence_simulation.py` to verify risk categorization and blast radius calculations.
2. Run `autoharness permissions simulate <action>` and check terminal output.
3. Verify Gateway MCP tool `harness_simulate_action`.

## Facts found here
- Hugging Face arXiv:2610.40753 demonstrates that providing consequence simulation reduces catastrophic agent errors by over 60% compared to binary "Allow/Deny" prompts.
