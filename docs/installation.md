# Installation

What autoharness is and how it works is in the [README](../README.md). This page covers installation
only.

Installation has three steps: install the CLI (once per machine), give the agent the skill (in the
project or globally), and optionally connect the gateway so new MCP servers work without restarting
the agent. Claude Code and Antigravity CLI have an optional fourth step: the hook.

## 1. The `autoharness` CLI

It is installed with [uv](https://docs.astral.sh/uv/) as a standalone tool: uv picks a suitable
Python (3.11+) itself and puts the `autoharness` command into its folder for executables. There are
no external dependencies.

If uv is not installed yet:

```bash
# Linux/macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Install autoharness straight from GitHub:

```bash
uv tool install git+https://github.com/D-Shey/autoharness
autoharness agents        # check: prints the supported agents and the one detected
```

The `autoharness` command must be on `PATH`: both the agent and the gateway call it. uv puts it into
`~/.local/bin` (on Windows `%USERPROFILE%\.local\bin`; the exact path is `uv tool dir --bin`). If
`autoharness agents` answers "command not found", run `uv tool update-shell` and open a new
terminal. Start the agent from a new terminal as well, or it will not see the updated `PATH`.

Upgrading and uninstalling:

```bash
uv tool upgrade autoharness       # pull the latest commit from GitHub
uv tool uninstall autoharness
```

If you work on autoharness itself, install it from a local clone in editable mode, so changes to the
code are seen by the `autoharness` command right away:

```bash
git clone https://github.com/D-Shey/autoharness.git
cd autoharness
uv tool install -e .
```

Most skills and MCP servers in the catalog also need Node.js (`npx`, including `npx skills` for
installing skills); Python servers run through `uvx`, which comes with uv.

## 2. The skill for the agent

The skill is a `SKILL.md` from which the agent learns how to use autoharness. Each agent gets its own
version: with the right agent name, the path to its MCP config and the hint about restarting. That
is why **the agent has to be named explicitly**: in a plain terminal autoharness does not know whom
it installs for and defaults to `claude`.

`--agent` is a global flag and goes **before** the subcommand:

```bash
cd /path/to/your/project                       # commands work on the current folder (or --project)

autoharness --agent claude   install-skill     # into the project: .claude/skills/autoharness/SKILL.md
autoharness --agent codex    install-skill --user   # globally: ~/.agents/skills/autoharness/SKILL.md
autoharness install-skill --all                # into the project, for every agent at once
```

| Agent | `--agent` | Skill in the project | Skill globally (`--user`) |
|---|---|---|---|
| Claude Code | `claude` | `.claude/skills/autoharness/` | `~/.claude/skills/autoharness/` |
| Codex CLI | `codex` | `.agents/skills/autoharness/` | `~/.agents/skills/autoharness/` |
| Gemini CLI | `gemini` | `.gemini/skills/autoharness/` | `~/.gemini/skills/autoharness/` |
| Qwen Code | `qwen` | `.qwen/skills/autoharness/` | `~/.qwen/skills/autoharness/` |
| GigaCode CLI | `gigacode` | `.gigacode/skills/autoharness/` | `~/.gigacode/skills/autoharness/` |
| Strands harness | `strands` | `.agent/skills/autoharness/` | `~/.agent/skills/autoharness/` (strands does not read it by itself: pass the path to `create_harness(skills=...)`) |
| Antigravity CLI | `agy` | `.agents/skills/autoharness/` | `~/.gemini/config/skills/autoharness/` |

Codex and Antigravity CLI read the same `.agents/skills`, so they share the project's `SKILL.md`: it
names `--agent` and the MCP config for each of the two agents. Their global skills differ: agy does
not read `~/.agents/skills`. In `/skills`, agy 1.4.0 suggests `~/.gemini/antigravity-cli/skills` for
global skills but loads them only from `~/.gemini/config/skills`, so `--user` writes there.

Project or global:

- **in the project**: the skill works only in this project; it can be committed and the whole team
  gets it;
- **globally (`--user`)**: the skill is there in every project on this machine.

Agents read skills when a session starts: start a new session after installing. Running
`install-skill` again overwrites `SKILL.md`, which is how it is updated after autoharness is
upgraded. `init` warns when an installed `SKILL.md` is out of date.

## 3. The gateway (optional, recommended)

Without the gateway every new MCP server is written straight into the agent's config, and the agent
has to be restarted. With the gateway only one restart is needed, after the gateway itself is
installed (how it works is in the [README](../README.md#gateway-servers-without-a-restart)):

```bash
autoharness --agent claude gateway install     # writes the `autoharness` server into the agent's config
autoharness --agent claude gateway status      # whether it is installed and which servers run
```

The gateway is installed into the project and **separately for each agent**: `gateway install` has no
`--all`, so repeat the command with each `--agent`. `add mcp` goes through the gateway only for an
agent that has the gateway installed. `--user` installs it into the agent's own config for every
project (agy, Gemini, Qwen, GigaCode).

Codex may need one more restart when a new server needs an environment variable the gateway does not
pass yet. `add` and `gateway approve` say so and write its name into `env_vars`; set the value in the
environment Codex is started from.

What to do in each agent after `gateway install`:

| Agent | Where the gateway is written | How the agent picks it up |
|---|---|---|
| Claude Code | `.mcp.json` | restart Claude Code (or `/mcp`) and **approve** the project server `autoharness` |
| Codex CLI | `.codex/config.toml` | restart Codex; the project config is read only if the project is marked **trusted** |
| Gemini CLI | `.gemini/settings.json` | restart or run `/mcp refresh` |
| Qwen Code | `.qwen/settings.json` | restart or run `/mcp refresh` |
| GigaCode CLI | `.gigacode/settings.json` | restart or run `/mcp refresh` |
| Strands harness | `.agent/mcp.json` | pass `create_harness(mcp_servers=".agent/mcp.json")` and create the agent again |
| Antigravity CLI | `.agents/mcp_config.json` | restart `agy` |

Check: the tools `harness_tools` and `harness_call` should appear in the agent. In Claude Code they
may arrive deferred; tool search finds them.

The config gets the full path to the current Python and the command
`-m autoharness --project <absolute project path> gateway serve`. That rules out starting another
installation from `PATH` by accident, or picking another project by the working directory. It works
as long as that Python is there: after `uv tool uninstall` or a reinstall, run `gateway install`
again, with `autoharness` on `PATH`.

## 4. The hook for Claude Code and Antigravity CLI (optional)

Without the hook the agent goes through autoharness when it decides by itself that the skill fits.
The hook makes it mandatory: on the first real request of a session it reminds the agent to call the
skill, and until a brief is recorded it denies the tools that change things (`Write`, `Edit`, shell
commands that write, MCP tools). Reading files, and shell commands that only list or read (`ls`,
`cat`, `git status`, `Get-ChildItem`), stay open.

The hook also:

- always denies writes to the task records in `.autoharness/`, by write tools and by the obvious
  shell commands (`>`, `tee`, `sed -i`, `Set-Content`, `Out-File`, `cp`/`mv`/`rm`, writes from
  `python -c`), and any tool's access to the `integrity.key`. The exception is
  `.autoharness/config.json`, the user's settings. This guards against shortcuts; it is not a
  sandbox: what gets past it is caught by the signature of `task.json` at teardown;
- stops a loop: the sixth identical tool call in a row (the same command, the same input) is denied.
  The agent is told to change the call, find what is missing (`autoharness need`) or ask the user.
  Any other call resets the count;
- leaves alone a subagent that was given a review prompt of the task (a critic or a vet reviewer): it
  reviews, it does not own the task and cannot claim it;
- in Claude Code (the `Stop` event) sends the agent back when it ends its turn while its own task is
  still open: to finish the check, the critic and `teardown`, or to tell the user what it is waiting
  for. It sends it back once per state of the task. If the agent stops again and nothing changed, the
  turn ends, so a question to the user still gets through. agy has no such event.

```bash
autoharness install-hook            # into the project's .claude/settings.json
autoharness install-hook --user     # into ~/.claude/settings.json, for every project
autoharness install-hook --remove   # take it out
```

It applies in new sessions (or open `/hooks` in the current one). To skip autoharness in one session
because the user said so: `autoharness waive <session id> --reason "<their words>"`.

### Antigravity CLI (agy)

```bash
autoharness --agent agy install-hook          # into the project's .agents/hooks.json
autoharness --agent agy install-hook --user   # into ~/.gemini/config/hooks.json, for every project
autoharness --agent agy install-hook --remove
```

agy has no event for the user's request, so the reminder comes from `PreInvocation` on the first
model call of a turn (`invocationNum` 0): the hook takes the request's text from the transcript and
answers with an `ephemeralMessage`. `PreToolUse` closes `write_to_file`, `replace_file_content`,
`run_command`, `call_mcp_tool` and the like with `{"decision": "deny"}`. A session is a
`conversationId`; `waive` takes it. agy runs the hook through `cmd /c` in the folder of `hooks.json`,
and the project comes from `workspacePaths`. Note: if the hook did not run (no `autoharness` on
`PATH`, a start error), agy denies the tool call, and it treats an empty `{}` from the hook as a
denial too. If every agy tool is suddenly refused with `pre-tool hook`, check `autoharness hook` in
the same console or remove the hook with `--remove`. It applies after agy restarts; a project
`hooks.json` is loaded once the folder is trusted.

## Checking the whole setup

Ask the agent in a new session: "run autoharness init for the task 'read https://example.com' and add
fetch". Expected: the agent calls `autoharness init`, then `autoharness add mcp fetch`; with the
gateway the answer contains `"via": "gateway"`, and the page is read through `harness_call` without a
restart.

## Several agents in one project

```bash
autoharness install-skill --all
autoharness --agent claude gateway install
autoharness --agent codex  gateway install
```

Tasks and the gateway are shared by the agents (`.autoharness/`); the configs are each agent's own.
Inside a session the agent is detected by the environment variables it sets for its commands
(`CLAUDECODE`, `CODEX_THREAD_ID`, `GEMINI_CLI`, `ANTIGRAVITY_AGENT`, …). `ANTIGRAVITY_AGENT` is
checked first: agy started from Claude Code inherits its `CLAUDECODE`. The order is `--agent`, then
`$AUTOHARNESS_AGENT`, then detection, else `claude`; the agent is stored in the task at `init`. If
detection fails, set `AUTOHARNESS_AGENT=<agent>` in the environment you start the agent from.

## How autoharness writes the configs of different agents

| `--agent` | Project MCP config | Project skills |
|---|---|---|
| `claude` (Claude Code) | `.mcp.json` | `.claude/skills` |
| `codex` (Codex CLI) | `.codex/config.toml` | `.agents/skills` |
| `gemini` (Gemini CLI) | `.gemini/settings.json` | `.gemini/skills` |
| `qwen` (Qwen Code) | `.qwen/settings.json` | `.qwen/skills` |
| `gigacode` (GigaCode CLI) | `.gigacode/settings.json` | `.gigacode/skills` |
| `strands` ([Strands harness](https://github.com/strands-agents/harness-sdk)) | `.agent/mcp.json` | `.agent/skills` |
| `agy` (Antigravity CLI) | `.agents/mcp_config.json` | `.agents/skills` |

Lockfiles and recipes keep a config in one canonical form (the `.mcp.json` format, secrets as
`${VAR}`), and the adapter translates it on write: for the Gemini family remote servers become
`httpUrl`/`url`; for Codex secrets go to `env_vars`, `bearer_token_env_var`, `env_http_headers`.
Codex does not expand `${VAR}` in `args`; then `add` returns `warnings`. Antigravity CLI does not
expand `${VAR}` either, but passes its environment to servers: `"K": "${K}"` is simply not written,
remote servers become `serverUrl`, and for `${VAR}` in headers, `args` and renamed variables `add`
returns `warnings`. Codex reads the project `.codex/config.toml` only for trusted projects. Edits to
TOML are wrapped in `# >>> autoharness:<name>` markers, so comments and the rest of the user's config
are left alone.

## Uninstalling

```bash
autoharness --agent claude gateway uninstall   # removes `autoharness` from the agent's config
autoharness install-hook --remove              # removes the hook
autoharness prune                              # removes everything tasks left in the project
```

The skill is removed with the `autoharness` folder in the skills directory (table above). The CLI:
`uv tool uninstall autoharness`.
