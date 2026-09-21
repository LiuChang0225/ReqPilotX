import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useDeferredValue,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from "react";
import { ApiError, api } from "../../shared/api/client";
import type {
  NativeStructureDocumentTreeV1,
  NativeStructureActivityEventV2,
  NativeStructureFeedbackV1,
  NativeStructureContinuationV1,
  NativeStructureHandoffAttemptPublicV1,
  NativeStructureLogicalTableV1,
  NativeStructureProposalV1,
  NativeStructureReferencePublicV1,
  NativeStructureSourceAnchorV1,
  NativeStructureSourceContentV1,
  NativeStructureTablePreviewV1,
  NativeStructureTreeNodeV1,
  NativeStructureUnresolvedV1,
  NativeStructureWorkingRevisionHeadV1,
  NativeStructureWorkingRevisionV1,
  SourceItemLedgerRowV1,
  SourceRequirementBaselineOutlineV1,
  SourceRequirementBaselineRunStatusV1,
  SourceRequirementOutlineRecordV1,
  SourceScopeSummaryV1,
} from "../../shared/api/contracts";
import { cx } from "../../shared/lib/format";
import { Icon } from "../../shared/ui/Icon";
import { StructuredTable } from "../restoration/StructuredTable";
import { tableDisplayTitle, tableDisplayAuthority } from "../restoration/tableDisplay";
import type {
  StructuredTableModel,
  TableCellImage,
} from "../restoration/restorationModel";
import {
  SourceItemLens,
  type SourceRequirementEvidenceOverlay,
} from "./SourceRequirementNodeLens";
import { NativeStructurePageAtlas } from "./NativeStructurePageAtlas";
import { NativeOfficeSourceView, NativeOfficePreviewTable, officeLocation } from "./NativeOfficeSourceView";
import { canonicalJson, canonicalJsonSha256 } from "./canonicalJsonSha256";
import { SourceItemDeliveryActions, useSourceItemReasoning, type SourceItemDeliveryState } from "./SourceItemReasoning";
import { SourceItemAgentLoop } from "./SourceItemAgentLoop";
import { analysisUnits, inAnalysisQueue, matchesAnalysisSearch, ANALYSIS_QUEUES, type AnalysisQueue } from "./analysisQueue";
import { SourceFindingsPanel } from "./SourceFindingsPanel";
import { originalNumber, scopeCountLabel, scopeCountDetail } from "./sourcePresentation";
import { SourceLedgerTable, derivedTableRowId } from "./SourceLedgerTable";
import type { SourcePreviewReader } from "./SourceRecordPreview";
import { useDocumentAgent } from "../workspace/DocumentAgentContext";
import { useWorkbenchHost } from "../workspace/WorkbenchHostContext";
import { ProjectKnowledgePanel } from "../knowledge/ProjectKnowledgePanel";
import { createPortal } from "react-dom";
import { SourceReviewToolbar } from "./SourceReviewToolbar";
import { filterSourceItemLedger, sourceReviewCounts, useSourceItemReviewContext, FOCUS_LABELS,
  type ReviewFilter, type SourceItemReviewContext } from "./sourceItemReviewContext";

import type { SourceWorkbenchReader } from "./SourceWorkbenchReader";
import { clearSourceFeedbackDraft, sourceFeedbackDraftKey, useSourceFeedbackDraft } from "./sourceItemDrafts";

interface NativeStructureReferenceDockProps {
  sourceReader?: SourceWorkbenchReader;
  projectId: string;
  documentId: string;
  sourceSha256: string;
  onPageChange: (page: number) => void;
  onAvailabilityChange?: (available: boolean) => void;
  onPageCountChange?: (pageCount: number) => void;
  sourceFileUrl?: string;
  activePage?: number;
  pageCount?: number;
  showSourcePreview?: boolean;
  onAdvancementChange?: (state: NativeStructureAdvancementState) => void;
  mode?: "STRUCTURE" | "SOURCE_REQUIREMENTS";
  refreshToken?: number;
  baselineRefreshSha?: string;
  purpose?: "review" | "analysis";
  onChangePurpose?: (purpose: "review" | "analysis") => void;
  initialReasoningState?: SourceItemDeliveryState;
}

export interface NativeStructureAdvancementState {
  available: boolean;
  advancementAllowed: boolean;
  sourceSha256: string;
  artifactId: string;
  continuationStatus: string;
}

interface StructureTreeBranchProps {
  node: NativeStructureTreeNodeV1;
  depth: number;
  childrenByParent: ReadonlyMap<string, NativeStructureTreeNodeV1[]>;
  expandedIds: ReadonlySet<string>;
  selectedId: string;
  onSelect: (node: NativeStructureTreeNodeV1) => void;
  onToggle: (nodeId: string) => void;
  recordCountsByNode?: ReadonlyMap<
    string,
    { recordCount: number; requirementCount: number; nonRequirementCount: number }
  >;
  showStructureFindings?: boolean;
  workingAffectedScopeIds?: ReadonlySet<string>;
}

interface SourceItemLedgerBranchProps {
  row: SourceItemLedgerRowV1;
  depth: number;
  rowsById: ReadonlyMap<string, SourceItemLedgerRowV1>;
  nodesById: ReadonlyMap<string, NativeStructureTreeNodeV1>;
  sourceItemsById: ReadonlyMap<string, SourceRequirementOutlineRecordV1>;
  expandedIds: ReadonlySet<string>;
  selectedRowId: string;
  onSelectStructure: (node: NativeStructureTreeNodeV1) => void;
  onSelectSourceItem: (
    sourceItem: SourceRequirementOutlineRecordV1,
    owner: NativeStructureTreeNodeV1,
  ) => void;
  onToggle: (rowId: string) => void;
  visibleRows?: ReadonlySet<string>;
  reviewContext?: SourceItemReviewContext | null;
  reviewCounts?: ReadonlyMap<string, { focus: number; attention: number; changed: number }>;
  derivedCounts?: ReadonlyMap<string, number>;
  scopeSummaries?: Record<string, SourceScopeSummaryV1>;
}

type StructureColumnShares = {
  tree: number;
  detail: number;
  source: number;
};

type StructureSplitter = "TREE_DETAIL" | "DETAIL_SOURCE";

const DEFAULT_COLUMN_SHARES: StructureColumnShares = {
  tree: 24,
  detail: 38,
  source: 38,
};

const COLUMN_LAYOUT_VERSION = 1;

function columnLayoutStorageKey(projectId: string, documentId: string): string {
  return `reqpilotx:native-structure-columns:${projectId}:${documentId}`;
}

function readColumnShares(
  projectId: string,
  documentId: string,
): StructureColumnShares {
  if (typeof window === "undefined") return { ...DEFAULT_COLUMN_SHARES };
  try {
    const raw = window.localStorage.getItem(
      columnLayoutStorageKey(projectId, documentId),
    );
    if (!raw) return { ...DEFAULT_COLUMN_SHARES };
    const value = JSON.parse(raw) as Partial<StructureColumnShares> & {
      version?: number;
    };
    const tree = Number(value.tree);
    const detail = Number(value.detail);
    const source = Number(value.source);
    const total = tree + detail + source;
    if (
      value.version !== COLUMN_LAYOUT_VERSION ||
      ![tree, detail, source].every(Number.isFinite) ||
      tree < 18 ||
      detail < 22 ||
      source < 22 ||
      Math.abs(total - 100) > 0.5
    ) {
      return { ...DEFAULT_COLUMN_SHARES };
    }
    return { tree, detail, source };
  } catch {
    return { ...DEFAULT_COLUMN_SHARES };
  }
}

function writeColumnShares(
  projectId: string,
  documentId: string,
  shares: StructureColumnShares,
): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      columnLayoutStorageKey(projectId, documentId),
      JSON.stringify({ version: COLUMN_LAYOUT_VERSION, ...shares }),
    );
  } catch {
    // Layout persistence is optional; resizing remains available in memory.
  }
}

const UNRESOLVED_COPY: Record<string, string> = {
  p9_attachment_reference_parent_scope:
    "P9 的附件引用紧随 5.8，但原文没有明确说明它属于 5.8 还是整个第 5 章。",
  vehicle_info_missing_discrete_cell_lineage:
    "P2 的车型表与多个标题、正文被原解析合并，需要保留视觉表格作为独立内容。",
  nested_tolerance_trailing_text_role:
    "阻尼公差小表后的“气反力公差”可能是父单元格文字或表尾注，当前不把它当成完整数据行。",
  connector_package_parent_scope:
    "P23、P24、P25–P29 内容相关，但原文没有给出一个共同的总标题。",
  p24_overflow_cell_text:
    "P24 个别窄列文字越过格线，表格位置明确，少数单元格内容仍需局部视觉恢复。",
  visual_asset_parent_binding_missing:
    "本范围内有图片位置可定位，但部分图片尚未绑定到唯一的原文标题。",
  p22_category_label_unclear:
    "P22 原文显示“一、零、目视类试验”，含义不明确，当前保持原样。",
};

function commandId(prefix = "native-structure-feedback"): string {
  const suffix =
    typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}:${suffix}`;
}

function pageLabel(pages: readonly number[]): string {
  if (!pages.length) return "未绑定页面";
  const ranges: string[] = [];
  let start = pages[0];
  let previous = pages[0];
  for (const page of pages.slice(1)) {
    if (page === previous + 1) {
      previous = page;
      continue;
    }
    ranges.push(start === previous ? `P${start}` : `P${start}–P${previous}`);
    start = page;
    previous = page;
  }
  ranges.push(start === previous ? `P${start}` : `P${start}–P${previous}`);
  return ranges.join("、");
}

function nodeTitle(node: NativeStructureTreeNodeV1): string {
  return [...new Set([node.label, node.title].filter(Boolean))].join(" ") || "未命名结构节点";
}

function nodeKindLabel(node: NativeStructureTreeNodeV1): string {
  if (node.kind === "DOCUMENT_ROOT") return "主文档";
  if (node.kind === "DOCUMENT_UNIT") return "补充文档";
  if (node.kind === "FRONT_MATTER") return "文档前置内容";
  if (node.kind === "SECTION" || node.kind === "CHAPTER") return "章节";
  return "原文条目";
}

function titleAuthorityLabel(node: NativeStructureTreeNodeV1): string {
  if (node.title_authority === "AGENT_SURVEY_LABEL") return "Agent 全文概览命名";
  if (node.title_authority === "DERIVED_FROM_VISIBLE_TABLE_ROLE") {
    return "Agent 根据表格用途命名";
  }
  if (node.title_authority === "VISIBLE_LABEL_ONLY") return "原文仅有编号";
  return "原文可见";
}

function feedbackStatusLabel(feedback: NativeStructureFeedbackV1): string {
  if (feedback.status === "ACKNOWLEDGED") return "Agent 已读取";
  if (feedback.status === "CONSUMED") return "已用于新草案";
  if (feedback.status === "REJECTED") return "未用于当前草案";
  return "已保存，等待 Agent 读取";
}

function structureToolIntentLabel(value: string): string {
  const labels: Record<string, string> = {
    READ_NATIVE_TEXT: "读取原生文字",
    RENDER_SCOPE: "渲染局部原文",
    INSPECT_LOGICAL_TABLE: "检查逻辑表",
    RESTORE_TABLE_CANDIDATE: "生成表格还原候选",
    VALIDATE_CONTENT_OWNERSHIP: "检查内容归属",
    VALIDATE_MATERIALIZATION: "检查物化结果",
    VALIDATE_STRUCTURE_GRANULARITY: "检查标题树粒度",
  };
  return labels[value] || "局部证据工具";
}

function activityKindLabel(value: unknown, index: number): string {
  const labels: Record<string, string> = {
    GOAL_SET: "设定目标",
    SOURCE_LOCKED: "核对来源",
    VISUAL_OPENED: "建立全局概览",
    SCOPE_REVIEWED: "核对页面范围",
    CANDIDATE_ASSEMBLED: "组装结构草案",
    VALIDATION_PASSED: "验证来源覆盖",
    FRESH_REPLAY_COMPARED: "复跑稳定性对照",
    COMPLETED_WITH_UNRESOLVED: "保留局部未决",
  };
  return labels[String(value || "")] || `执行步骤 ${index + 1}`;
}

function activityPublicSummary(activity: Record<string, unknown>): string {
  const kind = String(activity.kind || "");
  const scope = String(activity.scope || "");
  if (kind === "GOAL_SET") {
    return "明确本轮只恢复原文结构，不进入需求判断或原子化。";
  }
  if (kind === "SOURCE_LOCKED") {
    return "已核对 29 页来源文件及可用表格证据。";
  }
  if (kind === "VISUAL_OPENED") {
    return "已建立整文页面概览，并继续逐页查看高清原文。";
  }
  if (kind === "SCOPE_REVIEWED") {
    if (scope.includes("P1-P9")) {
      return "已核对主文档 P1–P9 的章节、正文与表格边界。";
    }
    if (scope.includes("P10-P21")) {
      return "已核对前后减振器试验大纲及跨页表格。";
    }
    if (scope.includes("P22-P29")) {
      return "已核对蓄能器与连接器相关附件。";
    }
    return `已核对 ${scope || "当前范围"} 的原文结构。`;
  }
  if (kind === "CANDIDATE_ASSEMBLED") {
    return "已组装整文结构草案；对象数量只作观察。";
  }
  if (kind === "VALIDATION_PASSED") {
    return "已验证页面、表格与来源覆盖完整。";
  }
  if (kind === "FRESH_REPLAY_COMPARED") {
    return "已完成隔离复跑，关键结构保持稳定。";
  }
  if (kind === "COMPLETED_WITH_UNRESOLVED") {
    return "已保留原文无法确定的局部关系，不阻止其他内容继续。";
  }
  return String(activity.summary || "已完成一步结构检查");
}

export function activeNativeStructureFocusPages(
  continuationStatus: string,
  activity: readonly NativeStructureActivityEventV2[],
): number[] {
  if (
    ![
      "QUEUED_FOR_CODEX_APP",
      "ACKNOWLEDGED",
      "IN_PROGRESS",
      "VERIFYING",
      "REVISION_REQUIRED",
    ].includes(continuationStatus)
  ) {
    return [];
  }
  return (
    [...activity]
      .reverse()
      .find(
        (item) =>
          [
            "ACKNOWLEDGED",
            "IN_PROGRESS",
            "VERIFYING",
            "REVISION_REQUIRED",
          ].includes(item.payload.status) &&
          Boolean(item.scope?.page_numbers.length),
      )?.scope?.page_numbers || []
  );
}

function nativeStructureProjectionLifecycleKey(
  continuation: NativeStructureContinuationV1,
): string {
  const handoff = continuation.latest_handoff;
  const attempt = handoff?.latest_attempt;
  return [
    continuation.status,
    handoff?.handoff_id || "",
    handoff?.status || "",
    attempt?.attempt_id || "",
    attempt?.attempt_ordinal || 0,
    attempt?.status || "",
    attempt?.validation_status || "",
    attempt?.rendered_output_sha256 || "",
    (attempt?.affected_scope_ids || []).join(","),
    (attempt?.rendered_reopened_scope_ids || []).join(","),
    (attempt?.pending_rendered_scope_ids || []).join(","),
  ].join("|");
}

function containsOrderedStrings(
  values: readonly string[],
  required: readonly string[],
): boolean {
  if (!required.length) return true;
  let requiredIndex = 0;
  for (const value of values) {
    if (value === required[requiredIndex]) requiredIndex += 1;
    if (requiredIndex === required.length) return true;
  }
  return false;
}

function nativeStructureProjectionSatisfiesAttempt(
  reference: NativeStructureReferencePublicV1,
  attempt: NativeStructureHandoffAttemptPublicV1,
  expectedHandoffId: string,
): boolean {
  const projection = reference.structure_projection;
  if (attempt.status === "VERIFIED") {
    if (projection.attempt_id === attempt.attempt_id) {
      return (
        projection.state === "ACCEPTED" &&
        projection.validation_status === "PASS"
      );
    }
    const currentHandoff = reference.continuation.latest_handoff;
    const currentAttempt = currentHandoff?.latest_attempt;
    return (
      projection.state === "WORKING_VERIFICATION_REQUIRED" &&
      Boolean(projection.attempt_id) &&
      currentHandoff?.handoff_id !== expectedHandoffId &&
      currentAttempt?.attempt_id === projection.attempt_id &&
      currentAttempt.status === "VERIFICATION_REQUIRED"
    );
  }
  if (projection.attempt_id !== attempt.attempt_id) return false;
  if (attempt.status === "VERIFICATION_REQUIRED") {
    return (
      projection.state === "WORKING_VERIFICATION_REQUIRED" &&
      containsOrderedStrings(
        projection.pending_rendered_scope_ids,
        attempt.pending_rendered_scope_ids || [],
      )
    );
  }
  return (
    reference.continuation.latest_handoff?.latest_attempt?.attempt_id ===
      attempt.attempt_id &&
    reference.continuation.latest_handoff.latest_attempt.status ===
      attempt.status
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message) return error.message;
  return "原文结构草案暂时无法读取。";
}

function apiErrorCode(error: unknown): string {
  if (!(error instanceof ApiError) || !error.payload) return "";
  const detail = error.payload.detail;
  if (!detail || typeof detail !== "object") return "";
  return String((detail as Record<string, unknown>).code || "");
}

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const AGENT_RUN_ID_PATTERN = /^agent-run-[0-9a-f]{32}$/;
const WORKING_REVISION_ID_PATTERN =
  /^native-structure-working-revision-[0-9a-f]{32}$/;

function workingRevisionKey(
  head: NativeStructureWorkingRevisionHeadV1,
): string {
  return `${head.run_id}:${head.revision}:${head.revision_sha256}`;
}

function workingRevisionMatchesBasis(
  revision: NativeStructureWorkingRevisionHeadV1 | NativeStructureWorkingRevisionV1,
  reference: NativeStructureReferencePublicV1,
): boolean {
  if (
    revision.source_sha256 !== reference.source_sha256 ||
    revision.base_artifact_id !== reference.artifact_id
  ) return false;
  const publishedHead = reference.working_revision_head;
  if (publishedHead?.validator_kind === "D2_HOST_COMPILER") {
    // A growing map has a stable survey basis and a different content hash per
    // revision. Polling may already have a newer head than this reference read.
    return (
      revision.validator_kind === "D2_HOST_COMPILER" &&
      revision.run_id === publishedHead.run_id &&
      revision.source_sha256 === publishedHead.source_sha256 &&
      revision.base_artifact_id === publishedHead.base_artifact_id &&
      revision.base_projection_sha256 === publishedHead.base_projection_sha256 &&
      revision.revision >= publishedHead.revision &&
      (revision.revision !== publishedHead.revision ||
        revision.revision_sha256 === publishedHead.revision_sha256)
    );
  }
  return revision.base_projection_sha256 === reference.structure_projection.projection_sha256;
}

function workingRevisionValidationError({
  expectedSourceSha256,
  acceptedReference,
  currentRevision,
  head,
  revision,
}: {
  expectedSourceSha256: string;
  acceptedReference: NativeStructureReferencePublicV1 | null;
  currentRevision: NativeStructureWorkingRevisionV1 | null;
  head: NativeStructureWorkingRevisionHeadV1;
  revision: NativeStructureWorkingRevisionV1;
}): string {
  if (
    head.schema_version !== "native-structure-working-revision-head/v1" ||
    revision.schema_version !== "native-structure-working-revision/v1" ||
    !AGENT_RUN_ID_PATTERN.test(head.run_id) ||
    !WORKING_REVISION_ID_PATTERN.test(head.revision_id) ||
    !SHA256_PATTERN.test(head.revision_sha256) ||
    !SHA256_PATTERN.test(head.base_projection_sha256) ||
    head.source_sha256 !== expectedSourceSha256 ||
    revision.source_sha256 !== expectedSourceSha256 ||
    revision.run_id !== head.run_id ||
    revision.handoff_id !== head.handoff_id ||
    revision.base_artifact_id !== head.base_artifact_id ||
    revision.base_projection_sha256 !== head.base_projection_sha256 ||
    revision.revision_id !== head.revision_id ||
    revision.revision !== head.revision ||
    revision.previous_revision_sha256 !== head.previous_revision_sha256 ||
    revision.revision_sha256 !== head.revision_sha256 ||
    revision.evidence_artifact_id !== head.evidence_artifact_id ||
    revision.validator_kind !== head.validator_kind ||
    revision.quality_claim !== head.quality_claim ||
    !Number.isFinite(Date.parse(revision.created_at_utc)) ||
    !Number.isFinite(Date.parse(head.created_at_utc)) ||
    Date.parse(revision.created_at_utc) !== Date.parse(head.created_at_utc) ||
    revision.state !== head.state ||
    revision.validation_status !== "PASS" ||
    !SHA256_PATTERN.test(revision.validator_receipt_sha256) ||
    !revision.validator_kind ||
    !revision.evidence_artifact_id ||
    !revision.quality_claim ||
    !revision.created_at_utc
  ) {
    return "工作结构身份或 Host 验证回执不一致。";
  }
  const expectedQualityClaim =
    revision.validator_kind === "ACCEPTED_REFERENCE_REPLAY"
      ? "TRANSPORT_REPLAY_ONLY"
      : "D2_HOST_VERIFIED";
  if (revision.quality_claim !== expectedQualityClaim) {
    return "工作结构验证器与质量声明不一致。";
  }
  if (
    revision.validator_kind === "D2_HOST_COMPILER" &&
    (!SHA256_PATTERN.test(revision.d2_compiler_receipt_sha256 || "") ||
      revision.d2_compiler_receipt_sha256 !== head.d2_compiler_receipt_sha256)
  ) {
    return "原文结构的来源验证回执不一致。";
  }
  if (
    acceptedReference &&
    !workingRevisionMatchesBasis(head, acceptedReference)
  ) {
    return "工作结构基线与当前已接受结构不一致。";
  }
  if (currentRevision) {
    if (
      revision.run_id !== currentRevision.run_id ||
      revision.base_artifact_id !== currentRevision.base_artifact_id ||
      revision.base_projection_sha256 !== currentRevision.base_projection_sha256 ||
      revision.revision <= currentRevision.revision
    ) {
      return "工作结构修订链不连续。";
    }
    if (
      revision.revision === currentRevision.revision + 1 &&
      revision.previous_revision_sha256 !== currentRevision.revision_sha256
    ) {
      return "工作结构修订链不连续。";
    }
  }

  const nodeIds = revision.document_tree.nodes.map((node) => node.node_id);
  const nodeIdSet = new Set(nodeIds);
  if (
    revision.document_tree.schema_version !==
      "native-document-structure-tree/v1" ||
    nodeIdSet.size !== nodeIds.length ||
    revision.document_tree.root_ids.some((nodeId) => !nodeIdSet.has(nodeId)) ||
    revision.document_tree.nodes.some(
      (node) => node.parent_id !== null && !nodeIdSet.has(node.parent_id),
    )
  ) {
    return "工作结构不是一棵完整、可定位的树。";
  }
  const nodeStateIds = Object.keys(revision.node_states);
  if (
    nodeStateIds.length !== nodeIds.length ||
    nodeStateIds.some((nodeId) => !nodeIdSet.has(nodeId)) ||
    nodeIds.some(
      (nodeId) =>
        !["WORKING_VERIFIED", "ACCEPTED"].includes(
          revision.node_states[nodeId],
        ),
    )
  ) {
    return "工作结构节点状态没有完整覆盖当前树。";
  }

  const inserted = new Set(revision.inserted_node_ids);
  const changed = new Set(revision.changed_node_ids);
  const retired = new Set(revision.retired_node_ids);
  if (
    inserted.size !== revision.inserted_node_ids.length ||
    changed.size !== revision.changed_node_ids.length ||
    retired.size !== revision.retired_node_ids.length ||
    [...inserted].some(
      (nodeId) =>
        !nodeIdSet.has(nodeId) ||
        changed.has(nodeId) ||
        retired.has(nodeId) ||
        (revision.state === "WORKING" &&
          revision.node_states[nodeId] !== "WORKING_VERIFIED"),
    ) ||
    [...changed].some(
      (nodeId) =>
        !nodeIdSet.has(nodeId) ||
        retired.has(nodeId) ||
        (revision.state === "WORKING" &&
          revision.node_states[nodeId] !== "WORKING_VERIFIED"),
    ) ||
    [...retired].some((nodeId) => nodeIdSet.has(nodeId)) ||
    Object.entries(revision.successor_node_ids).some(
      ([retiredId, successorIds]) =>
        !retired.has(retiredId) ||
        !successorIds.length ||
        new Set(successorIds).size !== successorIds.length ||
        successorIds.some((nodeId) => !nodeIdSet.has(nodeId)),
    ) ||
    (revision.state === "WORKING" &&
      Object.values(revision.node_states).some(
        (state) => state !== "WORKING_VERIFIED",
      )) ||
    (revision.state === "ACCEPTED" &&
      Object.values(revision.node_states).some((state) => state !== "ACCEPTED"))
  ) {
    return "工作结构变更集合与节点状态不一致。";
  }

  if (
    currentRevision &&
    revision.revision === currentRevision.revision + 1
  ) {
    const derived = nativeTreeNodeDiff(
      currentRevision.document_tree.nodes,
      revision.document_tree.nodes,
    );
    if (
      !sameStringSet(new Set(derived.inserted), inserted) ||
      !sameStringSet(new Set(derived.retired), retired) ||
      !sameStringSet(new Set(derived.changed), changed)
    ) {
      return "工作结构变更摘要与完整投影不一致。";
    }
  }
  const logicalTableIds = revision.logical_tables.map((table) => table.scope_id);
  const bindingIds = revision.table_preview_bindings.map(
    (binding) => binding.scope_id,
  );
  if (
    new Set(bindingIds).size !== bindingIds.length ||
    !sameStringSet(new Set(logicalTableIds), new Set(bindingIds)) ||
    revision.table_preview_bindings.some(
      (binding) =>
        binding.schema_version !==
          "native-structure-table-preview-binding/v1" ||
        !SHA256_PATTERN.test(binding.preview_sha256) ||
        !SHA256_PATTERN.test(binding.object_sha256) ||
        binding.object_size <= 0 ||
        binding.media_type !==
          "application/vnd.reqpilotx.native-structure-table-preview+json" ||
        binding.source_sha256 !== revision.source_sha256 ||
        binding.base_artifact_id !== revision.base_artifact_id ||
        binding.base_projection_sha256 !== revision.base_projection_sha256 ||
        !binding.object_key,
    )
  ) {
    return "工作结构表格预览绑定不完整或身份漂移。";
  }
  return "";
}

function buildChildrenByParent(
  tree: NativeStructureDocumentTreeV1,
): Map<string, NativeStructureTreeNodeV1[]> {
  const result = new Map<string, NativeStructureTreeNodeV1[]>();
  for (const node of tree.nodes) {
    const key = node.parent_id || "__root__";
    const siblings = result.get(key) || [];
    siblings.push(node);
    result.set(key, siblings);
  }
  for (const siblings of result.values()) {
    siblings.sort((left, right) => left.order - right.order);
  }
  return result;
}

export function reconcileNativeTreeUiState({
  previousNodes,
  nextTree,
  selectedNodeId,
  expandedIds,
  successorNodeIds = {},
}: {
  previousNodes: readonly NativeStructureTreeNodeV1[];
  nextTree: NativeStructureDocumentTreeV1;
  selectedNodeId: string;
  expandedIds: ReadonlySet<string>;
  successorNodeIds?: Readonly<Record<string, readonly string[]>>;
}): { selectedNodeId: string; expandedIds: Set<string> } {
  const previousById = new Map(
    previousNodes.map((node) => [node.node_id, node]),
  );
  const nextById = new Map(nextTree.nodes.map((node) => [node.node_id, node]));
  let nextSelectedId = selectedNodeId;
  if (!nextById.has(nextSelectedId)) {
    nextSelectedId =
      (successorNodeIds[nextSelectedId] || []).find((nodeId) =>
        nextById.has(nodeId),
      ) || "";
    let cursor = previousById.get(selectedNodeId)?.parent_id || null;
    while (cursor && !nextById.has(cursor)) {
      cursor = previousById.get(cursor)?.parent_id || null;
    }
    nextSelectedId = nextSelectedId || cursor || nextTree.root_ids[0] || "";
  }
  const nextExpandedIds = new Set<string>();
  for (const nodeId of expandedIds) {
    if (nextById.has(nodeId)) {
      nextExpandedIds.add(nodeId);
      continue;
    }
    for (const successorId of successorNodeIds[nodeId] || []) {
      if (nextById.has(successorId)) nextExpandedIds.add(successorId);
    }
  }
  let ancestor = nextById.get(nextSelectedId)?.parent_id || null;
  while (ancestor) {
    nextExpandedIds.add(ancestor);
    ancestor = nextById.get(ancestor)?.parent_id || null;
  }
  return { selectedNodeId: nextSelectedId, expandedIds: nextExpandedIds };
}

function sameStringSet(
  left: ReadonlySet<string>,
  right: ReadonlySet<string>,
): boolean {
  return (
    left.size === right.size && [...left].every((value) => right.has(value))
  );
}

function nativeTreeNodeDiff(
  previousNodes: readonly NativeStructureTreeNodeV1[],
  nextNodes: readonly NativeStructureTreeNodeV1[],
): { inserted: string[]; changed: string[]; retired: string[] } {
  const previousById = new Map(
    previousNodes.map((node) => [node.node_id, node]),
  );
  const nextById = new Map(nextNodes.map((node) => [node.node_id, node]));
  return {
    inserted: [...nextById.keys()].filter((nodeId) => !previousById.has(nodeId)),
    retired: [...previousById.keys()].filter((nodeId) => !nextById.has(nodeId)),
    changed: [...nextById.keys()].filter(
      (nodeId) =>
        previousById.has(nodeId) &&
        canonicalJson(previousById.get(nodeId)) !== canonicalJson(nextById.get(nodeId)),
    ),
  };
}

function flattenVisibleTree(
  tree: NativeStructureDocumentTreeV1,
  childrenByParent: ReadonlyMap<string, NativeStructureTreeNodeV1[]>,
  expandedIds: ReadonlySet<string>,
): NativeStructureTreeNodeV1[] {
  const nodesById = new Map(tree.nodes.map((node) => [node.node_id, node]));
  const result: NativeStructureTreeNodeV1[] = [];
  const visit = (node: NativeStructureTreeNodeV1) => {
    result.push(node);
    if (!expandedIds.has(node.node_id)) return;
    for (const child of childrenByParent.get(node.node_id) || []) visit(child);
  };
  for (const rootId of tree.root_ids) {
    const root = nodesById.get(rootId);
    if (root) visit(root);
  }
  return result;
}

function flattenVisibleSourceItemLedger(
  outline: SourceRequirementBaselineOutlineV1 | null,
  expandedIds: ReadonlySet<string>,
): SourceItemLedgerRowV1[] {
  if (!outline) return [];
  const rowsById = new Map(outline.ledger.rows.map((row) => [row.row_id, row]));
  const result: SourceItemLedgerRowV1[] = [];
  const visit = (row: SourceItemLedgerRowV1) => {
    result.push(row);
    if (!expandedIds.has(row.row_id)) return;
    for (const childId of row.child_row_ids) {
      const child = rowsById.get(childId);
      if (child) visit(child);
    }
  };
  for (const rootId of outline.ledger.root_row_ids) {
    const root = rowsById.get(rootId);
    if (root) visit(root);
  }
  return result;
}

function sourceContentText(
  node: NativeStructureTreeNodeV1,
  content: NativeStructureSourceContentV1,
): string {
  const nonBusinessIds = new Set(
    Array.isArray(node.content_sequence)
      ? node.content_sequence
          .filter((item) => item.disposition === "NON_BUSINESS")
          .map((item) => item.source_node_id)
      : [],
  );
  if (content.source_handles.length && content.source_handles.every((id) => nonBusinessIds.has(id))) {
    return "";
  }
  const text = (content.display_text || content.text).trim();
  if (!content.is_title) return text;
  const candidates = [
    [node.label, node.title].filter(Boolean).join(" "),
    `${node.label}${node.title}`,
    node.title,
    node.label,
  ]
    .map((value) => value.trim())
    .filter(Boolean)
    .sort((left, right) => right.length - left.length);
  for (const candidate of candidates) {
    if (!text.startsWith(candidate)) continue;
    return text
      .slice(candidate.length)
      .replace(/^[\s：:、，,；;。]+/, "")
      .trim();
  }
  return text;
}

function bboxOverlapRatio(
  bbox: readonly number[],
  region: readonly number[],
): number {
  const left = Math.max(bbox[0] || 0, region[0] || 0);
  const top = Math.max(bbox[1] || 0, region[1] || 0);
  const right = Math.min(bbox[2] || 0, region[2] || 0);
  const bottom = Math.min(bbox[3] || 0, region[3] || 0);
  if (right <= left || bottom <= top) return 0;
  const area = Math.max(
    1,
    ((bbox[2] || 0) - (bbox[0] || 0)) *
      ((bbox[3] || 0) - (bbox[1] || 0)),
  );
  return ((right - left) * (bottom - top)) / area;
}

function axisSizes(
  cells: NativeStructureTablePreviewV1["fragments"][number]["cells"],
  count: number,
  axis: "ROW" | "COLUMN",
): number[] {
  const boundaries = new Map<number, number[]>();
  const record = (index: number, value: number) => {
    const values = boundaries.get(index) || [];
    values.push(value);
    boundaries.set(index, values);
  };
  for (const cell of cells) {
    const index = axis === "ROW" ? cell.row : cell.column;
    const span = axis === "ROW" ? cell.row_span : cell.column_span;
    const start = axis === "ROW" ? cell.bbox[1] : cell.bbox[0];
    const end = axis === "ROW" ? cell.bbox[3] : cell.bbox[2];
    record(index, start);
    record(index + span, end);
  }
  const positions = Array.from({ length: count + 1 }, (_, index) => {
    const values = boundaries.get(index) || [];
    if (!values.length) return Number.NaN;
    return values.reduce((total, value) => total + value, 0) / values.length;
  });
  if (
    positions.some((value) => !Number.isFinite(value)) ||
    positions.some((value, index) => index > 0 && value <= positions[index - 1])
  ) {
    return [];
  }
  return positions.slice(1).map((value, index) => value - positions[index]);
}

function canonicalBoundaryRatios(
  preview: NativeStructureTablePreviewV1,
  canonicalCount: number,
): number[] {
  const published = preview.presentation?.canonical_projection;
  const publishedRatios = published?.boundary_ratios || [];
  if (
    published?.status === "EXACT" &&
    published.canonical_column_count === canonicalCount &&
    publishedRatios.length === canonicalCount + 1 &&
    publishedRatios.every(
      (value, index) =>
        Number.isFinite(value) &&
        value >= 0 &&
        value <= 1 &&
        (index === 0 || value > publishedRatios[index - 1]),
    )
  ) {
    return publishedRatios;
  }
  const firstFragment = preview.fragments[0];
  if (!firstFragment) return [];
  const headerStarts = Array.from(
    new Set(
      firstFragment.cells
        .filter((cell) => cell.row === 0 && cell.text.trim())
        .map((cell) => cell.bbox[0]),
    ),
  ).sort((left, right) => left - right);
  const width = firstFragment.bbox[2] - firstFragment.bbox[0];
  if (headerStarts.length !== canonicalCount || width <= 0) return [];
  const ratios = headerStarts.map(
    (value) => (value - firstFragment.bbox[0]) / width,
  );
  ratios[0] = 0;
  ratios.push(1);
  return ratios.every(
    (value, index) =>
      Number.isFinite(value) &&
      value >= 0 &&
      value <= 1 &&
      (index === 0 || value > ratios[index - 1]),
  )
    ? ratios
    : [];
}

function canonicalMappingsByFragment(
  preview: NativeStructureTablePreviewV1,
): Map<string, Map<string, { column: number; columnSpan: number }>> {
  const result = new Map<
    string,
    Map<string, { column: number; columnSpan: number }>
  >();
  for (const fragment of preview.presentation?.canonical_projection?.fragments || []) {
    result.set(
      fragment.fragment_id,
      new Map(
        fragment.cell_mappings.map((mapping) => [
          mapping.cell_id,
          {
            column: mapping.column,
            columnSpan: mapping.column_span,
          },
        ]),
      ),
    );
  }
  return result;
}

export function canonicalTableGridIssues(table: StructuredTableModel): string[] {
  const issues: string[] = [];
  const coverage = Array.from({ length: table.rowCount }, () =>
    Array.from({ length: table.columnCount }, () => 0),
  );
  for (const cell of table.cells) {
    if (
      cell.row < 0 ||
      cell.column < 0 ||
      cell.rowSpan < 1 ||
      cell.columnSpan < 1 ||
      cell.row + cell.rowSpan > table.rowCount ||
      cell.column + cell.columnSpan > table.columnCount
    ) {
      issues.push(`单元格 ${cell.id} 超出规范行列范围`);
      continue;
    }
    for (let row = cell.row; row < cell.row + cell.rowSpan; row += 1) {
      for (
        let column = cell.column;
        column < cell.column + cell.columnSpan;
        column += 1
      ) {
        coverage[row][column] += 1;
      }
    }
  }
  coverage.forEach((row, rowIndex) => {
    if (row.some((value) => value !== 1)) {
      issues.push(`第 ${rowIndex + 1} 行没有恰好覆盖 ${table.columnCount} 列`);
    }
  });
  return issues;
}

export function canonicalStructuredTablesAreValid(
  preview: NativeStructureTablePreviewV1,
  tables: StructuredTableModel[],
): boolean {
  const canonicalCount = preview.presentation?.canonical_column_count || 0;
  if (!canonicalCount) return true;
  const published = preview.presentation?.canonical_projection;
  if (published) {
    const mappings = canonicalMappingsByFragment(preview);
    if (
      published.status !== "EXACT" ||
      published.canonical_column_count !== canonicalCount ||
      preview.fragments.some((fragment) => {
        const fragmentMappings = mappings.get(fragment.fragment_id);
        return (
          !fragmentMappings ||
          fragment.cells.some((cell) => !fragmentMappings.has(cell.cell_id))
        );
      })
    ) {
      return false;
    }
  }
  return (
    tables.length === preview.fragments.length &&
    tables.every(
      (table) =>
        table.columnCount === canonicalCount &&
        canonicalTableGridIssues(table).length === 0,
    )
  );
}

export function nativeStructuredTables(
  preview: NativeStructureTablePreviewV1,
  anchors: readonly NativeStructureSourceAnchorV1[],
): StructuredTableModel[] {
  if (preview.fragments.some(fragment => officeLocation(fragment.native_locator))) return [];
  if (preview.presentation?.mode === "VISUAL_REGION_ONLY") {
    return preview.fragments.map((fragment, segmentIndex) => ({
      id: fragment.fragment_id,
      sourceNodeId: fragment.table_token,
      sourceOrder: fragment.page * 1000 + segmentIndex,
      rowCount: 1,
      columnCount: 1,
      headerRegions: [],
      cells: [
        {
          id: `${fragment.fragment_id}:visual-region`,
          row: 0,
          column: 0,
          rowSpan: 1,
          columnSpan: 1,
          text: "",
          horizontalAlignment: "",
          verticalAlignment: "",
          indentLevel: 0,
          page: fragment.page,
          header: false,
          textSource: "visual_table_region",
          images: [
            {
              id: `${fragment.fragment_id}:visual-region-image`,
              assetId: "",
              mediaType: "image/png",
              page: fragment.page,
              bbox: [...fragment.bbox],
              available: true,
            },
          ],
        },
      ],
      notes: ["原生单元格混入相邻内容，当前直接展示原 PDF 表格区域。"],
      page: fragment.page,
      pageEnd: fragment.page,
      reconstructionMode: "VISUAL_REGION_ONLY",
      mergeSemantics: "UNRESOLVED",
      geometryPreserved: false,
      widthPoints: fragment.bbox[2] - fragment.bbox[0],
      heightPoints: fragment.bbox[3] - fragment.bbox[1],
      columnWidthsPoints: [],
      rowHeightsPoints: [],
      logicalTableId: preview.logical_table_scope_id,
      continuationStatus:
        preview.fragments.length === 1
          ? "SINGLE"
          : segmentIndex === 0
            ? "START"
            : "CONTINUATION",
      segmentIndex,
      segmentCount: preview.fragments.length,
    }));
  }
  const imageAnchors = anchors.filter(
    (anchor) =>
      anchor.anchor_kind === "IMAGE" &&
      !(preview.presentation?.floating_images || []).some(
        (floating) =>
          (floating.placement_id || floating.anchor_id) ===
          (anchor.placement_id || anchor.anchor_id),
      ),
  );
  const imageAssignments = new Map<string, TableCellImage[]>();
  for (const anchor of imageAnchors) {
    const candidates = preview.fragments.flatMap((fragment) =>
      fragment.page !== anchor.page
        ? []
        : fragment.cells.map((cell) => ({
            cell,
            score: bboxOverlapRatio(anchor.bbox, cell.bbox),
            area: Math.max(
              1,
              (cell.bbox[2] - cell.bbox[0]) * (cell.bbox[3] - cell.bbox[1]),
            ),
          })),
    );
    candidates.sort((left, right) => right.score - left.score || left.area - right.area);
    const best = candidates[0];
    if (!best || best.score < 0.6) continue;
    const images = imageAssignments.get(best.cell.cell_id) || [];
    images.push({
      id: anchor.placement_id || anchor.anchor_id,
      assetId: anchor.asset_id || "",
      mediaType: "image/png",
      page: anchor.page,
      bbox: [...anchor.bbox],
      available: true,
    });
    imageAssignments.set(best.cell.cell_id, images);
  }
  const canonicalCount = preview.presentation?.canonical_column_count || 0;
  const boundaryRatios = canonicalBoundaryRatios(preview, canonicalCount);
  const publishedMappings = canonicalMappingsByFragment(preview);
  return preview.fragments.map((fragment, segmentIndex) => {
    const fragmentWidth = fragment.bbox[2] - fragment.bbox[0];
    const canonicalBounds = boundaryRatios.map(
      (ratio) => fragment.bbox[0] + ratio * fragmentWidth,
    );
    const overlapTolerance = Math.max(
      0.75,
      Math.min(2, fragmentWidth * 0.002),
    );
    const fragmentMappings = publishedMappings.get(fragment.fragment_id);
    const columnBoundaries = canonicalBounds.length
      ? []
      : Array.from(
          new Set(
            fragment.cells.flatMap((cell) => [
              cell.column,
              cell.column + cell.column_span,
            ]),
          ),
        ).sort((left, right) => left - right);
    const compactColumnIndex = new Map(
      columnBoundaries.map((boundary, index) => [boundary, index]),
    );
    const rawCompactCells = fragment.cells.map((cell) => {
      const isSourceHeader = (fragment.source_header_regions || []).some(
        (region) =>
          cell.row >= region.row &&
          cell.row + cell.row_span <= region.row + region.row_span &&
          cell.column >= region.column &&
          cell.column + cell.column_span <= region.column + region.column_span,
      );
      if (!canonicalBounds.length) {
        return {
          ...cell,
          is_source_header: isSourceHeader,
          column: compactColumnIndex.get(cell.column) || 0,
          column_span: Math.max(
            1,
            (compactColumnIndex.get(cell.column + cell.column_span) || 1) -
              (compactColumnIndex.get(cell.column) || 0),
          ),
          source_cell_ids: [cell.cell_id],
        };
      }
      const publishedMapping = fragmentMappings?.get(cell.cell_id);
      if (publishedMapping) {
        return {
          ...cell,
          is_source_header: isSourceHeader,
          column: publishedMapping.column,
          column_span: publishedMapping.columnSpan,
          source_cell_ids: [cell.cell_id],
        };
      }
      const overlappingColumns = canonicalBounds
        .slice(0, -1)
        .map((start, index) => ({
          index,
          overlap: Math.max(
            0,
            Math.min(cell.bbox[2], canonicalBounds[index + 1]) -
              Math.max(cell.bbox[0], start),
          ),
        }))
        .filter((item) => item.overlap > overlapTolerance);
      if (!overlappingColumns.length) {
        const fallbackColumns = canonicalBounds
          .slice(0, -1)
          .map((start, index) => ({
            index,
            overlap: Math.max(
              0,
              Math.min(cell.bbox[2], canonicalBounds[index + 1]) -
                Math.max(cell.bbox[0], start),
            ),
          }))
          .sort((left, right) => right.overlap - left.overlap);
        if (fallbackColumns[0]?.overlap > 0) {
          overlappingColumns.push(fallbackColumns[0]);
        }
      }
      const startColumn = overlappingColumns[0]?.index || 0;
      const endColumn =
        (overlappingColumns[overlappingColumns.length - 1]?.index ||
          startColumn) + 1;
      return {
        ...cell,
        is_source_header: isSourceHeader,
        column: startColumn,
        column_span: Math.max(1, endColumn - startColumn),
        source_cell_ids: [cell.cell_id],
      };
    });
    const compactCells = canonicalBounds.length
      ? Array.from(
          rawCompactCells
            .reduce((groups, cell) => {
              const key = `${cell.row}:${cell.column}:${cell.column_span}`;
              const existing = groups.get(key);
              if (!existing) {
                groups.set(key, { ...cell });
                return groups;
              }
              existing.cell_id = `${existing.cell_id}+${cell.cell_id}`;
              existing.row_span = Math.max(existing.row_span, cell.row_span);
              existing.is_source_header ||= cell.is_source_header;
              existing.bbox = [
                Math.min(existing.bbox[0], cell.bbox[0]),
                Math.min(existing.bbox[1], cell.bbox[1]),
                Math.max(existing.bbox[2], cell.bbox[2]),
                Math.max(existing.bbox[3], cell.bbox[3]),
              ];
              const texts = [existing.text, cell.text]
                .map((value) => value.trim())
                .filter(Boolean);
              existing.text = Array.from(new Set(texts)).join("\n");
              existing.source_cell_ids.push(cell.cell_id);
              return groups;
            }, new Map<string, (typeof rawCompactCells)[number]>())
            .values(),
        )
      : rawCompactCells;
    const columnCount = canonicalBounds.length
      ? canonicalBounds.length - 1
      : Math.max(1, columnBoundaries.length - 1);
    const columnWidthsPoints = canonicalBounds.length
      ? canonicalBounds
          .slice(1)
          .map((value, index) => value - canonicalBounds[index])
      : axisSizes(compactCells, columnCount, "COLUMN");
    const rowHeightsPoints = axisSizes(compactCells, fragment.row_count, "ROW");
    const headerRowCount =
      fragment.source_header_regions === undefined && segmentIndex === 0
        ? Math.max(
            1,
            ...compactCells
              .filter((cell) => cell.row === 0)
              .map((cell) => cell.row_span),
          )
        : 0;
    const geometryPreserved =
      columnWidthsPoints.length === columnCount &&
      rowHeightsPoints.length === fragment.row_count;
    return {
      id: fragment.fragment_id,
      sourceNodeId: fragment.table_token,
      sourceOrder: fragment.page * 1000 + segmentIndex,
      rowCount: fragment.row_count,
      columnCount,
      headerRegions: fragment.source_header_regions !== undefined
        ? compactCells.filter((cell) => cell.is_source_header).map((cell) => ({
            row_start: cell.row,
            row_end: cell.row + cell.row_span - 1,
            column_start: cell.column,
            column_end: cell.column + cell.column_span - 1,
          }))
        : headerRowCount
        ? [
            {
              row_start: 0,
              row_end: headerRowCount - 1,
              column_start: 0,
              column_end: columnCount - 1,
            },
          ]
        : [],
      cells: compactCells.map((cell) => ({
        id: cell.cell_id,
        sourceCellIds: [...cell.source_cell_ids],
        row: cell.row,
        column: cell.column,
        rowSpan: cell.row_span,
        columnSpan: cell.column_span,
        text: cell.text,
        horizontalAlignment: "",
        verticalAlignment: "",
        indentLevel: 0,
        page: fragment.page,
        header: fragment.source_header_regions !== undefined
          ? cell.is_source_header
          : headerRowCount > 0 && cell.row < headerRowCount,
        textSource: "native_table_cell",
        images: cell.source_cell_ids.flatMap(
          (cellId) => imageAssignments.get(cellId) || [],
        ),
      })),
      floatingImages: (preview.presentation?.floating_images || [])
        .filter((image) => image.page === fragment.page)
        .map((image) => {
          const sourceRowIndexes = new Set<number>();
          fragment.cells.forEach((cell) => {
            const verticalOverlap = Math.max(
              0,
              Math.min(image.bbox[3], cell.bbox[3]) -
                Math.max(image.bbox[1], cell.bbox[1]),
            );
            if (verticalOverlap <= 0.5) return;
            for (let row = cell.row; row < cell.row + cell.row_span; row += 1) {
              sourceRowIndexes.add(row);
            }
          });
          return {
            id: image.placement_id || image.anchor_id,
            assetId: image.asset_id || "",
            mediaType: "image/png",
            page: image.page,
            bbox: [...image.bbox],
            available: true,
            sourceRowIndexes: Array.from(sourceRowIndexes).sort(
              (left, right) => left - right,
            ),
          };
        }),
      notes: fragment.source_notes || [],
      page: fragment.page,
      pageEnd: fragment.page,
      reconstructionMode: "STRUCTURED_TEXT_GRID",
      mergeSemantics: "PRESERVED",
      geometryPreserved,
      widthPoints: fragment.bbox[2] - fragment.bbox[0],
      heightPoints: fragment.bbox[3] - fragment.bbox[1],
      columnWidthsPoints,
      rowHeightsPoints,
      logicalTableId: preview.logical_table_scope_id,
      continuationStatus:
        preview.fragments.length === 1
          ? "SINGLE"
          : segmentIndex === 0
            ? "START"
            : "CONTINUATION",
      segmentIndex,
      segmentCount: preview.fragments.length,
    };
  });
}

function StructureTreeBranch({
  node,
  depth,
  childrenByParent,
  expandedIds,
  selectedId,
  onSelect,
  onToggle,
  recordCountsByNode,
  showStructureFindings = true,
  workingAffectedScopeIds = new Set(),
}: StructureTreeBranchProps) {
  const children = childrenByParent.get(node.node_id) || [];
  const expanded = children.length > 0 && expandedIds.has(node.node_id);
  const selected = selectedId === node.node_id;
  const recordCounts = recordCountsByNode?.get(node.node_id);
  return (
    <li role="none">
      <div
        className={cx(
          "native-hierarchy-tree-row",
          selected && "is-selected",
          showStructureFindings && node.finding_codes.length > 0 && "has-finding",
        )}
        role="treeitem"
        aria-level={depth + 1}
        aria-selected={selected}
        aria-expanded={children.length ? expanded : undefined}
        style={{ "--tree-depth": depth } as CSSProperties}
      >
        {children.length ? (
          <button
            type="button"
            className="native-hierarchy-tree-toggle"
            tabIndex={-1}
            aria-label={`${expanded ? "收起" : "展开"} ${nodeTitle(node)}`}
            onClick={() => onToggle(node.node_id)}
          >
            <Icon name={expanded ? "chevron-down" : "chevron"} size={13} />
          </button>
        ) : (
          <span className="native-hierarchy-tree-spacer" aria-hidden="true" />
        )}
        <button
          type="button"
          className="native-hierarchy-tree-select"
          data-tree-node={node.node_id}
          tabIndex={selected ? 0 : -1}
          onClick={() => onSelect(node)}
        >
          <Icon
            name={
              node.kind === "DOCUMENT_ROOT" || node.kind === "DOCUMENT_UNIT"
                ? "document"
                : node.content_blocks.length
                  ? "matrix"
                  : "evidence"
            }
            size={14}
          />
          <span>
            <strong>{nodeTitle(node)}</strong>
            <small>
              {pageLabel(node.pages)}
              {node.content_blocks.length ? " · 含表格" : ""}
            </small>
          </span>
          {["DERIVED_FROM_VISIBLE_TABLE_ROLE", "AGENT_SURVEY_LABEL"].includes(node.title_authority) ? (
            <em>Agent 命名</em>
          ) : null}
          {workingAffectedScopeIds.has(node.node_id) ? <em>正在确认</em> : null}
          {recordCounts?.recordCount ? (
            <em className="source-requirement-tree-count">
              {recordCounts.requirementCount} 需求
              {recordCounts.nonRequirementCount
                ? ` · ${recordCounts.nonRequirementCount} 非需求`
                : ""}
            </em>
          ) : null}
          {showStructureFindings && node.finding_codes.length ? (
            <span className="native-hierarchy-finding-dot" aria-label="有待判断项" />
          ) : null}
        </button>
      </div>
      {expanded ? (
        <ul role="group">
          {children.map((child) => (
            <StructureTreeBranch
              key={child.node_id}
              node={child}
              depth={depth + 1}
              childrenByParent={childrenByParent}
              expandedIds={expandedIds}
              selectedId={selectedId}
              onSelect={onSelect}
              onToggle={onToggle}
              recordCountsByNode={recordCountsByNode}
              showStructureFindings={showStructureFindings}
              workingAffectedScopeIds={workingAffectedScopeIds}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function sourceItemRoleLabel(record: SourceRequirementOutlineRecordV1): string {
  if (record.requirementness === "REQUIREMENT") return "需求";
  const role = record.non_requirement_role;
  if (!role) return "非需求内容";
  return (
    {
      HEADING: "层级标题",
      METADATA: "文控信息",
      REVISION_HISTORY: "修订记录",
      CONTEXT: "背景说明",
      DEFINITION: "定义",
      RATIONALE: "理由",
      EXAMPLE: "示例",
      NOTE: "注释",
      REFERENCE: "外部引用",
    }[role] || "非需求内容"
  );
}

function sourceItemKindSummary(record: SourceRequirementOutlineRecordV1): string {
  if (record.requirementness !== "REQUIREMENT") return "";
  return record.requirement_kinds
    .map(
      (value) =>
        ({ FUNCTIONAL: "功能", QUALITY: "质量", CONSTRAINT: "约束", PROJECT: "项目", PROCESS: "过程" })[
          value
        ] || value,
    )
    .join(" / ");
}

function SourceItemLedgerBranch({
  row,
  depth,
  rowsById,
  nodesById,
  sourceItemsById,
  expandedIds,
  selectedRowId,
  onSelectStructure,
  onSelectSourceItem,
  onToggle,
  visibleRows,
  reviewContext,
  reviewCounts,
  derivedCounts, scopeSummaries,
}: SourceItemLedgerBranchProps) {
  if (visibleRows && !visibleRows.has(row.row_id)) return null;
  const node = nodesById.get(row.structure_node_id);
  if (!node) return null;
  const sourceItem = row.source_item_id
    ? sourceItemsById.get(row.source_item_id)
    : undefined;
  const children = row.child_row_ids
    .map((rowId) => rowsById.get(rowId))
    .filter((value): value is SourceItemLedgerRowV1 => Boolean(value));
  const directChildSourceItems = children
    .filter((child) => child.row_role === "SOURCE_ITEM" && child.source_item_id)
    .map((child) => sourceItemsById.get(child.source_item_id || ""))
    .filter(
      (value): value is SourceRequirementOutlineRecordV1 => Boolean(value),
    );
  const directTableItemCount = directChildSourceItems.filter((item) =>
    item.source_form.startsWith("TABLE_"),
  ).length;
  const directTextItemCount =
    directChildSourceItems.length - directTableItemCount;
  const directItemCountLabel = scopeSummaries?.[row.structure_node_id] ? scopeCountLabel(scopeSummaries[row.structure_node_id]) :
    row.is_table_structure && directChildSourceItems.length
      ? [
          directTableItemCount
            ? `表格条目 ${directTableItemCount}`
            : "",
          directTextItemCount ? `其他原文 ${directTextItemCount}` : "",
        ]
          .filter(Boolean)
          .join(" · ")
      : row.direct_source_item_count
        ? `直属原文 ${row.direct_source_item_count}`
        : "";
  const expanded = children.length > 0 && expandedIds.has(row.row_id);
  const selected = selectedRowId === row.row_id;
  const isSourceItem = row.row_role === "SOURCE_ITEM";
  const select = () => {
    if (sourceItem) onSelectSourceItem(sourceItem, node);
    else onSelectStructure(node);
  };
  const primaryLabel =
    isSourceItem && sourceItem ? sourceItem.display_label : nodeTitle(node);
  const identifier = sourceItem
    ? sourceItem.requirement_display_id || sourceItem.source_display_id
    : "";
  const kindSummary = sourceItem ? sourceItemKindSummary(sourceItem) : "";
  const review = sourceItem ? reviewContext?.records[sourceItem.source_record_id] : null;
  const scopeCount = reviewCounts?.get(row.row_id);
  const derivedCount = sourceItem ? review?.derived_count || derivedCounts?.get(sourceItem.source_record_id) || 0 : 0;
  return (
    <li role="none">
      <div
        className={cx(
          "native-hierarchy-tree-row",
          "source-item-ledger-row",
          isSourceItem && "is-source-item",
          row.is_table_structure && "is-table-structure",
          selected && "is-selected",
          Boolean(review?.focus_areas.length || (!sourceItem && scopeCount?.focus)) && "has-review-focus",
        )}
        role="treeitem"
        aria-level={depth + 1}
        aria-selected={selected}
        aria-expanded={children.length ? expanded : undefined}
        style={{ "--tree-depth": depth } as CSSProperties}
      >
        {children.length ? (
          <button
            type="button"
            className="native-hierarchy-tree-toggle"
            tabIndex={-1}
            aria-label={`${expanded ? "收起" : "展开"} ${primaryLabel}`}
            onClick={() => onToggle(row.row_id)}
          >
            <Icon name={expanded ? "chevron-down" : "chevron"} size={13} />
          </button>
        ) : (
          <span className="native-hierarchy-tree-spacer" aria-hidden="true" />
        )}
        <button
          type="button"
          className="native-hierarchy-tree-select"
          data-tree-row={row.row_id}
          title={primaryLabel}
          tabIndex={selected ? 0 : -1}
          onClick={select}
        >
          <Icon
            name={
              row.is_table_structure || sourceItem?.source_form.startsWith("TABLE_")
                ? "matrix"
                : isSourceItem
                  ? "evidence"
                  : "document"
            }
            size={14}
          />
          <span>
            <strong>{sourceItem?.source_number !== undefined && <span className="tree-source-number" title="客户原编号">{originalNumber(sourceItem)} </span>}{primaryLabel}</strong>
            <small>
              {identifier ? `${identifier} · ` : ""}
              {sourceItem
                ? `P${sourceItem.pages.join("–P")}${
                    kindSummary ? ` · ${kindSummary}` : ""
                  }`
                : `${pageLabel(node.pages)}${
                    row.is_table_structure ? " · 原表" : ""
                  }`}
            </small>
            {sourceItem && (derivedCount > 0 || review?.has_visual_interpretation || review?.focus_areas.length) ?
              <small className="source-tree-review-signals">{[
                review?.focus_areas.map(area => FOCUS_LABELS[area]).join(" / "),
                derivedCount ? `派生 ${derivedCount}` : "",
                review?.has_visual_interpretation ? "图片释读" : "",
              ].filter(Boolean).join(" · ")}</small> : null}
            {!sourceItem && scopeCount?.focus ? <small className="source-tree-review-signals">重点分析 {scopeCount.focus}{scopeCount.attention ? ` · 待核对 ${scopeCount.attention}` : ""}</small> : null}
          </span>
          {review?.needs_attention || review?.pending_feedback_count ? <em className="source-tree-attention">待核对</em> : review?.focus_areas.length ? <em className="source-tree-focus">重点</em> : sourceItem ? (
            <em
              className={cx(
                "source-item-tree-role",
                sourceItem.requirementness === "REQUIREMENT" && "is-requirement",
              )}
            >
              {sourceItemRoleLabel(sourceItem)}
            </em>
          ) : directItemCountLabel ? (
            <em className="source-requirement-tree-count" title={scopeCountDetail(scopeSummaries?.[row.structure_node_id])}>
              {directItemCountLabel}
            </em>
          ) : null}
        </button>
      </div>
      {expanded ? (
        <ul role="group">
          {children.map((child) => (
            <SourceItemLedgerBranch
              key={child.row_id}
              row={child}
              depth={depth + 1}
              rowsById={rowsById}
              nodesById={nodesById}
              sourceItemsById={sourceItemsById}
              expandedIds={expandedIds}
              selectedRowId={selectedRowId}
              onSelectStructure={onSelectStructure}
              onSelectSourceItem={onSelectSourceItem}
              onToggle={onToggle}
              visibleRows={visibleRows}
              reviewContext={reviewContext}
              reviewCounts={reviewCounts}
              derivedCounts={derivedCounts}
              scopeSummaries={scopeSummaries}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export function NativeStructureReferenceDock({
  projectId,
  documentId,
  sourceSha256,
  onPageChange,
  onAvailabilityChange,
  onPageCountChange,
  sourceFileUrl = "",
  activePage = 1,
  showSourcePreview = false,
  onAdvancementChange,
  mode = "STRUCTURE",
  refreshToken = 0,
  baselineRefreshSha = "",
  purpose = "review",
  onChangePurpose,
  initialReasoningState,
  sourceReader,
}: NativeStructureReferenceDockProps) {
  const host = useWorkbenchHost();
  const viewerUrl = host.pdfViewerUrl(sourceFileUrl, 1, true);
  const viewerOrigin = new URL(viewerUrl, window.location.origin).origin;
  const documentAgent = useDocumentAgent();
  const [reviewPresentation, setReviewPresentation] = useState<"table" | "source">(() => documentAgent ? "table" : "source");

  const [reference, setReference] =
    useState<NativeStructureReferencePublicV1 | null>(null);
  const [workingRevisionHead, setWorkingRevisionHead] =
    useState<NativeStructureWorkingRevisionHeadV1 | null>(null);
  const [workingRevision, setWorkingRevision] =
    useState<NativeStructureWorkingRevisionV1 | null>(null);
  const [workingRevisionNotice, setWorkingRevisionNotice] = useState("");
  const [workingRevisionAnnouncement, setWorkingRevisionAnnouncement] =
    useState("");
  const [workingRevisionRetryGeneration, setWorkingRevisionRetryGeneration] =
    useState(0);
  const [loading, setLoading] = useState(true);
  const [referenceResolved, setReferenceResolved] = useState(false);
  const [notAvailable, setNotAvailable] = useState(false);
  const [error, setError] = useState("");
  const [selectedNodeId, setSelectedNodeId] = useState("");
  const [nativeFocusIds, setNativeFocusIds] = useState<string[]>([]);
  useEffect(() => setNativeFocusIds([]), [projectId, documentId, selectedNodeId]);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [draftsByNode, setDraftsByNode] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [continuing, setContinuing] = useState(false);
  const [liveActivity, setLiveActivity] = useState<
    NativeStructureActivityEventV2[]
  >([]);
  const [activitySyncNotice, setActivitySyncNotice] = useState("");
  const [feedbackNotice, setFeedbackNotice] = useState("");
  const [continueNotice, setContinueNotice] = useState("");
  const [proposalMutation, setProposalMutation] = useState("");
  const [proposalNotice, setProposalNotice] = useState("");
  const [focusedProposalIds, setFocusedProposalIds] = useState<string[]>([]);
  const [focusedEvidenceOverlay, setFocusedEvidenceOverlay] = useState<{
    id: string;
    page: number;
    type: "TEXT" | "TABLE";
    label: string;
    bbox: [number, number, number, number];
    sourceBounds: [number, number, number, number];
  } | null>(null);
  const [tablePreviewsById, setTablePreviewsById] = useState<
    Record<string, NativeStructureTablePreviewV1>
  >({});
  const [tablePreviewLoadingIds, setTablePreviewLoadingIds] = useState<Set<string>>(
    new Set(),
  );
  const [tablePreviewErrors, setTablePreviewErrors] = useState<Record<string, string>>(
    {},
  );
  const [sourceRequirementOutline, setSourceRequirementOutline] =
    useState<SourceRequirementBaselineOutlineV1 | null>(null);
  const nativeReasoning = useSourceItemReasoning(projectId, documentId,
    !sourceReader && mode === "SOURCE_REQUIREMENTS" ? sourceRequirementOutline?.baseline.artifact_sha256 || "" : "", initialReasoningState);
  const sourceItemReasoning = sourceReader ? { ...nativeReasoning, state: sourceReader.reasoning ? { ...sourceReader.reasoning, analysis_policy: sourceReader.analysis_policy } : null } : nativeReasoning;
  const previewReader = useMemo<SourcePreviewReader | undefined>(() => sourceRequirementOutline ? {
    scope: `${projectId}:${documentId}:${sourceSha256}:${sourceReader?.revision_id || sourceRequirementOutline.baseline.artifact_sha256}`,
    sourceSha256, baselineSha256: sourceRequirementOutline.baseline.artifact_sha256,
    mapSha256: sourceRequirementOutline.document_map.artifact_sha256, cache: new Map(),
    loadRecord: sourceReader?.loadRecord || ((id, signal) => api.sourceRequirementRecord(projectId, documentId, id, signal)),
  } : undefined, [projectId, documentId, sourceSha256, sourceRequirementOutline?.baseline.artifact_sha256, sourceRequirementOutline?.document_map.artifact_sha256, sourceReader?.revision_id, sourceReader?.loadRecord]);
  const [selectedSourceItemId, setSelectedSourceItemId] = useState("");
  const [selectedDerived, setSelectedDerived] = useState<{ sourceId: string; unitId: string; baseline: string } | null>(null);
  const restoredDerivedSelectionRef = useRef("");
  const [collapsedDerivedIds, setCollapsedDerivedIds] = useState<Set<string>>(new Set());
  const positionKey = `reqpilotx:source-position:v1:${projectId}:${documentId}:${sourceSha256}`;
  const [sourceRequirementError, setSourceRequirementError] = useState("");
  const [sourceRequirementRunStatus, setSourceRequirementRunStatus] =
    useState<SourceRequirementBaselineRunStatusV1 | null>(null);
  const [sourceRequirementRunError, setSourceRequirementRunError] = useState("");
  const [sourceRequirementRunStartingScopeId, setSourceRequirementRunStartingScopeId] =
    useState("");
  const [sourceRequirementRunRefreshToken, setSourceRequirementRunRefreshToken] =
    useState(0);
  const [sourceRequirementRefreshToken, setSourceRequirementRefreshToken] =
    useState(0);
  const nativeReviewRequest = useSourceItemReviewContext(projectId, documentId,
    !sourceReader && mode === "SOURCE_REQUIREMENTS" ? sourceRequirementOutline?.baseline.artifact_sha256 || "" : "", sourceRequirementRefreshToken + refreshToken);
  const reviewRequest = sourceReader ? { value: sourceReader.review, error: "" } : nativeReviewRequest;
  const [reviewQuery, setReviewQuery] = useState("");
  const deferredReviewQuery = useDeferredValue(reviewQuery);
  const [reviewView, setReviewView] = useState<ReviewFilter>("ALL");
  const [analysisQueue, setAnalysisQueue] = useState<AnalysisQueue>("ALL");
  const [reviewCategory, setReviewCategory] = useState("");
  const [reviewTopic, setReviewTopic] = useState("");
  const [reviewMore, setReviewMore] = useState(false);
  const [filterCollapsed, setFilterCollapsed] = useState<Set<string>>(new Set());
  const reviewSearchRef = useRef<HTMLInputElement | null>(null);
  const [sourceRequirementOverlays, setSourceRequirementOverlays] = useState<
    SourceRequirementEvidenceOverlay[]
  >([]);
  const [columnShares, setColumnShares] = useState<StructureColumnShares>(() =>
    readColumnShares(projectId, documentId),
  );
  const [compactPanel, setCompactPanel] = useState<"content" | "source">(
    "content",
  );
  const [wideReading, setWideReading] = useState(false);
  const [pageAtlasOpen, setPageAtlasOpen] = useState(false);
  const pdfFrameRef = useRef<HTMLIFrameElement | null>(null);
  const treeRef = useRef<HTMLUListElement | null>(null);
  const treeViewportRef = useRef<HTMLElement | null>(null);
  const workbenchRef = useRef<HTMLDivElement | null>(null);
  const resizeDragRef = useRef<{
    splitter: StructureSplitter;
    pointerId: number;
    startX: number;
    totalWidth: number;
    treeWidth: number;
    detailWidth: number;
    sourceWidth: number;
  } | null>(null);
  const resizeFrameRef = useRef<number | null>(null);
  const activityCursorRef = useRef(0);
  const activityStatusRef = useRef("");
  const activityProjectionLifecycleRef = useRef("");
  const handoffMismatchBackoffMsRef = useRef(3_000);
  const authoritativeReferenceRef =
    useRef<NativeStructureReferencePublicV1 | null>(null);
  const workingRevisionHeadRef =
    useRef<NativeStructureWorkingRevisionHeadV1 | null>(null);
  const modeRef = useRef(mode);
  const workingRevisionRef =
    useRef<NativeStructureWorkingRevisionV1 | null>(null);
  const workingRevisionRequestRef = useRef<{
    key: string;
    controller: AbortController;
    promise: Promise<NativeStructureWorkingRevisionV1>;
  } | null>(null);
  const consumedWorkingRevisionKeysRef = useRef<Set<string>>(new Set());
  const workingRevisionFailureCountRef = useRef<Map<string, number>>(new Map());
  const workingRevisionRetryTimerRef = useRef<number | null>(null);
  const treeUiStateRef = useRef({ selectedNodeId: "", expandedIds: new Set<string>() });
  const pendingTreeAnchorRef = useRef<{
    nodeId: string;
    relativeTop: number;
    hadTreeFocus: boolean;
    focusedNodeId: string;
    successorNodeIds: Record<string, string[]>;
    previousNodes: NativeStructureTreeNodeV1[];
  } | null>(null);
  const renderAckAttemptRef = useRef("");
  const autoOpenedAttemptIdsRef = useRef<Set<string>>(new Set());
  const sourceRequirementAutoSelectionRef = useRef("");
  const sourceRequirementCompletedJobRef = useRef("");
  const sourceRequirementRunDocumentKeyRef = useRef("");
  const previousTreeNodesRef = useRef<NativeStructureTreeNodeV1[]>([]);
  const onAvailabilityChangeRef = useRef(onAvailabilityChange);
  const onPageCountChangeRef = useRef(onPageCountChange);
  const authoritativeRefreshContextKey = `${projectId}:${documentId}:${sourceSha256}`;
  const authoritativeRefreshStateRef = useRef<{
    contextKey: string;
    requestedKeys: Set<string>;
    chain: Promise<NativeStructureReferencePublicV1 | null>;
  }>({
    contextKey: authoritativeRefreshContextKey,
    requestedKeys: new Set(),
    chain: Promise.resolve(null),
  });
  if (
    authoritativeRefreshStateRef.current.contextKey !==
    authoritativeRefreshContextKey
  ) {
    authoritativeRefreshStateRef.current = {
      contextKey: authoritativeRefreshContextKey,
      requestedKeys: new Set(),
      chain: Promise.resolve(null),
    };
  }
  modeRef.current = mode;

  useEffect(() => {
    onAvailabilityChangeRef.current = onAvailabilityChange;
    onPageCountChangeRef.current = onPageCountChange;
  }, [onAvailabilityChange, onPageCountChange]);

  const refreshAuthoritativeReference = useCallback(
    (
      refreshKey: string,
      targetSatisfied: (value: NativeStructureReferencePublicV1) => boolean,
    ): Promise<NativeStructureReferencePublicV1 | null> => {
      const state = authoritativeRefreshStateRef.current;
      const current = authoritativeReferenceRef.current;
      if (current && targetSatisfied(current)) return Promise.resolve(current);
      if (state.requestedKeys.has(refreshKey)) return state.chain;
      state.requestedKeys.add(refreshKey);
      state.chain = state.chain
        .catch(() => null)
        .then(async () => {
          const latest = authoritativeReferenceRef.current;
          if (latest && targetSatisfied(latest)) return latest;
          for (let attempt = 0; attempt < 2; attempt += 1) {
            if (attempt > 0) {
              await new Promise((resolve) => window.setTimeout(resolve, 250));
              if (authoritativeRefreshStateRef.current !== state) return null;
            }
            try {
              const result = await api.nativeStructureReference(
                projectId,
                documentId,
              );
              if (authoritativeRefreshStateRef.current !== state) return null;
              if (result.source_sha256 !== sourceSha256) {
                setActivitySyncNotice(
                  "运行状态已切换到新的文档版本，请刷新后继续。",
                );
                return null;
              }
              authoritativeReferenceRef.current = result;
              activityProjectionLifecycleRef.current =
                nativeStructureProjectionLifecycleKey(result.continuation);
              setReference(result);
              setNotAvailable(false);
              onAvailabilityChangeRef.current?.(true);
              onPageCountChangeRef.current?.(
                Math.max(0, result.visual_summary.page_count),
              );
              if (targetSatisfied(result)) {
                setActivitySyncNotice("");
                return result;
              }
              setActivitySyncNotice(
                attempt === 0
                  ? "最新结构投影仍在生成，正在重试；当前显示仍是上一版。"
                  : "最新结构投影尚未就绪，当前显示仍是已读取的最近版本。",
              );
            } catch (caught) {
              if (authoritativeRefreshStateRef.current !== state) return null;
              setActivitySyncNotice(
                attempt === 0
                  ? "最新结构投影同步暂时延迟，正在重试；当前显示仍是上一版。"
                  : caught instanceof ApiError
                    ? "最新结构投影同步仍未恢复，当前显示仍是上一版；原文核对和其他节点不受影响。"
                    : "最新结构投影暂时未更新，当前显示仍是上一版；原文核对和其他节点不受影响。",
              );
            }
          }
          state.requestedKeys.delete(refreshKey);
          return null;
        });
      return state.chain;
    },
    [documentId, projectId, sourceSha256],
  );

  useEffect(() => {
    if (sourceReader || (!refreshToken && !baselineRefreshSha)) return;
    setSourceRequirementRefreshToken(value => value + 1);
    if (!authoritativeReferenceRef.current) {
      void refreshAuthoritativeReference(`source-graph:${refreshToken}`, () => true).then(value => {
        if (value && !treeUiStateRef.current.selectedNodeId) {
          const first = value.document_tree.root_ids[0] || "";
          setSelectedNodeId(first); setExpandedIds(first ? new Set([first]) : new Set());
        }
      });
    }
  }, [refreshToken, baselineRefreshSha, refreshAuthoritativeReference]);

  useEffect(() => {
    const controller = new AbortController();
    workingRevisionRequestRef.current?.controller.abort();
    workingRevisionRequestRef.current = null;
    consumedWorkingRevisionKeysRef.current = new Set();
    workingRevisionFailureCountRef.current = new Map();
    if (workingRevisionRetryTimerRef.current !== null) {
      window.clearTimeout(workingRevisionRetryTimerRef.current);
      workingRevisionRetryTimerRef.current = null;
    }
    workingRevisionHeadRef.current = null;
    workingRevisionRef.current = null;
    setWorkingRevisionHead(null);
    setWorkingRevision(null);
    setWorkingRevisionNotice("");
    setWorkingRevisionAnnouncement("");
    setWorkingRevisionRetryGeneration(0);
    pendingTreeAnchorRef.current = null;
    setLoading(true);
    setReferenceResolved(false);
    setNotAvailable(false);
    setError("");
    setSelectedNodeId("");
    setExpandedIds(new Set());
    setDraftsByNode({});
    setFeedbackNotice("");
    setContinueNotice("");
    setLiveActivity([]);
    setActivitySyncNotice("");
    activityCursorRef.current = 0;
    activityProjectionLifecycleRef.current = "";
    authoritativeReferenceRef.current = null;
    renderAckAttemptRef.current = "";
    autoOpenedAttemptIdsRef.current = new Set();
    setProposalMutation("");
    setProposalNotice("");
    setFocusedProposalIds([]);
    setFocusedEvidenceOverlay(null);
    setTablePreviewsById({});
    setTablePreviewLoadingIds(new Set());
    setTablePreviewErrors({});
    setSourceRequirementOverlays([]);
    setSelectedSourceItemId("");
    setColumnShares(readColumnShares(projectId, documentId));
    setCompactPanel("content");
    setPageAtlasOpen(false);
    onPageCountChangeRef.current?.(0);
    void (sourceReader ? Promise.resolve(sourceReader.reference) : api.nativeStructureReference(projectId, documentId, controller.signal))
      .then((result) => {
        if (controller.signal.aborted) return;
        if (result.source_sha256 !== sourceSha256) {
          setNotAvailable(true);
          setReference(null);
          onAvailabilityChangeRef.current?.(false);
          onPageCountChangeRef.current?.(0);
          return;
        }
        const firstRoot = result.document_tree.root_ids[0] || "";
        authoritativeReferenceRef.current = result;
        activityProjectionLifecycleRef.current =
          nativeStructureProjectionLifecycleKey(result.continuation);
        setReference(result);
        setSelectedNodeId(firstRoot);
        setExpandedIds(firstRoot ? new Set([firstRoot]) : new Set());
        onAvailabilityChangeRef.current?.(true);
        onPageCountChangeRef.current?.(
          Math.max(0, result.visual_summary.page_count),
        );
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        if (caught instanceof ApiError && caught.status === 404) {
          setNotAvailable(true);
          setReference(null);
          onAvailabilityChangeRef.current?.(false);
          onPageCountChangeRef.current?.(0);
          return;
        }
        setError(errorMessage(caught));
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
          setReferenceResolved(true);
        }
      });
    return () => controller.abort();
  }, [
    documentId,
    projectId,
    sourceSha256,
    sourceReader?.revision_id,
  ]);

  useEffect(() => {
    if (sourceReader || mode !== "STRUCTURE") return;
    const controller = new AbortController();
    let timer: number | null = null;
    const poll = async () => {
      try {
        const response = await api.nativeStructureWorkingRevisionHead(
          projectId,
          documentId,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        if (
          response.schema_version !==
            "native-structure-working-revision-head-response/v1" ||
          response.source_sha256 !== sourceSha256
        ) {
          setWorkingRevisionNotice(
            "工作结构状态属于其他文件版本；当前继续显示最近有效结构。",
          );
          timer = window.setTimeout(poll, 3_000);
          return;
        }
        const head = response.head;
        if (head) {
          if (
            head.source_sha256 !== sourceSha256 ||
            !AGENT_RUN_ID_PATTERN.test(head.run_id) ||
            !WORKING_REVISION_ID_PATTERN.test(head.revision_id) ||
            !SHA256_PATTERN.test(head.revision_sha256) ||
            !SHA256_PATTERN.test(head.base_projection_sha256) ||
            (head.previous_revision_sha256 !== null &&
              !SHA256_PATTERN.test(head.previous_revision_sha256)) ||
            !head.evidence_artifact_id ||
            !["ACCEPTED_REFERENCE_REPLAY", "D2_HOST_COMPILER"].includes(
              head.validator_kind,
            ) ||
            head.quality_claim !==
              (head.validator_kind === "ACCEPTED_REFERENCE_REPLAY"
                ? "TRANSPORT_REPLAY_ONLY"
                : "D2_HOST_VERIFIED") ||
            !head.created_at_utc ||
            head.revision < 1
          ) {
            setWorkingRevisionNotice(
              "工作结构状态身份不完整；当前继续显示最近有效结构。",
            );
            timer = window.setTimeout(poll, 3_000);
            return;
          }
          const previous = workingRevisionHeadRef.current;
          const sameLineage =
            previous &&
            previous.run_id === head.run_id &&
            previous.source_sha256 === head.source_sha256 &&
            previous.base_artifact_id === head.base_artifact_id &&
            previous.base_projection_sha256 === head.base_projection_sha256;
          if (
            sameLineage &&
            (head.revision < previous.revision ||
              (head.revision === previous.revision &&
                (head.revision_sha256 !== previous.revision_sha256 ||
                  head.state !== previous.state)))
          ) {
            setWorkingRevisionNotice(
              "工作结构 head 出现回退或同身份漂移；当前继续显示最近有效结构。",
            );
            if (previous.state === "WORKING") {
              timer = window.setTimeout(poll, 3_000);
            }
            return;
          }
          if (
            !previous ||
            workingRevisionKey(previous) !== workingRevisionKey(head) ||
            previous.state !== head.state
          ) {
            workingRevisionHeadRef.current = head;
            setWorkingRevisionHead(head);
          }
          if (head.state === "WORKING") {
            timer = window.setTimeout(poll, 1_500);
          }
        } else if (workingRevisionHeadRef.current?.state !== "ACCEPTED") {
          timer = window.setTimeout(poll, 3_000);
        }
      } catch (caught) {
        if (controller.signal.aborted) return;
        if (!(caught instanceof ApiError && caught.status === 404)) {
          setWorkingRevisionNotice(
            "工作结构状态暂时无法同步；当前继续显示最近有效结构。",
          );
        }
        if (
          !(caught instanceof ApiError && caught.status === 404) &&
          workingRevisionHeadRef.current?.state !== "ACCEPTED"
        ) {
          timer = window.setTimeout(poll, 3_000);
        }
      }
    };
    void poll();
    return () => {
      controller.abort();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [
    documentId,
    mode,
    projectId,
    reference?.artifact_id,
    reference?.structure_projection.projection_sha256,
    reference?.structure_projection.state,
    sourceSha256,
  ]);

  useEffect(() => {
    if (
      mode !== "STRUCTURE" ||
      !referenceResolved ||
      !workingRevisionHead ||
      reference?.structure_projection.state ===
        "WORKING_VERIFICATION_REQUIRED"
    ) {
      return;
    }
    const key = workingRevisionKey(workingRevisionHead);
    if (
      consumedWorkingRevisionKeysRef.current.has(key) ||
      workingRevisionRef.current?.revision_sha256 ===
        workingRevisionHead.revision_sha256
    ) {
      return;
    }
    let request = workingRevisionRequestRef.current;
    if (request) {
      // The request owner below is also the sole settlement owner. Reusing the
      // same Promise here would attach a second handler that validates the
      // just-applied revision against itself and reports a false chain break.
      return;
    }
    const controller = new AbortController();
    const currentAtStart = workingRevisionRef.current;
    if (
      currentAtStart &&
      (currentAtStart.run_id !== workingRevisionHead.run_id ||
        currentAtStart.base_artifact_id !==
          workingRevisionHead.base_artifact_id ||
        currentAtStart.base_projection_sha256 !==
          workingRevisionHead.base_projection_sha256)
    ) {
      setWorkingRevisionNotice(
        "工作结构运行身份发生冲突；当前继续显示最近有效结构。",
      );
      return;
    }
    request = {
      key,
      controller,
      promise: api.nativeStructureWorkingRevision(
        projectId,
        documentId,
        workingRevisionHead.run_id,
        workingRevisionHead.revision,
        workingRevisionHead.revision_sha256,
        controller.signal,
      ),
    };
    workingRevisionRequestRef.current = request;
    const requestedHead = workingRevisionHead;
    const requested = request;
    void requested.promise
      .then((revision) => {
        const latestHead = workingRevisionHeadRef.current || requestedHead;
        const stillSameLineage =
          latestHead.run_id === requestedHead.run_id &&
          latestHead.source_sha256 === requestedHead.source_sha256 &&
          latestHead.base_artifact_id === requestedHead.base_artifact_id &&
          latestHead.base_projection_sha256 ===
            requestedHead.base_projection_sha256 &&
          latestHead.revision >= requestedHead.revision;
        if (requested.controller.signal.aborted || !stillSameLineage) {
          return;
        }
        const validationError = workingRevisionValidationError({
          expectedSourceSha256: sourceSha256,
          acceptedReference: authoritativeReferenceRef.current,
          currentRevision: workingRevisionRef.current,
          head: requestedHead,
          revision,
        });
        if (validationError) throw new Error(validationError);
        consumedWorkingRevisionKeysRef.current.add(key);
        workingRevisionFailureCountRef.current.delete(key);
        if (modeRef.current !== "STRUCTURE") {
          workingRevisionRef.current = revision;
          setWorkingRevision(revision);
          return;
        }

        const lineagePreviousNodes =
          workingRevisionRef.current?.document_tree.nodes || [];
        const revisionDiff = nativeTreeNodeDiff(
          lineagePreviousNodes,
          revision.document_tree.nodes,
        );
        const previousNodes =
          workingRevisionRef.current?.document_tree.nodes ||
          authoritativeReferenceRef.current?.document_tree.nodes ||
          [];
        const viewport = treeViewportRef.current;
        const viewportRect = viewport?.getBoundingClientRect();
        const rowElements = viewport
          ? Array.from(
              viewport.querySelectorAll<HTMLElement>("[data-tree-node]"),
            )
          : [];
        const anchorElement = viewportRect
          ? rowElements.find(
              (element) => element.getBoundingClientRect().bottom > viewportRect.top,
            ) || rowElements[0]
          : undefined;
        const activeElement =
          document.activeElement instanceof HTMLElement
            ? document.activeElement.closest<HTMLElement>("[data-tree-node]")
            : null;
        pendingTreeAnchorRef.current = anchorElement && viewportRect
          ? {
              nodeId: anchorElement.dataset.treeNode || "",
              relativeTop:
                anchorElement.getBoundingClientRect().top - viewportRect.top,
              hadTreeFocus: Boolean(
                activeElement && viewport?.contains(activeElement),
              ),
              focusedNodeId: activeElement?.dataset.treeNode || "",
              successorNodeIds: revision.successor_node_ids,
              previousNodes: [...previousNodes],
            }
          : null;
        const reconciled = reconcileNativeTreeUiState({
          previousNodes,
          nextTree: revision.document_tree,
          selectedNodeId: treeUiStateRef.current.selectedNodeId,
          expandedIds: treeUiStateRef.current.expandedIds,
          successorNodeIds: revision.successor_node_ids,
        });
        setSelectedNodeId(reconciled.selectedNodeId);
        setSelectedSourceItemId("");
        setExpandedIds(reconciled.expandedIds);
        workingRevisionRef.current = revision;
        setWorkingRevision(revision);
        setWorkingRevisionNotice("");
        setWorkingRevisionAnnouncement(
          `结构已更新：新增 ${revisionDiff.inserted.length} 项，调整 ${revisionDiff.changed.length} 项，移除 ${revisionDiff.retired.length} 项。`,
        );
        setTablePreviewsById({});
        setTablePreviewLoadingIds(new Set());
        setTablePreviewErrors({});
        setFocusedProposalIds([]);
        setFocusedEvidenceOverlay(null);
        onAvailabilityChangeRef.current?.(true);
        onPageCountChangeRef.current?.(
          Math.max(0, revision.visual_summary.page_count),
        );
      })
      .catch((caught) => {
        if (requested.controller.signal.aborted) return;
        const failureCount =
          (workingRevisionFailureCountRef.current.get(key) || 0) + 1;
        workingRevisionFailureCountRef.current.set(key, failureCount);
        setWorkingRevisionNotice(
          `${errorMessage(caught)} 当前继续显示最近有效结构。`,
        );
        if (workingRevisionRetryTimerRef.current !== null) {
          window.clearTimeout(workingRevisionRetryTimerRef.current);
        }
        workingRevisionRetryTimerRef.current = window.setTimeout(() => {
          workingRevisionRetryTimerRef.current = null;
          setWorkingRevisionRetryGeneration((value) => value + 1);
        }, Math.min(4_000, 500 * 2 ** (failureCount - 1)));
      })
      .finally(() => {
        if (workingRevisionRequestRef.current === requested) {
          workingRevisionRequestRef.current = null;
          if (
            workingRevisionKey(workingRevisionHeadRef.current || requestedHead) !==
            key
          ) {
            setWorkingRevisionRetryGeneration((value) => value + 1);
          }
        }
      });
  }, [
    documentId,
    mode,
    projectId,
    reference?.structure_projection.state,
    referenceResolved,
    sourceSha256,
    workingRevisionHead,
    workingRevisionRetryGeneration,
  ]);

  useEffect(() => {
    if (mode !== "SOURCE_REQUIREMENTS") {
      setSourceRequirementOutline(null);
      setSourceRequirementError("");
      setSelectedSourceItemId("");
      sourceRequirementAutoSelectionRef.current = "";
      return;
    }
    const controller = new AbortController();
    setSourceRequirementError("");
    void (sourceReader ? Promise.resolve(sourceReader.outline!) : api.sourceRequirementBaselineOutline(projectId, documentId, controller.signal))
      .then((value) => {
        if (controller.signal.aborted) return;
        if (value.source_sha256 !== sourceSha256) {
          setSourceRequirementOutline(null);
          setSourceRequirementError("原文需求清单属于旧文件版本，请重新生成当前切片。");
          return;
        }
        setSourceRequirementOutline(value);
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        setSourceRequirementOutline(null);
        setSourceRequirementError(
          caught instanceof ApiError && caught.status === 404
            ? "Agent 尚未发布原文需求清单切片。"
            : errorMessage(caught),
        );
      });
    return () => controller.abort();
  }, [
    documentId,
    mode,
    projectId,
    sourceRequirementRefreshToken,
    refreshToken,
    sourceSha256,
    sourceReader?.revision_id,
    sourceReader?.outline,
  ]);

  useEffect(() => {
    if (sourceReader) return;
    const documentKey = `${projectId}:${documentId}:${sourceSha256}`;
    if (sourceRequirementRunDocumentKeyRef.current !== documentKey) {
      sourceRequirementRunDocumentKeyRef.current = documentKey;
      sourceRequirementCompletedJobRef.current = "";
    }
    if (mode !== "SOURCE_REQUIREMENTS") {
      setSourceRequirementRunStatus(null);
      setSourceRequirementRunError("");
      setSourceRequirementRunStartingScopeId("");
      return;
    }
    const controller = new AbortController();
    let timer: number | null = null;
    const poll = async () => {
      try {
        const value = await api.sourceRequirementBaselineRunStatus(
          projectId,
          documentId,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setSourceRequirementRunStatus(value);
        setSourceRequirementRunError("");
        const jobStatus = value.job?.status || value.status;
        if (
          jobStatus === "SUCCEEDED" &&
          value.job?.job_id &&
          sourceRequirementCompletedJobRef.current !== value.job.job_id
        ) {
          sourceRequirementCompletedJobRef.current = value.job.job_id;
          setSourceRequirementRefreshToken((current) => current + 1);
        }
        if (["PENDING", "RUNNING", "CANCEL_REQUESTED"].includes(jobStatus)) {
          timer = window.setTimeout(poll, 1_500);
        }
      } catch (caught) {
        if (controller.signal.aborted) return;
        setSourceRequirementRunError(
          caught instanceof ApiError
            ? caught.message
            : "Agent 运行状态暂时无法同步；当前清单仍可浏览。",
        );
        timer = window.setTimeout(poll, 3_000);
      }
    };
    void poll();
    return () => {
      controller.abort();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [
    documentId,
    mode,
    projectId,
    sourceRequirementRunRefreshToken,
    sourceSha256,
  ]);

  useEffect(() => {
    if (
      mode !== "SOURCE_REQUIREMENTS" ||
      !reference ||
      !sourceRequirementOutline?.records.length
    ) {
      return;
    }
    const selectionKey = `${sourceRequirementOutline.baseline.artifact_id}:${documentId}`;
    if (sourceRequirementAutoSelectionRef.current === selectionKey) return;
    let saved: { sourceItemId?: string; nodeId?: string } = {};
    try { const value = JSON.parse(localStorage.getItem(positionKey) || "{}"); if (value && typeof value === "object") saved = value; } catch { /* First visit or unavailable local storage. */ }
    const firstSourceItem = sourceRequirementOutline.records.find(item => item.source_record_id === saved.sourceItemId)
      || sourceRequirementOutline.records.find(item => item.requirementness === "REQUIREMENT")
      || sourceRequirementOutline.records[0];
    const savedStructure = saved.sourceItemId === "" ? reference.document_tree.nodes.find(node => node.node_id === saved.nodeId) : undefined;
    const firstNodeId = savedStructure?.node_id || firstSourceItem.node_id;
    const firstNode = savedStructure || reference.document_tree.nodes.find(node => node.node_id === firstNodeId);
    if (!firstNode) return;
    sourceRequirementAutoSelectionRef.current = selectionKey;
    setSelectedNodeId(firstNodeId);
    setSelectedSourceItemId(savedStructure ? "" : firstSourceItem.source_record_id);
    setExpandedIds((current) => {
      const next = new Set(current);
      next.add(firstNodeId);
      const byId = new Map(
        reference.document_tree.nodes.map((node) => [node.node_id, node]),
      );
      let cursor = firstNode.parent_id ? byId.get(firstNode.parent_id) : undefined;
      while (cursor) {
        next.add(cursor.node_id);
        cursor = cursor.parent_id ? byId.get(cursor.parent_id) : undefined;
      }
      return next;
    });
    const firstPage = savedStructure ? firstNode.pages[0] : firstSourceItem.pages[0];
    if (firstPage) onPageChange(firstPage);
  }, [
    documentId,
    mode,
    onPageChange,
    reference,
    sourceRequirementOutline,
    positionKey,
  ]);

  const activityHandoffId =
    reference?.continuation.latest_handoff?.handoff_id || "";
  const activityArtifactId = reference?.artifact_id || "";
  const activitySourceSha256 = reference?.source_sha256 || "";
  activityStatusRef.current = reference?.continuation.status || "";

  useEffect(() => {
    onAdvancementChange?.({
      available: Boolean(reference),
      advancementAllowed: reference?.continuation.advancement_allowed === true,
      sourceSha256: reference?.source_sha256 || "",
      artifactId: reference?.artifact_id || "",
      continuationStatus: reference?.continuation.status || "IDLE",
    });
  }, [onAdvancementChange, reference]);

  useEffect(() => {
    if (sourceReader) return;
    if (!activityArtifactId || !activityHandoffId || !activitySourceSha256) return;
    const controller = new AbortController();
    let timer: number | null = null;
    activityCursorRef.current = 0;
    handoffMismatchBackoffMsRef.current = 3_000;
    setLiveActivity([]);
    setActivitySyncNotice("");

    const poll = async () => {
      try {
        const stream = await api.nativeStructureActivity(
          projectId,
          documentId,
          activityCursorRef.current,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        if (
          stream.source_sha256 !== activitySourceSha256 ||
          stream.artifact_id !== activityArtifactId
        ) {
          setActivitySyncNotice("运行状态已切换到新的文档版本，请刷新后继续。");
          return;
        }
        const activityHead = stream.working_revision_head;
        if (
          modeRef.current === "STRUCTURE" &&
          activityHead &&
          activityHead.source_sha256 === sourceSha256 &&
          AGENT_RUN_ID_PATTERN.test(activityHead.run_id) &&
          SHA256_PATTERN.test(activityHead.revision_sha256)
        ) {
          const currentHead = workingRevisionHeadRef.current;
          if (
            !currentHead ||
            currentHead.run_id !== activityHead.run_id ||
            activityHead.revision > currentHead.revision
          ) {
            workingRevisionHeadRef.current = activityHead;
            setWorkingRevisionHead(activityHead);
          }
        }
        if (stream.handoff_id !== activityHandoffId) {
          setActivitySyncNotice(
            "检测到其他客户端推进了当前结构任务，正在同步最新工作投影。",
          );
          const refreshed = await refreshAuthoritativeReference(
            `handoff-promoted-from:${activityHandoffId}:to:${stream.handoff_id}`,
            (value) =>
              Boolean(value.continuation.latest_handoff?.handoff_id) &&
              value.continuation.latest_handoff?.handoff_id !==
                activityHandoffId,
          );
          if (controller.signal.aborted) return;
          if (refreshed) {
            handoffMismatchBackoffMsRef.current = 3_000;
            return;
          }
          const retryAfterMs = handoffMismatchBackoffMsRef.current;
          handoffMismatchBackoffMsRef.current = Math.min(
            30_000,
            retryAfterMs * 2,
          );
          setActivitySyncNotice(
            `最新工作投影暂时不可用；当前仍显示上一版，将在 ${retryAfterMs / 1_000} 秒后再次检查运行状态。`,
          );
          timer = window.setTimeout(poll, retryAfterMs);
          return;
        }
        handoffMismatchBackoffMsRef.current = 3_000;
        activityCursorRef.current = stream.cursor;
        const nextProjectionLifecycle =
          nativeStructureProjectionLifecycleKey(stream.continuation);
        const projectionLifecycleChanged =
          Boolean(activityProjectionLifecycleRef.current) &&
          activityProjectionLifecycleRef.current !== nextProjectionLifecycle;
        if (stream.events.length) {
          setLiveActivity((current) => {
            const byId = new Map(current.map((event) => [event.event_id, event]));
            for (const event of stream.events) byId.set(event.event_id, event);
            return Array.from(byId.values()).sort(
              (left, right) => left.sequence - right.sequence,
            );
          });
        }
        setReference((current) =>
          current && current.artifact_id === stream.artifact_id
            ? {
                ...current,
                continuation: stream.continuation,
                feedback_capability: {
                  ...current.feedback_capability,
                  agent_delivery: stream.continuation.status,
                  message: stream.continuation.message,
                },
              }
            : current,
        );
        setActivitySyncNotice("");
        if (projectionLifecycleChanged) {
          await refreshAuthoritativeReference(
            `continuation:${nextProjectionLifecycle}`,
            (value) =>
              nativeStructureProjectionLifecycleKey(value.continuation) ===
              nextProjectionLifecycle,
          );
        }
        if (
          [
            "QUEUED_FOR_CODEX_APP",
            "ACKNOWLEDGED",
            "IN_PROGRESS",
            "VERIFYING",
            "REVISION_REQUIRED",
          ].includes(stream.continuation.status)
        ) {
          timer = window.setTimeout(poll, 1500);
        }
      } catch (caught) {
        if (controller.signal.aborted) return;
        setActivitySyncNotice(
          caught instanceof ApiError
            ? "运行状态同步暂时延迟，原文核对和其他节点不受影响。"
            : "运行状态暂时未更新，原文核对和其他节点不受影响。",
        );
        if (
          [
            "QUEUED_FOR_CODEX_APP",
            "ACKNOWLEDGED",
            "IN_PROGRESS",
            "VERIFYING",
            "REVISION_REQUIRED",
          ].includes(activityStatusRef.current)
        ) {
          timer = window.setTimeout(poll, 3000);
        }
      }
    };

    void poll();
    return () => {
      controller.abort();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [
    activityArtifactId,
    activityHandoffId,
    activitySourceSha256,
    documentId,
    projectId,
    refreshAuthoritativeReference,
  ]);

  useEffect(
    () => () => {
      workingRevisionRequestRef.current?.controller.abort();
      if (workingRevisionRetryTimerRef.current !== null) {
        window.clearTimeout(workingRevisionRetryTimerRef.current);
      }
      if (resizeFrameRef.current !== null) {
        cancelAnimationFrame(resizeFrameRef.current);
      }
    },
    [],
  );

  treeUiStateRef.current = { selectedNodeId, expandedIds };
  const workingRevisionMatchesReference = Boolean(
    workingRevision &&
      (!reference ||
        (workingRevisionMatchesBasis(workingRevision, reference) &&
          reference.structure_projection.state === "ACCEPTED")),
  );
  const displayWorkingRevision =
    mode === "STRUCTURE" && workingRevisionMatchesReference
      ? workingRevision
      : null;
  const tree = displayWorkingRevision?.document_tree || reference?.document_tree;
  const activePageOwnership =
    displayWorkingRevision?.page_ownership || reference?.page_ownership || [];
  const activeLogicalTables =
    displayWorkingRevision?.logical_tables || reference?.logical_tables || [];
  const activeStructureProposals =
    displayWorkingRevision?.structure_proposals ||
    reference?.structure_proposals ||
    [];
  const activeUnresolved =
    displayWorkingRevision?.unresolved || reference?.unresolved || [];
  const activeVisualSummary =
    displayWorkingRevision?.visual_summary || reference?.visual_summary || {
      opened_page_count: 0,
      page_count: 0,
      receipts: [],
    };
  useEffect(() => {
    setTablePreviewsById({});
    setTablePreviewLoadingIds(new Set());
    setTablePreviewErrors({});
  }, [displayWorkingRevision?.revision_sha256, mode]);
  useEffect(() => {
    if (!workingRevision || !reference || workingRevisionMatchesReference) return;
    workingRevisionRequestRef.current?.controller.abort();
    workingRevisionRequestRef.current = null;
    workingRevisionHeadRef.current = null;
    workingRevisionRef.current = null;
    consumedWorkingRevisionKeysRef.current = new Set();
    setWorkingRevisionHead(null);
    setWorkingRevision(null);
    setWorkingRevisionAnnouncement("");
    setWorkingRevisionNotice("");
    setTablePreviewsById({});
    setTablePreviewLoadingIds(new Set());
    setTablePreviewErrors({});
  }, [reference, workingRevision, workingRevisionMatchesReference]);
  const nodesById = useMemo(
    () => new Map((tree?.nodes || []).map((node) => [node.node_id, node])),
    [tree],
  );
  const childrenByParent = useMemo(
    () => (tree ? buildChildrenByParent(tree) : new Map()),
    [tree],
  );

  useEffect(() => {
    if (!tree) {
      previousTreeNodesRef.current = [];
      return;
    }
    const reconciled = reconcileNativeTreeUiState({
      previousNodes: previousTreeNodesRef.current,
      nextTree: tree,
      selectedNodeId,
      expandedIds,
      successorNodeIds: displayWorkingRevision?.successor_node_ids,
    });
    if (reconciled.selectedNodeId !== selectedNodeId) {
      setSelectedNodeId(reconciled.selectedNodeId);
      setSelectedSourceItemId("");
    }
    if (!sameStringSet(reconciled.expandedIds, expandedIds)) {
      setExpandedIds(reconciled.expandedIds);
    }
    previousTreeNodesRef.current = tree.nodes;
  }, [tree]);

  useLayoutEffect(() => {
    const pending = pendingTreeAnchorRef.current;
    const viewport = treeViewportRef.current;
    if (!pending || !viewport || !tree) return;
    pendingTreeAnchorRef.current = null;
    const anchorId = reconcileNativeTreeUiState({
      previousNodes: pending.previousNodes,
      nextTree: tree,
      selectedNodeId: pending.nodeId,
      expandedIds: new Set(),
      successorNodeIds: pending.successorNodeIds,
    }).selectedNodeId;
    const anchor = viewport.querySelector<HTMLElement>(
      `[data-tree-node="${CSS.escape(anchorId)}"]`,
    );
    if (anchor) {
      const relativeTop =
        anchor.getBoundingClientRect().top -
        viewport.getBoundingClientRect().top;
      viewport.scrollTop += relativeTop - pending.relativeTop;
    }
    if (pending.hadTreeFocus) {
      const focusId = reconcileNativeTreeUiState({
        previousNodes: pending.previousNodes,
        nextTree: tree,
        selectedNodeId: pending.focusedNodeId || pending.nodeId,
        expandedIds: new Set(),
        successorNodeIds: pending.successorNodeIds,
      }).selectedNodeId;
      viewport
        .querySelector<HTMLElement>(
          `[data-tree-node="${CSS.escape(focusId)}"]`,
        )
        ?.focus({ preventScroll: true });
    }
  }, [displayWorkingRevision?.revision_sha256, tree]);
  const verificationAttempt = displayWorkingRevision
    ? undefined
    : reference?.continuation.latest_handoff?.latest_attempt;
  const verificationScopeId =
    verificationAttempt?.status === "VERIFICATION_REQUIRED"
      ? verificationAttempt.pending_rendered_scope_ids?.[0] ||
        verificationAttempt.affected_scope_ids[0] ||
        ""
      : "";

  useEffect(() => {
    if (
      !verificationAttempt ||
      !verificationScopeId ||
      autoOpenedAttemptIdsRef.current.has(verificationAttempt.attempt_id)
    ) {
      return;
    }
    const node = nodesById.get(verificationScopeId);
    if (!node) return;
    autoOpenedAttemptIdsRef.current.add(verificationAttempt.attempt_id);
    setSelectedNodeId(node.node_id);
    setSelectedSourceItemId("");
    setExpandedIds((current) => {
      const next = new Set(current);
      let cursor = node.parent_id ? nodesById.get(node.parent_id) : undefined;
      while (cursor) {
        next.add(cursor.node_id);
        cursor = cursor.parent_id ? nodesById.get(cursor.parent_id) : undefined;
      }
      return next;
    });
    if (node.pages[0]) onPageChange(node.pages[0]);
  }, [nodesById, onPageChange, verificationAttempt, verificationScopeId]);
  const visibleTreeNodes = useMemo(
    () =>
      tree
        ? flattenVisibleTree(tree, childrenByParent, expandedIds)
        : [],
    [childrenByParent, expandedIds, tree],
  );
  const expandableNodeIds = useMemo(
    () =>
      tree?.nodes
        .filter((node) => (childrenByParent.get(node.node_id) || []).length > 0)
        .map((node) => node.node_id) || [],
    [childrenByParent, tree],
  );
  const sourceItemLedgerRowsById = useMemo(
    () =>
      new Map(
        (sourceRequirementOutline?.ledger.rows || []).map((row) => [
          row.row_id,
          row,
        ]),
      ),
    [sourceRequirementOutline],
  );
  const sourceItemsById = useMemo(
    () =>
      new Map(
        (sourceRequirementOutline?.records || []).map((item) => [
          item.source_record_id,
          item,
        ]),
      ),
    [sourceRequirementOutline],
  );
  const relatedReviewItems = useMemo(() => new Map(Object.entries(sourceItemReasoning.state?.decomposition?.records || {})
    .map(([id, children]) => [id, children.map(child => ({ search_text: `${child.singular_requirement_id || child.node_id || ""} ${child.requirement_display_id} ${child.source_number || ""} ${child.text}`,
      requirement_kinds: child.requirement_kinds || sourceItemsById.get(id)?.requirement_kinds || [] }))])), [sourceItemReasoning.state?.decomposition, sourceItemsById]);
  const baseReviewFilter = useMemo(() => filterSourceItemLedger(sourceRequirementOutline, reviewRequest.value,
    { query: deferredReviewQuery, view: !reviewRequest.value && !["ALL", "REQUIREMENTS"].includes(reviewView) ? "ALL" : reviewView,
      category: reviewCategory, topic: reviewRequest.value ? reviewTopic : "" }, relatedReviewItems),
    [sourceRequirementOutline, reviewRequest.value, deferredReviewQuery, reviewView, reviewCategory, reviewTopic, relatedReviewItems]);
  const responseUnits = useMemo(() => analysisUnits(sourceItemsById.values(), sourceItemReasoning.state), [sourceItemsById, sourceItemReasoning.state]);
  const visibleResponseUnits = useMemo(() => responseUnits.filter(unit => (purpose !== "analysis" || inAnalysisQueue(unit, analysisQueue))
    && matchesAnalysisSearch(unit, deferredReviewQuery) && (!reviewCategory || unit.kinds?.includes(reviewCategory))),
    [responseUnits, purpose, analysisQueue, deferredReviewQuery, reviewCategory]);
  const visibleDerivedIds = useMemo(() => new Set(visibleResponseUnits.filter(unit => baseReviewFilter.matched.has(unit.sourceId)).map(unit => unit.id)), [visibleResponseUnits, baseReviewFilter]);
  useEffect(() => { setSelectedDerived(null); setCollapsedDerivedIds(new Set()); }, [analysisQueue, deferredReviewQuery, reviewCategory, reviewView, reviewTopic, positionKey]);
  const reviewFilter = useMemo(() => {
    if (purpose !== "analysis" || !sourceItemReasoning.state) return baseReviewFilter;
    const matched = new Set(visibleResponseUnits.map(unit => unit.sourceId).filter(id => baseReviewFilter.matched.has(id)));
    const visible = new Set<string>(), ancestors = new Set<string>();
    for (const row of sourceItemLedgerRowsById.values()) {
      if (!row.source_item_id || !matched.has(row.source_item_id)) continue;
      visible.add(row.row_id);
      let parent = row.parent_row_id;
      while (parent && !ancestors.has(parent)) { ancestors.add(parent); visible.add(parent); parent = sourceItemLedgerRowsById.get(parent)?.parent_row_id || null; }
    }
    return { ...baseReviewFilter, matched, visible, ancestors, active: true };
  }, [purpose, sourceItemReasoning.state, visibleResponseUnits, baseReviewFilter, sourceItemLedgerRowsById]);
  useEffect(() => setFilterCollapsed(new Set()), [deferredReviewQuery, reviewView, reviewCategory, reviewTopic, analysisQueue]);
  const reviewExpandedIds = useMemo(() => {
    if (!reviewFilter.active) return expandedIds;
    const next = new Set([...expandedIds, ...reviewFilter.ancestors]);
    for (const id of filterCollapsed) next.delete(id);
    return next;
  }, [expandedIds, reviewFilter, filterCollapsed]);
  const visibleSourceItemLedgerRows = useMemo(
    () => flattenVisibleSourceItemLedger(sourceRequirementOutline, reviewExpandedIds).filter(row => reviewFilter.visible.has(row.row_id)),
    [reviewExpandedIds, sourceRequirementOutline, reviewFilter],
  );
  const reviewCountsByRow = useMemo(() => sourceReviewCounts(sourceRequirementOutline?.ledger.rows || [], reviewRequest.value),
    [sourceRequirementOutline, reviewRequest.value]);
  const derivedCounts = useMemo(() => new Map(Object.entries(sourceItemReasoning.state?.decomposition?.records || {}).map(([id, children]) => [id, children.length])), [sourceItemReasoning.state?.decomposition]);
  const reviewSummary = useMemo(() => {
    const rows = Object.values(reviewRequest.value?.records || {});
    return { focus: rows.filter(row => row.focus_areas.length).length,
      attention: rows.filter(row => row.needs_attention || row.pending_feedback_count).length,
      changed: rows.filter(row => row.changed_from_previous === true).length };
  }, [reviewRequest.value]);
  const sourceItemLedgerExpandableIds = useMemo(
    () =>
      (sourceRequirementOutline?.ledger.rows || [])
        .filter((row) => row.child_row_ids.length > 0)
        .map((row) => row.row_id),
    [sourceRequirementOutline],
  );
  const selectedSourceItemLedgerRow = useMemo(
    () =>
      selectedSourceItemId
        ? (sourceRequirementOutline?.ledger.rows || []).find(
            (row) => row.source_item_id === selectedSourceItemId,
          ) || null
        : null,
    [selectedSourceItemId, sourceRequirementOutline],
  );
  const selectedLedgerRowId =
    selectedSourceItemLedgerRow?.row_id || selectedNodeId;
  const selectedDerivedId = selectedDerived?.sourceId === selectedSourceItemId && selectedDerived.baseline === sourceRequirementOutline?.baseline.artifact_sha256
    && sourceItemReasoning.state?.decomposition?.records[selectedSourceItemId]?.some(child => (child.singular_requirement_id || child.node_id) === selectedDerived.unitId)
    ? selectedDerived.unitId : undefined;
  const selectedTableRowId = selectedDerivedId ? derivedTableRowId(selectedDerivedId) : selectedLedgerRowId;
  useEffect(() => {
    if (!selectedSourceItemId || !sourceRequirementOutline || !sourceItemReasoning.state) return;
    const baseline = sourceRequirementOutline.baseline.artifact_sha256;
    const revision = sourceReader?.revision_id || "";
    const scope = `${positionKey}:${baseline}:${revision}`;
    if (restoredDerivedSelectionRef.current === scope) return;
    restoredDerivedSelectionRef.current = scope;
    try {
      const saved = JSON.parse(localStorage.getItem(positionKey) || "{}");
      if (saved.sourceItemId !== selectedSourceItemId || saved.baselineSha !== baseline || saved.resultRevision !== revision || !saved.derivedUnitId) return;
      const child = sourceItemReasoning.state.decomposition?.records[selectedSourceItemId]?.find(value => (value.singular_requirement_id || value.node_id) === saved.derivedUnitId);
      if (child) setSelectedDerived({ sourceId: selectedSourceItemId, unitId: saved.derivedUnitId, baseline });
    } catch { /* Missing or old viewing preferences do not change the selected source. */ }
  }, [positionKey, selectedSourceItemId, sourceRequirementOutline, sourceItemReasoning.state, sourceReader?.revision_id]);
  const locatedReviewRow = useRef("");
  useEffect(() => {
    const key = `${positionKey}:${selectedTableRowId}:${reviewPresentation}:${reviewRequest.value ? "indexed" : "basic"}`;
    if (mode !== "SOURCE_REQUIREMENTS" || !selectedLedgerRowId || locatedReviewRow.current === key) return;
    const frame = requestAnimationFrame(() => {
      const element = reviewPresentation === "table"
        ? treeViewportRef.current?.querySelector<HTMLElement>(`[data-ledger-row="${CSS.escape(selectedTableRowId)}"]`)
        : treeRef.current?.querySelector<HTMLElement>(`[data-tree-row="${CSS.escape(selectedLedgerRowId)}"]`);
      if (element) {
        const viewport = treeViewportRef.current;
        const parent = selectedDerivedId && viewport?.querySelector<HTMLElement>(`[data-ledger-row="${CSS.escape(selectedLedgerRowId)}"]`);
        const toolbar = viewport?.querySelector<HTMLElement>(".source-review-tree-toolbar");
        const top = toolbar?.getBoundingClientRect().bottom || viewport?.getBoundingClientRect().top || 0;
        const bottom = viewport?.getBoundingClientRect().bottom || 0;
        if (parent && viewport && element.getBoundingClientRect().bottom - parent.getBoundingClientRect().top <= bottom - top - 8) {
          if (parent.getBoundingClientRect().top < top + 8) viewport.scrollTop += parent.getBoundingClientRect().top - top - 8;
          else if (element.getBoundingClientRect().bottom > bottom) viewport.scrollTop += element.getBoundingClientRect().bottom - bottom + 8;
        } else element.scrollIntoView?.({ block: selectedDerivedId ? "nearest" : "center" });
        locatedReviewRow.current = key;
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [positionKey, selectedLedgerRowId, selectedTableRowId, selectedDerivedId, mode, visibleSourceItemLedgerRows, reviewRequest.value, reviewPresentation]);
  const selectedSourceItem = selectedSourceItemId
    ? sourceItemsById.get(selectedSourceItemId) || null
    : null;
  const externalBind = documentAgent?.bind;
  useEffect(() => {
    if (!sourceReader || !externalBind) return;
    return externalBind({ sourceSha256, baselineSha256: sourceReader.outline?.baseline.artifact_sha256 || "", revisionId: sourceReader.revision_id,
      selectedSourceItemId, selectedNodeId, selectedDerivedUnitId: selectedDerivedId || undefined, selectedLabel: selectedSourceItem ? `${selectedDerivedId ? "父条款 " : ""}${selectedSourceItem.requirement_display_id || selectedSourceItem.source_display_id}` : nodesById.get(selectedNodeId)?.title || "当前结构",
      onUpdated: () => setSourceRequirementRefreshToken(value => value + 1) });
  }, [externalBind, sourceReader?.revision_id, sourceSha256, selectedSourceItemId, selectedNodeId, selectedDerivedId, selectedSourceItem, nodesById]);
  const reviewNavigationRows = useMemo(() => flattenVisibleSourceItemLedger(sourceRequirementOutline, new Set(sourceItemLedgerExpandableIds))
    .filter(row => row.source_item_id && reviewFilter.matched.has(row.source_item_id)), [sourceRequirementOutline, sourceItemLedgerExpandableIds, reviewFilter]);
  const reviewNavigationIndex = reviewNavigationRows.findIndex(row => row.source_item_id === selectedSourceItemId);
  const nextReviewRow = reviewNavigationRows[reviewNavigationIndex + 1];
  const tablesById = useMemo(
    () =>
      new Map(
        activeLogicalTables.map((table) => [table.scope_id, table]),
      ),
    [activeLogicalTables],
  );
  const findingsByCode = useMemo(
    () =>
      new Map(
        activeUnresolved.map((finding) => [finding.code, finding]),
      ),
    [activeUnresolved],
  );
  const selectedNode =
    nodesById.get(selectedNodeId) ||
    nodesById.get(tree?.root_ids[0] || "") ||
    null;
  const isOfficeSource = reference?.source_format === "docx" || reference?.source_format === "xlsx"
    || Boolean(selectedNode?.source_anchors.some(anchor => officeLocation(anchor.native_locator)));
  const hasSourcePreview = showSourcePreview && Boolean(sourceFileUrl || isOfficeSource);
  const selectedTables = useMemo(
    () =>
      (selectedNode?.content_blocks || [])
        .map((block) => tablesById.get(block.logical_table_scope_id))
        .filter(
          (table): table is NativeStructureLogicalTableV1 => Boolean(table),
        ),
    [selectedNode, tablesById],
  );
  const selectedTableIds = useMemo(
    () => selectedTables.map((table) => table.scope_id).sort(),
    [selectedTables],
  );
  const selectedTableIdsKey = JSON.stringify(selectedTableIds);
  const selectedFindings = (selectedNode?.finding_codes || [])
    .map((code) => findingsByCode.get(code))
    .filter((finding): finding is NativeStructureUnresolvedV1 => Boolean(finding));
  const selectedProposals = activeStructureProposals.filter(
    (proposal) => proposal.node_id === selectedNode?.node_id,
  );
  const selectedProposalGroups = Array.from(
    selectedProposals.reduce<Map<string, NativeStructureProposalV1[]>>(
      (groups, proposal) => {
        const key = `${proposal.finding_code}:${proposal.recommended_action}:${proposal.status}`;
        groups.set(key, [...(groups.get(key) || []), proposal]);
        return groups;
      },
      new Map(),
    ),
  ).map(([key, proposals]) => ({ key, proposals }));
  const autoAppliedProposalCount = selectedProposals.filter(
    (proposal) => proposal.status === "AUTO_APPLIED_PENDING_REVIEW",
  ).length;
  const suggestedProposalCount = selectedProposals.length - autoAppliedProposalCount;
  const proposalFindingCodes = new Set(
    selectedProposals.map((proposal) => proposal.finding_code),
  );
  const unproposedFindings = selectedFindings.filter(
    (finding) => !proposalFindingCodes.has(finding.code),
  );
  const selectedPages = Array.from(
    new Set([
      ...(selectedNode?.pages || []),
      ...selectedTables.flatMap((table) => table.pages),
    ]),
  ).sort((left, right) => left - right);
  const selectedSourceContent = useMemo(
    () => (selectedNode?.source_content || []).map(content => {
      const projection = reference?.reading_projections?.[content.content_id];
      const valid = projection?.source_sha256 === sourceSha256 && projection.raw_text_sha256 === content.text_sha256;
      return { ...content, display_text: valid ? projection.display_text : content.text };
    }),
    [selectedNode, reference?.reading_projections, sourceSha256],
  );
  const sourceRequirementTableModelsById = useMemo<
    Record<string, StructuredTableModel[]>
  >(
    () =>
      Object.fromEntries(
        selectedTables.flatMap((table) => {
          const preview = tablePreviewsById[table.scope_id];
          if (!preview || !selectedNode) return [];
          return [
            [
              table.scope_id,
              nativeStructuredTables(preview, selectedNode.source_anchors),
            ],
          ];
        }),
    ),
    [selectedNode, selectedTables, tablePreviewsById],
  );
  const tableGridErrors = useMemo(
    () =>
      Object.fromEntries(
        selectedTables.flatMap((table) => {
          const preview = tablePreviewsById[table.scope_id];
          if (!preview || preview.fragments.some(fragment => officeLocation(fragment.native_locator))) return [];
          const models = sourceRequirementTableModelsById[table.scope_id] || [];
          return canonicalStructuredTablesAreValid(preview, models)
            ? []
            : [
                [
                  table.scope_id,
                  "续页列映射没有完整覆盖规范表头，已停止将该表标记为还原完成。",
                ],
              ];
        }),
      ) as Record<string, string>,
    [
      selectedTables,
      sourceRequirementTableModelsById,
      tablePreviewsById,
    ],
  );
  const verifiedTableModelsById = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(sourceRequirementTableModelsById).filter(
          ([scopeId]) => !tableGridErrors[scopeId],
        ),
      ),
    [sourceRequirementTableModelsById, tableGridErrors],
  );
  const contentSequence = useMemo(() => {
    if (!selectedNode || isOfficeSource) return [];
    const textItems = selectedSourceContent
      .map((content) => ({
        kind: "TEXT" as const,
        id: content.content_id,
        page: content.page,
        bbox: content.bbox,
        content,
        text: sourceContentText(selectedNode, content),
      }))
      .filter((item) => item.text);
    const tableItems = selectedTables.flatMap((table) => {
      const preview = tablePreviewsById[table.scope_id];
      const models = sourceRequirementTableModelsById[table.scope_id] || [];
      if (!preview || !models.length || tableGridErrors[table.scope_id]) return [];
      const fragment = preview.fragments[0];
      if (!fragment) return [];
      return [{
        kind: "TABLE" as const,
        id: `logical-table:${preview.logical_table_scope_id}`,
        page: fragment.page,
        bbox: fragment.bbox,
        table: preview,
        fragment,
        models,
      }];
    });
    return [...textItems, ...tableItems].sort(
      (left, right) =>
        left.page - right.page ||
        left.bbox[1] - right.bbox[1] ||
        (left.kind === "TEXT" ? -1 : 1),
    );
  }, [
    isOfficeSource,
    selectedNode,
    selectedSourceContent,
    selectedTables,
    sourceRequirementTableModelsById,
    tableGridErrors,
    tablePreviewsById,
  ]);

  useEffect(() => {
    const handoff = reference?.continuation.latest_handoff;
    if (
      !reference ||
      !handoff ||
      !verificationAttempt ||
      !verificationScopeId ||
      reference.structure_projection.state !==
        "WORKING_VERIFICATION_REQUIRED" ||
      reference.structure_projection.attempt_id !==
        verificationAttempt.attempt_id ||
      selectedNodeId !== verificationScopeId ||
      renderAckAttemptRef.current === verificationAttempt.attempt_id
    ) {
      return;
    }
    const tableIds = selectedTables.map((table) => table.scope_id);
    if (
      tableIds.some(
        (scopeId) =>
          !tablePreviewsById[scopeId] ||
          tablePreviewLoadingIds.has(scopeId) ||
          Boolean(tablePreviewErrors[scopeId]),
      )
    ) {
      return;
    }
    const invalidTableId = tableIds.find((scopeId) => tableGridErrors[scopeId]);
    if (invalidTableId) {
      setActivitySyncNotice(
        "局部表格仍存在续页列映射冲突；已停止提交复验通过，原文和其他节点仍可继续核对。",
      );
      return;
    }
    let firstFrame = 0;
    let secondFrame = 0;
    let cancelled = false;
    firstFrame = window.requestAnimationFrame(() => {
      secondFrame = window.requestAnimationFrame(() => {
        if (cancelled) return;
        renderAckAttemptRef.current = verificationAttempt.attempt_id;
        void api
          .acknowledgeNativeStructureRenderedReopen(projectId, documentId, {
            schema_version: "native-structure-rendered-reopen-request/v1",
            command_id:
              `rendered-reopen:${verificationAttempt.attempt_id}:` +
              verificationScopeId,
            expected_source_sha256: reference.source_sha256,
            expected_artifact_id: reference.artifact_id,
            handoff_id: handoff.handoff_id,
            attempt_id: verificationAttempt.attempt_id,
            scope_id: verificationScopeId,
            expected_projection_sha256:
              reference.structure_projection.projection_sha256,
            expected_rendered_output_sha256:
              verificationAttempt.rendered_output_sha256,
          })
          .then(async (result) => {
            if (!cancelled) {
              setActivitySyncNotice("局部渲染已重新打开，Host 后置验证已提交。");
              await refreshAuthoritativeReference(
                `rendered-reopen:${verificationAttempt.attempt_id}:` +
                  result.reopen_receipt_sha256,
                (value) =>
                  nativeStructureProjectionSatisfiesAttempt(
                    value,
                    result.attempt,
                    handoff.handoff_id,
                  ),
              );
            }
          })
          .catch(() => {
            if (!cancelled) {
              renderAckAttemptRef.current = "";
              setActivitySyncNotice(
                "局部渲染复验暂时未完成，原文核对和其他节点不受影响。",
              );
            }
          });
      });
    });
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(firstFrame);
      window.cancelAnimationFrame(secondFrame);
    };
  }, [
    contentSequence,
    documentId,
    projectId,
    reference,
    refreshAuthoritativeReference,
    selectedNodeId,
    selectedTables,
    tablePreviewErrors,
    tablePreviewLoadingIds,
    tablePreviewsById,
    tableGridErrors,
    verificationAttempt,
    verificationScopeId,
  ]);
  const parentNode = selectedNode?.parent_id
    ? nodesById.get(selectedNode.parent_id) || null
    : null;
  const tableNodeCount = tree?.nodes.filter(
    (node) => node.content_blocks.length > 0,
  ).length || 0;
  const derivedTitleCount = tree?.nodes.filter(
    (node) => node.title_authority === "DERIVED_FROM_VISIBLE_TABLE_ROLE",
  ).length || 0;
  const externalDraftKey = sourceFeedbackDraftKey(projectId, documentId, `structure:${selectedNodeId}`, sourceReader?.revision_id || "");
  const externalDraft = useSourceFeedbackDraft(externalDraftKey, sourceReader?.revision_number || 0);
  const message = sourceReader ? externalDraft.text : selectedNode ? draftsByNode[selectedNode.node_id] || "" : "";
  const selectedFeedbacks = (reference?.feedbacks || [])
    .filter(
      (feedback) =>
        feedback.scope_kind === "STRUCTURE_NODE" &&
        feedback.scope_id === selectedNode?.node_id,
    )
    .slice(-3)
    .reverse();

  const loadTablePreview = useCallback(
    async (scopeId: string, signal?: AbortSignal) => {
      setTablePreviewLoadingIds((current) => new Set(current).add(scopeId));
      setTablePreviewErrors((current) => {
        const next = { ...current };
        delete next[scopeId];
        return next;
      });
      try {
        const preview = sourceReader ? sourceReader.tables[scopeId] : displayWorkingRevision
          ? await api.nativeStructureWorkingRevisionTablePreview(
              projectId,
              documentId,
              displayWorkingRevision.run_id,
              displayWorkingRevision.revision,
              scopeId,
              displayWorkingRevision.revision_sha256,
              signal,
            )
          : await api.nativeStructureTablePreview(
              projectId,
              documentId,
              scopeId,
              signal,
            );
        if (signal?.aborted) return;
        if (
          preview.schema_version !== "native-structure-table-preview/v1" ||
          preview.source_sha256 !== sourceSha256 ||
          preview.logical_table_scope_id !== scopeId ||
          (displayWorkingRevision &&
            preview.artifact_id !== displayWorkingRevision.base_artifact_id)
        ) {
          throw new Error("表格预览身份与当前工作结构不一致。");
        }
        if (displayWorkingRevision) {
          const binding = displayWorkingRevision.table_preview_bindings.find(
            (item) => item.scope_id === scopeId,
          );
          if (
            !binding ||
            canonicalJsonSha256(preview) !== binding.preview_sha256
          ) {
            throw new Error("表格预览内容与工作修订绑定不一致。");
          }
        }
        setTablePreviewsById((current) => ({ ...current, [scopeId]: preview }));
      } catch (caught) {
        if (signal?.aborted) return;
        setTablePreviewErrors((current) => ({
          ...current,
          [scopeId]:
            apiErrorCode(caught) ===
            "working_revision_table_evidence_not_materialized"
              ? "该工作节点的表格证据尚未完成不可变物化；当前不会回退到旧表格，PDF 原文和其他节点仍可继续核对。"
              : errorMessage(caught),
        }));
      } finally {
        if (!signal?.aborted) {
          setTablePreviewLoadingIds((current) => {
            const next = new Set(current);
            next.delete(scopeId);
            return next;
          });
        }
      }
    },
    [sourceReader, displayWorkingRevision, documentId, projectId, sourceSha256],
  );

  useEffect(() => {
    if (!selectedTableIds.length) return;
    const controller = new AbortController();
    for (const scopeId of selectedTableIds) {
      if (!scopeId || tablePreviewsById[scopeId]) continue;
      void loadTablePreview(scopeId, controller.signal);
    }
    return () => controller.abort();
  }, [loadTablePreview, selectedTableIds, selectedTableIdsKey]);
  const focusedProposals = selectedProposals.filter((proposal) =>
    focusedProposalIds.includes(proposal.proposal_id),
  );
  const selectedOverlays = useMemo(
    () => {
      const requirementOverlays =
        mode === "SOURCE_REQUIREMENTS"
          ? sourceRequirementOverlays
              .filter((overlay) => overlay.page === activePage)
              .map((overlay) => ({
                ...overlay,
                selected: true,
                focus: true,
                precision: "EXACT_SOURCE_REQUIREMENT_RECORD",
                channel: "NATIVE",
              }))
          : [];
      if (requirementOverlays.length) return requirementOverlays;
      if (focusedEvidenceOverlay?.page === activePage) {
        return [
          {
            ...focusedEvidenceOverlay,
            selected: true,
            focus: true,
            precision: "EXACT_SOURCE_EVIDENCE",
            channel: "NATIVE",
          },
        ];
      }
      const proposalOverlays = focusedProposals
        .filter((proposal) => proposal.page === activePage)
        .map((proposal) => ({
          id: `proposal:${proposal.proposal_id}`,
          type:
            proposal.object_kind === "IMAGE_PLACEMENT"
              ? "FIGURE"
              : proposal.object_kind === "TABLE_CELL_RECOVERY"
                ? "TABLE"
                : "TEXT",
          label: proposal.proposal_title || "Agent 发现位置",
          bbox: proposal.bbox,
          sourceBounds: proposal.source_bounds,
          selected: true,
          focus: true,
          precision: "EXACT_PROPOSAL_EVIDENCE",
          channel: "NATIVE",
        }));
      if (proposalOverlays.length) return proposalOverlays;
      const contentOverlays = (selectedNode?.source_content || [])
        .filter((content) => content.page === activePage)
        .map((content, index) => ({
          id: `content:${content.content_id}`,
          type: "TEXT",
          label: `直接正文 ${index + 1}`,
          bbox: content.bbox,
          sourceBounds: content.source_bounds,
          selected: true,
          focus: true,
          precision: content.precision,
          channel: "NATIVE",
        }));
      const structuralOverlays = (selectedNode?.source_anchors || [])
        .filter((anchor) => anchor.page === activePage)
        .filter(
          (anchor) =>
            anchor.anchor_kind === "TABLE_REGION" ||
            anchor.anchor_kind === "IMAGE" ||
            contentOverlays.length === 0,
        )
        .map((anchor, index) => ({
          id: anchor.anchor_id,
          type:
            anchor.anchor_kind === "TABLE_REGION"
              ? "TABLE"
              : anchor.anchor_kind === "IMAGE"
                ? "FIGURE"
                : "TEXT",
          label:
            anchor.anchor_kind === "TABLE_REGION"
              ? `表格 ${index + 1}`
              : anchor.anchor_kind === "IMAGE"
                ? `图片 ${index + 1}`
                : selectedNode?.label || "标题",
          bbox: anchor.bbox,
          sourceBounds: anchor.source_bounds,
          selected: true,
          focus: true,
          precision: anchor.precision,
          channel: "NATIVE",
        }));
      return [...contentOverlays, ...structuralOverlays];
    },
    [
      activePage,
      focusedEvidenceOverlay,
      focusedProposals,
      mode,
      selectedNode,
      sourceRequirementOverlays,
    ],
  );
  const approximateOverlayCount = selectedOverlays.filter((overlay) =>
    overlay.precision.includes("APPROXIMATE"),
  ).length;

  const breadcrumb = useMemo(() => {
    if (!selectedNode) return [];
    const result: NativeStructureTreeNodeV1[] = [];
    let cursor: NativeStructureTreeNodeV1 | undefined = selectedNode;
    while (cursor) {
      result.unshift(cursor);
      cursor = cursor.parent_id ? nodesById.get(cursor.parent_id) : undefined;
    }
    return result;
  }, [nodesById, selectedNode]);

  const syncPdf = useCallback(() => {
    pdfFrameRef.current?.contentWindow?.postMessage(
      {
        type: "REQPILOTX_PDF_NAVIGATE",
        page: activePage,
        overlays: selectedOverlays,
      },
      viewerOrigin,
    );
  }, [activePage, selectedOverlays, viewerOrigin]);

  useEffect(() => {
    syncPdf();
  }, [syncPdf]);

  useEffect(() => {
    const listener = (event: MessageEvent) => {
      if (
        event.origin !== viewerOrigin ||
        event.source !== pdfFrameRef.current?.contentWindow
      ) {
        return;
      }
      const message = event.data as { type?: unknown; page?: unknown };
      if (message.type === "pdf-viewer-ready") syncPdf();
      if (message.type === "pdf-page-changed") {
        const nextPage = Number(message.page || activePage);
        if (Number.isFinite(nextPage) && nextPage > 0) {
          onPageChange(Math.trunc(nextPage));
        }
      }
    };
    window.addEventListener("message", listener);
    return () => window.removeEventListener("message", listener);
  }, [activePage, onPageChange, syncPdf, viewerOrigin]);

  if (
    (loading && !reference && !displayWorkingRevision) ||
    (notAvailable && !displayWorkingRevision)
  ) {
    return null;
  }

  const toggleNode = (nodeId: string) => {
    setExpandedIds((current) => {
      const next = new Set(current);
      if (next.has(nodeId)) next.delete(nodeId);
      else next.add(nodeId);
      return next;
    });
  };

  const selectNode = (node: NativeStructureTreeNodeV1) => {
    setSelectedNodeId(node.node_id);
    setSelectedSourceItemId("");
    setSelectedDerived(null);
    if (mode === "SOURCE_REQUIREMENTS") {
      try { localStorage.setItem(positionKey, JSON.stringify({ sourceItemId: "", nodeId: node.node_id })); } catch { /* navigation remains usable */ }
    }
    setFeedbackNotice("");
    setProposalNotice("");
    setFocusedProposalIds([]);
    setSourceRequirementOverlays([]);
    if (node.pages[0]) onPageChange(node.pages[0]);
    setExpandedIds((current) => {
      const next = new Set(current);
      let cursor = node.parent_id ? nodesById.get(node.parent_id) : undefined;
      while (cursor) {
        next.add(cursor.node_id);
        cursor = cursor.parent_id ? nodesById.get(cursor.parent_id) : undefined;
      }
      return next;
    });
  };

  const selectSourceItem = (
    sourceItem: SourceRequirementOutlineRecordV1,
    owner: NativeStructureTreeNodeV1,
  ) => {
    setSelectedNodeId(owner.node_id);
    setSelectedSourceItemId(sourceItem.source_record_id);
    setSelectedDerived(null);
    try { localStorage.setItem(positionKey, JSON.stringify({ sourceItemId: sourceItem.source_record_id, nodeId: owner.node_id })); } catch { /* Navigation still works without persistence. */ }
    setFeedbackNotice("");
    setProposalNotice("");
    setFocusedProposalIds([]);
    setSourceRequirementOverlays([]);
    if (sourceItem.pages[0]) onPageChange(sourceItem.pages[0]);
    const parents: string[] = [];
    let cursor: SourceItemLedgerRowV1 | undefined = sourceItemLedgerRowsById.get(sourceItem.source_record_id) || sourceItemLedgerRowsById.get(owner.node_id);
    while (cursor?.parent_row_id) { parents.push(cursor.parent_row_id); cursor = sourceItemLedgerRowsById.get(cursor.parent_row_id); }
    setExpandedIds(current => new Set([...current, ...parents]));
    setFilterCollapsed(current => { const next = new Set(current); parents.forEach(id => next.delete(id)); return next; });
  };

  const focusTreeNode = (node: NativeStructureTreeNodeV1) => {
    selectNode(node);
    requestAnimationFrame(() => {
      treeRef.current
        ?.querySelector<HTMLElement>(
          `[data-tree-node="${CSS.escape(node.node_id)}"]`,
        )
        ?.focus();
    });
  };

  const selectDerivedUnit = (row: SourceItemLedgerRowV1, unitId: string) => {
    const child = row.source_item_id ? sourceItemReasoning.state?.decomposition?.records[row.source_item_id]?.find(child => (child.singular_requirement_id || child.node_id) === unitId) : undefined;
    if (!row.source_item_id || !child) return;
    const page = child.source_excerpts?.find(excerpt => excerpt.page)?.page;
    if (page) onPageChange(page);
    setSelectedDerived({ sourceId: row.source_item_id, unitId, baseline: sourceRequirementOutline!.baseline.artifact_sha256 });
    try { localStorage.setItem(positionKey, JSON.stringify({ sourceItemId: row.source_item_id, nodeId: row.structure_node_id,
      derivedUnitId: unitId, baselineSha: sourceRequirementOutline!.baseline.artifact_sha256, resultRevision: sourceReader?.revision_id || "" })); } catch { /* In-page navigation remains available without local persistence. */ }
    setCollapsedDerivedIds(current => { const next = new Set(current); next.delete(row.source_item_id!); return next; });
  };
  const focusSourceItemLedgerRow = (row: SourceItemLedgerRowV1, derivedId?: string) => {
    const owner = nodesById.get(row.structure_node_id);
    if (!owner) return;
    const sourceItem = row.source_item_id
      ? sourceItemsById.get(row.source_item_id)
      : undefined;
    if (sourceItem) selectSourceItem(sourceItem, owner);
    else selectNode(owner);
    if (derivedId && sourceItem) selectDerivedUnit(row, derivedId);
    requestAnimationFrame(() => {
      const element = reviewPresentation === "table"
        ? treeViewportRef.current?.querySelector<HTMLElement>(`[data-ledger-row="${CSS.escape(derivedId ? derivedTableRowId(derivedId) : row.row_id)}"] .ledger-select`)
        : treeRef.current?.querySelector<HTMLElement>(`[data-tree-row="${CSS.escape(row.row_id)}"]`);
      element?.focus(); element?.scrollIntoView?.({ block: "nearest" });
    });
  };

  const expandBranch = (nodeId: string) => {
    setExpandedIds((current) => {
      const next = new Set(current);
      const pending = [nodeId];
      while (pending.length) {
        const currentId = pending.pop()!;
        const children: NativeStructureTreeNodeV1[] =
          childrenByParent.get(currentId) || [];
        if (children.length) next.add(currentId);
        pending.push(...children.map((child) => child.node_id));
      }
      return next;
    });
  };

  const collapseBranch = (nodeId: string) => {
    setExpandedIds((current) => {
      const next = new Set(current);
      const pending = [nodeId];
      while (pending.length) {
        const currentId = pending.pop()!;
        next.delete(currentId);
        const children: NativeStructureTreeNodeV1[] =
          childrenByParent.get(currentId) || [];
        pending.push(...children.map((child) => child.node_id));
      }
      return next;
    });
  };

  const handleTreeKeyDown = (event: ReactKeyboardEvent<HTMLUListElement>) => {
    if (
      ![
        "ArrowDown",
        "ArrowUp",
        "ArrowLeft",
        "ArrowRight",
        "Home",
        "End",
      ].includes(event.key)
    ) {
      return;
    }
    event.preventDefault();
    const index = Math.max(
      0,
      visibleTreeNodes.findIndex((node) => node.node_id === selectedNodeId),
    );
    const current = visibleTreeNodes[index];
    const focusAt = (nextIndex: number) => {
      const bounded = Math.max(
        0,
        Math.min(visibleTreeNodes.length - 1, nextIndex),
      );
      const next = visibleTreeNodes[bounded];
      if (next) focusTreeNode(next);
    };
    if (event.key === "Home") return focusAt(0);
    if (event.key === "End") return focusAt(visibleTreeNodes.length - 1);
    if (event.key === "ArrowDown") return focusAt(index + 1);
    if (event.key === "ArrowUp") return focusAt(index - 1);
    if (!current) return;
    const children = childrenByParent.get(current.node_id) || [];
    if (event.shiftKey && event.key === "ArrowRight") {
      expandBranch(current.node_id);
      return;
    }
    if (event.shiftKey && event.key === "ArrowLeft") {
      collapseBranch(current.node_id);
      return;
    }
    if (event.key === "ArrowRight" && children.length) {
      if (!expandedIds.has(current.node_id)) {
        setExpandedIds((values) => new Set(values).add(current.node_id));
      } else {
        focusTreeNode(children[0]);
      }
      return;
    }
    if (event.key === "ArrowLeft") {
      if (children.length && expandedIds.has(current.node_id)) {
        setExpandedIds((values) => {
          const next = new Set(values);
          next.delete(current.node_id);
          return next;
        });
      } else if (current.parent_id) {
        const parent = nodesById.get(current.parent_id);
        if (parent) focusTreeNode(parent);
      }
    }
  };

  const handleSourceItemLedgerKeyDown = (
    event: ReactKeyboardEvent<HTMLUListElement>,
  ) => {
    if (
      ![
        "ArrowDown",
        "ArrowUp",
        "ArrowLeft",
        "ArrowRight",
        "Home",
        "End",
      ].includes(event.key)
    ) {
      return;
    }
    event.preventDefault();
    const index = Math.max(
      0,
      visibleSourceItemLedgerRows.findIndex(
        (row) => row.row_id === selectedLedgerRowId,
      ),
    );
    const current = visibleSourceItemLedgerRows[index];
    const focusAt = (nextIndex: number) => {
      const bounded = Math.max(
        0,
        Math.min(visibleSourceItemLedgerRows.length - 1, nextIndex),
      );
      const next = visibleSourceItemLedgerRows[bounded];
      if (next) focusSourceItemLedgerRow(next);
    };
    if (event.key === "Home") return focusAt(0);
    if (event.key === "End") return focusAt(visibleSourceItemLedgerRows.length - 1);
    if (event.key === "ArrowDown") return focusAt(index + 1);
    if (event.key === "ArrowUp") return focusAt(index - 1);
    if (!current) return;
    const children = current.child_row_ids.filter(id => reviewFilter.visible.has(id));
    if (event.shiftKey && event.key === "ArrowRight") {
      setFilterCollapsed(values => {
        const next = new Set(values);
        const pending = [current.row_id];
        while (pending.length) {
          const rowId = pending.pop()!;
          next.delete(rowId);
          pending.push(...(sourceItemLedgerRowsById.get(rowId)?.child_row_ids || []));
        }
        return next;
      });
      setExpandedIds((values) => {
        const next = new Set(values);
        const pending = [current.row_id];
        while (pending.length) {
          const rowId = pending.pop()!;
          const row = sourceItemLedgerRowsById.get(rowId);
          if (!row?.child_row_ids.length) continue;
          next.add(rowId);
          pending.push(...row.child_row_ids);
        }
        return next;
      });
      return;
    }
    if (event.shiftKey && event.key === "ArrowLeft") {
      if (reviewFilter.active) setFilterCollapsed(values => new Set(values).add(current.row_id));
      setExpandedIds((values) => {
        const next = new Set(values);
        const pending = [current.row_id];
        while (pending.length) {
          const rowId = pending.pop()!;
          next.delete(rowId);
          const row = sourceItemLedgerRowsById.get(rowId);
          if (row) pending.push(...row.child_row_ids);
        }
        return next;
      });
      return;
    }
    if (event.key === "ArrowRight" && children.length) {
      if (!reviewExpandedIds.has(current.row_id)) {
        setFilterCollapsed(values => { const next = new Set(values); next.delete(current.row_id); return next; });
        setExpandedIds((values) => new Set(values).add(current.row_id));
      } else {
        const first = sourceItemLedgerRowsById.get(children[0]);
        if (first) focusSourceItemLedgerRow(first);
      }
      return;
    }
    if (event.key === "ArrowLeft") {
      if (children.length && reviewExpandedIds.has(current.row_id)) {
        if (reviewFilter.active) setFilterCollapsed(values => new Set(values).add(current.row_id));
        setExpandedIds((values) => {
          const next = new Set(values);
          next.delete(current.row_id);
          return next;
        });
      } else if (current.parent_row_id) {
        const parent = sourceItemLedgerRowsById.get(current.parent_row_id);
        if (parent) focusSourceItemLedgerRow(parent);
      }
    }
  };

  const updateColumnShares = (
    updater: (current: StructureColumnShares) => StructureColumnShares,
  ) => {
    setColumnShares((current) => {
      const next = updater(current);
      writeColumnShares(projectId, documentId, next);
      return next;
    });
  };

  const resetColumnShares = () => {
    updateColumnShares(() => ({ ...DEFAULT_COLUMN_SHARES }));
  };

  const beginColumnResize = (
    splitter: StructureSplitter,
    event: ReactPointerEvent<HTMLDivElement>,
  ) => {
    const workbench = workbenchRef.current;
    const treeColumn = workbench?.querySelector<HTMLElement>(
      ".native-hierarchy-tree",
    );
    const detailColumn = workbench?.querySelector<HTMLElement>(
      ".native-hierarchy-detail",
    );
    const sourceColumn = workbench?.querySelector<HTMLElement>(
      ".native-hierarchy-source",
    );
    if (!treeColumn || !detailColumn || !sourceColumn) return;
    const treeWidth = treeColumn.getBoundingClientRect().width;
    const detailWidth = detailColumn.getBoundingClientRect().width;
    const sourceWidth = sourceColumn.getBoundingClientRect().width;
    resizeDragRef.current = {
      splitter,
      pointerId: event.pointerId,
      startX: event.clientX,
      totalWidth: treeWidth + detailWidth + sourceWidth,
      treeWidth,
      detailWidth,
      sourceWidth,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  };

  const moveColumnResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = resizeDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const nextX = event.clientX;
    if (resizeFrameRef.current !== null) {
      cancelAnimationFrame(resizeFrameRef.current);
    }
    resizeFrameRef.current = requestAnimationFrame(() => {
      const delta = nextX - drag.startX;
      let treeWidth = drag.treeWidth;
      let detailWidth = drag.detailWidth;
      let sourceWidth = drag.sourceWidth;
      if (drag.splitter === "TREE_DETAIL") {
        treeWidth = Math.max(
          280,
          Math.min(
            drag.treeWidth + delta,
            drag.totalWidth - drag.sourceWidth - 340,
          ),
        );
        detailWidth = drag.totalWidth - treeWidth - sourceWidth;
      } else {
        detailWidth = Math.max(
          340,
          Math.min(
            drag.detailWidth + delta,
            drag.totalWidth - drag.treeWidth - 340,
          ),
        );
        sourceWidth = drag.totalWidth - treeWidth - detailWidth;
      }
      updateColumnShares(() => ({
        tree: (treeWidth / drag.totalWidth) * 100,
        detail: (detailWidth / drag.totalWidth) * 100,
        source: (sourceWidth / drag.totalWidth) * 100,
      }));
      resizeFrameRef.current = null;
    });
  };

  const endColumnResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (resizeDragRef.current?.pointerId !== event.pointerId) return;
    resizeDragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const nudgeColumnResize = (
    splitter: StructureSplitter,
    event: ReactKeyboardEvent<HTMLDivElement>,
  ) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const direction = event.key === "ArrowRight" ? 1 : -1;
    const step = event.shiftKey ? 4 : 1.5;
    updateColumnShares((current) => {
      const next = { ...current };
      if (splitter === "TREE_DETAIL") {
        const delta = Math.max(
          -current.tree + 18,
          Math.min(direction * step, current.detail - 22),
        );
        next.tree += delta;
        next.detail -= delta;
      } else {
        const delta = Math.max(
          -current.detail + 22,
          Math.min(direction * step, current.source - 22),
        );
        next.detail += delta;
        next.source -= delta;
      }
      return next;
    });
  };

  const locatePage = (page: number) => {
    setFocusedProposalIds([]);
    setFocusedEvidenceOverlay(null);
    onPageChange(page);
    if (showSourcePreview) setCompactPanel("source");
  };

  const locateProposal = (proposals: NativeStructureProposalV1[]) => {
    if (!proposals.length) return;
    setFocusedEvidenceOverlay(null);
    setFocusedProposalIds(proposals.map((proposal) => proposal.proposal_id));
    onPageChange(proposals[0].page);
    if (showSourcePreview) setCompactPanel("source");
  };

  const locateContent = (content: NativeStructureSourceContentV1) => {
    setFocusedProposalIds([]);
    setFocusedEvidenceOverlay({
      id: `content:${content.content_id}`,
      page: content.page,
      type: "TEXT",
      label: nodeTitle(selectedNode!),
      bbox: content.bbox,
      sourceBounds: content.source_bounds,
    });
    onPageChange(content.page);
    if (showSourcePreview) setCompactPanel("source");
  };

  const locateTableFragment = (
    table: NativeStructureTablePreviewV1,
    fragment: NativeStructureTablePreviewV1["fragments"][number],
  ) => {
    setFocusedProposalIds([]);
    setFocusedEvidenceOverlay({
      id: `table:${fragment.fragment_id}`,
      page: fragment.page,
      type: "TABLE",
      label: tableDisplayTitle(table),
      bbox: fragment.bbox,
      sourceBounds: fragment.source_bounds,
    });
    onPageChange(fragment.page);
    if (showSourcePreview) setCompactPanel("source");
  };

  const submitFeedback = async () => {
    if (host.readOnly) return;
    const normalized = message.trim();
    if (!reference || !selectedNode || !normalized || submitting) return;
    setSubmitting(true);
    setError("");
    setFeedbackNotice("");
    try {
      if (sourceReader) {
        await sourceReader.saveFeedback(normalized, selectedNode.node_id, []);
        clearSourceFeedbackDraft(externalDraftKey, message);
        setFeedbackNotice("指引已保存，等待当前插件任务取回并修订。");
        return;
      }
      const result = await api.submitNativeStructureFeedback(projectId, documentId, {
        schema_version: "native-structure-feedback-request/v1",
        command_id: commandId(),
        expected_source_sha256: reference.source_sha256,
        expected_artifact_id: reference.artifact_id,
        scope_kind: "STRUCTURE_NODE",
        scope_id: selectedNode.node_id,
        message: normalized,
      });
      setReference((current) =>
        current
          ? {
              ...current,
              feedbacks: [
                ...current.feedbacks.filter(
                  (item) => item.feedback_id !== result.feedback.feedback_id,
                ),
                result.feedback,
              ],
              continuation: {
                ...current.continuation,
                status: "READY_TO_CONTINUE",
                can_continue: true,
                pending_feedback_count:
                  current.continuation.pending_feedback_count + 1,
                message: "有新的节点指引，可继续交给 Agent。",
              },
            }
          : current,
      );
      setDraftsByNode((current) => {
        const next = { ...current };
        delete next[selectedNode.node_id];
        return next;
      });
      setFeedbackNotice(result.delivery_message);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setSubmitting(false);
    }
  };

  const continueWithFeedback = async () => {
    if (host.readOnly || sourceReader) return;
    if (!reference?.continuation.can_continue || continuing) return;
    setContinuing(true);
    setError("");
    setContinueNotice("");
    try {
      const result = await api.continueNativeStructureReference(
        projectId,
        documentId,
        {
          schema_version: "native-structure-continue-request/v1",
          command_id: commandId("native-structure-continue"),
          expected_source_sha256: reference.source_sha256,
          expected_artifact_id: reference.artifact_id,
        },
      );
      const refreshed = await api.nativeStructureReference(projectId, documentId);
      authoritativeReferenceRef.current = refreshed;
      activityProjectionLifecycleRef.current =
        nativeStructureProjectionLifecycleKey(refreshed.continuation);
      setReference(refreshed);
      setContinueNotice(result.delivery_message);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setContinuing(false);
    }
  };

  const decideProposalGroup = async (
    proposals: NativeStructureProposalV1[],
    action: "CONFIRM" | "UNDO" | "KEEP_UNRESOLVED",
  ) => {
    if (host.readOnly || sourceReader || !reference || !proposals.length || proposalMutation) return;
    const mutationKey = `${proposals[0].finding_code}:${action}`;
    setProposalMutation(mutationKey);
    setProposalNotice("");
    setError("");
    try {
      await Promise.all(
        proposals.map((proposal) =>
          api.submitNativeStructureProposalDecision(projectId, documentId, {
            schema_version: "native-structure-proposal-decision-request/v1",
            command_id: commandId("native-structure-proposal-decision"),
            expected_source_sha256: reference.source_sha256,
            expected_artifact_id: reference.artifact_id,
            proposal_id: proposal.proposal_id,
            action,
          }),
        ),
      );
      const refreshed = await api.nativeStructureReference(projectId, documentId);
      authoritativeReferenceRef.current = refreshed;
      activityProjectionLifecycleRef.current =
        nativeStructureProjectionLifecycleKey(refreshed.continuation);
      setReference(refreshed);
      const imageOnly = proposals.every(
        (proposal) => proposal.object_kind === "IMAGE_PLACEMENT",
      );
      const requiresLocalProcessing = proposals.some(
        (proposal) =>
          proposal.recommended_action === "RUN_SCOPED_VISUAL_CELL_RECOVERY",
      );
      setProposalNotice(
        action === "CONFIRM"
          ? requiresLocalProcessing
            ? "已确认处理方向；等待局部证据恢复，不影响其他节点继续。"
            : imageOnly
              ? "已确认 Agent 的图片归属建议。"
              : "已采纳结构建议；将在下一次局部草案中应用。"
          : action === "UNDO"
            ? "已撤销自动归属；图片保留为未决证据。"
            : "已保持未决；其他节点可以继续处理。",
      );
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setProposalMutation("");
    }
  };

  const continuation = reference?.continuation;
  const workingRevisionActive = displayWorkingRevision?.state === "WORKING";
  const workingStructureProjection =
    reference?.structure_projection.state ===
    "WORKING_VERIFICATION_REQUIRED";
  const workingAffectedScopeIds = new Set([
    ...(workingStructureProjection
      ? reference?.structure_projection.affected_scope_ids || []
      : []),
    ...Object.entries(displayWorkingRevision?.node_states || {})
      .filter(([, state]) => state === "WORKING_VERIFIED")
      .map(([nodeId]) => nodeId),
  ]);
  const latestAttempt = continuation?.latest_handoff?.latest_attempt;
  const transportReplayActive =
    displayWorkingRevision?.validator_kind === "ACCEPTED_REFERENCE_REPLAY";
  const hostVerifiedD2Active =
    displayWorkingRevision?.validator_kind === "D2_HOST_COMPILER";
  const attemptSummary = continuation?.latest_handoff?.attempt_summary;
  const uncertainWorkingDecision =
    continuation?.status === "NO_CHANGE" &&
    ["CONFLICTING", "INSUFFICIENT"].includes(
      continuation.evidence_state || "",
    );
  const legacyStructureAgentStatusTitle = continuation?.can_continue
    ? "反馈待继续"
    : continuation?.status === "CONSUMED"
      ? "本轮已完成"
      : continuation?.status === "NO_CHANGE"
        ? uncertainWorkingDecision
          ? "已明确保留当前结构"
          : "本轮无需修改"
      : continuation?.status === "NEEDS_GUIDANCE"
        ? "未决项已继续"
      : continuation?.status === "UNRESOLVED_CONTINUED"
        ? "已按默认策略继续"
      : continuation?.status === "VERIFYING"
        ? "Agent 正在复验"
      : continuation?.status === "REVISION_REQUIRED"
        ? "Agent 正在修正"
        : continuation?.status === "IN_PROGRESS" ||
            continuation?.status === "ACKNOWLEDGED"
      ? "Agent 正在处理"
      : continuation?.status === "QUEUED_FOR_CODEX_APP"
        ? "Agent 待读取"
        : "本轮结构已生成";
  const legacyStructureAgentCurrentAction = continuation?.can_continue
    ? `${continuation.pending_feedback_count} 条指引、${continuation.pending_proposal_decision_count} 项选择等待继续`
    : continuation?.status === "CONSUMED"
      ? latestAttempt?.public_summary || continuation.message
      : continuation?.status === "NO_CHANGE"
        ? uncertainWorkingDecision
          ? `${continuation.latest_handoff?.current_action || continuation.message} · 已给出工作判断，证据状态保留，不阻断下一阶段。`
          : continuation.latest_handoff?.current_action || continuation.message
      : continuation?.status === "NEEDS_GUIDANCE" ||
          continuation?.status === "UNRESOLVED_CONTINUED"
        ? `${continuation.latest_handoff?.current_action || continuation.message} · 未决标记保留，不阻断下一阶段；可随时反馈。`
      : continuation?.status === "REVISION_REQUIRED" ||
          continuation?.status === "VERIFYING" ||
          continuation?.status === "IN_PROGRESS" ||
          continuation?.status === "ACKNOWLEDGED"
      ? continuation.latest_handoff?.current_action || continuation.message
      : continuation?.status === "QUEUED_FOR_CODEX_APP"
        ? "反馈已提交，等待 Agent 真实读取回执"
      : continuation?.message || "等待你的指引";
  const structureAgentStatusTitle = transportReplayActive
    ? "传输回放 · 非 Agent 重解析"
    : hostVerifiedD2Active
      ? "Host 已验证工作结构"
      : legacyStructureAgentStatusTitle;
  const structureAgentCurrentAction = transportReplayActive
    ? `正在回放第 ${displayWorkingRevision?.revision || 0} 版不可变结构投影；未重新调用 Agent。`
    : hostVerifiedD2Active
      ? `第 ${displayWorkingRevision?.revision || 0} 版工作结构已通过来源验证。${workingRevisionActive ? "当前为局部结果，其余范围尚未发布；可先核对已恢复的正文和表格。" : ""}`
      : legacyStructureAgentCurrentAction;
  const sourceRequirementMode = mode === "SOURCE_REQUIREMENTS";
  const sourceRequirementRunJob = sourceRequirementRunStatus?.job || null;
  const sourceRequirementRunJobStatus =
    sourceRequirementRunJob?.status || sourceRequirementRunStatus?.status || "IDLE";
  const sourceRequirementRunActive = [
    "PENDING",
    "RUNNING",
    "CANCEL_REQUESTED",
  ].includes(sourceRequirementRunJobStatus);
  const sourceRequirementRunComplete =
    sourceRequirementRunJobStatus === "SUCCEEDED";
  const sourceRequirementRunScopePublished = Boolean(
    sourceRequirementRunComplete &&
      sourceRequirementRunJob &&
      sourceRequirementOutline &&
      (sourceRequirementOutline.baseline.covered_scope_ids.includes(
        sourceRequirementRunJob.scope_id,
      ) ||
        (Boolean(sourceRequirementRunJob.scope_ids?.length) &&
          sourceRequirementRunJob.scope_ids!.every((scopeId) =>
            sourceRequirementOutline.baseline.covered_scope_ids.includes(
              scopeId,
            ),
          ))),
  );
  const sourceRequirementRunFailed = ["FAILED", "CANCELLED"].includes(
    sourceRequirementRunJobStatus,
  );
  const sourceRequirementRunProgress = sourceRequirementRunJob?.progress;
  const sourceRequirementRunProgressLabel = sourceRequirementRunProgress
    ? sourceRequirementRunProgress.total_window_count > 0
      ? `${sourceRequirementRunProgress.completed_window_count}/${sourceRequirementRunProgress.total_window_count} ${
          sourceRequirementRunProgress.strategy === "TEXT_BATCH"
            ? "个正文范围已处理"
            : "个窗口已验证"
        }`
      : sourceRequirementRunProgress.message
    : "";
  const startSourceRequirementScope = async (
    scopeId: string,
    title: string,
  ) => {
    if (host.readOnly || sourceReader) return;
    if (
      !reference ||
      !sourceRequirementMode ||
      sourceRequirementRunActive ||
      sourceRequirementRunStartingScopeId
    ) {
      return;
    }
    setSourceRequirementRunStartingScopeId(scopeId);
    setSourceRequirementRunError("");
    try {
      const job = await api.startSourceRequirementBaselineRun(
        projectId,
        documentId,
        {
          schema_version: "source-requirement-baseline-start-request/v1",
          command_id: commandId("source-requirement-scope"),
          expected_source_sha256: reference.source_sha256,
          expected_artifact_id: reference.artifact_id,
          logical_table_scope_id: scopeId,
        },
      );
      setSourceRequirementRunStatus({
        schema_version: "source-requirement-baseline-run-status/v1",
        status: job.status,
        result_ready: job.status === "SUCCEEDED",
        job,
        message:
          job.progress.message || `Agent 已接收“${title}”的原文条目处理任务。`,
      });
      setSourceRequirementRunRefreshToken((current) => current + 1);
    } catch (caught) {
      setSourceRequirementRunError(
        caught instanceof ApiError
          ? caught.message
          : `“${title}”暂时无法启动；原表与已有条目保持不变。`,
      );
    } finally {
      setSourceRequirementRunStartingScopeId("");
    }
  };
  const startSourceItemTextScope = async (
    nodeId: string,
    title: string,
  ) => {
    if (host.readOnly || sourceReader) return;
    const scopeId = `text:${nodeId}`;
    if (
      !reference ||
      !sourceRequirementMode ||
      sourceRequirementRunActive ||
      sourceRequirementRunStartingScopeId
    ) {
      return;
    }
    setSourceRequirementRunStartingScopeId(scopeId);
    setSourceRequirementRunError("");
    try {
      const job = await api.startSourceItemTextBaselineRun(
        projectId,
        documentId,
        {
          schema_version: "source-item-text-baseline-start-request/v1",
          command_id: commandId("source-item-text-scope"),
          expected_source_sha256: reference.source_sha256,
          expected_artifact_id: reference.artifact_id,
          node_ids: [nodeId],
        },
      );
      setSourceRequirementRunStatus({
        schema_version: "source-requirement-baseline-run-status/v1",
        status: job.status,
        result_ready: job.status === "SUCCEEDED",
        job,
        message:
          job.progress.message || `Agent 已接收“${title}”的直接正文处理任务。`,
      });
      setSourceRequirementRunRefreshToken((current) => current + 1);
    } catch (caught) {
      setSourceRequirementRunError(
        caught instanceof ApiError
          ? caught.message
          : `“${title}”的直接正文暂时无法启动；已有条目保持不变。`,
      );
    } finally {
      setSourceRequirementRunStartingScopeId("");
    }
  };
  const agentStatusTitle = sourceRequirementMode
    ? sourceRequirementRunActive
      ? sourceRequirementRunJobStatus === "PENDING"
        ? "Agent 任务已排队"
        : "Agent 正在生长原文条目"
      : sourceRequirementRunComplete
        ? sourceRequirementRunScopePublished
          ? "当前范围已长入清单"
          : "当前范围候选已验证"
        : sourceRequirementRunFailed
          ? "当前范围可从检查点重试"
          : sourceRequirementOutline
      ? "当前原文清单"
      : "等待原文需求清单"
    : structureAgentStatusTitle;
  const agentCurrentAction = sourceRequirementMode
    ? sourceRequirementRunActive
      ? sourceRequirementRunProgress?.message ||
        `正在处理当前原文范围${sourceRequirementRunProgressLabel ? ` · ${sourceRequirementRunProgressLabel}` : ""}。`
      : sourceRequirementRunComplete
        ? sourceRequirementRunScopePublished
          ? `当前范围已合并到原文需求清单第 ${sourceRequirementOutline?.baseline.revision || 0} 版；原表、条目与 PDF 证据保持同一层级。`
          : `${sourceRequirementRunStatus?.message || "当前范围已完成。"} Host 已保存候选与检查点，下一步合并到工作清单修订。`
        : sourceRequirementRunFailed
          ? sourceRequirementRunStatus?.message ||
            "当前范围没有完成；已接受窗口仍保留，重试只处理失败窗口。"
          : sourceRequirementOutline
      ? `${sourceRequirementOutline.summary.requirement_count} 条需求 · ${sourceRequirementOutline.summary.non_requirement_count} 条保留内容。可核对原文、按需拆分并继续分析。`
      : sourceRequirementError || "原文结构保持可浏览；Agent 切片尚未发布。"
    : structureAgentCurrentAction;
  const publicActivityKinds = new Set(
    (reference?.activity || []).map((activity) => String(activity.kind || "")),
  );
  const structureRunFinished = [
    "CONSUMED",
    "NO_CHANGE",
    "UNRESOLVED_CONTINUED",
  ].includes(continuation?.status || "");
  const structureRunCompletion = hostVerifiedD2Active
    ? [true, true, !workingRevisionActive, !workingRevisionActive]
    : [
    true,
    publicActivityKinds.has("VISUAL_OPENED") ||
      publicActivityKinds.has("SCOPE_REVIEWED") ||
      publicActivityKinds.has("CANDIDATE_ASSEMBLED") ||
      structureRunFinished,
    publicActivityKinds.has("CANDIDATE_ASSEMBLED") ||
      publicActivityKinds.has("VALIDATION_PASSED") ||
      structureRunFinished,
    publicActivityKinds.has("VALIDATION_PASSED") || structureRunFinished,
  ];
  const currentStructureRunStep = structureRunCompletion.findIndex(
    (complete) => !complete,
  );
  const structureRunSteps = [
    "准备原文证据",
    "理解全文范围",
    "逐层生长结构",
    "复验来源覆盖",
  ].map((label, index) => ({
    label,
    state: structureRunCompletion[index]
      ? "complete"
      : index === currentStructureRunStep
        ? "current"
        : "pending",
  }));
  const agentFocusPages = activeNativeStructureFocusPages(
    continuation?.status || "",
    liveActivity,
  );

  const legacyHistory = (
        <details className="native-hierarchy-status-details">
          <summary>{documentAgent ? "成果生成记录" : "运行轨迹"}</summary>
          <div className="native-hierarchy-agent-trace">
            <header>
              <div>
                <span className="native-hierarchy-live-dot" aria-hidden="true" />
                <strong>{agentStatusTitle}</strong>
              </div>
              <small>{sourceRequirementMode ? "原文需求清单" : "文档结构恢复"}</small>
              <p>
                {sourceRequirementMode
                  ? "沿原文层级逐步生成可定位、可分类、可导出的原文条目"
                  : "将文档转化为可逐条核对的原文结构"}
              </p>
            </header>
            <section className="native-hierarchy-agent-current">
              <span>当前行动</span>
              <p>{agentCurrentAction}</p>
            </section>
            {!sourceRequirementMode && latestAttempt ? (
              <section className="native-hierarchy-agent-attempt">
                <span>本轮可核对记录</span>
                <ul>
                  <li>
                    工具：
                    {attemptSummary?.tool_call_count
                      ? `${attemptSummary.tool_call_count} 次 · ${attemptSummary.tool_intents
                          .map((item) => structureToolIntentLabel(item))
                          .join("、")
                        }`
                      : latestAttempt.tool_calls.length
                        ? latestAttempt.tool_calls
                            .map((item) => structureToolIntentLabel(item.intent))
                            .join("、")
                      : "现有证据充分，未额外调用"}
                  </li>
                  <li>
                    Agent 修订：
                    {attemptSummary?.candidate_revision_count ??
                      latestAttempt.candidate_revision_count} 次
                  </li>
                  {latestAttempt.default_applied_work_item_count ? (
                    <li>
                      可逆默认处理：
                      {latestAttempt.default_applied_work_item_count} 项 ·
                      未决标记保留
                    </li>
                  ) : null}
                  <li>
                    判断依据：
                    {latestAttempt.evidence_state === "CONFLICTING"
                      ? "证据存在冲突 · 已采用首选可逆方案"
                      : latestAttempt.evidence_state === "INSUFFICIENT"
                        ? "证据有限 · 已采用首选可逆方案"
                        : "证据充分"}
                  </li>
                  {latestAttempt.affected_scope_ids.length > 1 ? (
                    <li>
                      范围复验：
                      {latestAttempt.rendered_reopened_scope_ids?.length || 0}/
                      {latestAttempt.affected_scope_ids.length} · 按来源顺序继续
                    </li>
                  ) : null}
                  <li>
                    Host 复验：
                    {latestAttempt.validation_status === "PASS"
                      ? "通过"
                      : latestAttempt.status === "VERIFICATION_REQUIRED"
                        ? "待重新打开确认"
                      : "未通过，继续修正"}
                  </li>
                </ul>
              </section>
            ) : null}
            <div className="native-hierarchy-agent-trace-heading">
              <strong>执行步骤</strong>
              <span>
                {sourceRequirementMode
                  ? (sourceRequirementOutline?.activity.length || 0) +
                    (sourceRequirementRunJob ? 1 : 0)
                  : (reference?.activity.length || 0) + liveActivity.length} 步已记录
              </span>
            </div>
            <ol>
              {(sourceRequirementMode
                ? sourceRequirementOutline?.activity || []
                : []
              ).map((activity) => (
                <li className="is-complete" key={`source-requirement:${activity.sequence}`}>
                  <span aria-hidden="true">
                    <Icon name="check" size={11} />
                  </span>
                  <div>
                    <strong>{activity.title}</strong>
                    <p>{activity.summary}</p>
                  </div>
                </li>
              ))}
              {sourceRequirementMode && sourceRequirementRunJob ? (
                <li
                  className={
                    sourceRequirementRunComplete
                      ? "is-complete"
                      : sourceRequirementRunFailed
                        ? "is-pending"
                        : "is-active"
                  }
                  key={`source-requirement-run:${sourceRequirementRunJob.job_id}`}
                >
                  <span aria-hidden="true">
                    <Icon
                      name={sourceRequirementRunComplete ? "check" : "split"}
                      size={11}
                    />
                  </span>
                  <div>
                    <strong>
                      {sourceRequirementRunComplete
                        ? sourceRequirementRunScopePublished
                          ? "当前表格已长入原文需求清单"
                          : "当前表格候选已通过 Host 验证"
                        : sourceRequirementRunFailed
                          ? "当前表格处理未完成"
                          : "正在识别业务记录并逐窗判断"}
                    </strong>
                    <p>
                      {sourceRequirementRunStatus?.message}
                      {sourceRequirementRunProgressLabel
                        ? ` · ${sourceRequirementRunProgressLabel}`
                        : ""}
                    </p>
                  </div>
                </li>
              ) : null}
              {(!sourceRequirementMode ? reference?.activity || [] : []).map((activity, index) => (
                <li className="is-complete" key={String(activity.sequence || index)}>
                  <span aria-hidden="true">
                    <Icon name="check" size={11} />
                  </span>
                  <div>
                    <strong>{activityKindLabel(activity.kind, index)}</strong>
                    <p>{activityPublicSummary(activity)}</p>
                  </div>
                </li>
              ))}
              {(!sourceRequirementMode ? liveActivity : []).map((activity) => (
                <li
                  className={
                    [
                      "VERIFIED",
                      "NO_CHANGE",
                      "UNRESOLVED_CONTINUED",
                    ].includes(activity.payload.status)
                      ? "is-complete"
                      : activity.payload.status === "NEEDS_GUIDANCE"
                        ? "is-pending"
                        : "is-active"
                  }
                  key={activity.event_id}
                >
                  <span aria-hidden="true">
                    <Icon
                      name={
                        [
                          "VERIFIED",
                          "NO_CHANGE",
                          "UNRESOLVED_CONTINUED",
                        ].includes(activity.payload.status)
                          ? "check"
                          : "split"
                      }
                      size={11}
                    />
                  </span>
                  <div>
                    <strong>{activity.display.title}</strong>
                    <p>{activity.display.summary}</p>
                  </div>
                </li>
              ))}
              {!sourceRequirementMode ? <li
                className={
                  [
                    "ACKNOWLEDGED",
                    "IN_PROGRESS",
                    "VERIFYING",
                    "REVISION_REQUIRED",
                  ].includes(
                    continuation?.status || "",
                  )
                    ? "is-active"
                    : [
                          "CONSUMED",
                          "NO_CHANGE",
                          "UNRESOLVED_CONTINUED",
                        ].includes(
                          continuation?.status || "",
                        )
                      ? "is-complete"
                    : "is-pending"
                }
              >
                <span aria-hidden="true">
                  <Icon name="split" size={11} />
                </span>
                <div>
                  <strong>{agentStatusTitle}</strong>
                  <p>{agentCurrentAction}</p>
                </div>
              </li> : null}
            </ol>
            <footer>
              <span>公开活动摘要，不包含模型隐藏思维链</span>
              {activitySyncNotice ? <p role="status">{activitySyncNotice}</p> : null}
              {continueNotice ? <p role="status">{continueNotice}</p> : null}
              {sourceRequirementMode && sourceRequirementRunError ? (
                <p role="alert">{sourceRequirementRunError}</p>
              ) : null}
            </footer>
          </div>
        </details>
  );
  return (
    <section
      className={cx("native-hierarchy", sourceRequirementMode && "is-source-requirement-mode", purpose === "analysis" && "is-analysis-workbench")}
      aria-label={host.readOnly ? sourceRequirementMode ? "已保存原文清单" : "已保存原文结构" : sourceRequirementMode ? "Agent 原文需求清单" : "Agent 原文结构草案"}
      onKeyDownCapture={event => {
        const target = event.target as HTMLElement;
        if (sourceRequirementMode && event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) && !target.isContentEditable && !target.closest('[role="dialog"]')) {
          event.preventDefault(); reviewSearchRef.current?.focus();
        }
      }}
    >
      <header className="native-hierarchy-header">
        <span className="native-hierarchy-agent-mark" aria-hidden="true">
          <Icon name="split" size={15} />
        </span>
        <div className="native-hierarchy-identity">
          <strong>{sourceRequirementMode ? documentAgent && purpose === "analysis" ? "响应分析" : "原文审阅" : host.readOnly ? "原文结构" : "Agent 原文结构草案"}</strong>
          <span>
            {loading
              ? "正在读取本轮结果"
              : sourceRequirementMode
                ? `${sourceReader ? "" : `第 ${sourceRequirementOutline?.baseline.revision || 1} 版 · `}原文 ${sourceRequirementOutline?.summary.source_item_count || 0} · 需求 ${sourceRequirementOutline?.summary.requirement_count || 0} · 保留 ${sourceRequirementOutline?.summary.non_requirement_count || 0}`
                : sourceReader ? `${tree?.nodes.length || 0} 个结构节点 · 可对照原文` : `文档结构恢复 · 已查看 ${activeVisualSummary.opened_page_count} 页`}
          </span>
          {workingRevisionActive ? (
            <span>
              {transportReplayActive
                ? "传输回放 · 非 Agent 重解析"
                : "工作结构 · Host 已验证"}
            </span>
          ) : null}
          {workingStructureProjection ? (
            <span>工作投影 · 待原文复验</span>
          ) : null}
        </div>
        <section
          hidden={Boolean(documentAgent) || Boolean(sourceReader) || sourceRequirementMode && !sourceRequirementRunActive && !sourceRequirementRunFailed}
          className={cx(
            "native-hierarchy-live-status",
            (sourceRequirementMode
              ? sourceRequirementRunComplete
              : continuation?.can_continue) && "is-ready",
            (sourceRequirementMode
              ? sourceRequirementRunJobStatus === "RUNNING"
              : [
                  "ACKNOWLEDGED",
                  "IN_PROGRESS",
                  "VERIFYING",
                  "REVISION_REQUIRED",
                ].includes(continuation?.status || "")) && "is-running",
            (sourceRequirementMode
              ? sourceRequirementRunJobStatus === "PENDING"
              : continuation?.status === "QUEUED_FOR_CODEX_APP") && "is-queued",
            sourceRequirementMode && sourceRequirementRunFailed && "is-attention",
          )}
          aria-label="Agent 下一步"
          aria-live="polite"
        >
          <span className="native-hierarchy-live-dot" aria-hidden="true" />
          <strong>{agentStatusTitle}</strong>
          <span title={agentCurrentAction}>{agentCurrentAction}</span>
        </section>
        {!sourceReader && !sourceRequirementMode && continuation?.can_continue ? (
          <button
            type="button"
            className="button primary compact native-hierarchy-continue-button"
            disabled={continuing}
            onClick={() => void continueWithFeedback()}
          >
            {continuing
              ? "正在提交…"
              : continuation.pending_feedback_count
                ? `继续处理 ${continuation.pending_feedback_count} 条指引`
                : "继续处理本轮选择"}
          </button>
        ) : null}
        {!sourceRequirementMode && tree ? (
          <button
            type="button"
            className={cx(
              "native-hierarchy-atlas-toggle",
              pageAtlasOpen && "is-active",
            )}
            aria-pressed={pageAtlasOpen}
            onClick={() => setPageAtlasOpen((value) => !value)}
          >
            <Icon name="eye" size={14} />
            全文概览
          </button>
        ) : null}
        {!sourceReader && sourceRequirementMode && sourceRequirementOutline && purpose === "analysis" ? <SourceItemDeliveryActions workflow={sourceItemReasoning} /> : null}
        {!sourceReader && sourceRequirementMode && sourceRequirementOutline && <SourceItemAgentLoop projectId={projectId} documentId={documentId}
          sourceSha256={sourceSha256} baselineSha256={sourceRequirementOutline.baseline.artifact_sha256}
          selectedSourceItemId={selectedSourceItemId} selectedLabel={selectedSourceItem?.requirement_display_id || selectedSourceItem?.source_display_id || undefined} onUpdated={() => setSourceRequirementRefreshToken(value => value + 1)} />}
        {sourceRequirementMode && (documentAgent || sourceReader) && <div className="review-presentation-tabs" role="group" aria-label="审阅方式">
          <button type="button" aria-pressed={reviewPresentation === "table"} title="逐项检查编号、分类、派生和响应；保持当前查看版本" onClick={() => { setReviewPresentation("table"); setCompactPanel("content"); setWideReading(false); }}><Icon name="matrix" size={14} />条目台账</button>
          <button type="button" aria-pressed={reviewPresentation === "source"} title="核对结构、完整内容和原页；切换不会重新提取" onClick={() => { setReviewPresentation("source"); setWideReading(false); }}><Icon name="tree" size={14} />原页对照</button>
        </div>}
        {sourceRequirementMode && showSourcePreview && (!(documentAgent || sourceReader) || reviewPresentation === "source") && <button type="button" className="button secondary compact source-review-wide-toggle"
          aria-pressed={wideReading} onClick={() => { setWideReading(value => !value); setCompactPanel("content"); }}>
          {wideReading ? "恢复 PDF 对照" : "宽屏阅读"}</button>}
        {!sourceReader && (documentAgent ? documentAgent.historyHost ? createPortal(legacyHistory, documentAgent.historyHost) : null : legacyHistory)}
        <SourceFindingsPanel value={sourceReader?.source_findings || sourceRequirementOutline?.source_findings || reference?.source_findings} sourceSha256={sourceSha256}
          onLocate={(nodeId, page) => { const row = sourceItemLedgerRowsById.get(nodeId); const node = nodesById.get(nodeId); if (row) focusSourceItemLedgerRow(row); else if (node) focusTreeNode(node); if (page) onPageChange(page); setReviewPresentation("source"); setCompactPanel("source"); }} />
        {!sourceReader && !sourceRequirementMode && !documentAgent ? (
          <ol
            className="native-hierarchy-progress-strip"
            aria-label="原文结构运行阶段"
          >
            {structureRunSteps.map((step) => (
              <li className={`is-${step.state}`} key={step.label}>
                <span aria-hidden="true">
                  {step.state === "complete" ? (
                    <Icon name="check" size={10} />
                  ) : null}
                </span>
                <strong>{step.label}</strong>
              </li>
            ))}
          </ol>
        ) : null}
      </header>

      {!sourceReader && purpose === "analysis" && <details className="analysis-knowledge-scope">
        <summary>下次分析依据 <span>{sourceItemReasoning.knowledgeSelection.ids === null ? "项目默认资料" : `已选 ${sourceItemReasoning.knowledgeSelection.ids.length} 份资料`}</span></summary>
        <ProjectKnowledgePanel projectId={projectId} selection={{ ids: sourceItemReasoning.knowledgeSelection.ids, onChange: sourceItemReasoning.knowledgeSelection.setIds, durable: sourceItemReasoning.knowledgeSelection.durable, disabled: sourceItemReasoning.busy }} />
      </details>}
      {purpose === "analysis" && <section className="response-queue-bar" aria-label="响应工作队列">
        <div><strong>响应工作队列</strong><span>按实际分析单元计数 · 保留完整原文关系</span></div>
        <div className="response-queue-filters">{ANALYSIS_QUEUES.map(([value, label]) => <button key={value} type="button" disabled={!sourceItemReasoning.state} aria-pressed={analysisQueue === value} onClick={() => setAnalysisQueue(value)}>{label}<span>{sourceItemReasoning.state ? responseUnits.filter(unit => inAnalysisQueue(unit, value)).length : "…"}</span></button>)}</div>
      </section>}
      <div className="native-hierarchy-body">
        {!sourceRequirementMode && workingRevisionAnnouncement ? (
          <p
            className="native-hierarchy-revision-announcement"
            role="status"
            aria-live="polite"
          >
            {workingRevisionAnnouncement}
          </p>
        ) : null}
        {!sourceRequirementMode && workingRevisionNotice ? (
          <p className="native-hierarchy-revision-notice" role="status">
            {workingRevisionNotice}
          </p>
        ) : null}
        {error ? (
          <div className="native-hierarchy-error" role="alert">
            <strong>当前操作没有完成</strong>
            <span>{error}</span>
          </div>
        ) : null}

        {pageAtlasOpen && tree && !isOfficeSource ? (
          <NativeStructurePageAtlas
            units={activePageOwnership}
            pageCount={activeVisualSummary.page_count}
            receipts={activeVisualSummary.receipts}
            logicalTables={activeLogicalTables}
            unresolved={activeUnresolved}
            proposals={activeStructureProposals}
            activePage={activePage}
            agentFocusPages={agentFocusPages}
            onSelectPage={(page) => {
              onPageChange(page);
              setCompactPanel("source");
            }}
            onClose={() => setPageAtlasOpen(false)}
          />
        ) : null}

        {(reference || displayWorkingRevision) && tree && selectedNode ? (
          <>
            <div
              ref={workbenchRef}
              className={cx(
                "native-hierarchy-workbench",
                hasSourcePreview && "has-source",
                sourceRequirementMode && "is-source-requirement-workbench",
                wideReading && "is-wide-reading",
                sourceRequirementMode && reviewPresentation === "table" && "is-table-view",
              )}
              data-compact-panel={compactPanel}
              style={
                {
                  "--native-tree-share": `${columnShares.tree}fr`,
                  "--native-detail-share": `${columnShares.detail}fr`,
                  "--native-source-share": `${columnShares.source}fr`,
                } as CSSProperties
              }
            >
              <nav
                ref={treeViewportRef}
                className="native-hierarchy-tree"
                aria-label={
                  sourceRequirementMode ? "原文层级与条目" : "文档标题树"
                }
              >
                {sourceRequirementMode ? <SourceReviewToolbar analysisMode={purpose === "analysis"}
                  query={reviewQuery} onQuery={setReviewQuery} view={reviewView} onView={setReviewView}
                  category={reviewCategory} onCategory={setReviewCategory} topic={reviewTopic} onTopic={setReviewTopic}
                  more={reviewMore} onMore={setReviewMore}
                  total={(purpose === "analysis" ? sourceRequirementOutline?.summary.requirement_count : sourceRequirementOutline?.summary.source_item_count) || 0} matched={reviewFilter.matched.size}
                  focus={reviewSummary.focus} attention={reviewSummary.attention} changed={reviewSummary.changed}
                  ready={Boolean(reviewRequest.value)} historyKnown={reviewRequest.value?.history_status === "AVAILABLE"}
                  focusStatus={reviewRequest.value?.focus_annotation_status || "UNAVAILABLE"}
                  error={reviewRequest.error} onRetry={() => setSourceRequirementRefreshToken(value => value + 1)}
                  inputRef={reviewSearchRef}
                  onExpand={() => { setCollapsedDerivedIds(new Set()); setFilterCollapsed(new Set()); setExpandedIds(new Set(sourceItemLedgerExpandableIds)); }}
                  onCollapse={() => { setSelectedDerived(null); setCollapsedDerivedIds(new Set(derivedCounts.keys())); setExpandedIds(new Set()); setFilterCollapsed(new Set(sourceItemLedgerExpandableIds)); }}
                  onLocate={() => { const row = sourceItemLedgerRowsById.get(selectedLedgerRowId); if (row) focusSourceItemLedgerRow(row, selectedDerivedId); }}
                  onChooseFirst={() => { const row = reviewNavigationRows[0]; if (row) {
                    const exact = visibleResponseUnits.find(unit => unit.sourceId === row.source_item_id && unit.displayId?.toLocaleLowerCase() === reviewQuery.trim().toLocaleLowerCase());
                    focusSourceItemLedgerRow(row, exact && exact.id !== row.source_item_id ? exact.id : undefined);
                  } }}
                /> : <>
                <header>
                  <div>
                    <strong>{sourceRequirementMode ? "原文层级" : "文档标题树"}</strong>
                    <span>
                      {tree.nodes.length} 个层级 · 按原文编号与标题展开
                      {sourceRequirementMode
                        ? ` · 已生成 ${sourceRequirementOutline?.summary.source_item_count || 0} 条原文条目`
                        : ""}
                    </span>
                  </div>
                  <div className="native-hierarchy-tree-actions">
                    <button
                      type="button"
                      onClick={() =>
                        setExpandedIds(
                          new Set(
                            sourceRequirementMode
                              ? sourceItemLedgerExpandableIds
                              : expandableNodeIds,
                          ),
                        )
                      }
                    >
                      全部展开
                    </button>
                    <button type="button" onClick={() => setExpandedIds(new Set())}>
                      全部收起
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        if (sourceRequirementMode) {
                          const row = sourceItemLedgerRowsById.get(selectedLedgerRowId);
                          if (row) focusSourceItemLedgerRow(row);
                        } else if (selectedNode) {
                          focusTreeNode(selectedNode);
                        }
                      }}
                    >
                      定位当前
                    </button>
                  </div>
                </header>
                <div className="native-hierarchy-tree-keyboard-hint">
                  ↑↓ 浏览 · ←→ 层级 · ⇧←→ 当前分支 · Home/End 首尾
                </div>
                </>}
                {sourceRequirementMode && reviewPresentation === "table" ? <SourceLedgerTable
                  previewReader={previewReader}
                  rows={visibleSourceItemLedgerRows} nodes={nodesById} records={sourceItemsById} selectedId={selectedLedgerRowId}
                  expandedIds={reviewExpandedIds} onSelect={focusSourceItemLedgerRow}
                  selectedDerivedId={selectedDerivedId} onSelectDerived={focusSourceItemLedgerRow}
                  collapsedDerivedIds={collapsedDerivedIds} visibleDerivedIds={visibleDerivedIds}
                  onToggleDerived={sourceId => { setCollapsedDerivedIds(values => { const next = new Set(values); if (next.has(sourceId)) next.delete(sourceId); else next.add(sourceId); return next; }); if (selectedDerived?.sourceId === sourceId) setSelectedDerived(null); }}
                  onToggle={id => { if (reviewFilter.active) setFilterCollapsed(values => { const next = new Set(values); if (reviewExpandedIds.has(id)) next.add(id); else next.delete(id); return next; }); else toggleNode(id); }}
                  titleFor={row => { const record = row.source_item_id ? sourceItemsById.get(row.source_item_id) : null; const node = nodesById.get(row.structure_node_id); return record ? record.display_label : node ? nodeTitle(node) : "原文内容"; }}
                  kindFor={sourceItemKindSummary} reasoning={sourceItemReasoning.state ? { ...sourceItemReasoning.state, analysis_policy: sourceReader?.analysis_policy || sourceItemReasoning.state.analysis_policy } : null} review={reviewRequest.value} scopeSummaries={sourceRequirementOutline?.scope_summaries || sourceReader?.scope_summaries} /> : (
                <ul
                  ref={treeRef}
                  role="tree"
                  aria-label={sourceRequirementMode ? "原文层级与条目" : "原文标题层级"}
                  onKeyDown={
                    sourceRequirementMode
                      ? handleSourceItemLedgerKeyDown
                      : handleTreeKeyDown
                  }
                >
                  {sourceRequirementMode && sourceRequirementOutline
                    ? sourceRequirementOutline.ledger.root_row_ids
                        .map((rowId) => sourceItemLedgerRowsById.get(rowId))
                        .filter(
                          (row): row is SourceItemLedgerRowV1 => Boolean(row),
                        )
                        .map((row) => (
                          <SourceItemLedgerBranch
                            key={row.row_id}
                            row={row}
                            depth={0}
                            rowsById={sourceItemLedgerRowsById}
                            nodesById={nodesById}
                            sourceItemsById={sourceItemsById}
                            expandedIds={reviewExpandedIds}
                            selectedRowId={selectedLedgerRowId}
                            onSelectStructure={selectNode}
                            onSelectSourceItem={selectSourceItem}
                            onToggle={(id) => {
                              if (reviewFilter.active) setFilterCollapsed(values => { const next = new Set(values); if (reviewExpandedIds.has(id)) next.add(id); else next.delete(id); return next; });
                              else toggleNode(id);
                            }}
                            visibleRows={reviewFilter.visible}
                            reviewContext={reviewRequest.value}
                            reviewCounts={reviewCountsByRow}
                            derivedCounts={derivedCounts}
                            scopeSummaries={sourceRequirementOutline?.scope_summaries || sourceReader?.scope_summaries}
                          />
                        ))
                    : tree.root_ids
                        .map((rootId) => nodesById.get(rootId))
                        .filter(
                          (node): node is NativeStructureTreeNodeV1 =>
                            Boolean(node),
                        )
                        .map((root) => (
                          <StructureTreeBranch
                            key={root.node_id}
                            node={root}
                            depth={0}
                            childrenByParent={childrenByParent}
                            expandedIds={expandedIds}
                            selectedId={selectedNode.node_id}
                            onSelect={selectNode}
                            onToggle={toggleNode}
                            showStructureFindings
                            workingAffectedScopeIds={workingAffectedScopeIds}
                          />
                        ))}
                </ul>
                )}
                {sourceRequirementMode && reviewFilter.active && !reviewFilter.matched.size && <p className="source-review-empty">没有匹配条目。清除筛选可恢复全部原文。</p>}
              </nav>

              {hasSourcePreview ? (
                <div
                  className="native-hierarchy-splitter is-tree-detail"
                  role="separator"
                  aria-label="调整标题树和原文条目宽度"
                  aria-orientation="vertical"
                  aria-valuemin={18}
                  aria-valuemax={55}
                  aria-valuenow={Math.round(columnShares.tree)}
                  tabIndex={0}
                  title="拖动调整列宽；方向键微调；双击恢复默认"
                  onPointerDown={(event) =>
                    beginColumnResize("TREE_DETAIL", event)
                  }
                  onPointerMove={moveColumnResize}
                  onPointerUp={endColumnResize}
                  onPointerCancel={endColumnResize}
                  onKeyDown={(event) =>
                    nudgeColumnResize("TREE_DETAIL", event)
                  }
                  onDoubleClick={resetColumnShares}
                >
                  <span aria-hidden="true" />
                </div>
              ) : null}

              {hasSourcePreview ? (
                <div className="native-hierarchy-compact-bar">
                  <span>
                    查看“{selectedSourceItem?.display_label || nodeTitle(selectedNode)}”
                  </span>
                  <div role="group" aria-label="当前节点查看方式">
                    <button
                      type="button"
                      className={compactPanel === "content" ? "is-active" : ""}
                      aria-pressed={compactPanel === "content"}
                      onClick={() => setCompactPanel("content")}
                    >
                      {sourceRequirementMode
                        ? selectedSourceItem
                          ? "条目内容"
                          : "层级内容"
                        : "节点内容"}
                    </button>
                    <button
                      type="button"
                      className={compactPanel === "source" ? "is-active" : ""}
                      aria-pressed={compactPanel === "source"}
                      onClick={() => setCompactPanel("source")}
                    >
                      PDF 原文
                    </button>
                  </div>
                </div>
              ) : null}

              <article
                className={cx(
                  "native-hierarchy-detail",
                  sourceRequirementMode &&
                    selectedSourceItemId &&
                    "has-source-requirement-records",
                )}
              >
                <nav className="native-hierarchy-breadcrumb" aria-label="当前层级路径">
                  {breadcrumb.filter(node => !selectedSourceItemLedgerRow?.is_one_to_one_item || node.node_id !== selectedNodeId).map((node, index) => (
                    <span key={node.node_id}>
                      {index ? <Icon name="chevron" size={11} /> : null}
                      {nodeTitle(node)}
                    </span>
                  ))}
                </nav>

                {!selectedSourceItemId ? (
                  <header className="native-hierarchy-detail-header">
                  <div>
                    <span>
                      {sourceRequirementMode
                        ? selectedTables.length
                          ? "原文层级 · 总表"
                          : "原文层级"
                        : nodeKindLabel(selectedNode)}
                    </span>
                    <h3>{nodeTitle(selectedNode)}</h3>
                    <small>{pageLabel(selectedPages)}</small>
                  </div>
                  <div className="native-hierarchy-detail-badges">
                    <span
                      className={cx(
                        selectedNode.title_authority ===
                          "DERIVED_FROM_VISIBLE_TABLE_ROLE" && "is-derived",
                      )}
                    >
                      {titleAuthorityLabel(selectedNode)}
                    </span>
                    {!sourceRequirementMode && autoAppliedProposalCount ? (
                      <span className="is-proposal">
                        {autoAppliedProposalCount} 项自动处理
                      </span>
                    ) : null}
                    {!sourceRequirementMode && suggestedProposalCount ? (
                      <span className="is-attention">
                        {suggestedProposalCount} 项处理建议
                      </span>
                    ) : null}
                    {!sourceRequirementMode && unproposedFindings.length ? (
                      <span className="is-attention">
                        {unproposedFindings.length} 项待判断
                      </span>
                    ) : null}
                  </div>
                  </header>
                ) : null}

                {sourceRequirementMode && reviewFilter.active && selectedSourceItemId && !reviewFilter.matched.has(selectedSourceItemId) &&
                  <p className="source-review-outside-filter" role="status">当前详情不在筛选结果内；选择左侧结果即可切换，草稿会保留。</p>}
                {sourceRequirementMode &&
                sourceRequirementOutline &&
                selectedSourceItemId ? (
                  <SourceItemLens
                    sourceReader={sourceReader}
                    onSelectContext={id => {
                      const row = [...sourceItemLedgerRowsById.values()].find(value => value.source_item_id === id);
                      if (row) focusSourceItemLedgerRow(row);
                    }}
                    projectId={projectId}
                    documentId={documentId}
                    sourceSha256={sourceSha256}
                    selectedSourceItemId={selectedSourceItemId}
                    outline={sourceRequirementOutline}
                    reasoning={sourceItemReasoning}
                    review={reviewRequest.value?.records[selectedSourceItemId]}
                    capabilities={reviewRequest.value?.capabilities}
                    analysisMode={purpose === "analysis"}
                    onChangePurpose={onChangePurpose}
                    analysisUnitIds={purpose === "analysis" || reviewPresentation === "table" ? visibleResponseUnits.filter(unit => unit.sourceId === selectedSourceItemId && baseReviewFilter.matched.has(unit.sourceId)).map(unit => unit.id) : undefined}
                    tableView={reviewPresentation === "table"} selectedDerivedUnitId={selectedDerivedId}
                    onAnalysisUnitChange={unitId => { if (selectedSourceItemLedgerRow) selectDerivedUnit(selectedSourceItemLedgerRow, unitId); }}
                    sourceHeading={selectedSourceItemLedgerRow?.is_one_to_one_item && !["AGENT_SURVEY_LABEL", "DERIVED_FROM_VISIBLE_TABLE_ROLE"].includes(selectedNode.title_authority) ? nodeTitle(selectedNode) : undefined}
                    onNextReviewItem={() => { if (nextReviewRow) focusSourceItemLedgerRow(nextReviewRow); }}
                    atReviewEnd={!nextReviewRow}
                    nextReviewLabel={reviewNavigationIndex < 0 ? "首个匹配项" : "下一项"}
                    tablePreviewsById={tablePreviewsById}
                    tableModelsById={verifiedTableModelsById}
                    activePage={activePage}
                    onPageChange={onPageChange}
                    onShowSource={() => { setReviewPresentation("source"); setCompactPanel("source"); }}
                    onLocateNative={ids => { setReviewPresentation("source"); setNativeFocusIds(ids); setCompactPanel("source"); }}
                    onEvidenceChange={setSourceRequirementOverlays}
                    onFeedbackSaved={() =>
                      setSourceRequirementRefreshToken((value) => value + 1)
                    }
                  />
                ) : sourceRequirementMode && sourceRequirementError ? (
                  <div className="source-requirement-state is-error" role="alert">
                    {sourceRequirementError}
                  </div>
                ) : null}

                {!(
                  sourceRequirementMode &&
                  selectedSourceItemId
                ) ? (
                  <section
                    className="native-hierarchy-source-content"
                    aria-label="节点直接原文内容"
                  >
                  <header>
                    <div>
                      <span>当前原文条目</span>
                      <strong>当前标题下的完整原文</strong>
                      <small>从当前标题到下一个标题；不重复汇总下级节点</small>
                    </div>
                    <strong className="native-hierarchy-content-count">
                      {isOfficeSource ? "原件段落与表格" : `${contentSequence.length} 项 · 正文与表格按原页顺序还原`}
                    </strong>
                  </header>
                  {sourceRequirementMode && selectedSourceContent.length ? (() => {
                    const textScopeId = `text:${selectedNode.node_id}`;
                    const textScopeCovered = Boolean(
                      sourceRequirementOutline?.baseline.covered_scope_ids.includes(
                        textScopeId,
                      ),
                    );
                    const currentRunMatchesText =
                      sourceRequirementRunJob?.scope_id === textScopeId;
                    if (textScopeCovered && !currentRunMatchesText) return null;
                    const disabled =
                      Boolean(sourceReader) || Boolean(sourceRequirementRunStartingScopeId) ||
                      sourceRequirementRunActive ||
                      sourceRequirementRunStatus?.status === "UNAVAILABLE" ||
                      (currentRunMatchesText && sourceRequirementRunComplete);
                    return (
                      <section
                        className={cx(
                          "source-requirement-scope-run",
                          "is-direct-text",
                          currentRunMatchesText &&
                            sourceRequirementRunActive &&
                            "is-running",
                          currentRunMatchesText &&
                            sourceRequirementRunComplete &&
                            "is-complete",
                          currentRunMatchesText &&
                            sourceRequirementRunFailed &&
                            "is-attention",
                        )}
                        aria-live="polite"
                      >
                        <div>
                          <strong>
                            {currentRunMatchesText && sourceRequirementRunActive
                              ? "Agent 正在处理当前直接正文"
                              : currentRunMatchesText &&
                                  sourceRequirementRunComplete
                                ? sourceRequirementRunScopePublished
                                  ? "当前正文已长入原文需求清单"
                                  : "当前正文候选已验证"
                                : currentRunMatchesText &&
                                    sourceRequirementRunFailed
                                  ? "当前正文可从候选检查点重试"
                                  : "当前直接正文尚未生长原文条目"}
                          </strong>
                          <span>
                            {currentRunMatchesText
                              ? sourceRequirementRunStatus?.message
                              : sourceRequirementRunActive
                                ? "Agent 正在处理另一范围；当前正文仍可核对。"
                                : "Agent 将保持逐字原文，判断完整条目边界与需求/非需求内容。"}
                          </span>
                          {sourceRequirementRunError ? (
                            <small role="alert">{sourceRequirementRunError}</small>
                          ) : null}
                        </div>
                        <button
                          type="button"
                          className="button secondary compact"
                          disabled={disabled}
                          onClick={() =>
                            void startSourceItemTextScope(
                              selectedNode.node_id,
                              nodeTitle(selectedNode),
                            )
                          }
                        >
                          {sourceRequirementRunStartingScopeId === textScopeId
                            ? "正在提交…"
                            : currentRunMatchesText && sourceRequirementRunActive
                              ? "处理中"
                              : currentRunMatchesText &&
                                  sourceRequirementRunComplete
                                ? sourceRequirementRunScopePublished
                                  ? "已进入清单"
                                  : "等待合并"
                                : currentRunMatchesText &&
                                    sourceRequirementRunFailed
                                  ? "重试当前正文"
                                  : "让 Agent 处理当前正文"}
                        </button>
                      </section>
                    );
                  })() : null}
                  <div className="native-hierarchy-content-sequence">
                    {contentSequence.map((item) => {
                      if (item.kind === "TEXT") {
                        return (
                        <article
                          key={item.id}
                          className="native-hierarchy-source-paragraph"
                        >
                          <p>{item.text}</p>
                          <button
                            type="button"
                            onClick={() => locateContent(item.content)}
                          >
                            P{item.page}
                          </button>
                        </article>
                        );
                      }
                      const scopeId = item.table.logical_table_scope_id;
                      const scopeCovered = Boolean(
                        sourceRequirementOutline?.baseline.covered_scope_ids.includes(
                          scopeId,
                        ),
                      );
                      const currentRunMatchesScope =
                        sourceRequirementRunJob?.scope_id === scopeId;
                      const showScopeRun =
                        sourceRequirementMode &&
                        (!scopeCovered || currentRunMatchesScope);
                      const scopeRunDisabled =
                        Boolean(sourceReader) || Boolean(sourceRequirementRunStartingScopeId) ||
                        sourceRequirementRunActive ||
                        sourceRequirementRunStatus?.status === "UNAVAILABLE" ||
                        (currentRunMatchesScope && sourceRequirementRunComplete);
                      return (
                        <section
                          key={item.id}
                          className={cx(
                            "native-hierarchy-shared-table",
                            item.models.length > 1 && "is-continuation",
                          )}
                          aria-label={
                            item.models.length > 1
                              ? "跨页表格完整内容"
                              : "原文表格内容"
                          }
                        >
                          {showScopeRun ? (
                            <section
                              className={cx(
                                "source-requirement-scope-run",
                                currentRunMatchesScope &&
                                  sourceRequirementRunActive &&
                                  "is-running",
                                currentRunMatchesScope &&
                                  sourceRequirementRunComplete &&
                                  "is-complete",
                                currentRunMatchesScope &&
                                  sourceRequirementRunFailed &&
                                  "is-attention",
                              )}
                              aria-live="polite"
                            >
                              <div>
                                <strong>
                                  {currentRunMatchesScope && sourceRequirementRunActive
                                    ? "Agent 正在处理当前原表"
                                    : currentRunMatchesScope &&
                                        sourceRequirementRunComplete
                                      ? sourceRequirementRunScopePublished
                                        ? "当前原表已长入原文需求清单"
                                        : "当前范围候选已验证"
                                      : currentRunMatchesScope &&
                                          sourceRequirementRunFailed
                                        ? "当前范围可从检查点重试"
                                        : "当前原表尚未生长原文条目"}
                                </strong>
                                <span>
                                  {currentRunMatchesScope
                                    ? sourceRequirementRunStatus?.message
                                    : sourceRequirementRunActive
                                      ? "Agent 正在处理另一范围；当前原表仍可核对，完成后可继续。"
                                      : "Agent 将先识别完整业务记录边界，再按窗口判断需求与非需求。"}
                                  {currentRunMatchesScope &&
                                  sourceRequirementRunProgressLabel
                                    ? ` · ${sourceRequirementRunProgressLabel}`
                                    : ""}
                                </span>
                                {sourceRequirementRunError ? (
                                  <small role="alert">
                                    {sourceRequirementRunError}
                                  </small>
                                ) : null}
                              </div>
                              <button
                                type="button"
                                className="button secondary compact"
                                disabled={scopeRunDisabled}
                                onClick={() =>
                                  void startSourceRequirementScope(
                                    scopeId,
                                    tableDisplayTitle(item.table),
                                  )
                                }
                              >
                                {sourceRequirementRunStartingScopeId === scopeId
                                  ? "正在提交…"
                                  : currentRunMatchesScope &&
                                      sourceRequirementRunActive
                                    ? "处理中"
                                    : currentRunMatchesScope &&
                                        sourceRequirementRunComplete
                                      ? sourceRequirementRunScopePublished
                                        ? "已进入清单"
                                        : "等待合并"
                                      : currentRunMatchesScope &&
                                          sourceRequirementRunFailed
                                        ? "重试失败窗口"
                                        : "让 Agent 处理当前表格"}
                              </button>
                            </section>
                          ) : null}
                          {item.table.evidence_recovery?.status === "RECOVERED" ? (
                            <div
                              className="native-table-evidence-recovered"
                              role="status"
                            >
                              <Icon name="check" size={14} />
                              <div>
                                <strong>
                                  PDF 原生表格已恢复为 {item.table.summary.row_count} 行 ×{" "}
                                  {Math.max(
                                    ...item.table.fragments.map(
                                      (fragment) => fragment.column_count,
                                    ),
                                  )} 列
                                </strong>
                                <span>
                                  旧合并块已弃用；当前页面与 Agent 共用这份经来源校验的单元格证据。
                                </span>
                              </div>
                            </div>
                          ) : null}
                          <StructuredTable
                            table={item.models[0]}
                            tables={item.models}
                            displayLabel={tableDisplayTitle(item.table)}
                            displayLabelAuthority={tableDisplayAuthority(item.table)}
                            fitContainer
                            initialRowLimit={12}
                            projectId={projectId}
                            documentId={documentId}
                            sourceSha256={sourceSha256}
                            onPageChange={(page) => {
                              const fragment = item.table.fragments.find(
                                (candidate) => candidate.page === page,
                              );
                              if (fragment) {
                                locateTableFragment(item.table, fragment);
                              } else {
                                locatePage(page);
                              }
                            }}
                          />
                        </section>
                      );
                    })}
                    {selectedTables.map((table) =>
                      tablePreviewLoadingIds.has(table.scope_id) ? (
                        <div
                          key={`loading:${table.scope_id}`}
                          className="native-hierarchy-table-state"
                          role="status"
                        >
                          <Icon name="matrix" size={15} />
                          <span>正在还原“{tableDisplayTitle(table)}”的原始表格…</span>
                        </div>
                      ) : tablePreviewErrors[table.scope_id] ? (
                        <div
                          key={`error:${table.scope_id}`}
                          className="native-hierarchy-table-state is-error"
                          role="alert"
                        >
                          <span>“{tableDisplayTitle(table)}”暂时无法显示：{tablePreviewErrors[table.scope_id]}</span>
                          <button
                            type="button"
                            onClick={() => void loadTablePreview(table.scope_id)}
                          >
                            重试
                          </button>
                        </div>
                      ) : tableGridErrors[table.scope_id] ? (
                        <div
                          key={`grid-error:${table.scope_id}`}
                          className="native-hierarchy-table-state is-error"
                          role="alert"
                        >
                          <span>
                            “{tableDisplayTitle(table)}”暂不显示：{tableGridErrors[table.scope_id]}
                          </span>
                          <button
                            type="button"
                            onClick={() => void loadTablePreview(table.scope_id)}
                          >
                            重新读取
                          </button>
                        </div>
                      ) : null,
                    )}
                    {isOfficeSource && <div className="native-office-node-content">
                      {selectedSourceContent.map(content => <article key={content.content_id}><p style={{ whiteSpace: "pre-wrap" }}>{content.text}</p></article>)}
                      {selectedTables.map(table => tablePreviewsById[table.scope_id] ? <NativeOfficePreviewTable key={table.scope_id} preview={tablePreviewsById[table.scope_id]} /> : null)}
                    </div>}
                    {!isOfficeSource && !contentSequence.length &&
                    !selectedTables.some((table) =>
                      tablePreviewLoadingIds.has(table.scope_id) ||
                      Boolean(tableGridErrors[table.scope_id]),
                    ) ? (
                      <div className="native-hierarchy-content-empty">
                        <strong>当前标题没有直接正文</strong>
                        <span>
                          {(childrenByParent.get(selectedNode.node_id) || []).length
                            ? "正文属于下级标题；请选择子标题查看，不把后代内容重复堆到父级。"
                            : "只识别到标题或定位证据，尚无可安全还原的直接内容。"}
                        </span>
                      </div>
                    ) : null}
                  </div>
                  </section>
                ) : null}

                {!(
                  sourceRequirementMode &&
                  selectedSourceItemId
                ) ? (
                  <>
                    <section
                      className="native-hierarchy-agent-brief"
                      aria-label="Agent 结构说明"
                    >
                  <span aria-hidden="true">
                    <Icon name="split" size={14} />
                  </span>
                  <div>
                    <strong>Agent 结构说明</strong>
                    <p>
                      {selectedNode.title_authority === "AGENT_SURVEY_LABEL"
                        ? `“${nodeTitle(selectedNode)}”是 Agent 在全文概览中建立的可修订范围，覆盖 ${pageLabel(selectedPages)}。该名称不是原文标题；具体内容将随局部结构恢复逐步呈现。`
                        : selectedNode.kind === "DOCUMENT_ROOT"
                        ? `本轮恢复了 ${tree.nodes.length} 个结构节点；${tableNodeCount} 个节点包含表格，${derivedTitleCount} 个补充单元因原文无明确标题而标记为“Agent 命名”，${activeUnresolved.length} 项关系保持待判断。`
                        : parentNode
                          ? `我将“${nodeTitle(selectedNode)}”放在“${nodeTitle(parentNode)}”下。${titleAuthorityLabel(selectedNode)}，覆盖 ${pageLabel(selectedPages)}；当前绑定 ${selectedSourceContent.length} 个原文内容块${selectedTables.length ? `和 ${selectedTables.length} 张表格` : ""}${autoAppliedProposalCount ? `，已自动处理 ${autoAppliedProposalCount} 个高置信证据归属` : ""}${suggestedProposalCount ? `，提供 ${suggestedProposalCount} 项不阻断建议` : ""}${unproposedFindings.length ? `，另保留 ${unproposedFindings.length} 项待判断` : ""}。`
                          : `“${nodeTitle(selectedNode)}”是一个独立文档单元，覆盖 ${pageLabel(selectedPages)}。${titleAuthorityLabel(selectedNode)}。`}
                    </p>
                  </div>
                </section>

                {selectedProposalGroups.length ? (
                  <section
                    className="native-hierarchy-proposals"
                    aria-label="Agent 处理建议"
                  >
                    <header>
                      <Icon name="check" size={15} />
                      <div>
                        <strong>Agent 发现与处理建议</strong>
                        <span>安全项自动处理；其余建议不阻止其他节点继续</span>
                      </div>
                    </header>
                    <div>
                      {selectedProposalGroups.map(({ key, proposals }) => {
                        const statuses = new Set(
                          proposals.map((proposal) => proposal.status),
                        );
                        const undone =
                          statuses.has("UNDONE") ||
                          statuses.has("KEPT_UNRESOLVED");
                        const confirmed = statuses.size === 1 && statuses.has("CONFIRMED");
                        const autoApplied = statuses.has(
                          "AUTO_APPLIED_PENDING_REVIEW",
                        );
                        const proposed = statuses.has("PROPOSED_CONTINUABLE");
                        const unresolved = statuses.has("UNRESOLVED_CONTINUABLE");
                        const imageGroup = proposals.every(
                          (proposal) => proposal.object_kind === "IMAGE_PLACEMENT",
                        );
                        const first = proposals[0];
                        const pages = Array.from(
                          new Set(proposals.map((proposal) => proposal.page)),
                        ).sort((left, right) => left - right);
                        return (
                          <article key={key}>
                            <div className="native-hierarchy-proposal-copy">
                              <div>
                                <strong>
                                  {imageGroup
                                    ? `${proposals.length} 张图片的父级归属`
                                    : first.proposal_title || "结构处理建议"}
                                </strong>
                                <span
                                  className={cx(
                                    "native-hierarchy-proposal-status",
                                    undone && "is-undone",
                                    confirmed && "is-confirmed",
                                  )}
                                >
                                  {undone
                                    ? statuses.has("KEPT_UNRESOLVED")
                                      ? "保持未决"
                                      : "已撤销"
                                    : confirmed
                                      ? "已确认"
                                      : autoApplied
                                        ? "已自动应用 · 待抽查"
                                        : unresolved
                                          ? "等待局部恢复 · 可继续"
                                          : proposed
                                            ? "建议待确认 · 可继续"
                                            : "可继续"}
                                </span>
                              </div>
                              <p>
                                {imageGroup
                                  ? `这些图片均完整落在“${nodeTitle(selectedNode)}”的已验证表格区域内，且没有竞争表格区域。`
                                  : first.observation}
                              </p>
                              {first.recommendation ? (
                                <p>
                                  <strong>建议：</strong>
                                  {first.recommendation}
                                </p>
                              ) : null}
                              <small>
                                影响：{first.impact}
                              </small>
                            </div>
                            <div className="native-hierarchy-proposal-actions">
                              <button
                                type="button"
                                className="button ghost compact"
                                onClick={() => locateProposal(proposals)}
                              >
                                框选原文位置
                              </button>
                              {undone ? (
                                <button
                                  type="button"
                                  className="button secondary compact"
                                  disabled={host.readOnly || Boolean(sourceReader) || !reference || Boolean(proposalMutation)}
                                  onClick={() =>
                                    void decideProposalGroup(proposals, "CONFIRM")
                                  }
                                >
                                  恢复建议
                                </button>
                              ) : (
                                <>
                                  {!confirmed ? (
                                    <button
                                      type="button"
                                      className="button secondary compact"
                                      disabled={host.readOnly || Boolean(sourceReader) || !reference || Boolean(proposalMutation)}
                                      onClick={() =>
                                        void decideProposalGroup(proposals, "CONFIRM")
                                      }
                                    >
                                      {imageGroup
                                        ? "确认归属"
                                        : unresolved
                                          ? "确认处理方向"
                                          : "采用建议"}
                                    </button>
                                  ) : null}
                                  {autoApplied || confirmed ? (
                                    <button
                                      type="button"
                                      className="button ghost compact"
                                      disabled={host.readOnly || Boolean(sourceReader) || !reference || Boolean(proposalMutation)}
                                      onClick={() =>
                                        void decideProposalGroup(proposals, "UNDO")
                                      }
                                    >
                                      {autoApplied ? "撤销" : "撤销确认"}
                                    </button>
                                  ) : (
                                    <button
                                      type="button"
                                      className="button ghost compact"
                                      disabled={host.readOnly || Boolean(sourceReader) || !reference || Boolean(proposalMutation)}
                                      onClick={() =>
                                        void decideProposalGroup(
                                          proposals,
                                          "KEEP_UNRESOLVED",
                                        )
                                      }
                                    >
                                      保持未决
                                    </button>
                                  )}
                                </>
                              )}
                            </div>
                          </article>
                        );
                      })}
                    </div>
                    {proposalNotice ? (
                      <p className="native-hierarchy-proposal-notice" role="status">
                        {proposalNotice}
                      </p>
                    ) : null}
                  </section>
                ) : null}

                {unproposedFindings.length ? (
                  <section className="native-hierarchy-findings" aria-label="Agent 待判断">
                    <header>
                      <Icon name="evidence" size={15} />
                      <strong>Agent 保留的待判断</strong>
                    </header>
                    <ul>
                      {unproposedFindings.map((finding) => (
                        <li key={finding.code}>
                          {UNRESOLVED_COPY[finding.code] || finding.detail}
                        </li>
                      ))}
                    </ul>
                  </section>
                ) : null}

                <section className="native-hierarchy-pages" aria-label="来源页面">
                  <span>来源页面</span>
                  <div>
                    {selectedPages.map((page) => (
                      <button
                        key={page}
                        type="button"
                        className={activePage === page ? "is-active" : ""}
                        aria-pressed={activePage === page}
                        onClick={() => locatePage(page)}
                      >
                        P{page}
                      </button>
                    ))}
                  </div>
                </section>

                {host.readOnly ? <div className="native-hierarchy-feedback-actions"><button type="button" className="button secondary compact" disabled={!documentAgent?.collaborationAvailable} onClick={() => documentAgent?.show("", "selected")}>在会话中讨论此节点</button></div> : sourceReader && documentAgent?.producer === "EXTERNAL_PLUGIN" && !message && !externalDraft.otherDrafts.length
                  ? <div className="native-hierarchy-feedback-actions"><small>修改意见将绑定当前节点，在协作区统一记录。</small><button type="button" className="button secondary compact" onClick={() => documentAgent.show("", "selected")}>在协作中调整此节点</button></div>
                  : <><label className="native-hierarchy-feedback">
                  <span>
                    告诉 Agent 这个节点应如何调整
                    <strong>保存后等待 Agent 读取</strong>
                  </span>
                  <textarea
                    aria-label="给 Agent 的节点指引"
                    rows={3}
                    maxLength={4000}
                    disabled={!reference}
                    value={message}
                    placeholder="例如：P23、P24 和 P25–29 应保持三个独立的补充文档。"
                    onChange={(event) => sourceReader ? externalDraft.update(event.target.value) :
                      setDraftsByNode((current) => ({
                        ...current,
                        [selectedNode.node_id]: event.target.value,
                      }))
                    }
                  />
                </label>
                <div className="native-hierarchy-feedback-actions">
                  <small>指引将绑定到“{nodeTitle(selectedNode)}”，不会修改其他节点。</small>
                  <button
                    type="button"
                    className="button primary compact"
                    disabled={!reference || !message.trim() || submitting || Boolean(sourceReader?.feedbackCapability && !sourceReader.feedbackCapability.enabled)}
                    onClick={() => void submitFeedback()}
                  >
                    {submitting ? "正在保存…" : "保存指引"}
                  </button>
                </div>
                {sourceReader?.feedbackCapability && !sourceReader.feedbackCapability.enabled && <p className="agent-policy-note">{sourceReader.feedbackCapability.reason || "当前版本暂不可提交指引。"}</p>}
                </>}
                {feedbackNotice ? (
                  <p className="native-hierarchy-feedback-notice" role="status">
                    {feedbackNotice}
                  </p>
                ) : null}
                {sourceReader && externalDraft.otherDrafts.length > 0 && <details>
                  <summary>其他版本的未提交指引（{externalDraft.otherDrafts.length}）</summary>
                  {externalDraft.otherDrafts.map(other => <article key={other.key}><strong>v{other.revision}</strong><p>{other.text}</p>
                    <button type="button" onClick={() => externalDraft.update([message, other.text].filter(Boolean).join("\n\n"))}>追加到当前草稿</button>
                  </article>)}
                </details>}
                {selectedFeedbacks.length ? (
                  <section className="native-hierarchy-feedback-history">
                    <strong>此节点最近的指引</strong>
                    <ul>
                      {selectedFeedbacks.map((feedback) => (
                        <li key={feedback.feedback_id}>
                          <span>{feedback.message}</span>
                          <small>{feedbackStatusLabel(feedback)}</small>
                        </li>
                      ))}
                    </ul>
                  </section>
                ) : null}
                  </>
                ) : null}
              </article>

              {hasSourcePreview ? (
                <div
                  className="native-hierarchy-splitter is-detail-source"
                  role="separator"
                  aria-label="调整原文条目和 PDF 宽度"
                  aria-orientation="vertical"
                  aria-valuemin={45}
                  aria-valuemax={82}
                  aria-valuenow={Math.round(
                    columnShares.tree + columnShares.detail,
                  )}
                  tabIndex={0}
                  title="拖动调整列宽；方向键微调；双击恢复默认"
                  onPointerDown={(event) =>
                    beginColumnResize("DETAIL_SOURCE", event)
                  }
                  onPointerMove={moveColumnResize}
                  onPointerUp={endColumnResize}
                  onPointerCancel={endColumnResize}
                  onKeyDown={(event) =>
                    nudgeColumnResize("DETAIL_SOURCE", event)
                  }
                  onDoubleClick={resetColumnShares}
                >
                  <span aria-hidden="true" />
                </div>
              ) : null}

              {hasSourcePreview && isOfficeSource ? (
                <section className="native-hierarchy-source" aria-label="Office 原文对照">
                  <NativeOfficeSourceView projectId={projectId} documentId={documentId} sourceSha256={sourceSha256}
                    sourceFileUrl={sourceFileUrl} focusIds={nativeFocusIds.length ? nativeFocusIds
                      : (selectedNode.source_anchors || []).map(anchor => anchor.source_handle || anchor.anchor_id)} />
                </section>
              ) : hasSourcePreview ? (
                <section
                  className="native-hierarchy-source"
                  aria-label="原文 PDF 对照"
                >
                  <header>
                    <div>
                      <strong>原文 PDF</strong>
                      <span>
                        {selectedOverlays.length
                          ? approximateOverlayCount
                            ? `${selectedOverlays.length} 个框 · ${approximateOverlayCount} 个粗定位`
                            : `${selectedOverlays.length} 个精确框`
                          : "当前页"}
                      </span>
                    </div>
                    <a
                      className="button ghost compact native-hierarchy-source-open"
                      href={host.pdfViewerUrl(sourceFileUrl, activePage, false)}
                      target="_blank"
                      rel="noreferrer"
                      aria-label="在新窗口打开原文 PDF"
                      title="在新窗口打开原文 PDF"
                    >
                      <Icon name="external" size={14} />
                    </a>
                  </header>
                  <iframe
                    key={sourceFileUrl}
                    ref={pdfFrameRef}
                    title={`原文 PDF · 第 ${activePage} 页`}
                    src={viewerUrl}
                    onLoad={syncPdf}
                    loading="eager"
                  />
                </section>
              ) : null}
            </div>

            {!documentAgent && !sourceReader && <footer className="native-hierarchy-disclosure">
              <span>
                默认只显示原文层级和用户可理解的内容；解析器编号与文件校验信息保留在后台证据中。
              </span>
              <span>
                {sourceRequirementMode
                  ? "当前反馈能力：保存并应用指引 · Agent 局部修订 · 可撤销 · 来源完整性检查"
                  : continuation?.status === "CONSUMED"
                  ? "当前反馈能力：保存交接 · Agent 局部修订 · Host 复验 · 刷新恢复"
                  : [
                        "QUEUED_FOR_CODEX_APP",
                        "ACKNOWLEDGED",
                        "IN_PROGRESS",
                        "VERIFYING",
                        "REVISION_REQUIRED",
                      ].includes(continuation?.status || "")
                    ? "当前反馈能力：真实运行状态同步 · 局部修订 · Host 后置验证"
                    : "当前反馈能力：保存节点指引 · 整文交接 · 无反馈时继续"}
              </span>
            </footer>}
          </>
        ) : loading ? (
          <div className="native-hierarchy-loading" role="status">
            正在读取标题层级与来源证据…
          </div>
        ) : null}
      </div>
    </section>
  );
}
