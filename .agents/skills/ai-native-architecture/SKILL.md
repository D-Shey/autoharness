---
name: ai-native-architecture
description: Use when designing or verifying AI-Native harness subsystems: Ontology 2.0 natural-language domain maps, Dormant Data Truth Engines, Delayed Assumption Postmortems, Devil's Advocate critics, Bounded Autonomy, and Anti-Cognitive-Surrender approval UX.
---

# AI-Native Architecture

## When to use
Use when building or auditing agentic harnesses where raw LLM capability is not the bottleneck, and failures come from:
- Missing domain semantics and canonical source-of-truth confusion (RAG shredding documents without causal context).
- Agentic self-reporting bias ("all tests pass") masking churn, rework, or silent scope shrinkage.
- Premature evaluation that misses delayed assumption failures across subsequent tasks.
- Human review fatigue ("cognitive surrender" / rubber-stamping) or excessive micro-gating inside bounded loops.

## Steps

### 1. Ontology 2.0 (Living Natural-Language Semantic Map)
Why: Formal ontologies (RDF/OWL) break on real-world ambiguity and cannot be maintained by practitioners, while naive RAG loses causal links and canonical vs legacy distinctions.
- Store the project map in a human- and LLM-editable Markdown file (e.g., `.autoharness/ontology.md`) organized into: Glossary/Terms, Authoritative Sources of Truth, Entity Lifecycles & Invariants, and Causal Links.
- Keep lookups selective (query relevant entries rather than dumping the entire map into context).
- Reconcile referenced file paths and AST symbols against ground-truth code so the map does not silently rot.

### 2. Dormant Data Truth Engine & Delayed Postmortem
Why: Curated status reports suffer from lossy compression and active filtering; truth lives in unfiltered execution traces, and architectural assumptions fail over time, not at immediate teardown.
- Compute dormant signals from repository history and task logs: file edit churn/thrashing, recurring followups after `done`, rework/regression ratios, and unverified/bypassed checks.
- Persist explicit task assumptions alongside touched files at `brief`/`teardown` time, and re-evaluate them against subsequent churn, rollbacks, and followups (`validated`, `challenged`, `falsified`).

### 3. Devil's Advocate & Anti-Cognitive-Surrender Gates
Why: Models optimize toward consensus and humans accept wrong AI outputs ~80% of the time when presented as routine sign-offs; checkpoints must justify their cost and force genuine judgment.
- Instruct independent critics to hunt for *missing counter-evidence* (what the author did not test or attach) and surface the 1–3 most *fragile assumptions* and quiet scope shrinkage ("pocket veto").
- Surface a crisp `judgment_focus` on human approval/simulation manifests (reversibility tier, irreversible blast radius, rollback plan, and the specific tradeoff question to judge) instead of burying risk in raw logs.
- Support explicit task autonomy bounds (`allowed_paths`, `max_tokens`, `escalate_on`) so routine reversible work proceeds at speed while boundary crossings trigger management by exception.

### 4. Outcome-Anchored Evals (Compute ROI & Anti-Workslop)
Why: Activity metrics (calls, speed) mask rework ("workslop") and runaway token spend.
- Pair pass rate and token/cost totals with outcome efficiency (pass rate per 1k tokens / per dollar) and rework/regression penalties while keeping all existing eval fields backward-compatible.

## How to verify
- Run unit tests against real temporary projects, SQLite databases, and git repositories (zero-mock tests) covering ontology parsing/drift detection, dormant churn/postmortem classification, critic prompt sections, approval judgment focus, bounded autonomy checks, and eval ROI calculation.
- Run the full existing test suite and `autoharness codemap --check-shims` to confirm zero regressions and zero stub implementations.

## Sources
- Kai-Fu Lee, *AI Native: The Mandate to Transform Your Company* (2026) — Chapters 5–9 (Ontology 2.0, Dormant Data, Boss AI, Delayed Postmortem, Cognitive Surrender, Bounded Autonomy).
- Anthropic, *Building Effective Agents* (Evaluator-Optimizer & Contract-First Tool Design): https://www.anthropic.com/engineering/building-effective-agents
- Google Research Harness²: https://arxiv.org/abs/2610.14224
- Consequence-Aware Approval: https://arxiv.org/abs/2610.40753
