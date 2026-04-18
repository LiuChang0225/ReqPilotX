---
name: reqpilot-track-angular-feasibility
description: Execute the ReqPilotX angular-feasibility track with strict scope on shell feasibility, auth integration, and migration reporting.
---

# Trigger

Use this skill only in the `angular-feasibility` worker window after the coordinator activates the track.

# Mandatory Reads

1. `AGENTS.md`
2. `docs/项目.md`
3. `docs/开发执行清单.md`
4. `docs/active-context.md`
5. `prototype/eval/progress/track_registry.json`
6. `prototype/docs/angular-feasibility-active-context.md`
7. `references/track-contract.md`
8. `references/gates.md`

# Scope

- Angular shell feasibility
- auth integration feasibility
- migration report

# Non-goals

- No direct replacement of the current workspace pages
- No parser/extractor or RAG tuning work
- No release-foundation auth implementation changes unless explicitly scoped as adapter work

# Required Gates

Read `references/gates.md`.

# Checkpoint Cadence

- Start with `trackctl start --track angular-feasibility`.
- Checkpoint after each spike result, blocker, and wrap-up.

# Done Definition

- Shell spike is runnable.
- Auth integration path is documented.
- Migration report is explicit about cost and risk.
- Global and angular-feasibility contexts are updated.

# Handoff Format

- Spike result
- Auth path result
- Migration recommendation
- Open risks

