---
name: autoharness
description: Use at the start of any non-trivial task in a bare agent setup, and whenever you are blocked because a tool or know-how is missing (not because you need to think harder). Figures out which capabilities the task needs, finds and vets MCP servers and skills, installs them for this task only, and removes them afterwards.
---

# AutoHarness

You start almost bare. Instead of expecting a pre-built harness, you build one
for this task, use it, and take it down afterwards. The `autoharness` CLI does
the bookkeeping (lockfiles, trust checks, teardown); you do the thinking.

Every command prints JSON. Exit code `3` means **stop and ask the user**.

## Loop

```
analyse task → capabilities → reuse recipe? → discover → vet → install → verify
      ↑                                                                │
      └──────── capability gap during execution ◄──── execute ◄────────┘
                                                        │
                                           done → verify → recipe → teardown
```

### 1. Analyse and start

```bash
autoharness --agent {{AGENT}} init "<the user's task, verbatim>"
```

The output has keyword `suggested` capabilities and matching `recipes`. Treat
them as hints. Decide yourself which capabilities the task really needs and
write them down as a short list: *capability — why — how I will verify it works*.
Pick the smallest set that lets you **verify** the result, not only produce it
(a web task needs a browser to check the page, not just an editor).

If a recipe scores above ~0.3 and fits, `autoharness recipe show <name>` and
reuse its entries rather than searching again.

### 2. Discover

For each capability:

```bash
autoharness need <capability> --reason "<why>"      # curated candidates first
autoharness search mcp "<query>"                     # official MCP registry
autoharness search skill "<query>"                   # skills.sh
```

Prefer, in order: curated entry → high-trust registry/skill result → nothing.
A capability you can cover with tools you already have (shell, a CLI such as
`gh`, `psql`, `npx playwright`) does not need an MCP server. Do not install
something just because it exists.

### 3. Vet and install

```bash
autoharness inspect mcp <name>
autoharness add mcp <name>
autoharness add skill <owner/repo@skill>
```

- If `add` exits `3` (`approval_required`), show the user the name, trust
  score, reasons and any secrets it needs, in one short message. Re-run with
  `--approve` only after a clear yes. Never add `--approve` on your own.
- If a skill comes back `rejected`, its files matched dangerous patterns
  (credential reads, `curl | sh`, hiding actions from the user). Pick another
  one. Use `--force` only if the user read the findings and insists.
- Never paste secrets into commands or files. When a server needs one, tell the
  user which env var to set.
- New MCP servers load at session start. They are written to `{{MCP_FILE}}`.
  After adding one, tell the user to {{RELOAD}}, then continue the parts of
  the task that don't depend on it.

### 4. Smoke-test

Before relying on a new capability, make one cheap call that proves it works
(open a page, list a schema, read one doc). If it fails, `autoharness remove`
it and try the next candidate. Do not debug a third-party tool for long.

### 5. Execute, and notice capability gaps

Work on the task normally. When you get stuck, ask: *would more thinking fix
this, or am I missing a capability?* Signs of a real gap: you cannot observe
the thing you need to check (a UI, network traffic, a database, logs), or you
keep guessing at an API whose current docs you cannot see. Then go back to
step 2 with `autoharness need <capability> --reason "<what blocked me>"`, and
resume from where you were.

### 6. Finish

1. Verify the result with the capabilities you installed. Run the checks; don't assume them.
2. End the task. If the setup was useful, save it as a recipe too:
   `autoharness teardown --save-recipe <short-name> --notes "<what it was good for>"`
   Otherwise `autoharness teardown`.
   What happens to the harness follows the `teardown` setting
   (`autoharness config get`): `keep` (the default) leaves it installed in the
   project for later tasks, `remove` uninstalls it. Pass `--keep` or `--remove`
   only when the user asked for that.
3. Tell the user what you installed, what stayed in the project and what was
   removed, and anything left for them to do (such as unsetting env vars or
   restarting to unload MCP servers). Kept items come off with
   `autoharness remove mcp|skill <name>`, or all at once with `autoharness prune`.

If `add` answers `already_installed`, an earlier task kept that item: use it.
`autoharness status` shows the current harness, what the project keeps, and the log.
