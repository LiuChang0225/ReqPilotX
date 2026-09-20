import type {
  EntryClassificationResponse,
  JsonRecord,
  RequirementAtomizationScopeState,
  RequirementSemanticPublicationCapabilityV2,
  RequirementSemanticPublicationCountsV2,
  RequirementSemanticPublicationPreviewRequestV2,
  RequirementSemanticPublicationPreviewResponseV2,
  RequirementSemanticPublishDraftResponseV2,
  RequirementTreeSnapshot,
} from "../../shared/api/contracts";
import {
  canonicalJson,
  canonicalJsonSha256,
  withoutCanonicalField,
} from "./canonicalJsonSha256";

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const SCOPE_RECEIPT_ID_PATTERN =
  /^atomization-scope-receipt-[0-9a-f]{32}$/;
const BINDING_PREVIEW_ID_PATTERN =
  /^requirement-enrichment-binding-preview-[0-9a-f]{32}$/;
const BINDING_ID_PATTERN = /^requirement-enrichment-binding-[0-9a-f]{32}$/;
const ATOMIZATION_RUN_ID_PATTERN = /^atomization-run-[0-9a-f]{32}$/;
const ATOMIZATION_NODE_ID_PATTERN = /^atomization-node-[0-9a-f]{32}$/;
const ATOMIC_REQUIREMENT_ID_PATTERN = /^atomic-obligation-[0-9a-f]{32}$/;
const PUBLICATION_PREVIEW_ID_PATTERN =
  /^requirement-publication-preview-[0-9a-f]{32}$/;
const PUBLICATION_ROW_KEYS = [
  "atomic_requirement_id",
  "atomization_node_id",
  "builder_binding_role",
  "builder_kind",
  "classification",
  "content_blocks",
  "decision_mode",
  "default_disposition",
  "deterministic_rule_evidence",
  "editable_structure_node_ids",
  "entry_kind",
  "figure_refs",
  "final_projection_role",
  "include_in_view",
  "intrinsic_analysis_eligible",
  "label",
  "original_statement",
  "page_end",
  "page_start",
  "parent_requirement_tree_node_id",
  "published_statement",
  "requirement_tree_node_id",
  "source_offset",
  "source_order",
  "source_refs",
  "source_spans",
  "source_working_tree_node_id",
  "status",
  "structure_node_id",
  "table_evidence_binding_id",
  "table_record_id",
  "table_refs",
  "workflow_node_id",
] as const;
const PUBLICATION_PROJECTION_KEYS = [
  "accepted_working_tree_id",
  "accepted_working_tree_sha256",
  "applied_review_node_ids",
  "atomization_active_revision",
  "atomization_classification_content_sha256",
  "atomization_revision",
  "atomization_run_id",
  "atomization_run_sha256",
  "atomization_scope_receipt_id",
  "atomization_scope_receipt_sha256",
  "atomization_scope_revision",
  "atomization_state",
  "document_id",
  "effective_structure_id",
  "entry_classification_content_sha256",
  "entry_classification_revision",
  "entry_classification_sha256",
  "entry_classification_snapshot_id",
  "project_id",
  "projection_sha256",
  "publication_key",
  "publication_lineage_sha256",
  "publication_preview_id",
  "publication_preview_sha256",
  "rows",
  "schema_version",
  "source_business_tree_node_ids",
  "source_sha256",
] as const;
const BUILDER_BINDING_ROLES = new Set([
  "STRUCTURE_PROXY",
  "DIRECT_CONTENT",
  "WHOLE_TABLE_REVIEW",
  "TABLE_RECORD",
  "REQUIREMENT",
]);

const CAPABILITY_KEYS = [
  "atomization_states",
  "default_dispositions",
  "not_run_preview_invokes_atomization",
  "preview_request_schema_version",
  "preview_response_schema_version",
  "publish_request_schema_version",
  "publish_response_schema_version",
  "publish_starts_analysis",
  "schema_version",
] as const;

export interface VerifiedRequirementSemanticPublicationPreviewV2 {
  response: RequirementSemanticPublicationPreviewResponseV2;
  bindingPreviewId: string;
  bindingPreviewSha256: string;
  counts: RequirementSemanticPublicationCountsV2;
}

export interface VerifiedRequirementSemanticPublishDraftV2 {
  response: RequirementSemanticPublishDraftResponseV2;
  requirementSetId: string;
  bindingId: string;
}

export type RequirementSemanticPublicationAtomizationBinding =
  | { atomizationState: "NOT_RUN" }
  | {
      atomizationState: "APPLIED";
      receiptId: string;
      receiptSha256: string;
      scopeRevision: number;
    };

function record(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

function exactKeys(value: JsonRecord, expected: readonly string[]): boolean {
  const keys = Object.keys(value).sort();
  const expectedKeys = [...expected].sort();
  return (
    keys.length === expectedKeys.length &&
    keys.every((key, index) => key === expectedKeys[index])
  );
}

export function exactRequirementSemanticPublicationCapabilityV2(
  value: unknown,
): RequirementSemanticPublicationCapabilityV2 | null {
  const capability = record(value);
  const dispositions = record(capability.default_dispositions);
  const states = Array.isArray(capability.atomization_states)
    ? capability.atomization_states
    : [];
  if (
    !exactKeys(capability, CAPABILITY_KEYS) ||
    capability.schema_version !==
      "requirement-enrichment-semantic-publication-capability/v2" ||
    capability.preview_request_schema_version !==
      "requirement-enrichment-publication-preview-request/v2" ||
    capability.preview_response_schema_version !==
      "requirement-enrichment-publication-preview-response/v2" ||
    capability.publish_request_schema_version !==
      "requirement-enrichment-publish-draft-request/v2" ||
    capability.publish_response_schema_version !==
      "requirement-enrichment-publish-draft-response/v2" ||
    states.length !== 2 ||
    states[0] !== "NOT_RUN" ||
    states[1] !== "APPLIED" ||
    capability.not_run_preview_invokes_atomization !== false ||
    capability.publish_starts_analysis !== false ||
    !exactKeys(dispositions, ["INFORMATION", "NON_TECHNICAL", "TECHNICAL"]) ||
    dispositions.TECHNICAL !== "ANALYZE" ||
    dispositions.NON_TECHNICAL !== "RETAIN" ||
    dispositions.INFORMATION !== "REFERENCE"
  ) {
    return null;
  }
  return capability as unknown as RequirementSemanticPublicationCapabilityV2;
}

export function buildRequirementSemanticPublicationPreviewRequestV2(options: {
  reviewRevision: number;
  classification: EntryClassificationResponse;
  scopeState: RequirementAtomizationScopeState;
  projectId: string;
  documentId: string;
  sessionId: string;
}): RequirementSemanticPublicationPreviewRequestV2 {
  const snapshot = options.classification.snapshot;
  if (
    snapshot.status !== "ACCEPTED" ||
    snapshot.project_id !== options.projectId ||
    snapshot.sor_document_id !== options.documentId ||
    snapshot.revision < 1 ||
    !SHA256_PATTERN.test(snapshot.payload_sha256)
  ) {
    throw new Error("当前已接受的分类结果无法验证，请重新读取分类后再生成。");
  }
  if (
    options.scopeState.classification_snapshot_id !== snapshot.snapshot_id ||
    options.scopeState.classification_revision !== snapshot.revision ||
    options.scopeState.classification_sha256 !== snapshot.payload_sha256
  ) {
    throw new Error("当前原子化结果并非基于刚接受的分类，请重新读取后再生成。");
  }
  const base = {
    schema_version:
      "requirement-enrichment-publication-preview-request/v2" as const,
    expected_review_revision: options.reviewRevision,
    accepted_entry_classification_snapshot_id: snapshot.snapshot_id,
    accepted_entry_classification_revision: snapshot.revision,
    accepted_entry_classification_sha256: snapshot.payload_sha256,
  };
  const atomization = requirementSemanticPublicationAtomizationBinding(
    options.scopeState,
  );
  if (atomization.atomizationState === "NOT_RUN") {
    return { ...base, atomization_state: "NOT_RUN" };
  }
  return {
    ...base,
    atomization_state: "APPLIED",
    atomization_scope_receipt_id: atomization.receiptId,
    atomization_scope_receipt_sha256: atomization.receiptSha256,
    atomization_scope_revision: atomization.scopeRevision,
  };
}

export function requirementSemanticPublicationAtomizationBinding(
  scopeState: RequirementAtomizationScopeState,
): RequirementSemanticPublicationAtomizationBinding {
  if (scopeState.last_attempt?.status === "RUNNING") {
    throw new Error(
      "原子化仍在执行，请等待进度完成并重新读取范围后再生成确认清单。",
    );
  }
  const receipt = scopeState.current_receipt;
  if (!receipt) {
    return { atomizationState: "NOT_RUN" };
  }
  if (!Array.isArray(receipt.cumulative_review_node_ids)) {
    throw new Error(
      "已应用的原子化结果不完整，请重新读取范围后再生成确认清单。",
    );
  }
  if (receipt.cumulative_review_node_ids.length === 0) {
    return { atomizationState: "NOT_RUN" };
  }
  if (
    !["CURRENT", "CONTENT_EQUIVALENT"].includes(
      scopeState.classification_binding_state,
    )
  ) {
    throw new Error(
      "已应用的原子化范围与当前分类不再一致；请重新执行或撤销原子化，再生成确认清单。",
    );
  }
  if (
    !SCOPE_RECEIPT_ID_PATTERN.test(receipt.receipt_id) ||
    !SHA256_PATTERN.test(receipt.payload_sha256) ||
    !Number.isInteger(receipt.scope_revision) ||
    receipt.scope_revision < 1
  ) {
    throw new Error(
      "已应用的原子化结果无法验证，请重新读取范围后再生成确认清单。",
    );
  }
  return {
    atomizationState: "APPLIED",
    receiptId: receipt.receipt_id,
    receiptSha256: receipt.payload_sha256,
    scopeRevision: receipt.scope_revision,
  };
}

function classificationMatches(
  value: unknown,
  classification: EntryClassificationResponse,
): boolean {
  const actual = record(value);
  const expected = classification.snapshot;
  return (
    exactKeys(actual, ["revision", "sha256", "snapshot_id"]) &&
    expected.status === "ACCEPTED" &&
    actual.snapshot_id === expected.snapshot_id &&
    actual.revision === expected.revision &&
    actual.sha256 === expected.payload_sha256
  );
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function hasUniqueStrings(values: readonly string[]): boolean {
  return new Set(values).size === values.length;
}

function isRecordArray(value: unknown): value is JsonRecord[] {
  return (
    Array.isArray(value) &&
    value.every(
      (item) => Boolean(item) && typeof item === "object" && !Array.isArray(item),
    )
  );
}

function isOptionalString(value: unknown): boolean {
  return value === null || typeof value === "string";
}

function validSourceSpans(value: unknown): value is JsonRecord[] {
  return (
    isRecordArray(value) &&
    value.every(
      (span) =>
        exactKeys(span, [
          "end_offset",
          "owner_structure_node_id",
          "semantic_unit_id",
          "start_offset",
        ]) &&
        typeof span.semantic_unit_id === "string" &&
        /^semantic-unit-[0-9a-f]{32}$/.test(span.semantic_unit_id) &&
        typeof span.owner_structure_node_id === "string" &&
        Boolean(span.owner_structure_node_id) &&
        Number.isInteger(span.start_offset) &&
        Number(span.start_offset) >= 0 &&
        Number.isInteger(span.end_offset) &&
        Number(span.end_offset) >= Number(span.start_offset),
    )
  );
}

function validatePublicationRowShape(row: JsonRecord): void {
  const rowId = String(row.requirement_tree_node_id || "");
  const sourceId = String(row.source_working_tree_node_id || "");
  const parentId = row.parent_requirement_tree_node_id;
  const atomizationNodeId = row.atomization_node_id;
  const atomicRequirementId = row.atomic_requirement_id;
  const sourceSpans = row.source_spans;
  const editableIds = row.editable_structure_node_ids;
  const contentBlocks = row.content_blocks;
  const isAnchor = rowId === sourceId;
  if (
    !exactKeys(row, PUBLICATION_ROW_KEYS) ||
    !rowId ||
    !sourceId ||
    typeof row.workflow_node_id !== "string" ||
    !row.workflow_node_id ||
    !isOptionalString(parentId) ||
    !isOptionalString(atomizationNodeId) ||
    !isOptionalString(atomicRequirementId) ||
    (atomizationNodeId !== null &&
      !ATOMIZATION_NODE_ID_PATTERN.test(String(atomizationNodeId))) ||
    (atomicRequirementId !== null &&
      !ATOMIC_REQUIREMENT_ID_PATTERN.test(String(atomicRequirementId))) ||
    !["HEADING", "REQUIREMENT", "REFERENCE"].includes(
      String(row.entry_kind || ""),
    ) ||
    !["REQUIREMENT", "REQUIREMENT_CONTAINER"].includes(
      String(row.builder_kind || ""),
    ) ||
    !["DIRECT", "ROLLUP", "NONE"].includes(
      String(row.decision_mode || ""),
    ) ||
    !["TECHNICAL", "NON_TECHNICAL", "INFORMATION"].includes(
      String(row.classification || ""),
    ) ||
    !BUILDER_BINDING_ROLES.has(String(row.builder_binding_role || "")) ||
    !BUILDER_BINDING_ROLES.has(String(row.final_projection_role || "")) ||
    typeof row.intrinsic_analysis_eligible !== "boolean" ||
    !["ANALYZE", "RETAIN"].includes(String(row.default_disposition || "")) ||
    !Number.isInteger(row.source_order) ||
    Number(row.source_order) < 0 ||
    !Number.isInteger(row.source_offset) ||
    Number(row.source_offset) < 0 ||
    typeof row.label !== "string" ||
    typeof row.original_statement !== "string" ||
    typeof row.published_statement !== "string" ||
    !row.published_statement ||
    !isOptionalString(row.structure_node_id) ||
    !(
      row.page_start === null ||
      (Number.isInteger(row.page_start) && Number(row.page_start) >= 1)
    ) ||
    !(
      row.page_end === null ||
      (Number.isInteger(row.page_end) && Number(row.page_end) >= 1)
    ) ||
    (row.page_start !== null &&
      row.page_end !== null &&
      Number(row.page_end) < Number(row.page_start)) ||
    !validSourceSpans(sourceSpans) ||
    !isRecordArray(row.source_refs) ||
    typeof row.status !== "string" ||
    !isOptionalString(row.table_record_id) ||
    !isOptionalString(row.table_evidence_binding_id) ||
    !isRecordArray(row.deterministic_rule_evidence) ||
    typeof row.include_in_view !== "boolean" ||
    !isRecordArray(contentBlocks) ||
    !isStringArray(editableIds) ||
    !hasUniqueStrings(editableIds) ||
    !isRecordArray(row.table_refs) ||
    !isRecordArray(row.figure_refs)
  ) {
    throw new Error("发布预览业务行不符合完整的清单投影合同。");
  }
  const contentStructureIds = new Set(
    contentBlocks.flatMap((block) => {
      const id = String(block.structure_node_id || "");
      return id ? [id] : [];
    }),
  );
  if (editableIds.some((id) => !contentStructureIds.has(id))) {
    throw new Error("发布预览业务行的可编辑内容来源无法验证。");
  }
  if (atomicRequirementId !== null && atomizationNodeId === null) {
    throw new Error("发布预览原子需求缺少原子化证据。");
  }
  if (atomizationNodeId !== null && sourceSpans.length === 0) {
    throw new Error("发布预览原子化结果缺少精确来源范围。");
  }
  if (!isAnchor) {
    if (
      parentId !== sourceId ||
      rowId !== atomizationNodeId ||
      atomicRequirementId === null ||
      row.entry_kind !== "REQUIREMENT" ||
      row.builder_kind !== "REQUIREMENT" ||
      row.decision_mode !== "DIRECT"
    ) {
      throw new Error("发布预览原子子项与原条目的父子身份不一致。");
    }
  } else if (parentId !== null) {
    throw new Error("发布预览原条目不能再挂接逻辑父项。");
  }
  if (row.entry_kind === "HEADING") {
    if (
      row.builder_kind !== "REQUIREMENT_CONTAINER" ||
      row.decision_mode !== "ROLLUP" ||
      atomicRequirementId !== null ||
      atomizationNodeId === null ||
      row.classification !== "TECHNICAL" ||
      row.final_projection_role !== "REQUIREMENT" ||
      row.intrinsic_analysis_eligible !== false ||
      row.default_disposition !== "RETAIN"
    ) {
      throw new Error("发布预览的原条目证据父级语义不完整。");
    }
  } else if (row.builder_kind === "REQUIREMENT_CONTAINER") {
    throw new Error("发布预览只有原条目证据父级可以作为容器。");
  } else if (
    row.entry_kind === "REQUIREMENT" &&
    row.decision_mode !== "DIRECT"
  ) {
    throw new Error("发布预览的需求行必须是直接判断项。");
  }
  if (
    row.entry_kind === "REFERENCE" &&
    (row.decision_mode !== "NONE" ||
      row.intrinsic_analysis_eligible !== false ||
      row.default_disposition !== "RETAIN")
  ) {
    throw new Error("发布预览的信息参考不能成为独立分析项。");
  }
  if (row.classification === "INFORMATION" && row.entry_kind !== "REFERENCE") {
    throw new Error("发布预览的信息分类必须保留为参考内容。");
  }
  if (
    row.classification === "TECHNICAL" &&
    row.entry_kind !== "HEADING" &&
    (row.entry_kind !== "REQUIREMENT" ||
      row.intrinsic_analysis_eligible !== true ||
      row.decision_mode !== "DIRECT" ||
      row.final_projection_role !== "REQUIREMENT" ||
      row.default_disposition !== "ANALYZE")
  ) {
    throw new Error("发布预览的技术需求默认分析语义不一致。");
  }
  if (
    row.classification === "NON_TECHNICAL" &&
    (row.entry_kind !== "REQUIREMENT" ||
      row.intrinsic_analysis_eligible !== true ||
      row.decision_mode !== "DIRECT" ||
      row.final_projection_role !== "DIRECT_CONTENT" ||
      row.default_disposition !== "RETAIN")
  ) {
    throw new Error("发布预览的非技术内容保留语义不一致。");
  }
  if (
    row.default_disposition === "ANALYZE" &&
    (row.classification !== "TECHNICAL" ||
      row.intrinsic_analysis_eligible !== true ||
      row.entry_kind !== "REQUIREMENT" ||
      row.final_projection_role !== "REQUIREMENT" ||
      row.decision_mode !== "DIRECT")
  ) {
    throw new Error("发布预览的默认分析项不是直接技术需求。");
  }
}

function validatedProjectionCounts(
  rowsValue: unknown,
  classification: EntryClassificationResponse,
  atomizationState: "NOT_RUN" | "APPLIED",
  sourceBusinessTreeNodeIds: readonly string[],
): RequirementSemanticPublicationCountsV2 {
  if (!Array.isArray(rowsValue) || rowsValue.length === 0) {
    throw new Error("发布预览没有返回完整业务行，系统不会继续发布。");
  }
  const classificationByTreeNodeId = new Map(
    classification.snapshot.entries.map((entry) => [
      entry.tree_node_id,
      entry,
    ]),
  );
  const rowIds = new Set<string>();
  let defaultAnalyze = 0;
  let defaultRetain = 0;
  let reference = 0;
  let atomized = 0;
  const rows = rowsValue.map((value) => record(value));
  for (const row of rows) {
    validatePublicationRowShape(row);
    const rowId = String(row.requirement_tree_node_id || "");
    const sourceTreeNodeId = String(
      row.source_working_tree_node_id || "",
    );
    const sourceClassification =
      classificationByTreeNodeId.get(sourceTreeNodeId);
    if (
      !rowId ||
      rowIds.has(rowId) ||
      !sourceClassification ||
      !sourceClassification.effective_type ||
      row.workflow_node_id !== sourceClassification.workflow_node_id ||
      row.classification !== sourceClassification.effective_type
    ) {
      throw new Error("发布预览与已接受的分类或当前整理结果不一致。");
    }
    rowIds.add(rowId);
    if (row.default_disposition === "ANALYZE") defaultAnalyze += 1;
    else if (row.default_disposition === "RETAIN") defaultRetain += 1;
    else throw new Error("发布预览包含未知的默认处理方式。");
    if (row.entry_kind === "REFERENCE") reference += 1;
    if (row.atomization_node_id !== null) atomized += 1;
    if (
      atomizationState === "NOT_RUN" &&
      (row.atomization_node_id !== null ||
        row.atomic_requirement_id !== null ||
        rowId !== sourceTreeNodeId ||
        row.parent_requirement_tree_node_id !== null)
    ) {
      throw new Error("发布预览在未执行原子化时包含了拆分结果。");
    }
  }
  const rowById = new Map(
    rows.map((row) => [String(row.requirement_tree_node_id), row]),
  );
  for (const row of rows) {
    const parentId = row.parent_requirement_tree_node_id;
    if (typeof parentId === "string" && !rowById.has(parentId)) {
      throw new Error("发布预览业务行的父项不存在。");
    }
    const visited = new Set<string>();
    let cursor: JsonRecord | undefined = row;
    while (cursor) {
      const cursorId = String(cursor.requirement_tree_node_id || "");
      if (visited.has(cursorId)) {
        throw new Error("发布预览业务行出现父子循环。");
      }
      visited.add(cursorId);
      const nextId: unknown = cursor.parent_requirement_tree_node_id;
      cursor = typeof nextId === "string" ? rowById.get(nextId) : undefined;
    }
  }
  const anchorIds = rows.flatMap((row) =>
    row.requirement_tree_node_id === row.source_working_tree_node_id
      ? [String(row.requirement_tree_node_id)]
      : [],
  );
  if (canonicalJson(anchorIds) !== canonicalJson(sourceBusinessTreeNodeIds)) {
    throw new Error("发布预览没有完整覆盖当前清单的全部原条目。");
  }
  const childrenByAnchor = new Map(
    sourceBusinessTreeNodeIds.map((id) => [id, [] as string[]]),
  );
  for (const row of rows) {
    if (row.requirement_tree_node_id !== row.source_working_tree_node_id) {
      childrenByAnchor
        .get(String(row.source_working_tree_node_id))
        ?.push(String(row.requirement_tree_node_id));
    }
  }
  const expectedOrder = sourceBusinessTreeNodeIds.flatMap((id) => [
    id,
    ...(childrenByAnchor.get(id) || []),
  ]);
  if (
    canonicalJson(rows.map((row) => row.requirement_tree_node_id)) !==
    canonicalJson(expectedOrder)
  ) {
    throw new Error("发布预览业务行没有保持原条目及其拆分项的完整顺序。");
  }
  return {
    total: rowsValue.length,
    default_analyze: defaultAnalyze,
    default_retain: defaultRetain,
    reference,
    atomized,
  };
}

function sameCounts(
  value: unknown,
  expected: RequirementSemanticPublicationCountsV2,
): boolean {
  const actual = record(value);
  return (
    exactKeys(actual, [
      "atomized",
      "default_analyze",
      "default_retain",
      "reference",
      "total",
    ]) &&
    actual.total === expected.total &&
    actual.default_analyze === expected.default_analyze &&
    actual.default_retain === expected.default_retain &&
    actual.reference === expected.reference &&
    actual.atomized === expected.atomized
  );
}

const REQUIREMENT_TREE_KEYS = [
  "mode",
  "nodes",
  "requirement_evidence_bundle",
  "root_node_ids",
  "schema_version",
  "source_identity",
  "state",
  "structure_identity",
  "table_evidence_bindings",
  "table_record_projections",
] as const;
const REQUIREMENT_TREE_NODE_KEYS = [
  "attention_count",
  "child_ids",
  "classification",
  "content_blocks",
  "depth",
  "deterministic_rule_evidence",
  "editable_structure_node_ids",
  "figure_refs",
  "id",
  "include_in_view",
  "kind",
  "label",
  "page_end",
  "page_start",
  "parent_id",
  "projection_role",
  "requirement_count",
  "requirement_entry_id",
  "requirement_evidence_binding_id",
  "review_item_count",
  "reviewed_count",
  "semantic_state",
  "source_offset",
  "source_order",
  "source_refs",
  "status",
  "structure_node_id",
  "summary",
  "table_evidence_binding_id",
  "table_record_id",
  "table_refs",
  "toc_entries",
  "workflow_node_id",
] as const;
const REQUIREMENT_TREE_BILINGUAL_CONTEXT_KEYS = [
  "authority_basis",
  "authority_language",
  "canonical_structure_node_id",
  "counterpart_group_id",
  "document_language_policy_id",
  "equal_validity",
  "member_structure_node_ids",
  "mismatch_review_required",
  "mismatch_signal_ids",
  "source_spans",
] as const;
const REQUIREMENT_TREE_BILINGUAL_SPAN_KEYS = [
  "bilingual_span_id",
  "boundary_basis",
  "end_offset",
  "language",
  "owner_structure_node_id",
  "source_ref",
  "start_offset",
] as const;

function validPublishedBilingualContext(node: JsonRecord): boolean {
  if (node.bilingual_context === undefined) return true;
  const context = record(node.bilingual_context);
  const members = Array.isArray(context.member_structure_node_ids)
    ? context.member_structure_node_ids
    : [];
  const mismatchIds = Array.isArray(context.mismatch_signal_ids)
    ? context.mismatch_signal_ids
    : [];
  const spans = Array.isArray(context.source_spans)
    ? context.source_spans.map((value) => record(value))
    : [];
  const languages = new Set(spans.map((span) => span.language));
  const authorityLanguage = String(context.authority_language || "");
  const authorityBasis = String(context.authority_basis || "");
  return (
    exactKeys(context, REQUIREMENT_TREE_BILINGUAL_CONTEXT_KEYS) &&
    /^bilingual-group-[0-9a-f]{32}$/.test(
      String(context.counterpart_group_id || ""),
    ) &&
    (context.document_language_policy_id === null ||
      /^document-language-policy-[0-9a-f]{32}$/.test(
        String(context.document_language_policy_id || ""),
      )) &&
    typeof context.canonical_structure_node_id === "string" &&
    context.canonical_structure_node_id.length > 0 &&
    isStringArray(members) &&
    members.length >= 1 &&
    members.length <= 2 &&
    hasUniqueStrings(members) &&
    members.includes(String(context.canonical_structure_node_id)) &&
    ["en", "zh-CN", "UNKNOWN"].includes(authorityLanguage) &&
    ["DECLARED_DOCUMENT_POLICY", "UNKNOWN"].includes(authorityBasis) &&
    (context.equal_validity === null ||
      typeof context.equal_validity === "boolean") &&
    isStringArray(mismatchIds) &&
    hasUniqueStrings(mismatchIds) &&
    mismatchIds.every((value) =>
      /^bilingual-mismatch-[0-9a-f]{32}$/.test(value),
    ) &&
    context.mismatch_review_required === (mismatchIds.length > 0) &&
    spans.length >= 2 &&
    languages.size === 2 &&
    languages.has("en") &&
    languages.has("zh-CN") &&
    spans.every((span) => {
      const sourceRef = record(span.source_ref);
      return (
        exactKeys(span, REQUIREMENT_TREE_BILINGUAL_SPAN_KEYS) &&
        /^bilingual-span-[0-9a-f]{32}$/.test(
          String(span.bilingual_span_id || ""),
        ) &&
        (span.language === "en" || span.language === "zh-CN") &&
        typeof span.owner_structure_node_id === "string" &&
        members.includes(span.owner_structure_node_id) &&
        Number.isInteger(span.start_offset) &&
        Number(span.start_offset) >= 0 &&
        Number.isInteger(span.end_offset) &&
        Number(span.end_offset) > Number(span.start_offset) &&
        ["DOCX_RUN_BOUNDARY", "SOURCE_BLOCK_BOUNDARY"].includes(
          String(span.boundary_basis || ""),
        ) &&
        typeof sourceRef.source_id === "string" &&
        sourceRef.source_id.length > 0
      );
    }) &&
    (authorityBasis === "UNKNOWN"
      ? authorityLanguage === "UNKNOWN"
      : authorityLanguage !== "UNKNOWN" &&
        context.document_language_policy_id !== null)
  );
}

function normalizedEvidenceSourceRefs(row: JsonRecord): JsonRecord[] {
  const candidates: JsonRecord[] = [...(row.source_refs as JsonRecord[])];
  for (const evidence of row.deterministic_rule_evidence as JsonRecord[]) {
    if (isRecordArray(evidence.source_refs)) {
      candidates.push(...evidence.source_refs);
    }
  }
  if (candidates.length === 0) {
    candidates.push(
      ...(row.content_blocks as JsonRecord[]).filter((block) =>
        Boolean(
          block.source_id ||
            block.semantic_unit_id ||
            block.cell_id ||
            block.visual_observation_id,
        ),
      ),
    );
  }
  const result: JsonRecord[] = [];
  const seen = new Set<string>();
  for (const item of candidates) {
    const sourceId = String(
      item.source_id ||
        item.semantic_unit_id ||
        item.cell_id ||
        item.visual_observation_id ||
        "",
    ).trim();
    if (!sourceId) continue;
    const rawStart = Number(item.start_offset);
    const rawEnd = Number(item.end_offset);
    const bothOffsets =
      item.start_offset !== null &&
      item.start_offset !== undefined &&
      item.end_offset !== null &&
      item.end_offset !== undefined &&
      Number.isInteger(rawStart) &&
      rawStart >= 0 &&
      Number.isInteger(rawEnd) &&
      rawEnd >= 0;
    const rawPageStart = Number(
      item.page_start || item.page_number || row.page_start,
    );
    const rawPageEnd = Number(
      item.page_end || item.page_number || row.page_end || rawPageStart,
    );
    const hasPages = Number.isInteger(rawPageStart) && rawPageStart >= 1;
    const normalized: JsonRecord = {
      kind: String(item.kind || "SOURCE_REF").trim(),
      source_id: sourceId,
      owner_structure_node_id:
        String(item.owner_structure_node_id || row.structure_node_id || "") ||
        null,
      start_offset: bothOffsets ? rawStart : null,
      end_offset: bothOffsets ? rawEnd : null,
      page_start: hasPages ? rawPageStart : null,
      page_end:
        hasPages && Number.isInteger(rawPageEnd) && rawPageEnd >= rawPageStart
          ? rawPageEnd
          : hasPages
            ? rawPageStart
            : null,
    };
    const key = canonicalJson(normalized);
    if (!seen.has(key)) {
      seen.add(key);
      result.push(normalized);
    }
  }
  return result;
}

function validatePublishedTreeTopology(tree: RequirementTreeSnapshot): void {
  const treeRecord = record(tree);
  const sourceIdentity = record(tree.source_identity);
  const structureIdentity = record(tree.structure_identity);
  if (
    !exactKeys(treeRecord, REQUIREMENT_TREE_KEYS) ||
    !exactKeys(sourceIdentity, [
      "document_id",
      "geometry_index_id",
      "geometry_index_sha256",
      "preview_generation_id",
      "project_id",
      "requirement_set_id",
      "source_ir_id",
      "source_sha256",
    ]) ||
    !exactKeys(structureIdentity, [
      "base_snapshot_id",
      "effective_snapshot_id",
      "effective_tree_sha256",
      "revision",
    ]) ||
    !isStringArray(tree.root_node_ids) ||
    !hasUniqueStrings(tree.root_node_ids) ||
    !Array.isArray(tree.nodes) ||
    tree.nodes.length === 0 ||
    !Array.isArray(treeRecord.table_record_projections) ||
    !Array.isArray(treeRecord.table_evidence_bindings) ||
    treeRecord.requirement_evidence_bundle === null
  ) {
    throw new Error("确认清单版本的完整结构或证据包无法验证。");
  }
  const nodes = tree.nodes.map((item) => record(item));
  const nodeById = new Map<string, JsonRecord>();
  const entryIds = new Set<string>();
  for (const node of nodes) {
    const id = String(node.id || "");
    const requirementEntryId = node.requirement_entry_id;
    const expectedNodeKeys =
      node.bilingual_context === undefined
        ? REQUIREMENT_TREE_NODE_KEYS
        : [...REQUIREMENT_TREE_NODE_KEYS, "bilingual_context"];
    if (
      !exactKeys(node, expectedNodeKeys) ||
      !validPublishedBilingualContext(node) ||
      !id ||
      nodeById.has(id) ||
      !isOptionalString(node.parent_id) ||
      !isStringArray(node.child_ids) ||
      !hasUniqueStrings(node.child_ids) ||
      !Number.isInteger(node.depth) ||
      Number(node.depth) < 0 ||
      !Number.isInteger(node.source_order) ||
      Number(node.source_order) < 0 ||
      !Number.isInteger(node.source_offset) ||
      Number(node.source_offset) < 0 ||
      !isOptionalString(requirementEntryId) ||
      !isOptionalString(node.requirement_evidence_binding_id) ||
      !isRecordArray(node.source_refs) ||
      !isRecordArray(node.deterministic_rule_evidence) ||
      !isRecordArray(node.content_blocks) ||
      !isStringArray(node.editable_structure_node_ids) ||
      !isRecordArray(node.table_refs) ||
      !isRecordArray(node.figure_refs) ||
      !Array.isArray(node.toc_entries)
    ) {
      throw new Error("确认清单版本包含无法验证的结构行。");
    }
    if (
      typeof requirementEntryId === "string" &&
      (entryIds.has(requirementEntryId) || !requirementEntryId)
    ) {
      throw new Error("确认清单版本重复绑定了同一业务条目。");
    }
    if (typeof requirementEntryId === "string") entryIds.add(requirementEntryId);
    nodeById.set(id, node);
  }
  const rootSet = new Set(tree.root_node_ids);
  const parentless = nodes
    .filter((node) => node.parent_id === null)
    .map((node) => String(node.id));
  if (
    parentless.length !== rootSet.size ||
    parentless.some((id) => !rootSet.has(id))
  ) {
    throw new Error("确认清单版本的根节点与父子关系不一致。");
  }
  for (const node of nodes) {
    const id = String(node.id);
    const parentId = node.parent_id;
    if (typeof parentId === "string") {
      const parent = nodeById.get(parentId);
      if (!parent || !(parent.child_ids as string[]).includes(id)) {
        throw new Error("确认清单版本的父项没有包含对应子项。");
      }
    }
    for (const childId of node.child_ids as string[]) {
      if (nodeById.get(childId)?.parent_id !== id) {
        throw new Error("确认清单版本的子项与父项关系不一致。");
      }
    }
  }
  const visited = new Set<string>();
  const preorder: string[] = [];
  const pending = [...tree.root_node_ids].reverse().map((id) => ({ id, depth: 0 }));
  while (pending.length) {
    const next = pending.pop();
    if (!next || visited.has(next.id)) {
      throw new Error("确认清单版本的结构存在循环。");
    }
    const node = nodeById.get(next.id);
    if (!node || node.depth !== next.depth) {
      throw new Error("确认清单版本的层级或节点身份不一致。");
    }
    visited.add(next.id);
    preorder.push(next.id);
    for (const childId of [...(node.child_ids as string[])].reverse()) {
      pending.push({ id: childId, depth: next.depth + 1 });
    }
  }
  if (
    visited.size !== nodes.length ||
    canonicalJson(preorder) !== canonicalJson(nodes.map((node) => node.id))
  ) {
    throw new Error("确认清单版本包含断开的节点或顺序漂移。");
  }
}

export function assertRequirementSemanticPublicationPreviewV2(
  response: RequirementSemanticPublicationPreviewResponseV2,
  expected: {
    projectId: string;
    documentId: string;
    sessionId: string;
    reviewRevision: number;
    baseActiveRequirementSetId: string;
    classification: EntryClassificationResponse;
    scopeState: RequirementAtomizationScopeState;
    request: RequirementSemanticPublicationPreviewRequestV2;
  },
): VerifiedRequirementSemanticPublicationPreviewV2 {
  const responseRecord = record(response);
  if (
    !exactKeys(responseRecord, [
      "analysis_started",
      "atomization_invoked",
      "binding_preview",
      "classification",
      "counts",
      "document_id",
      "project_id",
      "schema_version",
      "serving_impact",
    ]) ||
    response.schema_version !==
      "requirement-enrichment-publication-preview-response/v2" ||
    response.project_id !== expected.projectId ||
    response.document_id !== expected.documentId ||
    response.serving_impact !== "NONE"
  ) {
    throw new Error("发布预览与当前项目或文档身份不一致。");
  }
  if (response.analysis_started !== false) {
    throw new Error("发布预览意外启动了需求分析，系统已停止发布。");
  }
  if (response.atomization_invoked !== false) {
    throw new Error("发布预览意外调用原子化，系统已停止发布。");
  }
  if (!classificationMatches(response.classification, expected.classification)) {
    throw new Error("发布预览并非基于刚接受的分类结果。");
  }
  const preview = response.binding_preview;
  const projection = preview?.projection;
  const previewRecord = record(preview);
  const projectionRecord = record(projection);
  const appliedReviewNodeIds = projection?.applied_review_node_ids;
  const sourceBusinessTreeNodeIds =
    projection?.source_business_tree_node_ids;
  if (
    !exactKeys(previewRecord, [
      "base_active_requirement_set_id",
      "binding_preview_id",
      "document_id",
      "payload_sha256",
      "project_id",
      "projection",
      "review_revision",
      "schema_version",
      "session_id",
    ]) ||
    preview?.schema_version !==
      "requirement-enrichment-publication-binding-preview/v2" ||
    !BINDING_PREVIEW_ID_PATTERN.test(preview.binding_preview_id) ||
    !SHA256_PATTERN.test(preview.payload_sha256) ||
    preview.project_id !== expected.projectId ||
    preview.document_id !== expected.documentId ||
    preview.session_id !== expected.sessionId ||
    preview.review_revision !== expected.reviewRevision ||
    preview.base_active_requirement_set_id !==
      expected.baseActiveRequirementSetId ||
    !exactKeys(projectionRecord, PUBLICATION_PROJECTION_KEYS) ||
    projection?.schema_version !==
      "requirement-enrichment-publication-projection/v2" ||
    projection.project_id !== expected.projectId ||
    projection.document_id !== expected.documentId ||
    projection.source_sha256 !==
      expected.classification.snapshot.source_sha256 ||
    projection.effective_structure_id !==
      expected.classification.snapshot.effective_structure_id ||
    projection.accepted_working_tree_id !==
      expected.classification.snapshot.working_tree_id ||
    projection.accepted_working_tree_sha256 !==
      expected.classification.snapshot.working_tree_sha256 ||
    !SHA256_PATTERN.test(projection.projection_sha256) ||
    projection.entry_classification_snapshot_id !==
      expected.classification.snapshot.snapshot_id ||
    projection.entry_classification_revision !==
      expected.classification.snapshot.revision ||
    projection.entry_classification_sha256 !==
      expected.classification.snapshot.payload_sha256 ||
    projection.entry_classification_content_sha256 !==
      expected.scopeState.classification_content_sha256 ||
    !SHA256_PATTERN.test(projection.entry_classification_content_sha256) ||
    !isStringArray(appliedReviewNodeIds) ||
    !hasUniqueStrings(appliedReviewNodeIds) ||
    !isStringArray(sourceBusinessTreeNodeIds) ||
    sourceBusinessTreeNodeIds.length === 0 ||
    !hasUniqueStrings(sourceBusinessTreeNodeIds) ||
    canonicalJson(sourceBusinessTreeNodeIds) !==
      canonicalJson(
        expected.classification.snapshot.entries.map(
          (entry) => entry.tree_node_id,
        ),
      )
  ) {
    throw new Error("发布预览或当前分类结果无法验证。");
  }
  const receipt = record(expected.scopeState.current_receipt);
  if (
    projection.atomization_state !== expected.request.atomization_state ||
    canonicalJson(appliedReviewNodeIds) !==
      canonicalJson(
        expected.request.atomization_state === "APPLIED"
          ? expected.scopeState.current_receipt
              ?.cumulative_review_node_ids || []
          : [],
      ) ||
    (expected.request.atomization_state === "NOT_RUN"
      ? projection.atomization_scope_receipt_id !== "" ||
        projection.atomization_scope_receipt_sha256 !== "" ||
        projection.atomization_scope_revision !== 0 ||
        projectionRecord.atomization_active_revision !== 0 ||
        projectionRecord.atomization_classification_content_sha256 !== "" ||
        projectionRecord.atomization_run_id !== "" ||
        projectionRecord.atomization_revision !== 0 ||
        projectionRecord.atomization_run_sha256 !== "" ||
        projectionRecord.publication_preview_id !== "" ||
        projectionRecord.publication_preview_sha256 !== "" ||
        projectionRecord.publication_key !== "" ||
        projectionRecord.publication_lineage_sha256 !== "" ||
        appliedReviewNodeIds.length !== 0
      : projection.atomization_scope_receipt_id !==
          expected.request.atomization_scope_receipt_id ||
        projection.atomization_scope_receipt_sha256 !==
          expected.request.atomization_scope_receipt_sha256 ||
        projection.atomization_scope_revision !==
          expected.request.atomization_scope_revision ||
        projectionRecord.atomization_active_revision !==
          receipt.active_revision ||
        projectionRecord.atomization_classification_content_sha256 !==
          receipt.entry_classification_content_sha256 ||
        projectionRecord.atomization_classification_content_sha256 !==
          expected.scopeState.classification_content_sha256 ||
        projectionRecord.atomization_run_id !== receipt.atomization_run_id ||
        projectionRecord.atomization_run_id !==
          expected.scopeState.current_atomization_run_id ||
        projectionRecord.atomization_revision !==
          receipt.atomization_run_revision ||
        projectionRecord.atomization_revision !==
          expected.scopeState.current_atomization_run_revision ||
        !Number.isInteger(projectionRecord.atomization_active_revision) ||
        Number(projectionRecord.atomization_active_revision) < 1 ||
        !ATOMIZATION_RUN_ID_PATTERN.test(
          String(projectionRecord.atomization_run_id || ""),
        ) ||
        !SHA256_PATTERN.test(
          String(projectionRecord.atomization_run_sha256 || ""),
        ) ||
        !PUBLICATION_PREVIEW_ID_PATTERN.test(
          String(projectionRecord.publication_preview_id || ""),
        ) ||
        !SHA256_PATTERN.test(
          String(projectionRecord.publication_preview_sha256 || ""),
        ) ||
        !SHA256_PATTERN.test(String(projectionRecord.publication_key || "")) ||
        !SHA256_PATTERN.test(
          String(projectionRecord.publication_lineage_sha256 || ""),
        ) ||
        appliedReviewNodeIds.length === 0)
  ) {
    throw new Error("发布预览与请求中的原子化结果不一致。");
  }
  const counts = validatedProjectionCounts(
    projection.rows,
    expected.classification,
    expected.request.atomization_state,
    sourceBusinessTreeNodeIds,
  );
  if (!sameCounts(response.counts, counts)) {
    throw new Error("发布预览计数与实际投影行不一致。");
  }
  if (
    projection.projection_sha256 !==
    canonicalJsonSha256(
      withoutCanonicalField(projectionRecord, "projection_sha256"),
    )
  ) {
    throw new Error("发布预览的完整内容校验失败。");
  }
  if (
    preview.payload_sha256 !==
    canonicalJsonSha256(withoutCanonicalField(previewRecord, "payload_sha256"))
  ) {
    throw new Error("发布预览的生成结果校验失败。");
  }
  return {
    response,
    bindingPreviewId: preview.binding_preview_id,
    bindingPreviewSha256: preview.payload_sha256,
    counts,
  };
}

export function assertRequirementSemanticPublishDraftV2(
  response: RequirementSemanticPublishDraftResponseV2,
  expected: {
    projectId: string;
    documentId: string;
    sessionId: string;
    reviewRevision: number;
    classification: EntryClassificationResponse;
    preview: VerifiedRequirementSemanticPublicationPreviewV2;
    workingTree: RequirementTreeSnapshot;
  },
): VerifiedRequirementSemanticPublishDraftV2 {
  const responseRecord = record(response);
  if (
    !exactKeys(responseRecord, [
      "analysis_started",
      "binding",
      "classification",
      "counts",
      "document_id",
      "project_id",
      "publication",
      "requirement_set_activated",
      "requirement_set_created",
      "requirement_tree",
      "schema_version",
      "serving_impact",
    ]) ||
    response.schema_version !==
      "requirement-enrichment-publish-draft-response/v2" ||
    response.project_id !== expected.projectId ||
    response.document_id !== expected.documentId ||
    response.serving_impact !== "DRAFT_ONLY" ||
    response.analysis_started !== false ||
    typeof response.requirement_set_created !== "boolean" ||
    response.requirement_set_activated !== false
  ) {
    throw new Error("发布结果不是当前文档中尚未启用的确认清单版本。");
  }
  if (!classificationMatches(response.classification, expected.classification)) {
    throw new Error("发布结果并非基于刚接受的分类结果。");
  }
  if (!sameCounts(response.counts, expected.preview.counts)) {
    throw new Error("发布结果计数与已验证预览不一致。");
  }
  const binding = response.binding;
  const bindingRecord = record(binding);
  if (
    !exactKeys(bindingRecord, [
      "binding_id",
      "preview_id",
      "preview_sha256",
      "sha256",
      "state",
    ]) ||
    binding?.state !== "READY" ||
    !BINDING_ID_PATTERN.test(binding.binding_id) ||
    !SHA256_PATTERN.test(binding.sha256) ||
    binding.preview_id !== expected.preview.bindingPreviewId ||
    binding.preview_sha256 !== expected.preview.bindingPreviewSha256
  ) {
    throw new Error("发布结果与已验证的预览不一致。");
  }
  const publication = record(response.publication);
  const requirementSetId = String(publication.requirement_set_id || "");
  const rowBindings = Array.isArray(publication.row_bindings)
    ? publication.row_bindings
    : [];
  const previewBinding = expected.preview.response.binding_preview;
  const previewProjection = previewBinding.projection;
  const tree = response.requirement_tree;
  const treeRecord = record(tree);
  if (
    !exactKeys(publication, [
      "base_active_requirement_set_id",
      "binding_id",
      "binding_preview_id",
      "binding_preview_sha256",
      "document_id",
      "payload_sha256",
      "project_id",
      "projection_sha256",
      "requirement_set_id",
      "requirement_set_version",
      "requirement_tree_object_key",
      "requirement_tree_schema_version",
      "requirement_tree_sha256",
      "review_revision",
      "row_bindings",
      "schema_version",
      "session_id",
    ]) ||
    publication.schema_version !==
      "requirement-enrichment-publication-binding/v2" ||
    publication.binding_id !== binding.binding_id ||
    publication.payload_sha256 !== binding.sha256 ||
    publication.binding_preview_id !== expected.preview.bindingPreviewId ||
    publication.binding_preview_sha256 !==
      expected.preview.bindingPreviewSha256 ||
    publication.project_id !== expected.projectId ||
    publication.document_id !== expected.documentId ||
    publication.session_id !== expected.sessionId ||
    publication.review_revision !== expected.reviewRevision ||
    publication.base_active_requirement_set_id !==
      previewBinding.base_active_requirement_set_id ||
    publication.projection_sha256 !== previewProjection.projection_sha256 ||
    !requirementSetId ||
    !Number.isInteger(publication.requirement_set_version) ||
    Number(publication.requirement_set_version) < 1 ||
    !String(publication.requirement_tree_object_key || "") ||
    !SHA256_PATTERN.test(String(publication.requirement_tree_sha256 || "")) ||
    publication.requirement_tree_schema_version !== tree?.schema_version ||
    rowBindings.length !== expected.preview.counts.total
  ) {
    throw new Error("确认清单版本或其完整结构无法验证。");
  }
  const workingTree = expected.workingTree;
  if (
    tree?.schema_version !== "requirement-tree-snapshot/v1" ||
    tree.mode !== "REQUIREMENT_SET" ||
    tree.state !== "READY" ||
    tree.source_identity?.project_id !== expected.projectId ||
    tree.source_identity?.document_id !== expected.documentId ||
    tree.source_identity?.source_ir_id !==
      workingTree.source_identity.source_ir_id ||
    tree.source_identity?.source_sha256 !==
      expected.classification.snapshot.source_sha256 ||
    tree.source_identity?.requirement_set_id !== requirementSetId ||
    tree.structure_identity?.effective_snapshot_id !==
      expected.classification.snapshot.effective_structure_id ||
    canonicalJson(tree.structure_identity) !==
      canonicalJson(workingTree.structure_identity) ||
    canonicalJson(tree.root_node_ids) !==
      canonicalJson(workingTree.root_node_ids) ||
    !Array.isArray(tree.nodes)
  ) {
    throw new Error("发布结构与当前确认清单版本或分类结果不一致。");
  }
  validatePublishedTreeTopology(tree);
  if (
    publication.requirement_tree_sha256 !== canonicalJsonSha256(treeRecord)
  ) {
    throw new Error("确认清单版本的结构校验值与完整内容不一致。");
  }
  const treeNodeById = new Map(tree.nodes.map((node) => [node.id, node]));
  const boundRequirementEntryIds = new Set<string>();
  for (let index = 0; index < rowBindings.length; index += 1) {
    const bindingRow = record(rowBindings[index]);
    const previewRow = previewProjection.rows[index];
    const treeNode = treeNodeById.get(
      String(bindingRow.requirement_tree_node_id || ""),
    );
    const requirementEntryId = String(
      bindingRow.requirement_entry_id || "",
    );
    const projectionRowSha256 = canonicalJsonSha256(previewRow);
    const decisionTraceSha256 = canonicalJsonSha256({
      schema_version: "requirement-enrichment-decision-trace/v2",
      binding_id: binding.binding_id,
      publication_order: index,
      requirement_entry_id: requirementEntryId,
      projection_row_sha256: projectionRowSha256,
    });
    if (
      !exactKeys(bindingRow, [
        "binding_id",
        "decision_trace_sha256",
        "payload_sha256",
        "projection_row",
        "projection_row_sha256",
        "publication_order",
        "requirement_entry_id",
        "requirement_tree_node_id",
        "schema_version",
      ]) ||
      bindingRow.schema_version !==
        "requirement-enrichment-publication-row-binding/v2" ||
      bindingRow.binding_id !== binding.binding_id ||
      bindingRow.publication_order !== index ||
      !requirementEntryId ||
      boundRequirementEntryIds.has(requirementEntryId) ||
      !SHA256_PATTERN.test(String(bindingRow.projection_row_sha256 || "")) ||
      !SHA256_PATTERN.test(String(bindingRow.decision_trace_sha256 || "")) ||
      !SHA256_PATTERN.test(String(bindingRow.payload_sha256 || "")) ||
      bindingRow.projection_row_sha256 !== projectionRowSha256 ||
      bindingRow.decision_trace_sha256 !== decisionTraceSha256 ||
      bindingRow.payload_sha256 !==
        canonicalJsonSha256(
          withoutCanonicalField(bindingRow, "payload_sha256"),
        ) ||
      canonicalJson(bindingRow.projection_row) !== canonicalJson(previewRow) ||
      bindingRow.requirement_tree_node_id !==
        previewRow.requirement_tree_node_id ||
      !treeNode ||
      treeNode.requirement_entry_id !== requirementEntryId
    ) {
      throw new Error(
        "发布行与已验证预览或结构条目不一致。",
      );
    }
    boundRequirementEntryIds.add(requirementEntryId);
  }
  const treeEntryIds = new Set(
    tree.nodes.flatMap((node) =>
      typeof node.requirement_entry_id === "string"
        ? [node.requirement_entry_id]
        : [],
    ),
  );
  if (
    treeEntryIds.size !== boundRequirementEntryIds.size ||
    [...treeEntryIds].some((id) => !boundRequirementEntryIds.has(id))
  ) {
    throw new Error("确认清单版本的数据库条目与完整结构没有精确对应。");
  }
  const evidenceBundle = record(treeRecord.requirement_evidence_bundle);
  const requirementEvidence = Array.isArray(evidenceBundle.requirement_bindings)
    ? evidenceBundle.requirement_bindings.map((item) => record(item))
    : [];
  const tableEvidence = Array.isArray(evidenceBundle.table_bindings)
    ? evidenceBundle.table_bindings
    : null;
  if (
    !exactKeys(evidenceBundle, [
      "evidence_sha256",
      "requirement_bindings",
      "schema_version",
      "source_ir_id",
      "source_sha256",
      "structure_snapshot_id",
      "table_bindings",
    ]) ||
    evidenceBundle.schema_version !== "requirement-evidence-bundle/v1" ||
    evidenceBundle.source_ir_id !== tree.source_identity.source_ir_id ||
    evidenceBundle.source_sha256 !== tree.source_identity.source_sha256 ||
    evidenceBundle.structure_snapshot_id !==
      tree.structure_identity.effective_snapshot_id ||
    !tableEvidence ||
    canonicalJson(tableEvidence) !==
      canonicalJson(treeRecord.table_evidence_bindings) ||
    requirementEvidence.length !== rowBindings.length
  ) {
    throw new Error("确认清单版本的证据包与当前结构来源不一致。");
  }
  const evidenceByEntryId = new Map<string, JsonRecord>();
  for (const item of requirementEvidence) {
    const entryId = String(item.requirement_entry_id || "");
    const evidenceSha256 = canonicalJsonSha256({
      atomic_requirement_id: item.atomic_requirement_id,
      requirement_entry_id: entryId,
      source_refs: item.source_refs,
      source_spans: item.source_spans,
      table_evidence_binding_id: item.table_evidence_binding_id,
    });
    if (
      !exactKeys(item, [
        "atomic_requirement_id",
        "binding_id",
        "evidence_sha256",
        "requirement_entry_id",
        "schema_version",
        "source_refs",
        "source_spans",
        "table_evidence_binding_id",
      ]) ||
      item.schema_version !== "requirement-evidence-binding/v1" ||
      !entryId ||
      evidenceByEntryId.has(entryId) ||
      item.evidence_sha256 !== evidenceSha256 ||
      item.binding_id !==
        `requirement-evidence-binding-${evidenceSha256.slice(0, 32)}`
    ) {
      throw new Error("确认清单版本包含无法验证的业务证据。");
    }
    evidenceByEntryId.set(entryId, item);
  }
  for (let index = 0; index < rowBindings.length; index += 1) {
    const bindingRow = record(rowBindings[index]);
    const previewRow = record(previewProjection.rows[index]);
    const entryId = String(bindingRow.requirement_entry_id || "");
    const evidence = evidenceByEntryId.get(entryId);
    const treeNode = treeNodeById.get(
      String(bindingRow.requirement_tree_node_id || ""),
    );
    const expectedKind =
      previewRow.entry_kind === "HEADING"
        ? "REQUIREMENT_CONTAINER"
        : previewRow.final_projection_role === "REQUIREMENT"
          ? "REQUIREMENT"
          : previewRow.final_projection_role === "DIRECT_CONTENT"
            ? "DIRECT_CONTENT"
            : treeNode?.kind;
    if (
      !evidence ||
      !treeNode ||
      treeNode.requirement_evidence_binding_id !== evidence.binding_id ||
      evidence.atomic_requirement_id !== previewRow.atomic_requirement_id ||
      canonicalJson(evidence.source_spans) !==
        canonicalJson(previewRow.source_spans) ||
      canonicalJson(evidence.source_refs) !==
        canonicalJson(normalizedEvidenceSourceRefs(previewRow)) ||
      evidence.table_evidence_binding_id !==
        previewRow.table_evidence_binding_id ||
      treeNode.kind !== expectedKind ||
      treeNode.workflow_node_id !== previewRow.workflow_node_id ||
      treeNode.classification !== previewRow.classification ||
      treeNode.projection_role !== previewRow.final_projection_role ||
      treeNode.source_order !== previewRow.source_order ||
      treeNode.source_offset !== previewRow.source_offset ||
      treeNode.label !== previewRow.label ||
      treeNode.summary !== previewRow.published_statement ||
      treeNode.page_start !== previewRow.page_start ||
      treeNode.page_end !== previewRow.page_end ||
      treeNode.status !== previewRow.status ||
      treeNode.table_evidence_binding_id !==
        previewRow.table_evidence_binding_id ||
      treeNode.include_in_view !== previewRow.include_in_view ||
      canonicalJson(treeNode.source_refs) !==
        canonicalJson(previewRow.source_refs) ||
      canonicalJson(treeNode.deterministic_rule_evidence) !==
        canonicalJson(previewRow.deterministic_rule_evidence) ||
      canonicalJson(treeNode.content_blocks) !==
        canonicalJson(previewRow.content_blocks) ||
      canonicalJson(treeNode.table_refs) !== canonicalJson(previewRow.table_refs) ||
      canonicalJson(treeNode.figure_refs) !== canonicalJson(previewRow.figure_refs) ||
      (previewRow.intrinsic_analysis_eligible === true &&
        treeNode.child_ids.length > 0)
    ) {
      throw new Error("确认清单版本的结构行、正文或证据与预览不一致。");
    }
  }
  const bundleSha256 = canonicalJsonSha256({
    requirement_bindings: requirementEvidence,
    source_ir_id: evidenceBundle.source_ir_id,
    source_sha256: evidenceBundle.source_sha256,
    structure_snapshot_id: evidenceBundle.structure_snapshot_id,
    table_bindings: tableEvidence,
  });
  if (evidenceBundle.evidence_sha256 !== bundleSha256) {
    throw new Error("确认清单版本的证据包校验值与完整内容不一致。");
  }
  if (
    publication.payload_sha256 !==
      canonicalJsonSha256(withoutCanonicalField(publication, "payload_sha256"))
  ) {
    throw new Error("确认清单版本的完整内容校验失败。");
  }
  return {
    response,
    requirementSetId,
    bindingId: binding.binding_id,
  };
}
