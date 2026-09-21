# ReqPilotX 应用架构与能力边界

更新：2026-09-20。本文描述保留的应用实现和目标能力边界，不规定第一阶段必须采用的框架。
开发顺序以[目标态优先计划](../需求提取/开发计划.md)为准，旧“唯一允许架构”措辞已退出探索阶段。

## 产品与能力核心

产品目标：真实客户文档 → 来源忠实的结构/条目 → 分类 → 显式派生拆分 → 人工核对 →
后续证据分析与矩阵交付。

公共业务能力负责语义方法、来源工件与验收标准；宿主适配负责模型/工具/权限/会话/事件。
Codex 原生环境用于目标态探索，能力包用于复现，ReqPilotX App 最终直接加载同一能力核心。
业务规则应保持同源，实际宿主差异须按接口和测试核对；共享核心不代表完整宿主流程已经验收。

## 当前应用实现

```text
React / Vite / TypeScript
          |
FastAPI HTTP adapters
          |
application -> domain / ports
          |
PostgreSQL + LocalObjectStore

independent worker -> application use cases
```

- `apps/server/src/reqpilotx_server/main.py` 是安装版薄入口，HTTP 装配在
  `reqpilotx_server/api/`；`prototype/app/main.py` 仅为旧 import 兼容别名。
- 业务用例在 `app/application/`，稳定对象/不变量在 `app/domain/`，外部能力通过 `app/ports/`。
- `app/adapters/` 提供文档、Agent、模型、存储与持久化实现；`app/worker/` 处理持久任务。
- `prototype/frontend/` 是现有应用唯一前端工程；生成的 dist 不提交。
- 正式数据使用 PostgreSQL 和对象存储。SQLite 仅供隔离测试或已完成迁移的历史来源，不做生产回退。
- SQLite 测试所需的建表逻辑放在 persistence adapter，application 不直接依赖 SQLite；正式数据库继续通过版本化 migration 演进。
- 本地不默认使用云端；阿里云 SAE/RDS/OSS 的脚本保留为后置部署资产，未据此声明已部署。

这些实现均可复用。阶段一可以隔离替换不合适的框架或工作面，不能因此损坏现有数据和已接受结果。

## 原文、语义和呈现

原始字节与 SourceIR/TableIR 是来源事实；结构、条目、分类和派生绑定来源；
前端、检索索引和导出是投影，不创造来源或业务身份。

同一来源层级渐进丰富为原文条目，再按明确请求生成单一需求。
原文父项、共享条件、表格、图片及非需求上下文始终可核对。
章节父子不等于需求派生/分配关系。

Agent 决定阅读策略、业务边界、语义归属与修订；Host 校验授权、来源、范围、稳定身份和数据完整性。
工具候选、parser 顺序、页数和 token 窗口不拥有语义权威。
详细合同见[需求工程能力设计](../需求提取/架构与技术设计.md)。

## 会话、工作状态与反馈

- 保持同一文档任务的全局发现、已接受范围、问题与当前版本；可持续会话或显式工件恢复。
- 用户反馈带来源、对象和版本，通过工具继续任务，不靠复制一段聊天替代状态。
- 已接受 scope 可独立保存；局部失败不丢失已完成结果。
- Activity 来自真实工具/工件回执，公开目标、当前行动、发现和安全摘要，不展示隐藏思维链。
- 当前 App Server 已有 Skill 注入、动态工具、局部 checkpoint/working revision 等组件，
  但自动全文 producer、全部反馈和正式入口仍不能宣称完成。
- 原生环境的能力不能仅凭同名模型推定内嵌环境等价；工具、视觉、上下文和权限需明确配置与验证。

## 存储与任务安全

- 数据库保存来源身份、对象 key/hash、大小及版本，不把临时机器路径作为来源身份。
- LocalObjectStore 保存私有来源与派生产物；历史版本仍引用的对象不能按“旧文件”垃圾回收。
- 现有 durable jobs 使用 PostgreSQL claim/lease/checkpoint 和受控发布；不存在为方便而重置当前数据库的许可。
- 新 revision 保留前驱和 lineage，锁定版本不原地改写，用户修订只影响关联结果。
- 业务提交必须重新校验当前身份和版本；模型不得直接写库、篡改校验器或覆盖客户文件。
- 客户原文、页图、完整候选、真实配置和密钥不进入 Git 或可分发插件。
- 内置知识图谱只提供独立创作的合成导航/检索演示，不能作为符合性证据；实际项目依据必须由授权文件及版本绑定。客户衍生 Wiki 与专用答案脚本不分发。
- 备份和历史迁移工具保留；已完成的 SQLite 迁移不得再次覆盖当前 PostgreSQL。

## AI 阅读与正式需求链

AI 阅读是独立单文档会话，保留原文、自由问答、追问与引用。
当前模型路径通过 Codex App Server；旧 Azure/百炼兼容代码不是普通用户的自动 fallback。
Reading 不读取正式 Requirement Set 或跨文档知识库，也不向提取隐式写入回答/笔记/大纲。
原文捕获能力可以复用，但阅读与正式需求状态隔离。

## Matrix、决定与交付

现有 Requirement Set、树/Matrix snapshot、人工结论和 Excel 代码保留：
在线与导出读取同一冻结版本与来源顺序，区分可见性、用户处置及分析资格。
人工决定优先；没有人工决定时，符合现有终态/时效规则的 AI 结论可作为带 provenance 的默认结果，
绝不冒充人工确认。旧结论或证据失效时明确 stale。

新 Agent 原文基线贯通分析/FORMAL 仍需端到端验收。
ReqIF 仅有映射方向，不宣称 ReqPilotX writer 已实现。

## 宿主和部署

能力包公共部分优先使用开放 Skills/MCP/工件接口。
Agent 定义、Hooks、权限、工具/视觉输入、会话恢复与 UI 放在宿主适配层并逐宿主验证。
具体模型、依赖和协议版本以代码/Kit 锁及运行证据为准，不在多个文档中复制易过期数值。

现有启动、排空、恢复和配置见[本地部署](../deployment/local-native-postgresql.md)。
云端说明是[后置部署参考](../deployment/aliyun-sae.md)，不是当前开发待办。
