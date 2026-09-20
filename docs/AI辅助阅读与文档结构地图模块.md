# ReqPilotX 独立 AI 阅读器

更新：2026-09-03。本文描述保留的阅读能力；当前开发主线仍为[目标态验证](需求提取/开发计划.md)。

## 产品边界

AI 阅读与正式需求提取平级。用户面对当前一份文档自由提问、连续追问、检查引用和恢复会话历史，
不必先选区或启动提取。选区和大纲是可选上下文与导航，不是正式结构或需求数据。

Reading 的对话、回答、翻译、笔记、引用和大纲不隐式进入需求提取、分类、需求分析或版本发布。
不跨文档检索，不读取项目知识库或 Requirement Set 作为阅读上下文。

## 现有实现位置

- `prototype/app/application/reading.py`、`reading_retrieval.py`：当前文档内容与会话用例。
- `prototype/app/adapters/models/reading_provider.py`、`reading_codex.py`：现有 Codex 运行路径。
- `prototype/app/adapters/agents/codex_app_server/reading_mcp.py`：来源绑定的读取与答案校验。
- `prototype/app/ports/reading_provider.py`、`reading_repository.py`：提供者和持久化合同。
- `prototype/frontend/src/features/reading/` 与 `features/pdf/`：对话、引用、选区和原文工作面。

普通路径通过本机 Codex App Server 和明确的 reading Skill，不因 Azure/百炼配置存在而自动转用旧服务。
兼容 JSON-chat/视觉 adapter 保留为代码资产，不是用户主路的承诺。

## 完整性与来源

当前回答只能引用本轮授权文档的有效内容及可验证 locator。
必需视觉内容缺失或全文超过实际能力时明确给出范围/限制，不用抽样答案冒充完整阅读。
原生文字与视觉 sidecar 分开保存；读取成功、页图存在和模型实际看过不是同一件事。

UI 显示人类可读的证据、页码与原文片段，支持回跳与对话历史。
这些来源事实可复用中立捕获能力，但阅读任务与正式提取的会话、状态和写入边界隔离。

## 配置与后续

配置与启动见[应用运行说明](../prototype/README.md)。
真实登录、模型、工具和来源资格必须核验；不复制认证文件或静默放开权限。
本轮不重写阅读器代码，不把旧阅读实施计划当作当前提取路线。
如后续明确要求扩展阅读能力，再按独立目标与风险选择测试和实现。
