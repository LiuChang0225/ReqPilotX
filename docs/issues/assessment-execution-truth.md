+++
id = "assessment-execution-truth"
title = "Assessment 执行状态与结论真实性"
status = "resolved"
severity = "critical"
category = "product-safety"
capability = "assessment"
work_package = "assessment-safety-001"
summary = "真实执行状态、Evidence identity、数值冲突保护与 W7 有效结论 provenance 已进入当前代码；真实业务准确率仍需 Gold 证据，但不再作为执行真实性缺陷混写。"
updated_at = "2026-08-09"
+++
# Assessment 执行状态与结论真实性

## 已解决的工程边界

- 保留的 legacy/shadow 兼容观察不会冒充当前版本的有效结果；具体服务结果由版本、发布和证据规则决定。
- provider/worker 失败、取消和 orphaned worker 进入明确的 FAILED/CANCELLED/STALE 状态，不伪装成完成。
- 每个 AI job 固定 Requirement Set 与启动时的知识库版本；同一 SOR 同时只允许一个 active job，版本切换或派生不能跨过运行中的 worker。发布前原子校验血缘和输入快照，迟到失败 worker 不覆盖较新页面投影。
- Evidence ID、内容哈希、来源位置和检索事实由服务端拥有，模型只能引用，不能替换。
- 数值约束和 Evidence 一致性检查可以阻止明显冲突的决定性候选。
- AI candidate 与人工 `final_decision` 分列，AI 不会写入或冒充人工字段。当前人工结论优先：
  AI `decision/compliance_status` 非空时为 `HUMAN_OVERRIDE`，否则为 `HUMAN_NATIVE`；
  没有人工结论时，当前终态 AI 结论以 `AI_DEFAULT` 成为有效结论。reason-only AI
  配合人工结论仍是 `HUMAN_NATIVE`。
- stale、pending、reason-only AI payload 本身和显式 `NOT_STARTED` 不构成有效结论。FORMAL 以
  `VISIBLE ∩ ANALYZE ∩ ELIGIBLE` 的当前有效结论覆盖为准，另外要求已锁定版本、
  `READY` Matrix 和稳定 preview 证明；不再要求用户逐条写入人工结论。
- 全部非 `SYSTEM_NON_BUSINESS` 节点都是用户业务行。`HUMAN_EXCLUDED` 保留
  `EXCLUDE` 及原因，但不进入分析与 FORMAL 分母。
- 派生 Requirement Set 的旧 AI/Evidence 使用 `STALE_REFERENCE`，旧人工结论只作为待确认参考。

## 不包含在“已解决”中的声明

以上说明执行和数据保护边界，不代表 AI 自动合规准确率已经证明。真实业务质量是独立的、尚未排期的验证工作；在明确推进需求分析时按[开发计划](../需求提取/开发计划.md)安排，不能因本问题 resolved 而省略，也不提前阻断当前目标态探索。

## 维护条件

如果后续出现 shadow 改写服务结果、失败任务显示成功、模型文本替换服务端 Evidence、
`AI_DEFAULT` 被写入或标成人工确认、stale/pending/reason-only/`NOT_STARTED` 被当成当前
有效结论，或 `HUMAN_EXCLUDED` 业务行被删除，应重新打开本问题。
