---
name: reqpilot-track-rag-quality
description: Execute the ReqPilotX rag-quality-loop track with strict scope on labeled evidence sets, retrieval tuning, and judgment-quality gates.
---

# Trigger

Use this skill only in the `rag-quality-loop` worker window.

# Mandatory Reads

1. `AGENTS.md`
2. `docs/项目.md`
3. `docs/开发执行清单.md`
4. `docs/active-context.md`
5. `prototype/eval/progress/track_registry.json`
6. `prototype/docs/rag-quality-active-context.md`
7. `references/track-contract.md`
8. `references/gates.md`

# Scope

- Labeled evidence sets
- retrieval tuning
- judgment-quality gates

# Non-goals

- No parser/extractor changes
- No Entra auth work
- No broad frontend refactor

# Required Gates

Read `references/gates.md`.

# Checkpoint Cadence

- Start with `trackctl start --track rag-quality-loop`.
- Checkpoint after each benchmark result, tuning round, blocker, and wrap-up.

# Done Definition

- Evidence metrics are measurable.
- `sample10` and labeled-set gates are current.
- Global and RAG contexts are updated.
- Residual risks are explicit.

# Handoff Format

- Benchmark set
- Retrieval change
- Gate result
- Next tuning direction

