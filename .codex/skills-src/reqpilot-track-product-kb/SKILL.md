---
name: reqpilot-track-product-kb
description: Execute the ReqPilotX product-kb track with strict scope on dual-scope knowledge architecture, package binding, and admin curation.
---

# Trigger

Use this skill only in the `product-kb` worker window after the coordinator activates the track.

# Mandatory Reads

1. `AGENTS.md`
2. `docs/项目.md`
3. `docs/开发执行清单.md`
4. `docs/active-context.md`
5. `prototype/eval/progress/track_registry.json`
6. `prototype/docs/product-kb-active-context.md`
7. `references/track-contract.md`
8. `references/gates.md`

# Scope

- Product package binding
- dual-scope retrieval
- admin curation

# Non-goals

- No Angular shell work
- No parser/extractor quality loop work
- No trial-run auth foundation work

# Required Gates

Read `references/gates.md`.

# Checkpoint Cadence

- Start with `trackctl start --track product-kb`.
- Checkpoint after each schema/API/UI milestone, blocker, and wrap-up.

# Done Definition

- Product package binding exists.
- Dual-scope retrieval behaves predictably.
- Global and product-kb contexts are updated.
- Residual risks are explicit.

# Handoff Format

- Package model
- Retrieval precedence
- Admin curation status
- Next integration step

