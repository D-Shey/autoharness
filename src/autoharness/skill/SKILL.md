---
name: autoharness
description: Use at the start of any non-trivial task, even when many tools are already available, especially work that must match something real (a film scene, a design, a product, a brand, an API) and so needs references, current docs or data you have not looked at yet, or a way to check the result against them. Also use whenever you are blocked because a tool, input or know-how is missing (not because you need to think harder). Works out what a good result is and what is missing, finds and vets MCP servers and skills against the user's constraints, installs them for the task, and at the end keeps them in the project or removes them (configurable).
---

# AutoHarness

You start almost bare. Instead of expecting a pre-built harness, you build one
for this task and use it; when the task ends it stays in the project or is
taken down, as configured. The `autoharness` CLI does the bookkeeping
(lockfiles, trust checks, teardown); you do the thinking.

Every command prints JSON. Exit code `3` means **stop and ask the user**.

If `autoharness install-hook` was run, a hook sends every substantial request of a
session here first: tools that do work stay blocked until you have run `init` and
recorded a `brief` (step 3), while reading files stays open. Skip it only when the user
explicitly said so: `autoharness waive <session id> --reason "<their words>"`, then tell them.

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

The output has keyword `suggested` capabilities, matching `recipes`, the full
catalog in `all_capabilities`, and `discovered`: skills from skills.sh and servers
from the MCP registry that `init` already searched for this task. Treat the suggestions as hints: they come from
keywords and can miss or over-match. Decide yourself which capabilities the task really needs and
write them down as a short list: *capability — why — how I will verify it works*.
Pick the smallest set that lets you **verify** the result, not only produce it
(a web task needs a browser to check the page, not just an editor).

Before you list tools, answer three questions in writing:

1. **What does a good result look like, and how will I tell?** Name the quality bar
   and the check, not just "it renders" or "it runs".
2. **What missing input do I need?** The subject you must match (a film still, a
   product photo, a design, an API's current docs, sample data), a spec, examples.
   Working from memory when the real thing can be looked up is a capability gap,
   often the biggest one. Plan to get the input first, before you produce anything.
3. **What are the user's constraints?** Write them down verbatim ("no external
   generators", "no new dependencies", "must run offline"). Every candidate you
   discover is checked against them in step 3.

Missing input and know-how are capabilities too: a way to see references, a way to
compare your output with them. They often need no install at all: a web search
tool, a browser or a headless render you already have. Say how you will cover them.

If a recipe scores above ~0.3 and fits, `autoharness recipe show <name>` and
reuse its entries rather than searching again.

### 2. Discover (never skip)

Looking is cheap; missing a skill that already solves the task is expensive. Read
every entry in `discovered` before deciding anything. Then, for each capability on
your list, and with your own words for the task where `discovered` falls short:

```bash
autoharness need <capability> --reason "<why>"      # curated candidates first
autoharness search mcp "<query>"                     # official MCP registry
autoharness search skill "<query>"                   # skills.sh
```

`need` also lists skills you or earlier agents wrote and saved (`local`); install one with
`autoharness add skill local:<name>`. Prefer, in order: local skill → curated entry →
high-trust registry/skill result → nothing.
"Everything I need is already at hand" is a conclusion you reach after looking, not
a reason to skip looking. Then install only what beats what you already have: a
capability you can cover with tools you already have (shell, a CLI such as `gh`,
`psql`, `npx playwright`) does not need an MCP server.

### 3. Brief

Before producing anything, write down what you decided:

```bash
autoharness brief --goal "<quality bar>" --inputs "<missing inputs and how you get them>" \
  --constraints "<the user's constraints, verbatim>" --verify "<checks, against what>" \
  --candidates "<name>: take|skip, why; <name>: ...; searched: <queries>" \
  --gaps "create <skill-name>: <capability>; ..."   # or: "none: <why tools at hand cover it>"
```

Give a verdict for every discovered candidate that is plausibly relevant (skip the
obvious noise in one phrase). Tools that do work are blocked until the brief exists.
Show the user the brief in a few lines when you start.

**No fitting skill? Create it now.** For every capability that no found skill covers
and that takes real know-how (a procedure, a verification trick, domain rules), plan
a skill in `--gaps` as `create <skill-name>: <capability>`. Use `none:` only when
plain tools you already have cover everything. Then, before producing the result:

1. `autoharness skill new <skill-name> --description "<when an agent should use it>"`.
   For how to write a good skill, `anthropics/skills@skill-creator` is the curated guide
   (`autoharness need skill-authoring`); a skill-writing skill you already have works too.
2. Fill in When to use / Steps / How to verify from what you found: the references,
   current docs, the user's constraints, the checks from your brief. Write it for any
   project: no absolute paths, secrets, or names of people or clients.
3. Follow it for this task, step by step, and update it whenever you learn something
   (a step that did not work, a better check).

Teardown refuses while a planned skill is missing or still an unfilled template.

### 4. Vet and install

```bash
autoharness inspect mcp <name>
autoharness add mcp <name>
autoharness add skill <owner/repo@skill>
```

- Check each candidate against the user's constraints before you propose it. Read
  what it actually does (for a skill, its SKILL.md and scripts; `inspect` for a
  server). A skill whose main content breaks a constraint (for example SVG
  generator scripts when the user said "no generators") is not a candidate, even
  if its name fits. If you find this out only after installing, `autoharness remove`
  it and say so.
- A low-trust skill that brings little is worse than no skill. When the know-how is
  thin (an SVG primer, a generic checklist), skip it and write down what you know.
- If `add` exits `3` (`approval_required`), show the user the name, trust
  score, reasons and any secrets it needs, in one short message. Re-run with
  `--approve` only after a clear yes. Never add `--approve` on your own.
- If a skill comes back `rejected`, its files matched dangerous patterns
  (credential reads, `curl | sh`, hiding actions from the user). Pick another
  one. Use `--force` only if the user read the findings and insists.
- Never paste secrets into commands or files. When a server needs one, tell the
  user which env var to set.
- If `add mcp` answers `via: gateway`, the server works right away: call
  `harness_tools` to see it, then `harness_call(server, tool, arguments)`, or the
  native `<server>__<tool>` tools if your client shows them (Claude Code lists them as
  deferred tools: load them with tool search first). No restart.
- Otherwise new MCP servers load at session start. They are written to `{{MCP_FILE}}`.
  After adding one, tell the user to {{RELOAD}}, then continue the parts of
  the task that don't depend on it. If `add` suggests `autoharness gateway install`,
  offer it to the user once: one last restart, and later servers need none.

### 5. Smoke-test

Before relying on a new capability, make one cheap call that proves it works
(open a page, list a schema, read one doc). If it fails, `autoharness remove`
it and try the next candidate. Do not debug a third-party tool for long.

### 6. Execute, and notice capability gaps

Work on the task normally. When you get stuck, ask: *would more thinking fix
this, or am I missing a capability?* Signs of a real gap: you cannot observe
the thing you need to check (a UI, network traffic, a database, logs), or you
keep guessing at an API whose current docs you cannot see, or you are drawing,
designing or matching something you have not looked at (no reference in front of you). Then go back to
step 2 with `autoharness need <capability> --reason "<what blocked me>"`, and
resume from where you were.

### 7. Finish

1. Verify the result with the capabilities you installed. Run the checks; don't assume them.
   Verify against the quality bar from your brief, not only that it works. When there is a
   reference, put your output and the reference side by side, list the biggest
   differences (scale, composition, angle, palette, likeness, missing elements), fix
   them, and repeat. One render is not a review: plan several rounds and stop when the
   remaining differences are small or cannot be fixed with the chosen medium. Tell
   the user which ones remain.
2. Save the skills you created: bring each one up to date with what the task taught
   you, then `autoharness skill save <name>`. If you learned something else reusable that
   no skill covers (how to build or check this kind of project, an API quirk), write it
   down the same way with `autoharness skill new`. Save answers exit code 3: show the user
   the name, description and warnings, and re-run with `--approve` only after a clear
   yes. If save reports `blocked`, remove what it flags.
3. End the task. If the setup was useful, save it as a recipe too:
   `autoharness teardown --save-recipe <short-name> --notes "<what it was good for>"`
   Otherwise `autoharness teardown`. If teardown warns that discovery or the brief
   was skipped, tell the user.
   What happens to the harness follows the `teardown` setting
   (`autoharness config get`): `keep` (the default) leaves it installed in the
   project for later tasks, `remove` uninstalls it. Pass `--keep` or `--remove`
   only when the user asked for that.
4. Tell the user what you installed, what stayed in the project and what was
   removed, and anything left for them to do (such as unsetting env vars or
   restarting to unload MCP servers). Kept items come off with
   `autoharness remove mcp|skill <name>`, or all at once with `autoharness prune`.

If `add` answers `already_installed`, an earlier task kept that item: use it.
`autoharness status` shows the current harness, what the project keeps, and the log.
