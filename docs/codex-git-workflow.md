# Codex Git / GitHub Workflow

适用范围：`/Users/liuchang/Documents/ReqPilotX`

这份文档定义当前仓库的控制塔、worktree、分支和上下文同步规则。执行真相源以控制塔为准，不再使用旧的“随任务自由开长期分支”方式。

## 1. 当前分支治理模型

当前模型固定为：

- `1` 个协调窗口
- 最多 `3` 条活跃实现支线
- 每条支线对应 `1` 个独立 worktree
- 每条支线必须映射到 `track_registry.json` 里的注册 track

当前第一阶段活跃支线：

1. `codex/release-foundation`
2. `codex/extraction-quality-loop`
3. `codex/rag-quality-loop`

排队支线：

1. `codex/product-kb`
2. `codex/angular-feasibility`

## 2. 控制塔是真相源

当前执行控制面固定为：

- 轨道注册表：`/Users/liuchang/Documents/ReqPilotX/prototype/eval/progress/track_registry.json`
- 控制塔页面：`/Users/liuchang/Documents/ReqPilotX/prototype/app/static/progress-dashboard.html`
- 控制塔接口：`/api/progress/*`
- 操作脚本：`/Users/liuchang/Documents/ReqPilotX/prototype/scripts/trackctl.py`

控制塔需要持续观测：

- `Tracks Overview`
- `Branch Lanes`
- `Gate Board`
- `Drift Alerts`

如果分支活动、门禁状态和控制塔不一致，以控制塔登记与实际 worktree 状态为准，先修正记录，再继续开发。

## 3. 标准 worktree 结构

标准目录固定为：

```text
/Users/liuchang/Documents/ReqPilotX/
├── prototype/                  # 主仓库，默认 main / 协调窗口
├── worktrees/
│   ├── _active/
│   │   ├── release-foundation/
│   │   ├── extraction-quality-loop/
│   │   └── rag-quality-loop/
│   └── _archive/
└── .codex/
    └── skills-src/
```

规则固定：

- `main` 只保留稳定基线与协调入口，不做长期功能实现
- 一个窗口只负责一个 worktree
- 不允许多个窗口共用同一个工作目录
- 不允许一个长周期支线拆成多个 agent 共用同一个 worktree

## 4. 启动一轮并行开发

先同步主线：

```bash
cd "/Users/liuchang/Documents/ReqPilotX"
git checkout main
git fetch origin
git pull --ff-only origin main
mkdir -p worktrees/_active
```

创建第一阶段 3 条活跃支线：

```bash
git worktree add worktrees/_active/release-foundation -b codex/release-foundation main
git worktree add worktrees/_active/extraction-quality-loop -b codex/extraction-quality-loop main
git worktree add worktrees/_active/rag-quality-loop -b codex/rag-quality-loop main
```

窗口分工固定为：

- 窗口 A：主仓库 `main`，协调窗口
- 窗口 B：`worktrees/_active/release-foundation`
- 窗口 C：`worktrees/_active/extraction-quality-loop`
- 窗口 D：`worktrees/_active/rag-quality-loop`

只有当至少一条活跃支线进入 `DONE` 或 `BLOCKED_BY_DECISION` 后，才允许开启 `product-kb` 或 `angular-feasibility`。

## 5. Repo-Managed Skills

技能源码固定放在仓库内：

- `/.codex/skills-src/reqpilot-control-tower`
- `/.codex/skills-src/reqpilot-track-release-foundation`
- `/.codex/skills-src/reqpilot-track-extraction-quality`
- `/.codex/skills-src/reqpilot-track-rag-quality`
- `/.codex/skills-src/reqpilot-track-product-kb`
- `/.codex/skills-src/reqpilot-track-angular-feasibility`

同步到本机 `$CODEX_HOME/skills`：

```bash
cd "/Users/liuchang/Documents/ReqPilotX"
python3 scripts/sync_codex_skills.py --validate
python3 scripts/sync_codex_skills.py --overwrite
```

同步完成后重启 Codex，确保新技能生效。

## 6. trackctl 使用规则

每条支线窗口都必须用 `trackctl` 回写控制塔：

```bash
cd "/Users/liuchang/Documents/ReqPilotX/<worktree>"
PYTHONPATH=prototype python prototype/scripts/trackctl.py init
python prototype/scripts/trackctl.py start --track <track-id>
python prototype/scripts/trackctl.py checkpoint --track <track-id> --progress 25 --status INFO --summary "..."
python prototype/scripts/trackctl.py gate --track <track-id> --gate <gate-name> --result PASS --detail "..."
python prototype/scripts/trackctl.py handoff --track <track-id> --summary "..." --next "..."
python prototype/scripts/trackctl.py status
```

固定节奏：

- 开工时 `start`
- 完成一个子目标时 `checkpoint`
- 遇到 blocker 时 `checkpoint --status RISK` 或 `BLOCKED`
- 完成门禁时 `gate`
- 交接前 `handoff`

## 7. 漂移规则

每条 track 都有 `allowed_paths` 和 `blocked_paths`。控制塔按以下规则标记：

- `ON_PLAN`
  - 修改范围留在本 track 的 `allowed_paths` 内
  - 最近 gate 结果健康
- `RISK`
  - 2 天没有 checkpoint
  - gate 失败或 blocker 未收口
- `OFF_PLAN`
  - 改动落入其它支线的 owner 范围
  - 分支没有对应注册 track
  - 长期直接在 `main` 上做实现开发

## 8. 上下文同步门禁

当 staged 改动涉及以下范围之一时：

- `prototype/app/`
- `prototype/tests/`
- `prototype/scripts/`
- `docs/项目.md`
- `docs/codex-git-workflow.md`
- `docs/next-agent-handoff-prompt.md`
- `AGENTS.md`

必须同时更新：

1. `docs/active-context.md`
2. 当前 track 对应的 `prototype/docs/*active-context.md`

检查脚本：

- `/Users/liuchang/Documents/ReqPilotX/scripts/check_context_sync.py`

安装本地 hook：

```bash
cd "/Users/liuchang/Documents/ReqPilotX"
./scripts/install-context-hook.sh
```

## 9. 推荐提交流程

在各自 worktree 中完成改动后：

```bash
git status -sb
git add .
git commit -m "implement <track-scope>"
git push -u origin <branch-name>
```

PR 仍然使用：

- `codex/* -> main`

合并后清理 worktree：

```bash
cd "/Users/liuchang/Documents/ReqPilotX"
git checkout main
git fetch origin
git pull --ff-only origin main
git worktree remove worktrees/_active/<track-name>
```

如需删除分支：

```bash
git branch -d codex/<track-name>
git push origin --delete codex/<track-name>
```

## 10. 最小实践模板

一轮标准并行开发顺序：

```bash
cd "/Users/liuchang/Documents/ReqPilotX"
git checkout main
git fetch origin
git pull --ff-only origin main
python3 scripts/sync_codex_skills.py --overwrite
mkdir -p worktrees/_active
git worktree add worktrees/_active/release-foundation -b codex/release-foundation main
git worktree add worktrees/_active/extraction-quality-loop -b codex/extraction-quality-loop main
git worktree add worktrees/_active/rag-quality-loop -b codex/rag-quality-loop main
```

然后：

1. 在主仓库窗口打开协调窗口
2. 在 3 个 worktree 中分别打开对应 worker 窗口
3. 每个窗口使用对应 track skill
4. 用 `trackctl` 回写 checkpoint 和 gate
5. 用控制塔页面检查 drift 和 next action
