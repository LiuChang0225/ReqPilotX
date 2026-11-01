# Provider 成功终态反馈接续（本地候选，2026-10-08）

基线 `812a4f18fb5e057bf8727301893fd74665d4a046`，tree
`0206e84c1ea8b8f96da9a5eb081b5aa011f72e3a`。正常 fetch / ls-remote 确认远端发布分支身份；
部署事实来自用户提供的上下文，本轮没有连接部署环境。候选未提交、推送、同步或部署。

## 已知问题与最小可靠边界

用户提供的真实运行记录：一页两条虚构要求，首次 36 次模型响应、43 次工具调用，usage 为
prompt **1,132,137** + completion **10,827** = **1,142,964**。分类反馈新增 23 次响应/工具调用，
单独 usage 为 **2,164,865** + **19,591** = **2,184,456**；两轮合计 **3,327,420**。
此前首次 `142,964` 是转录错误。这是运行记录，不是账单。

反馈首个请求含 84 条消息、242,427 字节及已完成的 43 次工具调用；最后请求为 130 条消息、
479,106 字节。最终工具文本 233,356 字节，占普通文本 90.5%；思考历史 90,240 字节，
两条图像 URL 共 67,388 字节。完整历史不断重送已证实，字节不能直接等同模型 token。
本轮没有重新读取私有运行正文或调用真实模型。

模型 `stop` / runtime `COMPLETED` 和应用保存成功是两个事实。终态新反馈只有同时满足以下条件，
才启用确定性 handoff：

- 前一 durable job 为 `SUCCEEDED`，`application_saved=true`，来自 provider tool loop。
- 完成 snapshot、receipt、终态消息、当前完整对话及 SHA 一致；所有 tool-call ID 与结果配对，没有 pending images。
- task/project/document/source/runtime identity 一致；新 job 固定前一保存 job ID 和基线。
- 已保存版本仍是当前基线，`review-verify` 通过，新增反馈的 review 是该保存版本的合法后继。
- 原件、review 原件副本、SourceIR、版本文件和同版交付存在且与固定证据匹配。
- 原始调用回执全部 `RETURNED`，身份、参数、结果和哈希匹配。

缺失或不匹配证明时，在 provider 请求前拒绝。未完成循环、取消、UNKNOWN、未配对结果、
未保存完成轮次不新建终态 handoff。已经开始的接续中断后，恢复固定 handoff 和完整活动链，
重新核验固定引用的原件、SourceIR、版本、review、交付、审计字节，以及已保存 predecessor 和
原始完成 snapshot/receipt/calls；没有新 pin 的恢复也执行同一核验。任何缺失/漂移在工具回放前
拒绝。不按最新业务状态重新归纳，不要求 mutable review 保持起始状态。新 steering 与发布前
重新验证/CAS 继续沿原路径；完成但未保存的后缀全部保留。

session schema 保持 `reqpilotx-provider-tool-loop-session/v1`。没有新 pin 的旧 job 继续原路径；
新恢复命令可从可核验的旧保存结果建立 handoff。必要工件缺失时不猜测或补造证明。
review-only 工作区没有 SourceIR producer ledger，不按文件名认领来源证据；遇到
`source_ir.json`、`source-ir.json` 或 `map-source-ir.json` 时明确拒绝本优化。没有这些文件的
review-only 接续仍保留固定原件和完整版本。raw-document 路径沿用已有 producer 与固定哈希。
没有新增调用次数、token、输出或金额限制，也未修改既有配额/SDK 策略。

## 保存与请求合同

`provider_continuation.py` 入队时记录内部 `continuation_predecessor_job_id`，执行时生成
`reqpilotx-continuation-handoff/v1`。handoff 是确定性导航/谱系索引：原件和 SourceIR 引用/哈希、
完整基线文件引用、基线 SHA/版本、目标稳定 record IDs、未解决反馈 IDs、当前/保存 review、
同版交付及审计引用。反馈原文保留在哈希绑定的 review，本轮目标保留在用户消息。
不调用 LLM 摘要，不改变原文条目、分类或导出算法。

`run-state.json.messages` 保留完整对话。不可变 `turn-state-*`、`receipt-*`、`calls/*` 和
`events.jsonl` 全部保留。完成 snapshot/receipt 和全部原始调用回执的逐字节副本存入现有工具
workspace 的 `continuation-audit/<attempt>/`，列表在 `calls-index.json`；现有 `reqpilot_read`
可按需读取。无新增工具或公开 API。

可选 `continuation_context` 保存 handoff、其 SHA 和活动消息起点。起点以完成历史的完整消息 SHA
验证，不能移入活动链中间。provider 请求为原系统/skill、handoff 用户消息、起点之后的全部
活动消息。活动 tool-call IDs、工具结果、图像和 Qwen `reasoning_content` 保持原顺序；Azure
继续过滤 reasoning，不接收 Qwen `extra_body`。原始审计不受请求投影影响。

每请求写私有 `request-<attempt>-<ordinal>.json`，receipt 增加 `provider_requests`。仅含
provider/model、attempt/ordinal、history mode/handoff SHA、消息数、消息/工具 JSON UTF-8 字节、STARTED/RECEIVED、usage、
cached tokens、响应 ID、请求 ID 和允许的 finish reason；不保存认证头、密钥、正文或图像。
缓存或请求 ID 未返回时为 `null`。消息字节口径为
`json.dumps(messages, ensure_ascii=False).encode('utf-8')`，不含工具 schema、HTTP envelope、
headers 或 SDK 实际编码；工具字节单列。

usage 在 length/结构解析前持久化，逐响应累计一次。length 仍 FAILED，解析错误仍走 UNKNOWN
边界。无响应时 metadata 保持 STARTED / 未知 usage；没有引入费用计算系统。

## 验证、审查与局限

新增测试使用虚构 PDF、真实 Map/Items 编译、review、分类修订和同版 Excel，以及 FakeCompletions /
OpenAI SDK MockTransport。覆盖终态边界、审计可读、原件/SourceIR/稳定 ID/引用/其他记录保留、
缺失和漂移拒绝、工具配对、活动 Qwen 恢复、旧 job、worker 参数、无新增输出参数，以及错误前 usage。

合成 fixture 为 35 个工具批次、43 次调用、36 次响应（8 个批次有第二次调用；无图片）：

| 反馈请求 | 消息数 | UTF-8 消息 JSON 字节 |
|---|---:|---:|
| 完整已完成历史 + 新反馈 | 82 | 329,210 |
| 系统 + handoff + 新反馈 | 3 | 5,679 |

这只证明合成请求结构变化，不证明模型质量、真实 token、缓存效果或费用下降。真实验收的
84/130 条消息和图像记录没有被冒充为 fixture 输出。

另行静态审查比较基线/候选 AST：provider 配置、Qwen/Azure 参数、reasoning 过滤、pending/tool
恢复函数、既有 `_MAX_*`、SDK 调用次数/参数名、runtime settings 和工具协议保持一致，handoff
无 SDK 调用或网络 import；12 项通过。另由独立 agent 审查并执行 56 项离线回归，全部通过。
其发现的活动恢复证据未重新核验（P1）与 review-only SourceIR 无来源证明（P2）已修复，独立
反例复跑确认拒绝，待配对工具回放为零。未做外部人工审查。
最终回归和复跑命令见[验证记录](../validation/provider-terminal-continuation-20261008.md)。

本次只处理成功保存后的新反馈。活动链大载荷仍完整保留；完整审计及其可读副本增加私有存储占用。
大载荷引用协议、存储扩展和缓存研究作为独立后续建议，不混入本次工具/API。未做真实模型、
部署环境验收。使用本机 PostgreSQL 16.14 新建只开放 Unix socket 的临时集群，随机数据库
执行发布竞态与 saved predecessor / durable cancellation / 连接重开恢复验证，均通过；随机
数据库和临时集群已清理。数据库证明不覆盖真实模型质量或完整 SaaS 验收。

下一次另行授权的有界真实验收应固定候选源码/发行、provider/model/thinking policy 与同一虚构原件，
完成首次交付后仅反馈一个 record，并做一次活动取消/恢复。保存 source SHA、SourceIR、稳定 IDs、
完成/应用保存回执、旧新 baseline/review/Excel 哈希、handoff、完整私有审计及每请求 metadata；
分别记录首次/反馈响应数、工具、时间和 usage。人工核对分类、其他记录、原文引用和同版交付；
缓存/请求 ID 以实际返回为准，金额需要独立账单或明确计价证据。这是验收范围，不是程序配额。
