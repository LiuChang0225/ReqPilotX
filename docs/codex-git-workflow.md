# ReqPilotX Git 与 Codex 协作

更新时间：2026-09-20

这是一套轻量工作方式，不是开发门禁。唯一目标是让代码、文档和 Git 历史容易理解。

## 唯一仓库

- 工作目录：当前检出的 ReqPilotX 仓库根目录。
- 远端：`https://github.com/LiuChang0225/ReqPilotX.git`
- ReqPilotNext 与 ReqPilot 使用各自目录和仓库；不要把它们的分支、技能、备份或文档放进 ReqPilotX。

## 开始工作

```bash
git status --short --branch
git fetch origin
```

先确认已有改动属于谁。工作区干净且本地没有独有提交时，可以使用 `git pull --ff-only`。本地已经有提交时，先看 `git log --oneline --left-right main...origin/main`，再根据实际情况选择 merge 或 rebase；不要用强制覆盖隐藏未检查的工作。

分支和 worktree 都是可选工具。小而明确的修改可以直接在当前分支完成；需要隔离长任务时再创建临时分支或外部 worktree。

## 修改与验证

1. 修改实现和直接相关的测试。
2. 同一轮更新受影响的 README、产品、架构、接口、环境或验证文档。
3. 先跑聚焦测试，再按影响范围扩大验证。
4. 检查 `git diff --check`、`git diff` 和 `git status`。
5. 用一个能说明结果的本地提交收口。

`prototype/eval/progress/program_registry.json` 只是进度看板的兼容输入。它不要求激活工作包，也不影响提交、分支、测试或文档更新。

## 结束工作

```bash
git status --short --branch
```

理想结果是没有未提交文件。若有尚不能提交的用户改动，应明确说明，而不是删除、隐藏或重置。只有用户明确要求时才向 GitHub 推送。

构建产物、运行数据库、客户原始文档、密钥、本地虚拟环境和临时评测输出应由 `.gitignore` 排除；需要保留的历史材料放在仓库外的项目归档目录。
