---
name: reqpilot-control-tower
description: Coordinate ReqPilotX active tracks, keep the control tower aligned with track_registry, inspect branch drift, update plan/events, and control when new tracks can start.
---

# Trigger

Use this skill only in the coordination window for ReqPilotX.

# Mandatory Reads

1. `AGENTS.md`
2. `docs/项目.md`
3. `docs/开发执行清单.md`
4. `docs/active-context.md`
5. `prototype/eval/progress/track_registry.json`
6. `references/track-contract.md`
7. `references/gates.md`

# Scope

- Refresh `/project-progress`, inspect `tracks / branches / gates / alerts`, and decide the current working set.
- Maintain progress plan items and milestone events.
- Check drift, gate failures, and stale tracks before opening new work.
- Prepare bounded task packets for worker windows.

# Non-goals

- Do not perform long-lived implementation in this window.
- Do not assign more than 3 active tracks at once.
- Do not let a worker change tracks mid-stream.

# Required Gates

Read `references/gates.md`. A queued track may become active only when an active track is `DONE` or `BLOCKED_BY_DECISION`.

# Checkpoint Cadence

- Start of day: refresh the control tower and inspect drift/gates.
- Every worker checkpoint: update plan progress or add an event.
- End of day: make sure each active track has a fresh checkpoint or risk event.

# Done Definition

- The active track set is correct.
- Every active track has an owner, a gate, and a next action.
- Drift alerts are triaged.
- Global `docs/active-context.md` is updated when project state changes.

# Handoff Format

- Active tracks
- Current gate status
- Drift / blockers
- Next task packet per worker

