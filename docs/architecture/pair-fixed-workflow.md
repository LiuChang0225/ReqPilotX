# PAIR 固定业务流程与任务模型选择（RX02）

更新：2026-10-31。本文记录 RX02 的当前实现，不声明真实模型、原生浏览器或业务语义验收已完成。验收范围仍由[规格](../../reports/pair-lightweight/work/inputs/spec.zh.md)拥有。

## 入口与资料引导

共享 `HomePage`、`WorkspacePage`、`HostedAgentPanel` 与 `ApplicationHost` 继续使用原工作台。PAIR Client 仍注册主面板和侧边栏；发现登记由 **Host**
适配器通过可选 `ctx.pairSkillCenter.registerApplication` 完成，使用
`id=reqpilotx`、`stage=requirement-analysis`、`panelId=reqpilotx` 和实际
应用包版本。`readStatus` 只反映 Host 管理的实际 workbench 状态，不表示模型或业务
结果已接受。缺少 skill-center 的 raw DSH 仍保留侧边栏；Client 不再轮询
`/config` 仅为完成登记。

文档起始页和任务面板显示：

- 必需的客户需求文档；
- 当前版本未接入的项目参考资料／团队知识库（如实不可用，RX04/05）；
- 固定流程预期成果和阶段顺序；
- 可选的补充说明和“先核对结构”选项。

用户只需开始一次。补充说明保存在任务意图中，不能取消阶段或成果检查。

本地 SQLite 版本不运行旧的原生 durable loop queue。`extraction-loop/latest` 和
`loop/latest` 在队列未装配时返回明确的 `NOT_STARTED` 与
`UNAVAILABLE_LOCAL_RUNTIME`，因此空文档显示“尚未发起任务/尚无原文工作版”，
而不是持续“任务状态暂未同步”。其它真实读取失败仍保留错误状态。

## 服务端固定流程

`reqpilot-fixed-workflow v1.0.0` 的版本、定义摘要、阶段顺序和启动 brief
由冻结能力包提供；Host 不编写业务目标。`host_launch.py` 组合固定目标和
可选补充说明，把工作流规格写入任务上下文。阶段顺序：

1. `PREPARATION`：任务上下文、原件字节与能力包；
2. `SOURCE_MAP`：已保存 Document Map；
3. `ITEMS_CLASSIFICATION`：已保存条目、分类及明确范围的派生；
4. `EVIDENCE_ANALYSIS`：有内部依据时的保存分析；声明无依据时 `SKIPPED`；
5. `SELF_CHECK`：与当前 Map/条目不冲突的 review；
6. `DELIVERABLE`：与当前条目同版的已保存交付。

`fixed_workflow.py` 只从这些已保存工件和分支声明投影状态。模型文字、会话
空闲、导出动作或补充说明都不能推进阶段。Task inspect 通过固定 TaskService
重新投影；Host 不把任务目录中的 `workflow-status.json` 当成权威证明，而是
通过固定包的 task inspect helper 重新读取，并校验 task/source/workflow/
brief、阶段顺序、SAVED 证据和 readiness。缓存损坏、外来任务或校验失败时
显示“状态未确认”并保留已保存工件，不回退到伪造 ready 或静默重置。

插件工作台的 `fixed_workflow` 入参同样只作为候选声明：服务端忽略发送方的
阶段状态和 `ready_to_review`，只根据已验证的 Map、条目、派生、分析策略及
当前同版交付版本回执重新推导。六个 SAVED 但没有工件、空证据、矛盾
readiness、旧 basis 或损坏交付回执都不能得到 ready；无内部依据保持
`SKIPPED`。经实际编译、复核并保存交付版本的完整流程仍可得到 ready。
历史任务没有工作流上下文时不生成该状态。`ready_to_review` 与 Pair 执行状态
分开显示，仍不等于人工接受。

Map 确认、无内部依据和显式派生是声明的分支；Map/items/delivery 原有校验
继续拥有前置条件。没有 Map 保存条目、错误来源或错误工作流版本都会被拒绝。

## 模型、请求与恢复

新任务使用 PAIR 当前选定的默认模型和推理档位。启动记录
`reqpilot-pair-launch-record/v3` 在派发前保存 `modelSelection`，在会话创建后
通过 `sessionController.selectSessionModel` 确认并保存
`selectedAgentModel`，然后才提交 prompt。显式部署 `agentModel` 仍作为覆盖。
缺少默认模型或会话模型选择能力时显示可操作的设置状态，不启动独立运行时。
bundle 不再固定 Codex 模型。

v3 任务返回 `reqpilot-pair-launch/v2` 公共 receipt，包含绑定来源的固定流程
和 `workflowState`。旧 v1/v2 启动记录与旧 receipt 保持原解释；新 Client 拒绝
缺失工作流的 v2 receipt，新 Host 拒绝未知 receipt/工作流版本。同一请求键、
同一 Session、同一 Work/Source 的重复提交、停止、继续和显式重连沿用原
任务，不另建业务台账。

## 本地验证边界

Host 登记使用 PAIR 源码检出中的真实 Cordis service/fiber 验证注册、运行状态变化
和随 fiber 释放。原生 Session 使用源码支持的 `pair` profile、隔离 `DSH_HOME`、
仅回环地址的 OpenAI-compatible 脚本端点和该路由的显式批准；真实
SessionController/Workspace/MCP 完成一次 `task_open` 后结束。脚本文字没有推进
未保存的 Map/条目阶段。该证明是 keyless/scripted，不是真实 Provider 或业务验收。
源码检出缺少预构建原生 system addon 时，runner 只把安装包中匹配的预构建复制到
私有输出并记录该精确依赖，不修改生产策略或用户 profile。

## 未覆盖范围

- 真实 Go Max 任务、原生 PAIR 浏览器和真实客户文档由父任务验收；
- RX03 的成果后原生会话解释／派生／修订本轮只保留既有 hooks；
- RX04/05 的项目参考和团队知识仍不可用，工作台如实显示不可用；
- 冻结能力包准备另使用合成 HTTP 与脚本化 Session 控制器；真实 DSH 原生
  Session 证明只覆盖 keyless 脚本路由。
