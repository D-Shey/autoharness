---
name: session-compactor
description: Dynamic multi-tiered context compaction and layered session transcript folding for agents. Use when agent context window exceeds token budgets during long-running sessions, multi-round tasks, or autonomous drive loops to collapse intermediate tool outputs and summarize past epochs while preserving full logs on disk.
---

# Session Compactor

## When to use
Use during long multi-step agent conversations, batch task runs, or autonomous agent execution loops to:
- Compress sprawling `transcript.jsonl` files and tool outputs into tiered levels of detail.
- Retain exact execution fidelity on recent steps while distilling noisy intermediate tool outputs (test suites, builds, grep searches).
- Condense completed task milestones or early epochs into high-density structured summaries.
- Maintain full reversibility by persisting uncompressed step traces to `.autoharness/folded/`.

## Steps
1. **Analyze Transcript / Session Volume**:
   - Inspect step count and token estimate (`autoharness compact status`).
   - Determine target token limit and retention count (`keep_last`).
2. **Apply Hierarchical Tiered Folding**:
   - **Anchor Tier (Prompt Cache Invariant, AP-5)**: Preserve the initial step(s) (system prompt, primary goal, base tool definitions) 100% verbatim. Never mutate or fold step 0: changing the prefix breaks KV-cache reuse on all modern LLM providers (Anthropic, Gemini, OpenAI, DeepSeek), destroying cache hit rates and causing 4–8× higher billing and TTFT latency spikes.
   - **Recent Tier (Level 0)**: Keep the last $N$ steps (default: 4-6) fully uncompressed to preserve active reasoning state.
   - **Distill Tier (Level 1)**: For intermediate steps, preserve caller intent and arguments, but compress long tool outputs (pytest, build, diffs) using cascading output compression (`compression.compress_output`).
   - **Epoch Tier (Level 2)**: For older completed milestones, aggregate intermediate steps into structured epoch summaries (`Goal -> Action Taken -> Outcome -> Evidence Artifacts`).
3. **Persist and Link Reversible Traces**:
   - Save full raw steps to `.autoharness/folded/<session_id>.full.jsonl`.
   - Embed lightweight backlink pointers (`[Full log: .autoharness/folded/...]`) in the folded transcript so agents can drill down on demand.
4. **Export Compact Transcript & Prompt Cache Verification**:
   - Output clean JSON or compact Markdown report for downstream model prompts with `prompt_cache_status`.
   - Verify prompt cache stability via `harness_check_prompt_cache` to ensure no volatile prefix tokens or tool drift occurred.

## How to verify
1. Run `autoharness compact status` to check compression ratio and token savings (target: 70-85% reduction on long sessions).
2. Verify that `tiers.anchor_steps >= 1` and `prompt_cache_status.anchor_preserved == true`.
3. Verify that folded transcripts retain all recent active steps intact and link back to full logs on disk.

## Facts found here
- Intermediate pytest traces and large git diffs constitute over 80% of token consumption in multi-round agent sessions.
- Preserving the initial anchor step (AP-5) guarantees up to 80-90% discount on input tokens across long multi-turn sessions by keeping provider KV-cache hot.
- Preserving the last 4-6 steps verbatim allows models to maintain immediate conversational momentum without hallucinating lost state.
