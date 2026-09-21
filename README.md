# ReqPilotX

ReqPilotX 是面向汽车 SOR 的需求工程工作台，把复杂文档转成可核对的原文条目、需求分类和显式派生要求，连接内部证据、工程师意见与同版本 Compliance Matrix / Excel。

目标承载平台为 Pair：ReqPilotX 提供业务服务、工作台、工具/技能和版本化成果，Pair 提供 Agent 会话与执行能力。原生应用与便携插件共用业务核心。当前已具备阶段成果与程序链路，完整产品尚未全部验收。

## 从这里开始

- [项目状态](docs/项目.md)：已实现能力与未验收边界。
- [产品定义](PRODUCT.md)、[模型 V1](docs/需求提取/文档结构与原文条目模型V1.md)：对象、关系、来源与人工决定。
- [开发计划](docs/需求提取/开发计划.md)：唯一实施顺序；[当前接续](docs/active-context.md)提供当前证据入口。
- [应用运行](prototype/README.md)、[本地部署](docs/deployment/local-native-postgresql.md)、[原生任务能力包](docs/deployment/native-loop-capabilities.md)：依赖、启动、能力登记与恢复。
- [可安装引擎发行](packages/engine/README.md)、[服务包](apps/server/README.md)、[可移植插件](plugins/reqpilot/README.md)、[Pair 适配](integrations/pair/README.md)：共享能力、发行物和宿主边界。
- [Web 应用](apps/web/README.md)、[共享工作台](packages/workbench-ui/README.md)、[桌面应用](apps/desktop/README.md)：同源 UI、Panel 合同与桌面成品入口；[R6 集成约定](docs/architecture/repository-productization-r6.md)说明组合构建和验证边界。
- [Agent 资产](agent-assets/README.md)、[Agent 评测](evals/agent_evals/README.md)、[实验](experiments/README.md)：技能/vendor 权威源、程序与语义评核、非生产实验边界。
- [UI/UX 合同](DESIGN.md)、[架构](docs/architecture/target-architecture.md)、[参考资料](docs/参考资料索引.md)。

## 本地运行

应用使用 React/Vite/TypeScript、FastAPI、worker、PostgreSQL 与本地对象存储。按照[运行说明](prototype/README.md)准备依赖和数据库后：

```bash
cd prototype
./start.sh
./status.sh
```

本地核心不依赖 Docker，也不默认连接云端。Agent 模型运行需要单独配置相应宿主、身份与能力；基础程序检查通过不代表模型语义或所有文档类型通过。

## 源码与资料

源码基线包含实现、测试、配置模板和明确合成的示例。客户原件、真实案例答案、知识库、运行数据、模型轨迹、凭据与构建产物保留在私有环境；详见[资料边界](docs/源码与私有资料边界.md)。历史文档中的私有入口不代表源码包含对应原件。

当前业务技能从 `agent-assets/` 维护；`.agents/skills` 与 `plugins/reqpilot/skills` 是宿主发现投影，发行构建会核对后直接复制权威源。兼容 `agent-kit/` 保留自身版本锁与旧任务身份，不自动升级成当前工作流。详见 [R5 资产生命周期](docs/architecture/repository-productization-r5.md)。

原文、稳定身份、既有版本和有效人工决定不得被整理工作覆盖。无内部依据时跳过自动符合性分析，继续来源整理、自查和人工响应。默认自主形成草稿，正式工程师接受独立记录。

本仓库只属于 ReqPilotX。ReqPilot 和 ReqPilotNext 是独立项目，不是安装或验证依赖。
