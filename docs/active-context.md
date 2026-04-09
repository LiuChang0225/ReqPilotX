# Active Context

更新时间：2026-04-09 23:23 CST  
适用仓库：`/Users/liuchang/Documents/New project`

长期背景：[`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/项目.md)

## 当前目标

- 把 ReqPilotX 的治理引导层落到仓库里：`track registry`、控制塔看板、repo-managed skills、track-local context routing
- 让后续多窗口工作按固定支线推进，不再默认依赖长聊天记录或统一读取 extraction 上下文

## 活跃分支 / 工作树

- 当前编辑分支：`main`
- 控制塔已初始化到：`/Users/liuchang/Documents/New project/.git/reqpilotx-progress/progress_tracker.json`
- 第一阶段活跃支线已注册：
  - `codex/release-foundation`
  - `codex/extraction-quality-loop`
  - `codex/rag-quality-loop`
- 第二阶段排队支线已注册：
  - `codex/product-kb`
  - `codex/angular-feasibility`
- 当前仓库还没有实际创建这 3 个活跃 worktree；控制塔现在会把它们显示为 `TODO / ON_PLAN / not_started`

## 当前样本

- 当前没有切换新的业务样本
- 当前重点样本从“文档内容”转成“治理执行面”：`/project-progress`、`track_registry.json`、`trackctl.py`

## 最近已修 / 已落地

- 新增 `prototype/eval/progress/track_registry.json`，注册 5 条固定 track
- 控制塔接口扩展为 tracks / branches / gates / alerts 视图，并支持 `POST /api/progress/checkpoint`
- `/project-progress` 已新增 `Tracks Overview`、`Branch Lanes`、`Gate Board`、`Drift Alerts`
- 新增 `prototype/scripts/trackctl.py`，支持 `init/start/checkpoint/gate/handoff/status`
- 新增 repo-managed skills 源码目录 `/.codex/skills-src/`，并已同步到 `$CODEX_HOME/skills`
- `prototype/docs/extraction-active-context.md` 已收窄为 extraction 专属上下文，不再是所有窗口默认必读
- `AGENTS.md`、`docs/next-agent-handoff-prompt.md`、`docs/codex-git-workflow.md`、`docs/项目.md` 已改成 track-based 路由和控制塔工作方式
- 定向验证已通过：
  - `tests.test_progress_control_tower`
  - `py_compile` for `progress_tracker.py` / `main.py` / `trackctl.py`
  - `python3 scripts/sync_codex_skills.py --validate`

## 当前 blockers

- 3 条活跃支线的实际 worktree 还没创建，所以 branch lane 现在仍停留在 `TODO / ON_PLAN / not_started`
- 新 skills 已同步到 `$CODEX_HOME/skills`，但要重启 Codex 后新窗口才能直接触发它们
- 当前只完成治理 bootstrap，还没开始 `release-foundation / extraction / rag` 的功能实现本体

## 下一步

- 重启 Codex，让新 skills 生效
- 按 `docs/next-agent-handoff-prompt.md` 和 `docs/codex-git-workflow.md` 创建 3 个活跃 worktree
- 在协调窗口打开 `/project-progress`，再给 3 个 worker 窗口下发各自 track prompt
- 每条支线开工后先运行 `python prototype/scripts/trackctl.py start --track <id>`

## 活跃端口

- 控制塔页面：`/project-progress`
- 应用主端口仍按现有本地运行约定使用 `8000`；本轮未重新启动服务，只完成了治理层实现与初始化
