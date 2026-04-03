# Active Context

更新时间：2026-04-03 14:59 CST  
适用仓库：`/Users/liuchang/Documents/New project`

长期背景：[`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/项目.md)

## 当前目标

- 完成 workspace 前端小迭代：知识库文档支持移除，分析/导出区去掉重复 AI 路径选择
- 保持 `main + 8000` 的本地 Azure 路径可运行
- 给 Windows 本地联调补一个可复用的 Qdrant 联动启动入口

## 活跃分支 / 工作树

- 当前分支：`main`
- 当前状态：存在未提交本地改动，涉及 `knowledge document delete`、workspace UI 精简、`prototype/start.ps1`、README 与上下文文档

## 当前样本

- 当前 live 验证页：`/workspace`
- 当前验证端口：`8000`
- 当前目标链路：项目选择 -> 知识文档上传/移除 -> 锁定 -> AI 分析 -> 导出

## 最近已修 / 已验证

- 后端新增 `DELETE /api/projects/{project_id}/knowledge-documents/{document_id}`
- 删除知识文档时会一并移除同名历史重复记录，级联删除 sync 记录，并 best-effort 清理本地上传文件与 payload JSON
- 前端知识文档清单已新增“移除文档”按钮
- 第 4 步“分析与导出”已移除重复的 AI 路径选择器；AI 运行现在直接沿用第 2 步默认路径
- 新增 `prototype/start.ps1`；可用 `-WithQdrant` 先拉起或复用本地 `reqpilotx-qdrant` 容器，再启动应用
- 定向回归：5 个 `UnifiedWorkspaceTests` 通过
- live 校验：`/api/health` 返回 `ready=true`，`/workspace` 返回新 asset 版本，旧 `ai-profile-select` 标记已不再输出

## 当前 blockers

- 知识文档删除当前不会主动硬删远端 Azure Search / Qdrant 索引，只是通过数据库过滤让已删文档不再参与检索；远端存储仍可能漂移
- `start.ps1 -WithQdrant` 依赖 Docker Desktop，且只负责拉起 Qdrant 服务；若要让 Aliyun 本地 POC 真正 runnable，仍需配置 DashScope 与 `REQPILOT_ALIYUN_RETRIEVAL_BACKEND=qdrant`
- 现有 readiness 语义、SQLite 连接泄漏风险、请求内同步上传/知识同步长任务问题仍未处理

## 下一步

- 如需 UI 实际操作验收，再补一轮浏览器点击测试，验证“移除文档”按钮的真实交互
- 评估是否给知识文档删除补远端索引硬删除
- 若本地 Qdrant 变成常用链路，再把 `start.ps1` 的 Qdrant 联动逻辑同步到其他启动脚本或统一 launcher

## 活跃端口

- `8000`：本地 ReqPilotX live 服务，当前通过 `prototype/start.ps1` 启动，PID `67384`
- `6333`：可选本地 Qdrant HTTP 端口；使用 `prototype/start.ps1 -WithQdrant` 时会检查/拉起
