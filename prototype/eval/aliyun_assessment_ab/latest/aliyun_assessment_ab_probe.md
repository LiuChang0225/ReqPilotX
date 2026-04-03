# Aliyun Assessment A/B Probe

- 生成时间：2026-04-03T11:40:38
- Profile：`aliyun`
- Chat model：`qwen3.5-plus`
- Timeout：`75.0`

## REQ-0014

- retrieval backend: `qdrant`
- evidence_count: `0`
- retrieval_gate_reason: `缺少法规类知识文档，当前无法为该法规条目提供直接证据。`

### default

- status: `ok`
- seconds: `43.818`
- ai_decision: `NEED_CLARIFICATION`
- validation_status: `accepted`
- evidence_count: `0`
- fallback_reason: ``

### enable_thinking_false

- status: `ok`
- seconds: `5.327`
- ai_decision: `NEED_CLARIFICATION`
- validation_status: `accepted`
- evidence_count: `0`
- fallback_reason: ``

### thinking_budget_50

- status: `ok`
- seconds: `12.49`
- ai_decision: `NEED_CLARIFICATION`
- validation_status: `accepted`
- evidence_count: `0`
- fallback_reason: ``

## REQ-0025

- retrieval backend: `qdrant`
- evidence_count: `3`
- retrieval_gate_reason: ``

### default

- status: `error`
- seconds: `48.089`
- error: `Request timed out.`

### enable_thinking_false

- status: `ok`
- seconds: `19.287`
- ai_decision: `NEED_CLARIFICATION`
- validation_status: `accepted`
- evidence_count: `0`
- fallback_reason: ``

### thinking_budget_50

- status: `ok`
- seconds: `7.746`
- ai_decision: `NEED_CLARIFICATION`
- validation_status: `accepted`
- evidence_count: `0`
- fallback_reason: ``

