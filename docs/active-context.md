# ReqPilotX 当前接续

更新：2026-10-16。当前授权：以 PR #10 最新 `fe97563eb6eec6537f821fd840cbf5a367acee8b` 为基线持续实施；首批员工入口为现有 ECS 方案的**私有 Web**。唯一实施顺序仍见[开发计划](需求提取/开发计划.md)。

## 当前源码与已实现范围

本轮在独立候选中保留 PR #10 的共享引擎、provider durable worker、SaaS 账户/项目作用域、人工响应和冻结交付。Pair 的发起、反馈、人工响应、版本、取消/继续/重连已存在，不能再按旧只读面板状态描述。

已核对并接入仓库外保存的 `99ad73d6` 增量（基于同一 fe97563e）：Web 账户至交付回归、注册能力包与备份根/systemd 预检、受限只读状态脚本。原始四提交与私有交付包保持不变；既有未提交历史对象退役代码没有混入本候选，也没有重新执行其数据处置。

新方法资产修正 T05，明确独立里程碑、同一交付事件与产品组成的区别。`worked-example --example source-units` 通过公开 TaskService 演示六类来源判断、混合 BOM、跨页试验、明确上下文、辅助图、局部分类纠错、无变更保留及同版 Excel。它是零模型的可执行教学答案，不是客户提取成绩。资产从 agent-assets 维护并同步投影。

当前实施和实际失败见[本轮验证记录](validation/employee-trial-20261016.md)。实例在线版本、真实模型成绩、浏览器旅程和员工接受分别核验，不从源码 HEAD 推断部署。

## 现有证据与不可跨用的边界

- PR #10 原提交 CI：后端 4,953 项（185 跳过）、插件/MCP 301、发行 6、前端 1,109；Pair 47 通过/31 跳过。它们属于原提交，不自动覆盖本候选。
- 9月22日阶段模型及 Pair 同案的语义 FAIL 保持；混合归属、策略及未决处置后来已实现，不能因旧失败重写历史或说新代码已经语义通过。
- 用户提供的真实 provider 一页合成任务与分类反馈，累计 usage 3,327,420，保留为运行观察。fe97563e 的 terminal handoff 当时只有离线/临时 PG 证据；真实耗时、usage 和语义收益需本次复验。
- 10月15日私有存档已有配套 Linux 发行物、Mac 隔离安装及实际备份/恢复结果；不是 Linux/ECS 当前运行或员工验收。
- 既有部署、真实百炼小样成功的历史不能写成从未执行；当前 active release 仍要现场读回，不能将新构建直接当作运行版本。

## 直接继续

固定本候选的 engine/server、能力包和 Web，先用全新隔离数据根验证已安装私有 Web 与批准模型；从真实浏览器完成账户登录、建项目、上传、委托、反馈、人工响应及冻结 Excel，再检查取消/恢复与版本。来源语义复核独立于工具成功和 UI 可见。真实原件与轨迹只保存在私有目录。

ECS 使用既有实例与批准路径。先核实际发行/服务及安全备份；不以 DNS、实例健康或本机 loopback 代替应用验收。只读脚本见 deploy/saas/status-readonly.py；访问未建立时报告具体缺项，继续可独立完成的本地工作。

## 历史与合同入口

- [产品定义](../PRODUCT.md)、[模型V1](需求提取/文档结构与原文条目模型V1.md)、[分类V1](需求提取/分类与验证关联规则V1.md)。
- [R6 集成](architecture/repository-productization-r6.md)、[SaaS 账户](architecture/saas-account-boundary.md)、[终态接续](architecture/provider-terminal-continuation.md)。
- [私有 Web 部署](deployment/saas-pilot.md)、[服务合成验证](development/cloud-service-acceptance-20261001.md)、[目标环境缺口](issues/release-clean-machine-evidence.md)。
- [9月22日方法复测](需求提取/读取恢复与标题BOM方法收敛20260922.md)、[Pair 同案复测](需求提取/PairMCP同案复测20260922.md)。

旧工作目录、canonical repo 已有改动、原件、人工意见、数据库及历史任务包保留；未经本轮明确授权不推送、不清理或迁移其数据。
