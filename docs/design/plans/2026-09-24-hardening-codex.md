# AutoHarness reliability and the Codex integration

The user approved the fixes from the audit and the Codex check. The work is done in order in the
current project; every defect is first pinned down by a test.

## Criteria and order

- [x] HTTP: refuse redirects before tokens are passed to another address, including when a session
  ends; regressions against a local HTTP server.
- [x] TOML: recognise the valid forms of an MCP declaration, check the result before writing, keep
  comments and the user's own entries.
- [x] Reproducibility: pin the versions of the curated executable packages, keep the version
  information in recipes and the docs.
- [x] State: serialise MCP install operations, keep a record of an unfinished install before the
  config changes, support safe recovery after a failure; check write errors and a rerun.
- [x] Codex: the real CLI reads the config; the skill is installed into the expected directory; the
  gateway config is tied to the project explicitly; Codex calls a local test MCP server. The checks
  use temporary CODEX_HOME/AUTOHARNESS_HOME without real secrets.
- [x] Full pytest, a build check, the docs and an independent final review.

## Decisions

- Refusing redirects was chosen over following them: the config must name the final MCP endpoint;
  this rules out going around the allowlist and passing arbitrary secret headers to another address.
- Python 3.10 stays supported: tomli is needed only before Python 3.11.
- Comparing the quality of real tasks needs a separate task set; the integration check in this work
  checks the real process boundaries.

## Log

- The original audit: 339 tests passed; the redirect and the TOML duplicate were reproduced.
- Codex CLI locally: 0.156.0; the MCP/skills requirements were checked against OpenAI's docs.
- 2026-09-25: the check of the unfinished changes resumed. JSON output on invalid TOML was fixed; the
  HTTP tests were extended to 301/302/303/307/308.
- The independent review found markers removed inside TOML strings and Codex env_vars not updated
  when installing through another agent. Both defects were reproduced by tests and fixed.
- All six pinned versions were checked against the npm/PyPI APIs. Pinning transitive dependencies and
  skill revisions remains a separate task.
- Recovery covers the MCP/gateway install for now; removing and copying skills are not transactional.
  CI for Windows/Linux is set up; a remote run has not been done.
