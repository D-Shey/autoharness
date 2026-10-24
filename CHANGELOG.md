# Changelog

All notable changes to AutoHarness are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

The first public version.

### The procedure

- `init` with `light`, `full`, `reviewed` and `auto` modes; a task that needs any catalog capability
  is raised from `light` to `full`.
- A brief before the work: the quality bar, the areas of know-how (each searched and covered by a
  found or created skill), the method from cited sources, the missing inputs, the user's constraints
  quoted verbatim, the open choices settled by what the person who asked would expect, and the checks.
- Evidence: the reference itself, facts measured from it before the first result, whole results and
  close-ups, the working-tree diff for code.
- `verify run`: autoharness runs a check itself; the exit code decides, and a run goes stale when the
  code changes.
- Independent review: a fresh critic (or `critic --run claude|codex|agy`) with a one-time `ANSWER-ID`,
  item-by-item coverage of the brief, blind A/B comparison for visual work, an optional code-quality
  reviewer (`--review pair`), disputes ruled by a fresh critic, rulings that later tasks respect.
- Guards against loops: a change of approach, an arbiter with a measurable check, then the user.
- Learning: skills the agent writes and saves to a local library, recipes that rerun their queries,
  lessons from friction.
- `skill test`: fresh agents do 1-3 realistic tasks with a new skill and without it, and a blind judge
  compares each pair. `skill new` explains how to write a skill that gets opened and followed, and
  `skill save` warns when a skill was never tested, lost its test, or its description retells the steps.

- The project's journal: when a project keeps one (`JOURNAL.md`, or the `journal` setting; `off`
  turns it off), a task is not done until it adds an entry. `autoharness journal` drafts it from the
  task's record (goal, checks, lessons, sources) in the journal's own heading, date format and order;
  a remark too short to be an entry does not count, and in light mode a missing one only warns.

### Tools and trust

- Discovery in Anthropic's plugin marketplace, the official MCP registry and skills.sh, with trust
  scores; a curated catalog of capabilities and trusted publishers.
- Static scanning of skills before they reach the project; vetting of candidates against the user's
  constraints by an independent reviewer.
- The gateway: MCP servers added during a task work without restarting the agent; per-machine approval
  of a committed `gateway.json`; a minimal, secret-free environment for local servers;
  `allowed_hosts` for remote ones.
- Signed task records (HMAC, the key outside the project); a change journal so only what autoharness
  installed is removed.

### Agents and languages

- Adapters for Claude Code, Codex CLI, Gemini CLI, Antigravity CLI, Qwen Code, GigaCode and Strands.
- A hook for Claude Code and Antigravity CLI that keeps write tools closed until the brief, stops
  identical-call loops, keeps tools away from the task records and the key, and lets a reviewer
  subagent read without taking the task over.
- Language files (`data/lang/<code>.json`): tasks are understood in English and Russian, and a
  language is added without code changes.

### Fixed

- A skill an earlier task kept in the project covers a later task's areas (`skill <name>` in the
  brief); naming it in `--gaps` again is refused at once with what to do instead, and `skill new`
  says to edit it in place. Before, the brief refused it as a cover, `skill new` refused to make it,
  and teardown waited for it to be created, with no way out.
- A notification the agent's harness sends as a prompt (`<task-notification>`, `<system-reminder>`)
  is not recorded as the user's message: it no longer counts as a lesson, nor as the user's word
  that `teardown --accepted` waits for.
