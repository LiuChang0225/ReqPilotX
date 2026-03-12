# ReqPilotX Repository Instructions

## Context Sync

After any substantial debugging, implementation, architectural decision, or sample-switching work, use the `$context-sync` skill and update:

1. `/Users/liuchang/Documents/New project/docs/active-context.md`
2. the current branch-local `prototype/docs/*active-context.md` file, if one exists

Treat this as required before ending the turn whenever project state changed in a way that a new Codex window would need to know.

Do not update context files for trivial edits with no state change.

## Scope

Keep context updates short and operational:

- current objective
- active sample
- recently fixed issues
- current blockers
- next steps
- active port if relevant

Do not duplicate large architecture content from `/Users/liuchang/Documents/New project/docs/项目.md`.
