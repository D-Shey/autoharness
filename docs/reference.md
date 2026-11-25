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
| `brief --goal ... --domains ... --method ... --expects ... --constraints ... [--techniques ...] [--verify ... \| --verify-template browser\|api\|logs\|database] [--review critic]` | the task's plan: the goal; the areas of know-how (`<area>: skill\|mcp\|create <name> (searched: <query>)`, at least two; the search must actually have run through `autoharness search`, what is taken is `take` in candidates, what is created is in gaps; created skills are filled in before the first result); the method (how such work is done well, from sources found and read, with URLs and `searched:` queries, or `nothing usable; searched: ...`; the critic checks the result against it); professional techniques (`--techniques "<name>: <principle>; ..."`, domain-level craftsmanship techniques injected into critic prompts to enforce commercial production standards and the "calligraphy principle" — zero tolerance for amateur shortcuts or patching over broken foundations); the open choices (`<choice>: <pick> - <why the person who asked would expect it>; ...` or `none: <why>`; each becomes a brief item E1, E2, …); inputs, constraints, the check, verdicts on candidates |
| `verify guide browser\|api\|api-client\|logs\|database` | the steps and expected evidence of a standard check |
| `verify record KIND --passed yes\|no --evidence FILE` | saves a check's outcome and its evidence file |
| `verify run "<command>" --for <brief item>` | runs a check itself; the exit code decides, the output is kept. Evaluates AST substance: shallow checks (`os.path.exists`, `assert "str" in file`, `assert len > 0` alone, and token/key membership assertions without property checks) are flagged as hollow checks (`hollow_check: true`) and cannot satisfy brief items. Windows/PowerShell argument splitting and trailing flags are safely reassembled |
| `stencil scan <file> [--threshold 0.0-1.0] [--max-points N]` \| `stencil list` \| `stencil show <id>` | extracts structural anchor geometry (edges, contours, curves, landmarks) from raster/vector references into domain-agnostic geometric stamps/stencils (`.autoharness/stencils/`) for high-fidelity alignment and ground-truth validation |
| `decision mcp\|skill NAME select\|skip --reason TEXT` / `audit [TASK_ID]` | the reason for a choice, and the structured decision log |
| `followup add "<what, where>"` / `followup list [--all]` / `followup done N [--note ...]` | findings outside the task: kept in the project, shown by `teardown` and the next `init` |
| `vet mcp\|skill <name> [--answer-file F \| --run claude\|codex\|agy]` | a prompt for an independent check of a candidate against the constraints; records the answer |
| `add mcp <name>` / `add skill owner/repo@skill` | installs into the MCP config (or the gateway) and the agent's skills folder, writes the lockfile |
| `add mcp <name> --direct` | past the gateway, straight into the agent's config |
| `remove mcp\|skill <name> [--reason TEXT]` | removes what this task installed, or what an earlier one kept in the project; the current task's reason for removing goes into the audit |
| `prune` | removes everything earlier tasks kept in the project |
| `evidence add <file> --kind reference\|facts\|result` / `evidence diff [--base REV]` | a reference, the facts from it (taken with a tool; when the requirements include a reference, the first result is refused without them), or a round's result; for code, the whole diff. Verifies artifact complexity (SVG geometry, path count, gradients, substantive code): hollow artifacts are flagged and block teardown. The first version is a block-in of the base, and the first critic round judges only that; edits after the first result without a critic are counted. `--closeup` (`--part`) is a close-up or a part of the latest whole result (a crop smaller than the whole is recognised by itself). The critic sees the latest version of each kind of result (diff, render, report) with its parts, not every draft; `verify record` evidence goes alongside and does not push the work out |
| `review` / `critic [--run claude\|codex\|agy]` | a review round; a prompt for an independent critic. Rejects `review --by self --worst none` when hollow artifacts, hollow checks, or reviewed mode are active. The critic also judges each check from `verify run` (the "Runs" section): a check that only looks for a name or a string in a file stops counting for its brief item |
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
| `gateway install\|uninstall\|status\|serve\|approve [--user]` | the autoharness MCP server through which new servers work without a restart, offering Code Mode, Active Context Pruning, persistent memory, AST Code Map, Harness\u00b2 Slot Inspection, Consequence Simulation, Output Compression, Regression Sentinel, Session Compaction, Swarm Barriers, Memory Reconciliation, and Permissions tools; `--user` puts it into the agent's own config (agy, Cursor, Gemini, Qwen, GigaCode), where it serves whatever project the agent runs in |
| `memory search\|index\|status [query] [--kind ...] [--mode hybrid\|fts\|vector] [--limit N]` | in-process SQLite FTS5 persistent memory with BM25 ranking, LEANN-style selective recomputation vector index, and Reciprocal Rank Fusion (RRF) hybrid retrieval (`.autoharness/memory.db`) |
| `memory reconcile [--prune-orphans] [--kind all\|ruling\|lesson\|review\|followup] [--json]` | audit persistent memory against ground-truth workspace files and AST symbols; detect orphaned entries (deleted task dirs, missing code files), stale references, and conflicting prescriptive policies; `--prune-orphans` removes broken entries in an atomic `SAVEPOINT` |
| `codemap [path] [-q QUERY] [--json]` | AST Code Map and structural symbol index: extracts classes, methods, signatures and docstrings via Python stdlib `ast`, reducing context tokens by 80-90% |
| `codemap <path> --impact <symbol>` | trace transitive blast radius: direct callers, callees, and impacted test files for a given symbol |
| `codemap <path> --callers <symbol>` | locate all direct call sites of a symbol across the repository |
| `codemap <path> --arch` | detect module dependency cycles and architectural boundary violations |
| `codemap <path> --check-shims` | Anti-Shim AST Sentinel: detect fake implementations (`EMPTY_STUB_BODY`, `TRIVIAL_RETURN_STUB`, `NOT_IMPLEMENTED_STUB`, `TRIVIAL_ASSERTION_CHEAT`); integrated into `evidence diff` to block evidence with stubs |
| `slot [export\|diff]` | inspect the 8 canonical harness slots (`system_prompt`, `guardrails`, `memory`, `skills`, `tool_guidance`, `scripts`, `subagents`, `middleware`); `export` dumps structured JSON; `diff --target <file>` computes semantic diff |
| `probe [-k N]` | Propose\u2013Probe\u2013Compose contrastive loop: generate K candidate harness variants, probe in sandboxed execution, synthesize evidence-backed improvements without ground truth |
| `simulate <target> [--action-type <type>]` | consequence-aware pre-execution simulation: calculate reversibility tier (`READ_ONLY`, `REVERSIBLE`, `HIGH_RISK`, `IRREVERSIBLE`), risk score (0-100), blast radius, and rollback guidance |
| `skim <file_or_command> [--kind auto\|pytest\|diff\|traceback\|compiler\|generic] [--max-lines N] [--json]` | intelligent cascading output compression: collapse passing test progress and verbose logs by 70-95% while isolating failure anchors, stack traces, and test counts; preserves full logs in `.autoharness/logs/` |
| `sentinel run [--test-command <cmd>] [--auto-rollback] [--pre-flight] [--no-shims] [--json]` | self-supervised regression sentinel: pre/post-flight test verification with Anti-Shim checks; auto-rolls back to verified baseline on regression or fake stubs |
| `sentinel snapshot [--label <name>]` | capture an atomic working tree fingerprint and lightweight file backup |
| `sentinel rollback [--snapshot-id <id>]` | restore repository to a verified snapshot with fail-safe protection and untracked artifact cleanup |
| `sentinel status` | inspect the active baseline snapshot and last verification result |
| `compact run [<transcript_path>] [--keep-last N] [--epoch-size N] [--json]` | dynamic context compactor: multi-tiered session compression (Tier 0 Recent, Tier 1 Distill, Tier 2 Epoch) saving 70-85% tokens; full logs in `.autoharness/folded/` |
| `compact status [--json]` | inspect compaction statistics, token reduction metrics, and active registry |
| `compact prune [--keep-last N] [--json]` | rotate and purge older folded transcripts to reclaim disk space |
| `swarm init\|run\|status\|list [plan_id] [--title ...] [--file ...] [--parallel N]` | multi-agent DAG swarm workflow coordinator and runner: topological scheduling, concurrent worker execution pool with barrier synchronization, role separation, hard verification gates, and crash-proof persistence |
| `swarm verify [<plan_id>] [--json]` | audit DAG topology and validate declarative artifact barriers (`expected_artifacts`, `barrier_assertions`, `allowed_paths`) and role boundaries; blocks downstream branches on upstream failure |
| `drive [run\|status\|queue\|rollback] [--max-tasks N] [--followup ID] [--dry-run]` | continuous autonomy self-driving backlog loop: prioritized resolution of `followups.json`, pre- & post-flight regression sentinel checks, and automated git rollback on failures |
| `update [--check] [--repo REPO] [--force] [--yes]` | check for updates and automated self-update of AutoHarness from GitHub releases/repository via `uv tool` or `pip` |
| `openspec [status\|sync\|changes] [--change NAME] [--json]` | OpenSpec spec-driven development compatibility bridge |
| `ontology [show\|init\|add\|query\|check] [target] [--section SEC] [--term TERM] [--json]` | Ontology 2.0 living natural-language semantic map (`.autoharness/ontology.md`), auto-indexed into persistent memory and injected into critic reviews |
| `truth [audit\|churn\|postmortem\|assumptions] [query] [--commits N] [--json]` | Dormant Data Truth Engine & Delayed Assumption Postmortem: detects git thrashing, recurring followups after `done`, unconfirmed reviews, and falsified assumptions |
| `install-skill` / `install-hook` / `waive` | installs the skill and the hook; skips autoharness in a session on the user's word |
| `agents` | the supported agents and their paths |

## Where everything is kept

| Path | What is there |
|---|---|
| `.autoharness/tasks/<id>/` | the task's state: `task.json`, lockfiles, `harness.md`, `events.jsonl`, `decisions.jsonl`, evidence, reviewers' answers |
| `.autoharness/evals.jsonl` | run measurements; a local file, ignored by Git |
| `.autoharness/kept.json`, `config.json`, `gateway.json`, `followups.json`, `ontology.md`, `assumptions.json` | what stayed in the project, project settings, gateway servers, deferred findings, living Ontology 2.0 map, and assumption postmortem log |
| `.autoharness/gateway.log` | the gateway's log |
| `.autoharness/logs/` | full uncompressed logs preserved by `skim` for drill-down debugging |
| `.autoharness/folded/` | reversible compacted session transcripts from `compact run` |
| `.autoharness/memory.db` | SQLite FTS5 persistent memory (rulings, lessons, reviews, follow-ups) |
| `.autoharness/snapshots/` | sentinel baseline snapshots for rollback guard |
| `~/.autoharness/` | `recipes/`, the library of your own skills `skills/`, the global `config.json`, gateway approvals, your own language files `lang/` (`AUTOHARNESS_HOME` overrides) |
| `src/autoharness/data/catalog.json` | the curated catalog of capabilities, servers and trusted publishers |
| `src/autoharness/data/lang/<code>.json` | the words of each language autoharness reads a task in |
| `src/autoharness/skill/SKILL.md` | the agent's instructions that `install-skill` installs |
