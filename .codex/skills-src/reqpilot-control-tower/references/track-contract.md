# Control Tower Contract

- Only the coordination window uses this skill.
- The source of truth is `prototype/eval/progress/track_registry.json` plus the runtime control-tower data.
- Never activate more than 3 tracks at once.
- Every active track must have:
  - one owner window
  - one branch / one worktree
  - one gate set
  - one current next action
- If a track is `OFF_PLAN`, freeze expansion and resolve drift first.

