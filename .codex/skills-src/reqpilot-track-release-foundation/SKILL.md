---
name: reqpilot-track-release-foundation
description: Execute the ReqPilotX release-foundation track with strict scope on P0 stability, Entra auth, feedback skeleton, and trial-run readiness.
---

# Trigger

Use this skill only in the `release-foundation` worker window.

# Mandatory Reads

1. `AGENTS.md`
2. `docs/项目.md`
3. `docs/开发执行清单.md`
4. `docs/active-context.md`
5. `prototype/eval/progress/track_registry.json`
6. `prototype/docs/release-foundation-active-context.md`
7. `references/track-contract.md`
8. `references/gates.md`

# Scope

- P0 stability
- Entra auth integration
- feedback skeleton
- trial-run operational foundation

# Non-goals

- No extraction parser tuning
- No RAG prompt/ranking tuning
- No product-package architecture work

# Required Gates

Read `references/gates.md`.

# Checkpoint Cadence

- Run `trackctl start --track release-foundation` at kickoff.
- Run `trackctl checkpoint` after each meaningful sub-goal, blocker, gate, and before wrap-up.

# Done Definition

- Scope stays inside this track contract.
- Gates are updated.
- Global and track-local context are refreshed.
- Residual risks and next actions are explicit.

# Handoff Format

- What changed
- What passed / failed
- Residual risk
- Suggested next action

