# Security: the details

The trust model in short is in the [README](../README.md#security-in-short). This page covers how
each protection works and where it ends.

## Trust score and the catalog

- **The curated catalog** (`src/autoharness/data/catalog.json`) is the trusted base: capabilities,
  vetted MCP servers, trusted server publishers and skill owners.
- **MCP servers from the registry** get a score: a verified namespace, a pinned package version, a
  publisher from the trusted list. Anything not "high", anything that needs secrets and any remote
  server is installed only with `--approve` after an explicit "yes" from the user.
  The publisher writes `repository.url` itself, so the trusted-list bonus is given only when the
  namespace `io.github.<owner>` was verified by the registry against the same owner, or when the DNS
  namespace (`com.stripe/…`) is in the catalog's curated table `trusted_mcp_namespaces` (domain →
  publisher) exactly, without subdomains: a subdomain can belong to anyone
  (`<bucket>.s3.amazonaws.com`). Other DNS namespaces prove the domain but not who is behind it, and
  get no bonus: such a server is installed after a "yes".
- **Skills** are first downloaded into a temporary staging folder, scanned statically (reading
  `~/.ssh`, `curl | sh`, "don't tell the user", obfuscation), and only then copied into the project.
  High findings block the install (`rejected`).
- Secrets are never written: a config holds only `${ENV_VAR}`.

## Checking candidates against the constraints (`vet`)

Curated local MCP servers and the skills installer run with exact package versions. Recipes save the
commands with those versions. That does not pin transitive dependencies, the contents of remote
services or revisions of external skills. Configs created earlier with `@latest` are not rewritten
automatically.

When the brief records the user's `--constraints`, `add` does not install a skill or a server
without the verdict of an independent check (status `vet_required`). `autoharness vet skill <spec>`
stages the skill in a temporary folder and builds a prompt from the constraints verbatim and the
contents of its files (for MCP: the config, permissions and `egress`). The prompt goes to a fresh
subagent, whose answer is saved to a file unedited and recorded with `--answer-file <file>`. Where
autoharness can start a reviewer of the agent's kind itself (`vet … --run claude|codex|agy`), a verdict
typed in by the agent (`--verdict`) is refused: the reviewer answers. On `breaks`, `add` answers with
code `3`: install only if the user agrees. If a skill's files at install time differ from the vetted
ones, `add` warns. Your own skills from the library (`local:`) need no check.

## Reviewers' answers are recorded as they are

To judge the work, `.autoharness/tasks/<id>/events.jsonl` records only the CLI command's name, its
exit code and time; arguments and secret values never go there. `.autoharness/evals.jsonl` keeps the
results of comparison runs locally and is ignored by Git. The fields `passed`, `tokens` and `cost` are
data from the agent or the user and are not certified by AutoHarness.

`critic`, `arbiter` and `vet` prompts get a one-time `ANSWER-ID`. The agent saves the subagent's
answer to a file unedited and passes it with `--answer-file`: autoharness checks that the id was
issued by this task for such a prompt and not used yet, and takes the list, `VERDICT`, `CHECK` and
`REASON` from the answer itself. `--worst` cannot be milder than the critic's `VERDICT`. This guards
against a retelling and against passing off an old or someone else's answer, not against forgery: the
id is visible to the agent in the prompt, and the agent could technically write a file "from the
critic" itself. Honesty here rests on the skill's instructions, not on cryptography; `critic --run`
and `vet --run` take the agent out of the loop where the agent's CLI is installed. A copy of the
answer stays in `.autoharness/tasks/<id>/answers/`. The old way (`--diff`/`--worst`,
`--verdict`/`--text`) still works but is flagged with a warning: it is the author's retelling.

A subagent that was given a pending review prompt of the task (it names the prompt's `ANSWER-ID` or
its file) is recognised as that reviewer: the hook lets it read, and it cannot `claim` the task from
the session that owns it.

`teardown` warns when the working tree changed after the last review of a recorded `evidence diff`:
nobody checked those changes. `autoharness evidence diff [--base REV]` saves as a result the whole
diff of the working copy against `REV` (`HEAD` by default), including new untracked files, without
touching the index.

## Where MCP servers may connect (`allowed_hosts`)

The `allowed_hosts` setting is a comma-separated list of host patterns (`api.github.com,*.example.com`);
empty (the default) means no limit. What it can honestly do:

- a **remote** server (HTTP/SSE) lives on one known host: for a host not on the list `add` asks the
  user's consent (code `3`), and the gateway does not connect to it and disconnects one already
  running if the list is narrowed later (within a second);
- a **local process** (`npx`, `uvx`, `docker`) connects wherever it wants; autoharness does not limit
  that. So with a list set, such a server is also installed only with the user's consent.

```bash
autoharness config set allowed_hosts "api.githubcopilot.com,*.context7.com"
autoharness inspect mcp github      # the egress field: host, enforced, ok, reason
```

## What the gateway passes to servers

The gateway's HTTP client refuses redirects, including when a session ends. `url` must name the
final MCP endpoint. Authorization headers and the session id are not sent to the address in
`Location`, even if the first host is allowed.

For Codex the gateway explicitly gets `AUTOHARNESS_HOME`, `AUTOHARNESS_GATEWAY_TIMEOUT` and the
variables the approved server configs refer to. Only names are written to `env_vars`. If `add` or
`gateway approve` adds a new name, the CLI warns that Codex needs a restart: the environment of a
running process cannot be changed.

Local servers and HTTP servers with headers (a token from the environment) go through the gateway.
Servers with OAuth sign-in and `add mcp --direct` are written into the agent's config directly.
Secrets are filled in from the agent process's environment; the log is `.autoharness/gateway.log`.

A local server inherits **only** a fixed safe set from the agent's environment: where the system, the
user, temporary folders, locale, certificates and proxies are (`PATH`, `HOME`, `SystemRoot`, `APPDATA`,
`TEMP`, `LC_*`, `XDG_*`, `HTTP(S)_PROXY`/`ALL_PROXY`/`NO_PROXY`, `SSH_AUTH_SOCK`, configuration and
account selection such as `DOCKER_*`, `AWS_PROFILE`, `KUBECONFIG`, `GH_HOST`, the caches
`npm_config_*`/`UV_*`/`PIP_*`, `PLAYWRIGHT_*` and the like; see `PASS_ENV` in `mcp_client.py`). Even
from those, names that look like secrets and URLs with a password inside (except proxies) are dropped.
If a server fails to start, the error lists the names (not the values) of the variables it did not
get.

Everything else, including `DB_PASS`, connection strings, webhook URLs and paths to key files
(`GOOGLE_APPLICATION_CREDENTIALS`, `AWS_SHARED_CREDENTIALS_FILE`), a server sees only if it is named
in its `env` as `${VAR}`. Such `${VAR}` are shown at `add` and need the user's consent: all of them,
not only those the registry marks as secret. To pass one more variable, add it to the server's `env`
in `gateway.json` and run `autoharness gateway approve <name>`. Servers written directly into the
agent's config (`--direct`) are started by the agent itself; none of this applies to them.

## Approving `gateway.json` on each machine

`gateway.json` can be committed, so after `git pull` it may contain someone else's command. The gateway
starts a server only if **exactly this config** was approved on this machine: `add mcp` records the
approval itself, and a server that came from the repository (or was changed there) waits with the
status `not approved`. Approvals are kept outside the repository, in `~/.autoharness/gateway-approved.json`
(or `$AUTOHARNESS_HOME`), by the project's absolute path: after the project moves to another folder
they have to be given again. `init`, `status` and `gateway status` show which servers wait for approval.

```bash
autoharness gateway status            # the unapproved field: what waits for approval
autoharness gateway approve <name>    # shows the config, code 3; with --approve after the user's "yes"
autoharness gateway approve --all     # the same for everything waiting
```

**After upgrading autoharness**, servers added through the gateway by an older version have no
approvals yet and do not start until the user runs `autoharness gateway approve --all` once (after
looking at the configs). Approvals cannot be seeded automatically: anything in the project, including
`kept.json` and `.autoharness/tasks/`, can arrive in a commit (`git add -f` bypasses `.gitignore`).

The agent runs `--approve`, and that the user really said "yes" rests on the skill's instructions,
as with every other `--approve`: this guards against a `gateway.json` swapped in through a pull, not
against an agent that decided to go around the user.

## Only what autoharness installed is removed

Installing an MCP server and installing the gateway write a change journal outside the repository:
`$AUTOHARNESS_HOME/transactions/` (`~/.autoharness/transactions/` by default). CLI operations are
serialised by a project lock. If a write is interrupted, the next CLI operation finishes it from the
journal. User files changed after the failure are not overwritten: recovery stops with the path to
the conflict and the journal. Removing tools and copying skills are not yet part of such
transactions.

Your own MCP servers and skills are never overwritten or removed; a skill changed after it was
installed is backed up (caches such as `__pycache__` do not count as a change). `kept.json` is
committed, so `remove`/`prune` take off by themselves only what a task left on this machine (recorded
in `~/.autoharness/kept-here.json`). An entry only the file knows about (a pull, an older autoharness,
another clone, a moved project) waits for the user's "yes": code `3`, nothing changes;
`prune --approve` / `remove … --approve` take it off. Likewise a task started on another machine
(`.autoharness/tasks/` can be committed too) removes nothing by its lockfiles: end it with
`teardown --keep`, then `prune --approve`.

Names from lockfiles and `kept.json` are compared with the folder listing letter for letter, so
`Mine`, `mine.` or `nul` do not open someone else's folder; autoharness does not touch a link
(symlink, junction); `add` does not replace a folder that differs from the one the task installed.
`prune` and `teardown --remove` check everything first and only then remove anything.

## Your own skills

`skill save` blocks secrets (tokens, private keys, `password=…`) and dangerous patterns, warns about
absolute paths and emails, and saves nothing without `--approve`.

## The hook

Before the brief, the hook (`install-hook`) lets through `autoharness …` as a command of its own,
plus shell commands that only list or read (`ls`, `cat`, `git status`, `Get-ChildItem`). An
`autoharness` command may have `cd <dir> &&` or `cd <dir>;` in front; in Bash, `\` line breaks, `\"`
inside double quotes and `AUTOHARNESS_*=…` before the command; and at the end, `2>&1` and read-only
filters (`| head`, `| tail`, `| jq`, `| grep` with arguments without substitutions,
`| Select-Object -First N`, `| Out-String`, `| ConvertFrom-Json`). No other `;`, `|`, `&&`, `#` or
redirections, and `$(…)` and backticks only inside single quotes. Any call that names the
`integrity.key` is denied first, whatever it looks like. This frames the procedure; it is not a
sandbox: the agent can also `waive`.
