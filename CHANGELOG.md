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
