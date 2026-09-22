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
   often the biggest one. Plan to get the input first, before you produce anything,
   and get the thing itself, not a description of it: the still, not an article
   about the scene; the API's actual response, not a blog post about it.
3. **What are the user's constraints?** Write them down verbatim ("no external
   generators", "no new dependencies", "must run offline"). Every candidate you
   discover is checked against them in step 3.

Missing input and know-how are capabilities too: a way to see references, a way to
compare your output with them. They often need no install at all: a web search
tool, a browser or a headless render you already have. Say how you will cover them.

If `init` warns that an installed copy of this skill is out of date, tell the user to run
the `autoharness install-skill` command it names: you may be following old instructions.

If a recipe scores above ~0.3 and fits, `autoharness recipe show <name>` and
reuse its entries rather than searching again. `init` has already re-run the recipe's
saved queries (entries in `discovered` marked `recipe`), and `recipe show` lists under
`fresh` what they find now besides the pinned entries: take a fresh one when it beats
a pinned entry.

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
  # optional: --fidelity "<level to judge at, e.g. stylised poster, not photographic>"
  # optional: --review critic   (require an independent critic even if no capability does)
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
- You do not judge the fit yourself. While the brief has constraints, `add` answers
  `vet_required` until a fresh reviewer has checked the candidate: run
  `autoharness vet mcp|skill <name>`, give its prompt unchanged to a fresh subagent,
  save its answer unedited to a file and record it with `autoharness vet mcp|skill <name>
  --answer-file <file>`. On `breaks`, pick another candidate; `add` asks for the
  user's yes (exit `3`) before installing it anyway.
- If the project sets `allowed_hosts`, `inspect` shows under `egress` where a server
  can reach. A remote server off the list, or any local process (its network cannot
  be fenced), needs the user's approval: show them the `egress` reason.
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

1. Verify against the quality bar from your brief, not only that it works, and keep
   the evidence. This works for any task; what counts as a reference and a result
   depends on it (a film still and a screenshot; a spec and a test log; the old
   report and the new one):

   ```bash
   autoharness evidence add <file> --kind reference   # what the result must match; the thing itself
   autoharness evidence add <file> --kind result      # this round: the whole result, then close-ups
   autoharness evidence diff                          # code: the whole working-tree diff, new files included
   autoharness review --by self --diff "1 <most important>; 2 ..." --worst blocker|major|minor|none
   autoharness critic                                 # prompt for an independent reviewer
   autoharness review --by critic --answer-file <file>  # the critic's answer, saved unedited
   ```

   Rounds: produce, add the result, compare it side by side with the reference, fix the
   most important differences, repeat. One pass is not a review.

   - **Severity.** `--worst` is the worst severity among the differences that are still
     fixable: blocker (fails its purpose), major (clearly wrong at a glance), minor
     (polish, or detail beyond the fidelity from your brief). The task may stop when the
     critic's verdict on the latest result is `minor` or `none`.
   - **Close-ups.** Where details matter (a face, a hand, one table in a report, one
     function in a diff), add a close-up of that part as another result of the same
     round; the critic sees every result added since its last review.
   - **You are not the judge of your own work.** Give the `critic` prompt unchanged to
     a fresh subagent that did not make it (Claude Code: the Agent tool; elsewhere a
     new session). Save its answer unedited to a file and pass it with `--answer-file`:
     autoharness checks the prompt's ANSWER-ID and reads the list and the VERDICT itself,
     so neither can be softened in the retelling. An id is valid for the latest prompt
     about the latest result only. `--diff`/`--worst` for a critic still work but are
     recorded as your retelling, with a warning.
   - **Changes after the review.** For code, record the round with `autoharness evidence
     diff`. If you edit the reviewed files afterwards, `teardown` warns that those changes
     were never reviewed: record `evidence diff` again and review it, or tell the user.
   - **Disagreeing is not dropping.** If you think a critic point is wrong, do not skip
     it: add a close-up of that part and run `autoharness critic --dispute "<the point>"`;
     a fresh critic decides. When it settles the point, record that with the review
     (`--settled "<point and ruling>"`): every later critic sees it and does not raise it again.
   - **Loops.** If a critic lists the same blocking problem three rounds in a row (anywhere
     in its list, not only first), `review` answers `change_approach`: the way you are
     fixing that part does not work. `critic` refuses until you record a different method
     with `autoharness approach --part "<part>" --was "<old>" --now "<new>"`: rebuild the part
     from the reference point by point, simplify it, or change technique, and treat missing
     know-how for it as a capability gap (step 6). The next critic judges that part first.
     When several different approaches failed, `review` answers `arbitrate`: do not decide
     yourself whether the part can be fixed. Run `autoharness arbiter`, give its prompt to a
     fresh subagent, save its answer unedited and record it with `autoharness arbitrate
     --answer-file <file>` (autoharness reads its VERDICT and CHECK line). A limit stops the part blocking
     (critics count it as minor). A fix becomes your next approach and comes with a measurable
     check: run it on the new result and record it with `autoharness check --result
     "<measured>" --pass yes|no` before the critic runs, so a fix that did not take effect is
     caught by you, not by the next critic. When arbiters' fixes keep failing, a final arbiter
     decides; if its fix fails too, `review` answers `limited`: the part is ruled out of reach
     and no longer blocks. Tell the user about such parts when you finish. Never cut
     `review`'s output short (`| head`, `> /dev/null`): its `next` is the instruction.
   - **The user decides when good is good enough.** Visual capabilities require image
     references, at least two results and a final critic; teardown refuses until then.
     If the user accepts the result as it is, run
     `autoharness teardown --accepted "<their words>"`. Tell the user which differences remain.
2. Save the skills you created: bring each one up to date with what the task taught
   you, then `autoharness skill save <name>`. If you learned something else reusable that
   no skill covers (how to build or check this kind of project, an API quirk), write it
   down the same way with `autoharness skill new`. Save answers exit code 3: show the user
   the name, description and warnings, and re-run with `--approve` only after a clear
   yes. If save reports `blocked`, remove what it flags.
3. End the task. If the setup was useful, save it as a recipe too:
   `autoharness teardown --save-recipe <short-name> --notes "<what it was good for>"`
   The searches that found what you installed are saved with it; add a query that
   should be re-run next time with `--query "skill:<query>"` or `--query "mcp:<query>"`.
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
