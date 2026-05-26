# SOR 智能合规分析平台 AI 提示词与判定规则模板 v1

> 说明：本文保留 `v1` 提示词与规则模板，主要用于记录早期目标协议。  
> 当前真实 AI 流水线已经拆成 `screening + assessment`，并增加了 `INFO` 标注、`assessment_runs` 审计轨迹、Azure `RBAC/API key` 双模式。  
> 最新实现态请优先参考：`/Users/liuchang/Documents/ReqPilotX/docs/项目.md`

## 1. 总体策略
- `LLM` 负责语义理解、归纳与结构化输出。
- `Rule Engine` 负责硬约束（法规红线、禁配、平台边界）。
- 最终输出 = 规则判定 + LLM 判定（规则优先级更高）。

## 2. 输出协议（统一 JSON Schema）
所有模型输出必须满足以下结构:

```json
{
  "reqId": "string",
  "normalizedRequirement": "string",
  "requirementType": "FUNCTIONAL|PERFORMANCE|INTERFACE|REGULATION|TEST|OTHER",
  "proposedDecision": "ACCEPT|REJECT|DEVIATION_ACCEPT|NEED_CLARIFICATION",
  "confidence": 0.0,
  "reason": "string",
  "riskLevel": "LOW|MEDIUM|HIGH",
  "clarificationQuestion": "string",
  "evidences": [
    {
      "sourceDoc": "string",
      "sourceVersion": "string",
      "location": "string",
      "excerpt": "string",
      "relevance": 0.0
    }
  ]
}
```

约束:
- `confidence` 取值范围 `0~1`。
- `evidences` 至少 1 条，否则 `proposedDecision` 必须是 `NEED_CLARIFICATION`。
- `reason` 必须可执行，不允许空泛结论（如“基本可行”）。

## 3. 提示词模板 A: 需求拆解（Extraction）

System Prompt:
```text
你是汽车行业需求工程师助手。你的任务是将客户SOR文本拆解为“原子需求”。
规则：
1) 一条原子需求只表达一个可验证约束。
2) 不改变原意，不自行补充客户未声明条件。
3) 输出必须包含来源定位（页码/段落/表格位置）。
4) 若句子含糊，请保留原文并在 ambiguity 字段说明疑点。
仅输出JSON数组，不要输出解释性文字。
```

User Prompt 模板:
```text
项目: {{project_name}}
文档: {{sor_name}} (version={{sor_version}})
页面: {{page_no}}
段落定位: {{source_anchor}}
原文:
{{source_text}}
```

输出示例:
```json
[
  {
    "reqIdCandidate": "REQ-1.2.1",
    "atomicRequirement": "ECU cold start time shall be <= 150ms at -20C.",
    "requirementType": "PERFORMANCE",
    "priority": "HIGH",
    "sourcePage": 12,
    "sourceAnchor": "Section 1.2 paragraph 3",
    "ambiguity": ""
  }
]
```

## 4. 提示词模板 B: RAG 判定（Assessment）

System Prompt:
```text
你是汽车产品合规评审助手。请基于给定需求和内部证据，给出判定建议。
判定可选值：ACCEPT, REJECT, DEVIATION_ACCEPT, NEED_CLARIFICATION。
必须遵守：
1) 只允许引用给定证据，不可臆造文档。
2) 若证据不足或相互矛盾，输出 NEED_CLARIFICATION。
3) reason 需明确“能/不能满足”的技术原因。
4) 输出严格符合JSON Schema。
```

User Prompt 模板:
```text
需求ID: {{req_id}}
客户需求:
{{requirement_text}}

候选证据:
{{evidence_blocks}}

输出时请给出:
- normalizedRequirement
- proposedDecision
- confidence
- reason
- clarificationQuestion(如需要)
- evidences(仅保留最关键1~3条)
```

## 5. 提示词模板 C: 复核改写助手（Reviewer Copilot）

System Prompt:
```text
你是工程师复核助手。你不能替代最终责任人，只能帮助改写更清晰的结论。
要求：
1) 保留原判定方向，不擅自改变 finalDecision。
2) 将工程师备注改写成面向客户/审核可读的技术说明。
3) 必须引用至少一条证据来源。
仅输出JSON。
```

User Prompt 模板:
```text
finalDecision: {{final_decision}}
工程师原备注: {{engineer_note}}
可引用证据: {{evidence_blocks}}
```

## 6. 规则引擎模板（硬规则优先）
建议配置为 `YAML` 或数据库规则表。

```yaml
rules:
  - id: REG-001
    name: "法规红线不可偏差接受"
    when:
      requirement_type: "REGULATION"
      regulation_mandatory: true
      capability_match: false
    then:
      force_decision: "REJECT"
      reason_template: "法规强制项不满足，不能接受偏差。"
    priority: 100

  - id: PLAT-003
    name: "平台边界可偏差接受"
    when:
      requirement_type: "PERFORMANCE"
      gap_ratio_lte: 0.2
      workaround_exists: true
    then:
      force_decision: "DEVIATION_ACCEPT"
      reason_template: "与目标存在可协商差距，建议偏差接受并附加约束。"
    priority: 80
```

## 7. 置信度门槛策略
- `confidence >= 0.85`: 可默认高亮建议，允许“一键接受”。
- `0.60 <= confidence < 0.85`: 正常人工复核。
- `confidence < 0.60`: 强制标红，需人工重判。
- 若证据条数 `<1` 或证据冲突: 直接 `NEED_CLARIFICATION`。

## 8. 防幻觉与质量护栏
- 护栏 1: 未引用证据时禁止输出 `ACCEPT/REJECT/DEVIATION_ACCEPT`。
- 护栏 2: 引用文档必须存在于知识库已发布版本。
- 护栏 3: 对 `REGULATION` 类型启用更严格阈值（建议 `0.9`）。
- 护栏 4: 输出前执行 Schema 校验，不通过则自动重试（最多2次）。

## 9. 评测集与验收建议
- 抽取历史项目中已完结矩阵，构建标注集（建议先 300~500 条）。
- 核心评估指标:
  - 判定一致率（与专家最终结论）
  - 证据命中率（引用是否有效）
  - 低置信度召回率（不确定场景是否拦截）
- 上线门槛（建议）:
  - 一致率 >= 75%
  - 有效证据率 >= 95%
  - 高风险误判率 <= 2%

## 10. 运行配置建议
- prompt 版本化: `prompt_version` 入库，支持灰度切换。
- 模型双通道:
  - 主模型: 高准确（在线）
  - 备模型: 低成本（批量）
- 失败回退:
  - LLM 超时/失败时，仅输出“可编辑空白模板 + 检索证据”。
