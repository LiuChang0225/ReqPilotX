# Next Agent Handoff Prompt

更新时间：2026-04-09 19:10 CST

把下面对应角色的提示词直接交给下一个窗口。

## 1. 协调窗口 Prompt

```text
You are taking over the ReqPilotX repository.

Workspace root:
/Users/liuchang/Documents/ReqPilotX

Use $reqpilot-control-tower.

You are the coordination window. Your job is to keep the control tower aligned with the active plan, not to do long-running implementation in the main window.

Read these files first:
1. AGENTS.md
2. docs/项目.md
3. docs/开发执行清单.md
4. docs/active-context.md
5. prototype/eval/progress/track_registry.json

Then:
1. Refresh /project-progress or /api/progress/snapshot
2. Check tracks, branches, gates, and drift alerts
3. Keep at most 3 active tracks
4. Generate or update worker task packages
5. Do not let workers drift outside their allowed paths

Current operating rules:
- Long-lived work uses separate worktrees and branches, not sub-agents in one directory
- Active tracks are:
  - release-foundation
  - extraction-quality-loop
  - rag-quality-loop
- Queued tracks are:
  - product-kb
  - angular-feasibility
- Only open a queued track when an active track becomes DONE or BLOCKED_BY_DECISION

Daily control loop:
1. Decide which active track keeps moving
2. Decide which risky track must be contained first
3. Decide whether a queued track can be promoted

Before ending the turn:
- ensure docs/active-context.md is current if the project state changed
- ensure the relevant track-local active-context is updated
- keep the control tower as the execution truth
```

## 2. Worker Prompt Template

把下面模板中的占位符替换后交给对应支线窗口。

```text
You are taking over the ReqPilotX repository.

Workspace root:
/Users/liuchang/Documents/ReqPilotX

Use $<track-skill-name>.

You are responsible only for track: <track_id>
Worktree: <worktree_path>
Branch: <branch_name>

Read these files first:
1. AGENTS.md
2. docs/项目.md
3. docs/开发执行清单.md
4. docs/active-context.md
5. prototype/eval/progress/track_registry.json
6. <track-local-active-context>

Goal:
<current goal>

Non-goals:
<things you must not expand into>

Keep green:
<tests / smoke / benchmark list>

Execution rules:
1. Only edit allowed_paths for this track
2. Start with: python prototype/scripts/trackctl.py start --track <track_id>
3. Record checkpoints with: python prototype/scripts/trackctl.py checkpoint ...
4. Record gates with: python prototype/scripts/trackctl.py gate ...
5. Before handoff, update docs/active-context.md and <track-local-active-context>
6. Before handoff, run: python prototype/scripts/trackctl.py handoff ...

Do not re-plan the project from scratch. Continue from the current documented state and keep your work inside this track.
```

## 3. Track -> Context / Skill 映射

- `release-foundation`
  - skill: `reqpilot-track-release-foundation`
  - local context: `prototype/docs/release-foundation-active-context.md`
- `extraction-quality-loop`
  - skill: `reqpilot-track-extraction-quality`
  - local context: `prototype/docs/extraction-active-context.md`
- `rag-quality-loop`
  - skill: `reqpilot-track-rag-quality`
  - local context: `prototype/docs/rag-quality-active-context.md`
- `product-kb`
  - skill: `reqpilot-track-product-kb`
  - local context: `prototype/docs/product-kb-active-context.md`
- `angular-feasibility`
  - skill: `reqpilot-track-angular-feasibility`
  - local context: `prototype/docs/angular-feasibility-active-context.md`

## 4. Worktree Bootstrap

```bash
cd "/Users/liuchang/Documents/ReqPilotX"
git checkout main
git fetch origin
git pull --ff-only origin main
mkdir -p worktrees/_active
git worktree add worktrees/_active/release-foundation -b codex/release-foundation main
git worktree add worktrees/_active/extraction-quality-loop -b codex/extraction-quality-loop main
git worktree add worktrees/_active/rag-quality-loop -b codex/rag-quality-loop main
```

## 5. Repo-Managed Skills

If skills were changed in the repo, sync them before opening new worker windows:

```bash
cd "/Users/liuchang/Documents/ReqPilotX"
python3 scripts/sync_codex_skills.py --overwrite
```

Then restart Codex so the synced skills are picked up.
