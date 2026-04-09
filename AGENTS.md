# ReqPilotX Repository Instructions

## Context Sync

After any substantial debugging, implementation, architectural decision, or sample-switching work, use the `$context-sync` skill and update:

1. `/Users/liuchang/Documents/New project/docs/active-context.md`
2. the current branch-local `prototype/docs/*active-context.md` file, if one exists for the active track/worktree

Treat this as required before ending the turn whenever project state changed in a way that a new Codex window would need to know.

Do not update context files for trivial edits with no state change.

## Context Routing

All windows must read this global set first:

1. `AGENTS.md`
2. `docs/项目.md`
3. `docs/开发执行清单.md`
4. `docs/active-context.md`
5. `prototype/eval/progress/track_registry.json`

Then read only the branch-local active-context that matches the current track:

- `release-foundation` -> `prototype/docs/release-foundation-active-context.md`
- `extraction-quality-loop` -> `prototype/docs/extraction-active-context.md`
- `rag-quality-loop` -> `prototype/docs/rag-quality-active-context.md`
- `product-kb` -> `prototype/docs/product-kb-active-context.md`
- `angular-feasibility` -> `prototype/docs/angular-feasibility-active-context.md`

`prototype/docs/extraction-active-context.md` is no longer a universal must-read. It is extraction-track-only context.

## Control Tower

The execution truth for multi-branch work now lives in:

- `prototype/eval/progress/track_registry.json`
- `/project-progress`
- `/api/progress/*`
- `prototype/scripts/trackctl.py`

Use repo-managed skills from `/.codex/skills-src/` and sync them into `$CODEX_HOME/skills` via `scripts/sync_codex_skills.py` when skills change.

## Scope

Keep context updates short and operational:

- current objective
- active sample
- recently fixed issues
- current blockers
- next steps
- active port if relevant

Do not duplicate large architecture content from `/Users/liuchang/Documents/New project/docs/项目.md`.
