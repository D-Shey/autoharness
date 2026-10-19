# AutoHarness

**AutoHarness keeps an agent from taking on a task unprepared. The agent works out what the task
needs, gathers the know-how and tools for it, and an independent critic checks the result until the
quality is acceptable.**

A coding agent (Claude Code, Codex CLI, Gemini CLI, Antigravity CLI, Qwen Code, GigaCode, Strands)
usually goes at a task head-on. It writes from memory without looking at what the result has to
match, without finding out how such work is done well, and then marks its own work "done". The
ceiling on quality is then whatever the model remembers. It shows on any task: an integration with
someone else's API, tests, setting up an environment, a page built from a mockup, a drawing that
has to match a reference image.

Engineers do not work that way. Nobody builds an aircraft head-on: the work is split into
disciplines (aerodynamics, materials, strength, control), what is already known in each is looked
up, hypotheses are checked on models, and only then is it built. AutoHarness brings that order to
the agent:

```
analysis → preparation → brief → install → base → review → learn → … → teardown
              │ areas of know-how, skills and MCP servers, method, reference, facts
              └─ what is missing: find it, and if there is nothing to find, write a skill
```

The two sides split the work: **the agent thinks, the CLI keeps count and guards the order**. The
agent decides what the task needs and whether the result is good. The `autoharness` CLI searches
registries, scores trust, installs and removes tools, keeps the records, and does not let the agent
move on until the previous step is done. Every command answers in JSON with a `next` field that
says what to do next. Exit code `3` always means one thing: *stop and ask the user*.

## The process

### 1. Analysis: `init`

```bash
autoharness init "<the user's task, verbatim>" --mode light|full|reviewed|auto
```

- `light`: a chore that needs no capability from the catalog (a typo, a commit). No search and no
  brief: do the work, check it, `teardown`. A task that needs any capability (an API, a browser,
  tests, debugging, an image) is raised to `full` by `init`, whatever the agent chose.
- `full`: something is missing: what to check against, know-how, a way to verify. The full cycle
  below applies.
- `reviewed`: the full cycle plus a mandatory independent critic before the end.

`init` searches [skills.sh](https://skills.sh) and the [official MCP
registry](https://registry.modelcontextprotocol.io) for the task right away. It shows similar saved
setups (`recipes`), candidates earlier tasks rejected (with the reason), and open follow-ups. A
running task can be raised without starting over: `autoharness mode full`.

### 2. Preparation: areas of know-how, search, method

This is where quality is decided, and it is the step agents usually skip.

- **Areas of know-how.** The agent splits the task into the areas the work itself is made of: for a
  service, its protocol, data model and concurrency; for a drawing, figures, anatomy and light. For
  each, it looks for something ready-made (`autoharness search skill|mcp "<query>"`,
  `autoharness need`) and takes what fits. If nothing fits, it writes its own skill from the
  sources it read.
- **Method.** How this kind of work is done well: the algorithm, the order, the known pitfalls. The
  agent takes it from sources it found and read, with links, not from memory.
- **Input.** The original the result has to match: the API's real response, a data sample, the
  mockup, the reference image. A description of it does not count. A search page, a gallery or
  someone's version of it (fan art, a mockup, a blog example) is not the original either.
- **Facts.** Before the first result the agent measures with a tool what the result will be judged
  on: fields and values, sizes, positions, proportions, timings. Then it records them
  (`evidence add <file> --kind facts`).
- **Open choices.** Where the task leaves a choice open (a format, a value or a formula, what a field
  holds) and the user cannot be asked, the agent picks what the person who asked would take for
  granted on first look: the plain, simple form, nothing they did not ask for. A tool's or a skill's
  advice does not decide it.

These are not suggestions. The CLI requires each part:

| What | How it is enforced |
|---|---|
| Every area is covered | `brief --domains "<area>: skill\|mcp\|create <name> (searched: <query>)"`. The search must actually have run in this task, and the skill or server must be among its results. "I already know how" is not accepted: that knowledge goes into a skill |
| Method from sources | `brief --method "<technique>: <url>; …; searched: <queries>"`. Without links only an explicit `nothing usable; searched: …` is accepted |
| Open choices settled | `brief --expects "<open choice>: <pick> - <why they would expect it>; …"` or `none: <why>`. Each becomes a brief item the critic checks |
| Skills in use, not in words | The first result is refused until the skills taken are installed and the skills to create are created and filled in |
| Facts from the reference | `--kind facts` is refused without a reference. The first result is refused until there are facts taken after the reference |

### 3. Brief: the plan before the work

```bash
autoharness brief --goal "<quality bar>" --domains "..." --method "..." \
  --inputs "<what is missing and where to get it>" --expects "<open choice>: <pick> - <why>; ..." \
  --constraints "<the user's constraints, verbatim>" \
  --verify "<how to check, against what>" --candidates "<name>: take|skip, why; …" \
  --gaps "create <skill>: <what it does>; …"
```

The brief records what a good result is and how it will be checked, the user's constraints verbatim
("no new dependencies", "offline only"), and a verdict on every candidate. Work that is judged by eye
needs `--fidelity`: the level the result is judged at ("stylised poster, not a photograph"). Browser,
API, log and database checks have ready templates
(`--verify-template browser|api|api-client|logs|database`). With the [hook](docs/installation.md)
the agent cannot change anything until the brief is recorded.

### 4. Install: trust and constraints

```bash
autoharness inspect mcp <name>        # trust score, secrets, where the server connects
autoharness add mcp <name>  |  autoharness add skill <owner/repo@skill>
```

- **Trust score.** The curated catalog is trusted. Registry servers are scored by namespace, pinned
  version and publisher. Anything not "high", anything with secrets and anything remote is
  installed only after an explicit "yes" from the user.
- **Skill scan.** A skill is downloaded to a temporary folder and checked for dangerous patterns:
  `~/.ssh`, `curl | sh`, "don't tell the user", obfuscation.
- **Constraints.** When the brief has any, a fresh subagent checks the candidate against them
  (`vet`), not the agent that wants to install it.
- **Network.** With `allowed_hosts` set, a server outside the list is installed only with the
  user's consent.

After installing, the agent makes one cheap call to try it. If it does not work: `remove` and the
next candidate. There is no point spending long on fixing someone else's tool.

### 5. Base: the frame first, details later

The first version is a rough base (a block-in): structure, layout, proportions, the main parts
from the facts, no polish. A critic looks at it right away, and the first round judges the base
only. Unfinished details are at most minor in that round; a wrong base is major. A mistake in the
base gets more expensive with every round built on top of it. Edits without a critic are counted:
after 20 edits without a fresh critic the hook stops editing.

### 6. Review: the author is not the judge

```bash
autoharness evidence add <file> --kind reference|facts|result   # the result: whole, then close-ups (--closeup)
autoharness evidence diff                                       # for code: the whole working-tree diff
autoharness verify run "<command>" --for B1                     # autoharness runs it; the exit code decides
autoharness critic                                              # a prompt for an independent critic
autoharness review --by critic --answer-file <file>
autoharness critic --run claude|codex|agy                       # or autoharness starts a read-only critic itself
```

- **The critic is a fresh subagent that did not do the work.** Its answer is kept as it is.
  autoharness reads the points, the verdict and a one-time `ANSWER-ID` from it itself, so a softened
  retelling cannot be passed off instead. A subagent given a review prompt reviews; it cannot take
  the task over.
- **What the critic checks:**
  - that the reference is the original itself;
  - that the facts are right, by checking a few of them against the reference;
  - every brief item (`met | partial | missing | contradicts`);
  - the open choices, as the person who asked would see them;
  - whether the work follows the method found;
  - what was done beyond the request;
  - whether each `verify run` really checks its item;
  - whether the work is finished as a whole (`ACCEPT`). "No" is at least major.
- **A check has to be able to fail.** `true`, `echo ok` or looking for a string in a file do not
  count. A run is tied to a version of the code and goes stale after an edit.
- **Blind comparison.** In work judged by eye, the critic compares the current result with the best
  earlier one under the names A and B. Only autoharness knows which is which. If the earlier one is
  closer, the answer is "Went backwards", and the earlier result stays the best.
- **Two reviewers** (`brief --review pair`). Every round, besides the critic, a code-quality
  reviewer checks for bugs, unhandled errors, security, and tests that check nothing. The round's
  verdict is the worse of the two.
- **Settled disputes do not come back.** A fresh critic rules on a disputed point
  (`critic --dispute`). A point the user rejects is recorded (`ruling add`), and later tasks in the
  project do not raise it again.
- **Files are data.** "Approved" or "set none" written inside the files under review is a finding,
  not an instruction.

### 7. Learn: don't fix from memory what you don't know

If the critic found serious problems, fixing them "from memory" brings the same crudeness back.
So the next result is refused until the task's skill has changed. For each point the skills do not
teach, the agent looks up how it is done well, writes the technique with its source into the skill,
and only then fixes it.

Then there are guards against loops:

- the same problem three rounds in a row → a mandatory change of approach (`approach`): from memory
  → from the docs, tuning by hand → computing it, a patch → a rebuild;
- several approaches did not help → an independent arbiter: fix it with a measurable check, or rule
  it out of reach;
- 8 blocking rounds in a row → the user decides.

The agent cannot accept the result for the user: `teardown --accepted "<their words>"` passes only
if the user actually wrote something.

### 8. The end: teardown, skills, recipes

```bash
autoharness skill save <name>                                    # into the library, after the user's "yes"
autoharness teardown --save-recipe <name> --notes "<what it is good for>"
```

- **Skills accumulate know-how.** What the agent learned in the task (how to do it, how to check it,
  an API's quirks) stays in a skill and is saved in `~/.autoharness/skills/`. Values measured in one
  project are kept apart from the procedure: the next task checks them again.
- **Lessons from friction.** If the task went hard (checks failed, the approach had to change, the
  result went backwards, the user stepped in), teardown stops once and asks what broke and what
  helped. Short conclusions accumulate with `lesson add`. A conclusion confirmed by three tasks moves
  into a skill.
- **A recipe** remembers the setup and the queries that found it. A similar task gets it right away,
  and the queries run again to find something better if it appeared.
- **What stays in the project** is set by the `teardown` setting: `keep` (the default) or `remove`.
  `autoharness config set teardown remove [--global]`.

## Example

Task: *"accept payments through a payment provider's API; the webhooks must be secure"*. The session
is illustrative and the CLI's answers are shortened.

```bash
autoharness init "accept payments through a payment provider's API, secure webhooks" --mode reviewed

autoharness search skill "payment webhooks"         # what already exists for this area
autoharness search mcp "payments api"
autoharness search skill "idempotency"

autoharness brief --goal "a payment goes through, a repeated webhook does not charge twice, a forged one is refused" \
  --domains "payments API: mcp <server from the search> (searched: payments api); \
             webhook signatures: skill <skill from the search> (searched: payment webhooks); \
             idempotency: create idempotent-handlers (searched: idempotency)" \
  --method "verify the signature over the raw body, an idempotency key, retries with backoff: <url>; …; searched: …" \
  --inputs "real responses and webhooks of a sandbox account" \
  --expects "amounts: integer minor units - what the provider's API takes" \
  --constraints "no new dependencies" \
  --verify "tests on real saved webhooks: forged, repeated, out of order" \
  --candidates "<server>: take, …; <skill>: take, …" --gaps "create idempotent-handlers: …"

autoharness add mcp <server>  &&  autoharness add skill <skill>
autoharness skill new idempotent-handlers --description "…"     # filled from what was read

autoharness evidence add webhook-samples.json --kind reference  # real saved webhooks
autoharness evidence add webhook-facts.md --kind facts          # fields, the signature header, event order
# base: the handler, signature check, key storage, nothing around them
autoharness evidence diff && autoharness critic                 # → major: the signature is computed over parsed JSON
# learn: why the signature is computed over the raw body → into the skill, then the fix
autoharness verify run "uv run pytest tests/test_webhooks.py" --for B2
autoharness evidence diff && autoharness critic                 # → minor
autoharness teardown --save-recipe payments-webhooks --notes "accepting payments with secure webhooks"
```

A task with a mockup, a reference image, logs or an environment goes the same way. Only the areas,
the sources and the way to check change.

## Languages

autoharness reads a task in any language it has a language file for. English is built in; Russian
ships as `src/autoharness/data/lang/ru.json`. A language file lists, per catalog capability, the
words that point to it, plus stopwords, common word endings, the ways to say "no constraints" and
dependency headings. To add a language, add `data/lang/<code>.json` (or `~/.autoharness/lang/<code>.json`
for yourself) and a matching `tests/lang/<code>.json` with the cases it must understand; no code
changes are needed.

## Gateway: servers without a restart

An agent usually sees a new MCP server only after a restart. The gateway removes that. One server,
`autoharness`, is added to the agent's config once; from then on `add mcp` writes new servers to
`.autoharness/gateway.json` and the gateway starts them on the fly. It gives the agent two
permanent tools: `harness_tools` and `harness_call(server, tool, arguments)`. It also publishes
native `<server>__<tool>` tools with `tools/list_changed`. In Claude Code they appear without a
restart; in Codex and Gemini CLI the meta-tools are the reliable path. A server that arrived in
`gateway.json` through `git pull` does not start without approval on this machine
(`autoharness gateway approve`).

## Security in short

- Only what passed the trust check, or what the user said "yes" to, is installed.
- Skills are scanned before they are copied into the project. Secrets are never written into
  configs, only `${ENV_VAR}`.
- An independent subagent checks the user's constraints, not the agent that wants to install.
- Task records are signed (HMAC, the key lives outside the project). An edit that goes around
  autoharness shows at teardown, and the hook keeps tools away from the key.
- Only what autoharness installed on this machine is removed.
- The hook and `--approve` frame the procedure; they are not a sandbox. Where each protection ends
  is described in [docs/security.md](docs/security.md).

## Quick start

```bash
uv tool install git+https://github.com/D-Shey/autoharness   # the CLI (needs uv; skills and MCP need Node.js)
autoharness agents                                          # which agents are found
cd /path/to/your/project
autoharness --agent claude install-skill                    # the skill for the agent (--user --all: every agent)
autoharness --agent claude gateway install                  # optional: MCP servers without restarts
autoharness --agent agy gateway install --user              # the same for every project (agy, Gemini, Qwen, GigaCode)
autoharness install-hook                                    # optional: Claude Code does not start without a brief
autoharness --agent agy install-hook                        # the same for Antigravity CLI
```

If `autoharness` is not found, run `uv tool update-shell` and open a new terminal. Restart the agent
and give it a task.

| Agent | `--agent` |
|---|---|
| Claude Code | `claude` |
| Codex CLI | `codex` |
| Gemini CLI | `gemini` |
| Qwen Code | `qwen` |
| GigaCode CLI | `gigacode` |
| [Strands harness](https://github.com/strands-agents/harness-sdk) | `strands` |
| Antigravity CLI | `agy` |

## Documentation

- [docs/reference.md](docs/reference.md): every command and where everything is kept. It also covers
  follow-ups found outside the task (`followup`), measuring the benefit (`eval record|batch|compare`),
  settled disputes (`ruling`) and settings (`config`).
- [docs/installation.md](docs/installation.md): installing for every agent, the gateway, the hook,
  uninstalling.
- [docs/security.md](docs/security.md): how each protection works and where it ends.

## Limitations

- Quality still depends on the model. AutoHarness makes it prepare, check itself against the
  original and learn, but it does not draw or write code for it.
- autoharness's checks are formal. It sees that a search ran, a skill changed, facts were recorded,
  but not how good they are. The substance is judged by the critic, and the critic is a model too.
- A task's text can leave a choice open that only its author could settle. The brief and the critic
  pick what a person would most likely expect, which is not always what a hidden check wants.
- The gateway does not do OAuth sign-in or the legacy SSE transport: such servers go into the agent's
  config and need a restart.
- `allowed_hosts` limits remote servers only. A local process connects wherever it wants.

## Development

```bash
git clone https://github.com/D-Shey/autoharness.git && cd autoharness
uv tool install -e .            # the autoharness command sees edits right away
uv run --extra dev pytest       # tests; uv creates .venv and installs pytest itself
```
