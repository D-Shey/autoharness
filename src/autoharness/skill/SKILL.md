---
name: autoharness
description: Use at the start of any non-trivial task, even when many tools are already available, especially work that must match something real (a film scene, a design, a product, a brand, an API) and so needs references, current docs or data you have not looked at yet, or a way to check the result against them. Also use whenever you are blocked because a tool, input or know-how is missing (not because you need to think harder). Works out what a good result is and what is missing, finds and vets MCP servers and skills against the user's constraints, installs them for the task, and at the end keeps them in the project or removes them (configurable).
---

# AutoHarness

You start almost bare and build the harness this task needs: references, docs, tools and a way to
check the result. The `autoharness` CLI does the bookkeeping (lockfiles, trust checks, teardown); you
do the thinking. Every command prints JSON with a `next` field: **follow `next`**, it carries the
detailed procedure for the step you are in, so never cut a command's output short. Exit code `3`
means **stop and ask the user**. Long texts with quotes, brackets or `;` (a brief, a review, a
measurement) go in a JSON file instead of the command line: `--args-file <file.json>` with
`{"diff": "...", "worst": "minor"}`. Before the brief no file can be written, so pass the JSON on
stdin: `autoharness brief --args-file - <<'EOF'` ... `EOF` (Bash) or `@'` ... `'@ | autoharness
brief --args-file -` (PowerShell).

If the hook is installed, tools that do work stay blocked until `init` (light) or `init` and `brief`
(full). Reading files stays open. `.autoharness/` holds the task's records: only autoharness
commands write there (the records are sealed, and teardown refuses an edited one). If the hook
stops the end of your turn while your task is open, finish it, or say you are waiting on the
user and end the turn again. Skip the harness only when the user explicitly said so:
`autoharness waive <session id> --reason "<their words>"`, then tell them.

A task belongs to the agent session that started it. If another session's task is active, you are
told about it and stay blocked: tell the user; only if they want you to carry it on (or that
session is gone), `autoharness claim <your session id> --reason "<their words>"`.

## 1. Start at the right level

```bash
autoharness --agent {{AGENT}} init "<the user's task, verbatim>" --mode light|full|reviewed|auto
```

- `light`: a chore that needs no capability (a typo, a git commit). No search, no brief: do the
  work, verify it, `teardown`. A task that needs any capability of the catalog (an API, a browser,
  tests, debugging, an image to match) is not a chore: `init` raises it to full.
- `full` (default): something the result must match or a way to check it is missing. Search,
  brief, vet, install.
- `reviewed`: full, and an independent critic must pass the result before teardown.
- `auto`: full when the task text matches a catalog capability, else light. Keyword matching
  misses both ways, so prefer choosing yourself.

Raise a running task instead of starting over: `autoharness mode full` when a capability gap
appears, `autoharness mode reviewed` when the result turns out to need an independent check.
If `init` warns that an installed copy of this skill is out of date, tell the user the
`install-skill` command it names.

In full mode, answer in writing before listing tools:

1. **What does a good result look like, and how will I tell?** A quality bar and a check, not
   "it renders".
2. **What input is missing?** The original the result must match, not someone's version of it (the film still, the product
   photo, the API's actual response, sample data), not a description of it. Working from memory
   when the real thing can be looked up is a capability gap, often the biggest. Get it first.
3. **What are the user's constraints?** Verbatim ("no new dependencies", "must run offline").
4. **What interface does the task name but leave open?** A function, command, file format or API
   that something else will call or read, without its exact form (argument order, names, types,
   output layout). The words the task does use are clues (it says "domain": take a domain, not
   your own `lower`/`upper`). Look for a real example first (docs, the usual package, callers in
   the code); without one, accept the common forms of the call and write the assumption into
   `--inputs`, so the critic checks it.

Then list capabilities: *capability — why — how I will verify it works*. Pick the smallest set
that lets you **verify** the result, not only produce it. Missing input and know-how often need
no install at all (web search, a browser, a headless render you already have).

## 2. Discover (full)

`init` already searched: read every entry in `discovered`, and reuse a matching recipe
(`autoharness recipe show <name>`, score above ~0.3). `rejected_before` names candidates earlier
tasks skipped or removed, with why: pass over them unless the reason does not apply. Where that falls short:

```bash
autoharness need <capability> --reason "<why>"   # curated + your saved local skills
autoharness search mcp|skill "<query>"            # MCP registry, skills.sh
```

Prefer local skill → curated entry → high-trust result → nothing. Install only what beats the
tools you have: a CLI (`gh`, `psql`, `npx playwright`) needs no MCP server.

**Prepare like an engineer, not head-on.** Building an aircraft takes aerodynamics, materials,
strength, control theory; every non-trivial task likewise splits into areas of know-how. List them
(`--domains`), and for each ask: what do I actually know, what is known out there (skills, servers,
docs, worked examples, algorithms), how will I test the hypothesis? Search each area
(`autoharness search skill|mcp "<query>"`, `autoharness need`), take what fits, and where nothing
fits create the skill yourself from what you read (`--gaps "create ..."`), filled before the first
result. "I know how" is not a cover: what you know goes into that skill.

**Research the method before you build.** Someone has usually solved this kind of task before:
search the web, the field's docs, papers, repos and tutorials for how it is done well (the
algorithm, the workflow, the known pitfalls), read the best two or three sources, and write the
steps down. This is deep research, not a single query: it is the difference between doing the
work from memory and doing it the way it is done well. The steps go into `--method`; reusable
know-how goes into a skill (`--gaps "create ..."`).

## 3. Brief (full)

```bash
autoharness brief --goal "<quality bar>" --inputs "<missing inputs, how you get them>" \
  --domains "<area>: skill|mcp|create <name> (searched: <query>); ..." \
  --method "<technique or steps>: <url>; ...; searched: <queries>" \
  --constraints "<verbatim>" --verify "<checks, against what>" \
  --candidates "<name>: take|skip, why; ...; searched: <queries>" \
  --gaps "create <skill-name>: <capability>; ..."   # or "none: <why tools at hand cover it>"
  # --fidelity "<level to judge at>" (required for work judged by eye: "stylised poster, not photographic")
  # optional: --review critic|pair (pair: + a code-quality reviewer)
  #           --preserve "<existing behaviour that must keep working>"
```

Number the goal's parts (`1 ...; 2 ...`): they become brief items B1, B2, ... (and P1, ... for
`--preserve`), which checks and the critic report on one by one.

Give a verdict for every plausible candidate. Show the user the brief in a few lines.
For browser, API (your own, or a third-party one: `api-client`), logs or database checks, `autoharness verify guide <kind>` has a template:
pass `--verify-template <kind>` instead of `--verify`. Visual work needs image references, at
least two results and a critic before teardown; `brief` lists what this task owes.

**No fitting skill for real know-how? Create it** (`--gaps "create ..."`):
`autoharness skill new <name> --description "<when to use>"`, fill When to use / Steps / How to
verify from what you found (no absolute paths, secrets or people's names) before producing the
result, follow it, and update it as you learn. Values you measured in this project (a threshold, a
mapping, a colour) go under "Facts found here", apart from the procedure: the next task re-checks
them. Teardown refuses while a planned skill is missing or unfilled.

## 4. Vet and install

```bash
autoharness inspect mcp <name>
autoharness add mcp <name>  |  autoharness add skill <owner/repo@skill>
autoharness decision mcp|skill <name> select|skip --reason "..."
```

- Read what a candidate actually does (a skill's SKILL.md and scripts; `inspect` for a server)
  and check it against the constraints. A skill whose main content breaks one is no candidate;
  if you find out after installing, `autoharness remove` it and say so.
- `vet_required`: you do not judge the fit yourself. `autoharness vet mcp|skill <name>`, give
  its prompt unchanged to a fresh subagent, save the answer unedited to a file, record it with
  `--answer-file <file>`. No subagents at hand: `autoharness vet mcp|skill <name> --run
  claude|codex|agy` starts the reviewer and records its answer. On `breaks`, pick another.
- `approval_required` (exit 3): show the user name, trust score, reasons, needed secrets and any
  `egress` reason in one message; `--approve` only after a clear yes, never on your own.
- `rejected`: dangerous patterns found; pick another (`--force` only if the user insists after
  reading the findings).
- Never paste secrets into commands or files: tell the user which env var to set.
- A low-trust skill that brings little is worse than none: write down what you know instead.
- `already_installed`: an earlier task kept it; use it.
- `via: gateway`: works now, use `harness_tools` / `harness_call`. Otherwise the server is in
  `{{MCP_FILE}}`: tell the user to {{RELOAD}}, and continue what does not depend on it. If `add`
  suggests `autoharness gateway install`, offer it once: later servers then need no restart.

Smoke-test each new capability with one cheap call before relying on it; if it fails,
`remove` it and try the next. Do not debug a third-party tool for long.

## 5. Execute

When stuck, ask: *would more thinking fix this, or am I missing a capability?* Not seeing the
thing you must check, guessing at an API whose docs you have not read, or drawing something
you have not looked at are gaps: `autoharness need <capability> --reason "<what blocked me>"`
(after `mode full` in a light task), then resume.

A browser tool that refuses `file://` (Playwright MCP does) opens a local page or file over
`http://localhost` instead: serve the folder (`python -m http.server 8000`) in the background.

Matching a reference (an API's response, a sample file, a layout, a picture, the old behaviour)?
Take its facts from the reference itself with a tool, not from memory: field names and values,
sizes, positions, timings, the exact output. Save them to a file and record it before the first
result (`autoharness evidence add <file> --kind facts`); the critic checks the result against them.
Build the first version as a block-in from these facts: layout, proportions and main structure,
no detail yet, and run the critic on it before adding detail. A wrong base costs every later round.

Found work outside this task (a bug next door, a critic point beyond the brief)? Do not fix it
now and do not drop it: `autoharness followup add "<what, where>"`. `init` shows open follow-ups;
close one a task settles with `autoharness followup done <id> --note "..."`.

## 6. Finish

1. **Verify against the quality bar and keep the evidence** (light mode: verify, then teardown).

   ```bash
   autoharness evidence add <file> --kind reference|facts|result   # the thing itself; the whole result, then close-ups
   autoharness evidence add <file> --kind result --closeup   # a close-up or part of the latest whole result
   autoharness verify run "<test command>" --for B1|preserve   # autoharness runs it: exit code decides
   autoharness evidence diff                                 # code: the whole working-tree diff
   autoharness review --by self --diff "1 <most important>; 2 ..." --worst blocker|major|minor|none
   autoharness critic                                        # prompt for an independent reviewer
   autoharness review --by critic --answer-file <file>
   autoharness critic --run claude|codex|agy                 # or: autoharness starts a read-only critic itself
   ```

   A check must be able to fail when its item is not met: one that only finds a name or a string in
   a file checks nothing, the critic says so and it stops counting for the item.
   A check that is a command goes through `verify run`, never "I ran it and it passed": the run is
   tied to the code it saw and goes stale when the code changes, so run it again after your last
   edit. With `--preserve` in the brief, teardown needs a passing `--for preserve` run. A command
   that cannot fail (`true`, `echo ok`, `python -c "pass"`) is not a check and does not count.
   When a check fails, fix the code, not the check. If the check itself was wrong, teardown shows
   that it changed after it failed, so tell the user why. A command
   with quotes (PowerShell mangles them) goes in a JSON file: `{"command": "..."}`, then
   `autoharness verify run --args-file <file> --for B1`. No subagents at hand (or the critic must
   be another model)? `critic --run <agent>` starts the reviewer itself.

   After a critic finds blocking problems, do not fix from memory what your skills do not teach:
   research how that part is done well and write it into the skill with the address of the page you
   took it from; with a reference, measure the parts the critic names from it and add that as facts
   (`--kind facts`). Then build them through those measurements, not by eye. The next result is
   refused until a skill cites a new page and the new facts are in. The critic's words restated as
   rules are not research.

   Work in rounds: block in the base, add the result, run the critic, then compare each result side by side with the reference, fix the most important
   differences, repeat. `--worst` is the worst still-fixable difference: blocker (fails its
   purpose), major (clearly wrong at a glance), minor (polish, or beyond the brief's fidelity).
   **You are not the judge of your own work**: give the `critic` prompt
   unchanged to a fresh subagent, save its answer unedited and pass the file; never write that
   file yourself. The task may stop when the latest critic verdict is `minor` or `none`.
   Disagreeing is not dropping: `autoharness critic --dispute "<point>"` lets a fresh critic
   rule; record its ruling with the review, `--settled "<point and ruling>"`. If the user
   rejects a critic's point, record it: `autoharness ruling add "<point>" --reason "<their
   words>"`. Settled points and rulings reach every later critic in the project. With
   `brief --review pair`, `critic` gives two prompts: the critic, and a code-quality reviewer
   under `quality`. Give them to two fresh subagents in parallel, or use `critic --run`. Record
   both in one round: `review --answer-file <critic's> --quality-file <quality reviewer's>`. When `review` answers `change_approach`, `arbitrate` or `limited`, its `next` says
   exactly what to do. In visual work each critic also compares the result blind with the best
   earlier one; "Went backwards" in `next` means the earlier one is closer: restore what got worse
   before fixing more. `ask_user` (exit 3, after many rounds that still block): stop, show the user
   its `trend` and ask whether to go on or accept. Code edited after a review needs a new `evidence diff` and review.
   Run an arbiter's check with `check --run "<script>"`; where you had to check differently from
   its words, say so with `--deviation "<what and why>"` rather than adjusting it quietly.
   If the user accepts the result as it is or tells you to stop: `autoharness teardown --accepted
   "<their words>"` (it does not hold the end up for lessons; offer them once instead). Only their
   words, never your own judgment: with the hook installed, `--accepted` and `--force` are refused
   until the user has written something since the task started.
2. **Save what you learned**: update each skill you created, then `autoharness skill save <name>`
   (exit 3: show name, description, warnings; `--approve` after a yes). Reusable know-how no
   skill covers goes into a new one the same way. When the task had friction (a check that failed
   before it passed, a loop, an arbiter, rounds that went backwards, the user stepping in),
   teardown stops once with status `lessons` and lists them: write what failed and what fixed it
   into a skill, save it, then tear down again (or again without, telling the user why). A
   conclusion too small for a skill is an insight: `autoharness lesson add "<when X, do Y,
   because Z>"`. The same conclusion from another task is merged into it, and its `confirmed`
   count grows. `init` shows the `insights` that bear on a task. Once teardown lists
   `ripe_insights`, fold each into a skill and run `lesson fold <id> --skill <name>`. A
   `local` skill or recipe in `init` may carry a `record` of earlier tasks; heed its `note`.
3. **End**: `autoharness teardown`, or `--save-recipe <name> --notes "<what it was good for>"`
   (`--query "skill|mcp:<query>"` to re-run a search next time) when the setup was useful. Pass `--keep`/`--remove` only when the user asked.
4. **Tell the user** what was installed, kept and removed, any part ruled out of reach, any
   teardown warning (skipped discovery or brief), follow-ups left for later, and what is left for
   them (env vars, restart). Report what was checked as teardown's `checked` says, without
   upgrading it: runs (current or stale), where each critic verdict came from, checks you only
   reported, and brief items nothing checked.

`autoharness status` shows the harness and log (`--full` for everything), `audit` the decisions,
`remove`/`prune` take kept items off, `eval record|compare` measures runs with and without the
harness.
