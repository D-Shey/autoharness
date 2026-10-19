# AutoHarness modes and run comparison

**Goal:** lower the cost of the procedure for simple tasks and get measurable data on AutoHarness's
benefit.

**Design:** the existing full cycle stays the default mode. `init --mode light` skips the automatic
search and the mandatory brief; `--mode auto` picks the full cycle when the task matches known
capabilities. When a tool is missing, the task moves to the full mode with `mode full`. The CLI
records the time and outcome of its own commands without arguments or secrets. Separate `eval record`
and `eval compare` commands take the measurements of finished runs and count results only for pairs of
the same task.

**Limits:** AutoHarness does not get the model's tokens or the final score automatically unless the
agent or the user passes them. The comparison shows observed values and draws no causal conclusions
without a controlled task set.

## Tasks

- [x] Pin down with tests the `full`, `light`, `auto` modes, raising to `full`, the hook's behaviour
  and ending a task.
- [x] Implement the modes, keeping the old behaviour of `init` without the flag.
- [x] Pin down with tests recording CLI events without arguments and computing a task's duration.
- [x] Implement a safe task event log.
- [x] Pin down with tests the eval input, pair matching, no division by zero and missing pairs.
- [x] Implement `eval record` and `eval compare` with JSON results and input validation.
- [x] Update the bootstrap skill, README and the command reference; show a short scenario of both
  modes and an example comparison.
- [x] Run the targeted tests, the full pytest and the package build.

## Continued: the remaining ideas from the harness comparison

**Goal:** turn the remaining proposals into checkable CLI capabilities while keeping existing tasks
compatible.

**Architecture:** profiles set the mode and the requirement of an independent check at `init`. The
decision log is kept apart from the CLI call log and records the origin and fingerprints of tools.
Batch evaluation runs an external agent runner on the same named cases, gets a JSON result from it
and measures the time. The check catalog gives ready steps and records that a check ran, with
evidence. Python 3.10+, no new required dependencies.

**Measurement limits:** the model's tool calls, tokens and cost can come only from an external runner
or an explicit record; AutoHarness does not see them automatically in every agent. Unverified data is
not passed off as measured.

- [x] **Profiles (Deep Agents):** add `quick`, `standard`, `reviewed` with explicit rules for the
  mode, install permission and the critic requirement; keep the old behaviour without `--profile`.
  Check the choice and that the rules cannot be bypassed.
- [x] **Measurements (mini-SWE-agent):** record the number of tool calls from the runner and batch
  runs of named cases through an external command; check case isolation, runner errors and the pair
  comparison.
- [x] **Decision log (OpenHands SDK):** record choosing/rejecting a candidate, installing with origin
  and fingerprint, checking, removing with a reason; add a command to view it. Do not record tool
  configs or command arguments.
- [x] **Check templates (OpenAI's approach):** give concrete steps for a browser, an API, logs and a
  database; tie them to the `brief`/result and evidence; check the templates and recording the
  outcome.
- [x] Update README, the CLI reference and the bootstrap skill; run targeted tests, the full pytest
  and the build.
