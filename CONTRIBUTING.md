# Contributing to AutoHarness

Thanks for taking the time. AutoHarness is a small, dependency-free Python CLI; most contributions
fit in one pull request.

## Set up

```bash
git clone https://github.com/D-Shey/autoharness.git && cd autoharness
uv tool install -e .            # the autoharness command sees your edits right away
uv run --extra dev pytest       # the whole suite; it needs no network
```

## What makes a good change

- **General, not task-specific.** AutoHarness works for any task an agent takes on: code, QA,
  environment setup, documents, visual work. A fix that only helps one benchmark or one prompt is not
  a fix. Describe the class of failure it closes.
- **A test first.** Pin the defect down with a failing test, then fix it. Tests live in `tests/` and
  run offline; fake registries and servers are already there (`tests/fake_mcp_server.py`,
  `fake_registry` in `tests/test_autoharness.py`).
- **No new runtime dependencies.** The CLI uses the standard library only.
- **Words for the agent are part of the interface.** Every command's `next` and every error tell the
  agent what to do. Keep them concrete: what is wrong, and the exact command that fixes it.
- **Match the code around you**: naming, comment density, and the way existing messages are phrased.

## Adding a language

AutoHarness reads tasks in every language it has a language file for. To add one:

1. Create `src/autoharness/data/lang/<code>.json` with any of: `keywords` and `exclude` per catalog
   capability, `stopwords`, `endings`, `no_constraints`, `dependency_headings`
   (see `ru.json` for a complete example).
2. Create `tests/lang/<code>.json` with the cases the language must handle: `classify`, `word_forms`,
   `filler_words`, `recipe`, `no_constraints`, `constraints`, `non_ascii_command`. The tests pick
   them up by themselves.
3. Run `uv run --extra dev pytest -k every_language`.

No code changes are needed.

## Adding a capability to the catalog

`src/autoharness/data/catalog.json` lists capabilities (with keywords, curated MCP servers and skills,
search queries and what a critic should look at) and trusted publishers. A new entry needs a reason it
is trustworthy (official publisher, pinned version) and a test in `tests/` that a typical task text
selects it and an unrelated one does not.

## Pull requests

- One topic per pull request, with the failing test and the fix together.
- `uv run --extra dev pytest` passes on your machine; CI runs it on Linux and Windows with Python
  3.11 to 3.13.
- Update `README.md` or `docs/` when behaviour the user sees changes, and add a line to
  `CHANGELOG.md`.

## Reporting a security issue

Please do not open a public issue; see [SECURITY.md](SECURITY.md).
