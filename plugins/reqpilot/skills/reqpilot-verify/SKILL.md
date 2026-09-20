---
name: reqpilot-verify
description: Verify source fidelity and delivery of an existing ReqPilotX result within the requested scope.
---

# Verify a real result

本阶段输入是原件、当前候选及其来源/版本和用户任务；产物是**有原文位置的核对结论、具体差异、未验范围与交接意见**。按本轮范围核对：Map-only检查原文、层级、原序、表图与已接入工作台；只有涉及条目、派生或交付时才检查相应分类、上下文和Excel。能打开首页不能证明当前文档或阶段已接入；文件、图片或Snn数量不能证明该范围业务完成。

完整流程核对实际阶段展示、当前修订版与未处理范围；仅对明确要求的结构确认或实际收到的反馈检查相应回执，不要求先有人反馈才能完成。当前仅离线或宿主缺少接入时明确未验，不将原生App或另一版本的证据替代当前plugin。常规导出仍由程序验证，不要求模型全文重读Excel。

本轮确需Excel且首次交付或内容没有实际修改时，优先使用所选task入口的task_deliver/task_check；脚本入口的 `workspace run deliver` 会验证并登记同版 Excel，返回 `CURRENT_DELIVERY_ATTACHED`，配套 `review-verify` 可作交接检查。不要为登记导出而制造空 `review-revise` 或 candidate。已有导出可用 [review-attach-delivery](../reqpilot/references/revision-loop.md#register-an-unchanged-current-delivery) 精确登记；`ATTACH_FAILED` 表示导出仍保留但登记未成功，按实际 CAS/版本原因处理。只有真正修改了工作内容的 candidate 才走 `review-close`。

Check the compiled Map's [source-first audit](../reqpilot/references/tools-map-audit.md) for exact unassigned text/rows/images and expanded-tree order. Resolve relevant leads against the original; a zero count does not establish semantic completeness, and a parallel capture does not require another business table. This supplements original-page inspection and does not replace it.

Use [the contract](../reqpilot/references/business-contract.md), [focused self-check](../reqpilot/references/semantic-self-check.md), and the triggered cases in [the index](../reqpilot/references/reading-guide.md). For an authorized independent verifier, also use [the cold protocol](../reqpilot/references/multi-agent.md); the producer's reasoning and expected counts are not source evidence.

Inspect the unfiltered hierarchy and uncertain boundaries: document/attachment ownership, actual body numbering, direct/trailing prose, whole front matter, logical tables and source order. Check each original business unit retains its governing conditions, shared values, notes and images without duplicate obligations.

For scopes including item judgments, check requirementness and classes by governed meaning, including normative statements without modal words. Verification methods/conditions do not alone create a separate PROCESS obligation. For requested derived results, recheck all normalized Snn against direct/shared source for logic, units, strict/inclusive bounds, negation and exceptions. Verify that definitions and applicable cross-section context are actually linked and available to the analysis, not merely retained elsewhere. Distinguish source children, engineering derivation, skipped analysis, attempted analysis, and effective engineer opinions.

For the authorized decomposition scope, inspect saved SPLIT/KEEP/UNDO `dispositions`, their reasons and `review_scope.remaining_count`. Check unreviewed parents rather than inferring coverage from an empty child list. Missing fields in a legacy artifact mean unknown coverage, not an implicit pass.

Meaningful images need a verified owner/disposition, not necessarily their own SRC. Covers and supporting table/clause images remain within those objects; genuine independent normative figures remain analyzable. Check original payload/pixels and source bindings, not merely media counts or a logo's presence. Interpretations are not customer-verbatim text.

Compare the same version's ledger/snapshot with program Excel: stable IDs, hierarchy/order, complete text/table/image content, classification, child links and response state. For application verification also inspect the workbench; normal exports do not need a new model reading of every cell. Layout QA belongs to relevant renderer changes.

For final self-check, correct supported defects and verify the revised scope before delivering; do not hand repairable errors back as a request for user feedback. An independent verifier reports exact findings to the producer/orchestrator, who applies the corrections and returns the affected scope for checking. Remaining gaps include severity, source range, affected identity and impact. Separate deterministic integrity, semantic review, independent-model evidence and user acceptance; none establishes absolute perfection or general quality.

For context-link creation or review, use [context applicability cases](../reqpilot/references/context-relations.md) to compare required links and exclusions against the existing saved set.
