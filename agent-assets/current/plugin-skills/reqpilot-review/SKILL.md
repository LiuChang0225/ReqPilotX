---
name: reqpilot-review
description: Create authorized requirement splits and cited response drafts from an existing source ledger.
---

# Review and derive

本阶段输入是完整原文条目、适用上下文、本轮授权范围及实际内部依据。产物包括**已检查保留整体及理由、必要派生子项与依据、尚未检查范围**；有内部依据时另给带引用的响应建议。原文父项保持完整，工程师能分别核对原文、派生和建议。完整流程已给的必要拆分授权不重复逐项询问；仅要求原文/结构的任务不扩大范围。

自主核对并修正派生逻辑、范围和证据适用性，不等待人工反馈。无内部依据时跳过产品符合性判断，仍完成客户原文矛盾、缺附件、TBD和不清楚判据的检查；合理回查后未解的问题保留定位与影响，继续其余范围和程序草稿交付。自查或局部反馈只更新受影响范围和依赖结果。

Apply [the shared contract](../reqpilot/references/business-contract.md) and [J07–J10](../reqpilot/references/judgment-cases.md#decomposition-and-response). Verify the parent identities and decomposition scope; an explicit end-to-end request can authorize it without per-item permission.

Understand complete original parents, governing context, shared table information and relevant exceptions/references, reusing matching-version evidence. Before independent responses or Snn, inspect linked context and investigate missing applicable definitions/conditions; a nearby source window may omit them. context_review/context_candidates and annotations_apply expose the existing source-link capability when task tools are available; script-only equivalents are described in the relevant tool section. Preserve original numbered children separately from engineering Snn. Keep-whole, not-reviewed and split are distinct decisions; use a material reason. Independent outcomes can justify separate responses, while parameters/steps defining one target normally stay together.

Use decomposition_apply and its returned packets when task tools are offered; use the [decomposition tool section](../reqpilot/references/tools-decomposition.md) for script-only syntax or a specific field question. Linked external source items provide context, not extra parent segment_index ranges; preserve their relationship instead of inventing parent text offsets. Preserve ALL_OF/ANY_OF logic, bounds, negation and applicability. Every normalized derivation needs source-based semantic review; exact cover alone cannot prove equivalence. Image-only source does not acquire valid text-range derivations through invented OCR bindings.

Check internal evidence and analysis intent before building analysis packets. With none supplied, skip automatic compliance analysis; source-gap checks and authorized decomposition can continue. Where analysis is applicable, use the [analysis section](../reqpilot/references/tools-analysis-delivery.md), supplied registered evidence and actual source locators. An item-specific evidence gap or unclear customer criterion is explicit, never compliance.

Use the same baseline/analysis in program Matrix/Excel. Proposed responses and engineer decisions remain separate, and neither is customer approval. Preserve unrelated results and stable IDs when revising.

Check saved `dispositions` for SPLIT/KEEP/UNDO and their reasons, and `review_scope` for the actual authorized, reviewed and remaining parents. Continue unreviewed authorized parents where evidence permits; an empty child list is not a keep-whole decision. A legacy artifact without these fields has unknown review coverage. Preserve useful partial results and disclose unresolved scope rather than inventing children or claiming whole-scope completion.
