# ReqPilotX Repository Instructions

## Context Sync

After any substantial debugging, implementation, architectural decision, or sample-switching work, use the `$context-sync` skill and update:

1. `/Users/liuchang/Documents/ReqPilotX/docs/active-context.md`
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

## Rule Ledgers

When a sample family turns into a recurring audit/fix program, keep a dedicated long-form rule ledger instead of hiding history in active-context handoff notes.

- For the SERES MDA extraction program, the authoritative ledger is:
  - `prototype/docs/seres-mda-rule-fix-context.md`
- This file is a repository rulebook, not a short handoff. Each meaningful SERES update should record:
  - document inventory and structure characteristics
  - discovered issues and affected sections/pages when known
  - suspected layer and root cause
  - promoted generic fix/rule, or the reason no rule was promoted
  - verification evidence, effectiveness, and residual risk
- Keep `docs/active-context.md` and branch-local `*active-context.md` files short; they should point to the ledger instead of duplicating its detail.
- Do not compress `prototype/docs/seres-mda-rule-fix-context.md` into a brief status summary once it becomes the active rule ledger for this document family.

## Scope

Keep context updates short and operational:

- current objective
- active sample
- recently fixed issues
- current blockers
- next steps
- active port if relevant

Do not duplicate large architecture content from `/Users/liuchang/Documents/ReqPilotX/docs/项目.md`.
