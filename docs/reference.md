# Reference

How all this fits into one cycle is in the [README](../README.md#the-process).

Every command prints JSON. Exit code `3` means the user's consent is needed. `--agent` and
`--project` are global flags and go before the subcommand.

## Commands

| Command | What it does |
|---|---|
| `init "<task>" [--mode light\|full\|reviewed\|auto]` | starts a task; `full` by default; `light` uses the tools at hand (output without catalog or search); `reviewed` is the full cycle with a mandatory critic; `auto` picks light/full from the catalog; `--profile quick\|standard\|reviewed` is an old synonym. Open follow-ups show in any mode; the full mode also shows `rejected_before`, candidates earlier tasks rejected, with the reason |
| `mode full\|reviewed` | raises a running task: a light one to the full mode with a search for candidates, or any task to a mandatory critic |
| `plan "<task>"` | the same without side effects |
| `need <capability> --reason ...` | records a capability gap and returns curated candidates and your own skills |
| `search mcp\|skill <query>` | MCP: Anthropic's plugin marketplace (claude-plugins-official, names `claude-plugins-official/<plugin>`), then the official MCP registry; skills: skills.sh; with a trust score |
| `inspect mcp\|skill <name>` | a detailed trust report; for MCP also `egress`, where the server may connect |
| `brief --goal ... --domains ... --method ... --expects ... --constraints ... [--verify ... \| --verify-template browser\|api\|logs\|database] [--review critic]` | the task's plan: the goal; the areas of know-how (`<area>: skill\|mcp\|create <name> (searched: <query>)`, at least two; the search must actually have run through `autoharness search`, what is taken is `take` in candidates, what is created is in gaps; created skills are filled in before the first result); the method (how such work is done well, from sources found and read, with URLs and `searched:` queries, or `nothing usable; searched: ...`; the critic checks the result against it); the open choices (`<choice>: <pick> - <why the person who asked would expect it>; ...` or `none: <why>`; each becomes a brief item E1, E2, …); inputs, constraints, the check, verdicts on candidates |
| `verify guide browser\|api\|api-client\|logs\|database` | the steps and expected evidence of a standard check |
| `verify record KIND --passed yes\|no --evidence FILE` | saves a check's outcome and its evidence file |
| `verify run "<command>" --for <brief item>` | runs a check itself; the exit code decides, the output is kept |
| `decision mcp\|skill NAME select\|skip --reason TEXT` / `audit [TASK_ID]` | the reason for a choice, and the structured decision log |
| `followup add "<what, where>"` / `followup list [--all]` / `followup done N [--note ...]` | findings outside the task: kept in the project, shown by `teardown` and the next `init` |
| `vet mcp\|skill <name> [--answer-file F \| --run claude\|codex\|agy]` | a prompt for an independent check of a candidate against the constraints; records the answer |
| `add mcp <name>` / `add skill owner/repo@skill` | installs into the MCP config (or the gateway) and the agent's skills folder, writes the lockfile |
| `add mcp <name> --direct` | past the gateway, straight into the agent's config |
| `remove mcp\|skill <name> [--reason TEXT]` | removes what this task installed, or what an earlier one kept in the project; the current task's reason for removing goes into the audit |
| `prune` | removes everything earlier tasks kept in the project |
| `evidence add <file> --kind reference\|facts\|result` / `evidence diff [--base REV]` | a reference, the facts from it (taken with a tool; when the requirements include a reference, the first result is refused without them), or a round's result; for code, the whole diff. The first version is a block-in of the base, and the first critic round judges only that; edits after the first result without a critic are counted. `--closeup` (`--part`) is a close-up or a part of the latest whole result (a crop smaller than the whole is recognised by itself). The critic sees the latest version of each kind of result (diff, render, report) with its parts, not every draft; `verify record` evidence goes alongside and does not push the work out |
| `review` / `critic [--run claude\|codex\|agy]` | a review round; a prompt for an independent critic. The critic also judges each check from `verify run` (the "Runs" section): a check that only looks for a name or a string in a file stops counting for its brief item |
| `approach` / `arbiter` / `arbitrate` / `check` | a change of approach to a part that does not pass review; the arbiter and its check |
| `teardown [--keep\|--remove] [--save-recipe NAME --notes ... --query kind:q] [--accepted ...]` | ends the task; `--accepted`/`--force` only in the user's words, and the hook refuses them until the user has written something since the task started |
| `claim [<session id>] --reason ...` | takes the task over from the agent session that started it: another session gets a summary of that task from the hook and stays blocked; only on the user's decision or if that session is gone. A session given a review prompt of the task cannot claim it |
| `status [--full]` | the current harness, what is kept in the project, the log; compact by default (a search is one line per query, the log its last 30 lines) |
| `eval record --case NAME --variant baseline\|harness --passed yes\|no [--seconds N] [--task-id ID] [--tokens N] [--cost N] [--tool-calls N]` | saves a run's measurement; a finished task supplies the time and CLI statistics |
| `eval batch --dataset FILE --variant baseline\|harness -- RUNNER [ARGS]` | runs an external runner on each JSONL case and records the measurements |
| `eval compare` | compares only the cases that have runs of both variants |
| `skill new\|test\|save\|list\|delete` | the agent's own skills and the local library; `test` has fresh agents do 1-3 tasks with and without the skill and a blind judge compare |
| `recipe list\|match\|show` | saved setups; `show` reruns the recipe's queries and returns what is new in `fresh` |
| `lesson add` | a short conclusion from a task's friction; one confirmed by three tasks moves into a skill |
| `journal` | the draft of this task's entry in the project's journal, and where it goes |
| `ruling add` | a review point the user rejected, so later critics in the project do not raise it again |
| `config get\|set [teardown keep\|remove \| allowed_hosts "<hosts>" \| min_mode any\|full\|reviewed \| journal "<path>"\|off] [--global]` | settings |
| `gateway install\|uninstall\|status\|serve\|approve [--user]` | the autoharness MCP server through which new servers work without a restart; `--user` puts it into the agent's own config (agy, Gemini, Qwen, GigaCode), where it serves whatever project the agent runs in |
| `install-skill` / `install-hook` / `waive` | installs the skill and the hook; skips autoharness in a session on the user's word |
| `agents` | the supported agents and their paths |

## Where everything is kept

| Path | What is there |
|---|---|
| `.autoharness/tasks/<id>/` | the task's state: `task.json`, lockfiles, `harness.md`, `events.jsonl`, `decisions.jsonl`, evidence, reviewers' answers |
| `.autoharness/evals.jsonl` | run measurements; a local file, ignored by Git |
| `.autoharness/kept.json`, `config.json`, `gateway.json`, `followups.json` | what stayed in the project, project settings, gateway servers, deferred findings; they can be committed (`.autoharness/.gitignore` lets only them through), the rest of the state stays out of git |
| `.autoharness/gateway.log` | the gateway's log |
| `~/.autoharness/` | `recipes/`, the library of your own skills `skills/`, the global `config.json`, gateway approvals, your own language files `lang/` (`AUTOHARNESS_HOME` overrides) |
| `src/autoharness/data/catalog.json` | the curated catalog of capabilities, servers and trusted publishers |
| `src/autoharness/data/lang/<code>.json` | the words of each language autoharness reads a task in |
| `src/autoharness/skill/SKILL.md` | the agent's instructions that `install-skill` installs |
