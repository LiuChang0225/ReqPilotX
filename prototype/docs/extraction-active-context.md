# Extraction Active Context

更新时间：2026-04-03 14:59 CST  
分支：`main`

## 分支目的

- 完成 workspace 小范围 UX/ops 收口，不改动主分析流程语义
- 把知识库文档生命周期补齐到“可上传、可重试同步、可移除”
- 给 Windows 本地联调补一个可复用的 Qdrant 启动入口

## 当前样本

- live 验证页：`/workspace`
- 当前主验证端口：`8000`
- 当前关注链路：知识文档表、AI 分析启动区、Windows 本地启动脚本

## 最近已修

- `app/main.py` 新增知识文档删除接口，删除同名 legacy duplicate 时按整组处理
- `app/static/app.js` 为知识文档表新增“移除文档”操作，并在删除后刷新 workspace / knowledge docs 状态
- `app/static/index.html` 已移除第 4 步重复的 `AI 路径` 选择器；AI 分析直接使用第 2 步默认路径
- `start.ps1` 已加入仓库，支持 `-WithQdrant` 拉起本地 `reqpilotx-qdrant` 容器
- 定向 `unittest` 通过 5 个相关用例；live `/workspace` 已返回新 JS asset 版本

## 当前已知问题

- 知识文档删除当前只清理本地文件与 payload，不硬删远端 Azure Search / Qdrant 索引
- `start.ps1 -WithQdrant` 只负责本地服务启动，不会自动补齐 Aliyun/DashScope 配置
- readiness / SQLite 连接 / 请求内长任务问题仍是后续主线技术债

## 当前验证结论

- `/api/health` 在 `8000` 返回 `ready=true`
- `/workspace` 已不再输出旧的 `ai-profile-select` 标记，新的前端 asset 已生效
- 知识文档删除后会递增 `knowledge_base_version`，并让相关 AI 状态进入 stale 流程

## 下一步

- 如需产品级验收，再做一次浏览器点击验证，确认“移除文档”按钮体验和提示文案
- 若知识文档删除要做彻底回收，下一步补远端索引硬删除
- 如果 `-WithQdrant` 成为常用入口，再考虑把同样逻辑并入其他平台启动脚本
