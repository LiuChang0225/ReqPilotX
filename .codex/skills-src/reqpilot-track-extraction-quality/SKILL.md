---
name: reqpilot-track-extraction-quality
description: Execute the ReqPilotX extraction-quality-loop track with strict scope on batch extraction regression, taxonomy, and benchmark stability.
---

# Trigger

Use this skill only in the `extraction-quality-loop` worker window.

# Mandatory Reads

1. `AGENTS.md`
2. `docs/项目.md`
3. `docs/开发执行清单.md`
4. `docs/active-context.md`
5. `prototype/eval/progress/track_registry.json`
6. `prototype/docs/extraction-active-context.md`
7. `references/track-contract.md`
8. `references/gates.md`

# Scope

- Batch extraction regression
- issue taxonomy
- benchmark stability

# Non-goals

- No auth or feedback product work
- No RAG evidence tuning
- No Entra / readiness changes

# Required Gates

Read `references/gates.md`.

# Checkpoint Cadence

- Start with `trackctl start --track extraction-quality-loop`.
- Checkpoint after every batch run, regression review, blocker, and wrap-up.

# Done Definition

- Batch artifacts are reproducible.
- Legacy benchmark stays green.
- Global and extraction contexts are updated.
- Residual risks are explicit.

# Handoff Format

- Batch used
- Findings
- Benchmark impact
- Next hypothesis

