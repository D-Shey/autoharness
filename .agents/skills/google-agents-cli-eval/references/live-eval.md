# Evaluating Live agents

`--mode adk_live` runs an eval over the agent's `/run_live` WebSocket instead of
`/run_sse`. Pass it on its own for a local autoboot, or alongside the agent's
`https://` `--url` when deployed (for Agent Runtime in Gemini platform, the full engine URL). The
dataset, the trace output, and the `eval grade` step are unchanged.

```bash
# Local autoboot
agents-cli eval generate --mode adk_live

# Deployed agent
agents-cli eval generate --mode adk_live --url https://my-live-agent.run.app --app-name app

# Chain generate + grade
agents-cli eval run --mode adk_live --url https://my-live-agent.run.app --app-name app \
  --metrics final_response_quality
```

## What gets graded

Live replies are audio, and are transcribed by default, so the **transcript** is
what gets graded. Raw audio and video bytes are dropped from the trace.

## How cases are played

Each user turn is sent as **text** over **one persistent socket**, in order. A
single-turn case (`prompt`) is just a one-turn conversation. History lives in
the live session rather than being seeded over HTTP, so the model conditions on
the real running conversation, and multi-turn cases produce a trajectory that
`multi_turn_*` metrics can score.

Author **user-only** turns. The agent generates every reply over the live
session, so pre-authored agent turns are ignored (the CLI warns) and are not
seeded as history.

## Two failures that look like transport bugs

Both let the socket connect and then fail inside the session, so the error
points at `/run_live` when the cause is agent configuration.

| Cause | Fix |
|---|---|
| The agent is not on a Live model. `--mode adk_live` changes only the transport; it cannot make a non-Live agent bidi. The scaffold default is not a Live model, so a fresh project fails with WebSocket code 1011. | Switch the agent to a Live model. |
| On Gemini platform, the model's region is not pinned. Live models are served from a regional endpoint such as `us-central1`, not `global`. Unpinned, the model falls back to `GOOGLE_CLOUD_LOCATION`, which `agents-cli deploy` sets to `global` on Agent Runtime. | Pin the region on the model itself rather than steering `GOOGLE_CLOUD_LOCATION`, which is shared with sessions, telemetry, and grading. |

Grading is unaffected by either: `eval grade` passes its own location
(`--region`, default `global`), so `eval run --mode adk_live` chains both steps
correctly in one command.

> **ADK projects.** Current Live model IDs, and the code to pin a model's
> region, are in `/google-agents-cli-adk-code` (`references/adk-python-live.md`,
> "Models"). Transport mechanics are on the same page under "Events" and
> "Serving and testing a Live agent".
