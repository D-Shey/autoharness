# Security policy

AutoHarness installs MCP servers and skills on behalf of a coding agent, so its trust checks are a
security boundary we take seriously. How each protection works, and where it ends, is described in
[docs/security.md](docs/security.md).

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
**Security → Report a vulnerability** on this repository
([private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)).
Do not open a public issue for it.

Useful to include:

- what an attacker controls (a registry entry, a skill's files, a committed `gateway.json`, the task
  text, a file under review) and what they gain;
- the AutoHarness version (`pip show autoharness` or the commit) and the agent involved;
- the smallest set of steps that reproduces it.

You can expect an acknowledgement within a few days and a fix or a mitigation plan once the issue is
confirmed. Credit is given in the release notes unless you prefer otherwise.

## In scope

- A skill or server installed without the trust check or the user's consent it should need.
- Secrets written into a config, passed to a server that was not given them, or leaked into logs.
- The hook letting a tool write the task records or read the integrity key.
- A review answer, vet verdict or approval that can be forged in a way the docs say is prevented.

## Out of scope

The hook and `--approve` frame the agent's procedure; they are not a sandbox. An agent that decides to
go around the user (for example by `waive`, or by writing a reviewer's answer file itself) is a known
limit described in [docs/security.md](docs/security.md), not a vulnerability. Local MCP processes
connect wherever they want; `allowed_hosts` limits remote servers only.
