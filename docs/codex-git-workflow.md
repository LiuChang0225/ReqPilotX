# Codex Git / GitHub Workflow

适用范围：`/Users/liuchang/Documents/New project`

这份文档定义当前项目的分支优先级、标准 worktree 结构，以及在 Codex 中并行处理不同任务的统一方式。

## 1. 当前主线策略

当前策略已经收敛为：**表格优先，活跃开发线控制在 3 条以内。**

正式优先级固定如下：

1. `codex/table-assets-mainline`
   - 唯一承载表格资产化主线
   - 负责 `table_asset / table_ref`、截图优先、Web/Excel 同源展示
2. `codex/workspace-stabilization`
   - 负责统一工作台稳定性、性能、虚拟滚动、锁定/导出状态一致性
3. `codex/ai-slice-integration`
   - 负责让 AI 改为消费 `slice_ai_projection + requirement 正文 + section context`

已淘汰方案：

- `codex/table-snapshot-assets`
  - 不再继续
  - 不再作为任何后续任务的候选基线

## 2. 基本原则

- `main` 只保留可运行、可演示、相对稳定的代码。
- 所有长期开发任务都放到 `codex/*` 分支，不直接在 `main` 上长期开发。
- 一个 Codex 窗口只负责一个正式 worktree。
- 不再允许两条“表格主线”并存。

## 3. 标准 worktree 结构

标准目录固定为：

```text
/Users/liuchang/Documents/New project/
├── prototype/                  # 主仓库，默认 main
└── worktrees/
    ├── _active/
    │   ├── table-assets-mainline/
    │   ├── workspace-stabilization/
    │   └── hotfix-<topic>/
    └── _archive/
        ├── legacy-table-assets-dual-track/
        └── retired-table-snapshot-assets/
```

规则固定：

- `_active/` 只放正式注册的 git worktree。
- `_archive/` 只放退役目录或历史快照。
- 活跃 worktree 不超过 2 条正式开发线，外加按需短命 `hotfix`。

## 4. 当前目录与分支处置约定

- 主仓库 `main`
  - 保持稳定基线
  - 不承担高风险实验开发
- `worktrees/_active/table-assets-mainline`
  - 表格资产化唯一主线
- `worktrees/_active/workspace-stabilization`
  - 工作台稳定性与性能主线
- `worktrees/_archive/legacy-table-assets-dual-track`
  - 历史实现来源，只归档，不继续开发
- `worktrees/_archive/retired-table-snapshot-assets`
  - 淘汰方案占位，不再使用

## 5. 日常开发流程

### 5.1 开工前同步主线

```bash
git checkout main
git fetch origin
git pull --ff-only origin main
```

### 5.2 新任务创建功能分支

```bash
git checkout -b codex/feature-name
```

如果远端已有同名分支，改为：

```bash
git fetch origin
git checkout -b codex/feature-name origin/codex/feature-name
```

### 5.3 开发中提交与推送

```bash
git add .
git commit -m "implement feature"
git push -u origin codex/feature-name
```

### 5.4 合并前同步主线

```bash
git fetch origin
git rebase origin/main
```

如不使用 `rebase`，可改为：

```bash
git fetch origin
git merge origin/main
```

### 5.5 合并回主线

- 在 GitHub 上提 PR：`codex/* -> main`
- 经过验证和 review 后再合并
- 合并完成后，本地更新：

```bash
git checkout main
git fetch origin
git pull --ff-only origin main
```

## 6. Codex 多窗口并行开发

可以，但不要多个窗口共用同一个工作目录。

正确方式是：**一个窗口对应一个 worktree，一个 worktree 对应一个分支。**

### 6.1 为什么不能多个窗口共用一个目录

如果两个 Codex 窗口同时改同一个工作目录，会出现这些问题：

- 文件改动互相覆盖
- `git status` 难以判断改动归属
- 一个窗口切分支会直接影响另一个窗口
- 运行中的服务、缓存、临时文件会互相污染

### 6.2 正确做法：使用标准 worktree

先保证主仓库是最新的：

```bash
git checkout main
git fetch origin
git pull --ff-only origin main
```

然后为新任务创建独立 worktree：

```bash
mkdir -p worktrees/_active worktrees/_archive
git worktree add worktrees/_active/<task-name> -b codex/<task-name> main
```

这样你就可以：

- 窗口 A 打开：`/Users/liuchang/Documents/New project/prototype`
- 窗口 B 打开：`/Users/liuchang/Documents/New project/worktrees/_active/table-assets-mainline`
- 窗口 C 打开：`/Users/liuchang/Documents/New project/worktrees/_active/workspace-stabilization`

### 6.3 查看当前所有 worktree

```bash
git worktree list
```

### 6.4 任务结束后移除 worktree

先确认改动已经提交并推送，然后执行：

```bash
git worktree remove worktrees/_active/<task-name>
```

如果对应分支也不再需要：

```bash
git branch -d codex/<task-name>
```

如果远端分支也要删除：

```bash
git push origin --delete codex/<task-name>
```

## 7. Codex 中与 GitHub 保持同步

Codex 本身不自动替你做双向同步。Codex 操作的是本地仓库，和 GitHub 的同步仍然靠 Git。

### 拉取远端最新代码

```bash
git fetch origin
git pull --ff-only origin main
```

### 推送当前分支

```bash
git push
```

首次推送新分支：

```bash
git push -u origin codex/feature-name
```

### 检查当前分支和工作区状态

```bash
git branch --show-current
git status -sb
```

## 8. 项目内的推荐约束

- `main` 不做长期开发
- 大改动先开分支，再开独立 worktree
- 一个功能一条主分支，避免“超大杂糅分支”
- 表格相关长期工作只进 `codex/table-assets-mainline`
- 工作台稳定性和性能只进 `codex/workspace-stabilization`
- 提交前至少做对应范围的最小验证

## 8.1 Context Sync 门禁

仓库内现在要求：

- 当 staged 里有代码或核心项目文档变更时
- 必须同时更新至少一个滚动上下文文件

检查脚本：

`/Users/liuchang/Documents/New project/scripts/check_context_sync.py`

安装本地 pre-commit hook：

```bash
cd "/Users/liuchang/Documents/New project"
./scripts/install-context-hook.sh
```

默认触发范围：

- `prototype/app/`
- `prototype/tests/`
- `prototype/scripts/`
- `prototype/README.md`
- `docs/项目.md`
- `docs/codex-git-workflow.md`
- `AGENTS.md`

满足以上任一改动时，必须同时更新至少一个：

- `docs/active-context.md`
- `prototype/docs/*active-context.md`

目的：

- 新窗口/新线程不依赖长对话历史
- 重要状态变化总能在仓库内找到最新滚动上下文

## 9. 最小实践模板

一个新任务，从开始到结束可以按这个顺序执行：

```bash
git checkout main
git fetch origin
git pull --ff-only origin main
mkdir -p worktrees/_active worktrees/_archive
git worktree add worktrees/_active/<task-name> -b codex/<task-name> main
cd worktrees/_active/<task-name>
git add .
git commit -m "implement <task-name>"
git push -u origin codex/<task-name>
```

完成并合并后：

```bash
git checkout main
git fetch origin
git pull --ff-only origin main
git worktree remove worktrees/_active/<task-name>
```
