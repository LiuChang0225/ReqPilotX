---
name: reqpilot-source-items
description: Extract and classify ordered source items from a ReqPilotX Document Map, or revise a bounded item scope.
---

# Form the original ledger

本阶段输入是经Agent回查的原文结构、完整上下文和本轮范围；仅明确要求结构确认的任务需要匹配确认。产物是**按原序、可回查原文的完整条目清单**，保留客户原编号，并给出需求/非需求判断与现行分类。程序签发SRC/REQ，分类不重排原文。定义、说明、条件、表格和相关图片仍须完整可读；摘要不能替代原文。每个有意义范围保存后同步到实际可用的工作台，自查并修复遗漏、重复、错序和判断问题后继续处理剩余范围，不等人工反馈或最终Excel才推进。无法消除的歧义保留来源与影响，不阻断其它条目的完整整理。

例如两个各自完整的编号性能要求分别成项；一份BOM保留其完整组成；一幅辅助说明图随既有条款保存和释读，不为了看过图片再造一个SRC。

Use [the contract and normal route](../reqpilot/references/reading-guide.md). Read complete owning provisions and necessary upper, adjacent and referenced context. Use [judgment cases](../reqpilot/references/judgment-cases.md) for class/role decisions and [table cases](../reqpilot/references/tables-and-relations.md) for table units. The role-specific cases carry normal and changed-condition examples; do not preload unrelated guides.

Inspect the request's `structural_role`. With the typed task tools, use `items_apply.decisions.retain_map_structure=true`: the program retains already-declared whole front matter and Map titles with zero SRC, including their complete source coverage. Supply body items and table grouping/classification decisions; ordinary body and non-title captions are never automatically hidden. The explicit `structure_only_spans` / FRONT_MATTER route remains available for precise choices. Ordinary body cannot be hidden with that label.

A pure heading has no independent statement to classify: preserve it as structure, not a `NON_REQUIREMENT/HEADING` SRC. If it is still declared as Map body, [repair that role](../reqpilot/references/tools-map-edit.md#repair-a-heading-captured-as-body) first; do not discard its text. A sentence that defines a term or governs the following clauses remains substantive content even when printed in bold. See the paired [heading examples](../reqpilot/references/judgment-cases.md#pure-heading-or-substantive-content).

Apply original numbered leaf boundaries, with explicit complete-unit exceptions. For ordinary prose use PARAGRAPH, numbered original provisions NUMBERED_CLAUSE, actual lists NATIVE_LIST. Original numbering remains a separately source-bound field; use the supported collection when a valid complete unit contains several numbers. Form never determines requirementness/class.

Use generated scope requests and the [item input section](../reqpilot/references/tools-items.md). Return boundary/classification decisions; the compiler copies source and allocates stable identities. Integrate through it, rather than independently numbering batches. Scope windows are execution aids, not semantic boundaries.

With task tools, follow `scope_read` returned by task open/inspect or Map writes to the real scopes/templates, and `source_items_read` to a saved ledger. On a navigation error, follow `next_action`, then the returned table/content/schema read; do not guess part or definition names. An empty number or `UNNUMBERED` projection does not prove the original has no number. Table/list numbers may still need exact source binding; this source check and governing-context links remain necessary when internal-evidence compliance analysis is skipped.

Use the current projected original numbers; add numbering annotations only for source-backed corrections or unresolved/compound numbering. After item compilation, inspect context_review when offered: its context_candidates are already-declared definitions/conditions, not proven applicability. Read applicable original context and use the existing annotations_apply annotation_updates/context_source_ids (script-only: compile-annotations) to preserve governing links and supporting-image readings. An explicit context list replaces the old set, so retain still-valid links. Check linked_context rather than assuming that preserving a definition elsewhere made it available to this item.

Inspect images with original pages and image-packet. Supporting images stay with their table/clause/whole front matter, including source-bound visual readings; compile-images is for genuine independent image items. Keep labels/readings separate from source pixels. No pseudo-Snn or duplicate image obligation is needed to demonstrate analysis.

Empty templates can retain the grid with zero SRC. Nonempty placeholders require exact values and a source reason; never infer that `/`, `0` or `N/A` is empty, or turn body rows into fake headers. Preserve all meaningful body/context and the versioned predecessor during revision. Finish with the [focused self-check](../reqpilot/references/semantic-self-check.md).

If `SOURCE_ITEM_STRUCTURE_ONLY_SPAN_NOT_TITLE` names a pure heading or table caption, inspect the original and correct its Map title/content role before retrying items. This rejection identifies a contract mismatch, not the semantic role. Do not manufacture a non-requirement SRC just to pass; actual explanations, notes and obligations still belong in the ledger. See the [Map role repair](../reqpilot/references/tools-map-edit.md#repair-a-misplaced-pure-caption).

For context-link creation or review, use [context applicability cases](../reqpilot/references/context-relations.md) to compare required links and exclusions against the existing saved set.
