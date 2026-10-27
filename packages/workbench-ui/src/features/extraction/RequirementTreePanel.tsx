/*
THESIS: One evidence tree replaces the old page stack and refuses parallel flat queues.
OWN-WORLD: cool engineering surfaces, Schaeffler green for action, and source-type blue/green/purple.
STORY: choose a structural clause, read its complete content, verify the exact PDF, then publish or review.
FIRST VIEWPORT: compact command bar over a 350px tree, elastic clause canvas, and a 40% fit-page PDF.
FORM: Operate-mode continuous workbench; hierarchy and source alignment are the memorable mechanism.
*/
import {
  type CSSProperties,
  type Dispatch,
  type KeyboardEvent as ReactKeyboardEvent,
  type SetStateAction,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ApiError,
  api,
  asArray,
  asRecord,
  request,
} from "../../shared/api/client";
import { useScopedUiState } from "../../shared/hooks/useScopedUiState";
import { isSaasAccountMode } from "../../shared/api/saasSession";
import { SAAS_KIT_UNAVAILABLE_MESSAGE, useSaasNativeAgentAvailability } from "./saasAgentAvailability";
import type {
  EntryClassificationPatchCommand,
  EntryClassificationResponse,
  EntryClassificationType,
  JsonRecord,
  NativeParentProvisionStatusV1,
  NativeStructureBaselineStatusV1,
  SourceRequirementBaselineStatusV1,
  RequirementAtomizationScopeState,
  RequirementEnrichmentState,
  RequirementSet,
  RequirementSetSemanticActivationRequestV1,
  RequirementSetSemanticActivationResponseV1,
  RequirementTreeNode,
  RequirementTreeSnapshot,
  RunProfileOverride,
  TableRecordProjectionRowCoordinate,
  TableRecordProjectionRowGroupReview,
} from "../../shared/api/contracts";
import { compactText, truncate, cx } from "../../shared/lib/format";
import {
  EmptyState,
  InlineNotice,
  LoadingBlock,
  StatusBadge,
} from "../../shared/ui/Feedback";
import { Dialog } from "../../shared/ui/Dialog";
import { Icon } from "../../shared/ui/Icon";
import { Tabs } from "../../shared/ui/Tabs";
import {
  buildVisualRestorationOverride,
  CAPABILITY_IDS,
  capabilityReady,
} from "../../shared/runtime/processingCapabilities";
import { StructuredTable } from "../restoration/StructuredTable";
import {
  normalizeStructuredTable,
  normalizeRestorationPage,
  pageBlockEvidenceChannel,
  pageBlocksForEvidenceView,
  sourceCropUrl,
  sourceGeometryForContentBlock,
  tableSegments,
  type PageSnapshot,
  type PageEvidenceView as PageContentView,
  type StructuredTableModel,
} from "../restoration/restorationModel";
import { useWorkspace } from "../workspace/WorkspaceContext";
import type { ExtractionCheckpoint } from "../workspace/route";
import {
  codePointOffset,
  excludeReviewNode,
  mergeReviewNode,
  reviewNodeText,
  reviewSessionView,
  splitReviewNode,
  type ReviewNode,
  type ReviewSessionView,
} from "../preparation/reviewEdits";
import {
  clauseLevelExpandedIds,
  expandableRequirementTreeIds,
  flattenRequirementTree,
  initialVisibleSelectionId,
  initialExpandedIds,
  isDirectContentReviewNode,
  isExactRequirementTreeContentLeaf,
  isPublishedRequirementNode,
  isRequirementTreeReviewItem,
  isRequirementTreeTocNode,
  isWholeTableReviewNode,
  nearestRequirementTreeTableNode,
  parentClause,
  requirementTreeEditableStructureNodeIds,
  requirementTreeNodeStatusLabel as statusLabel,
  requirementTreeAncestorIds,
  requirementTreeNodeDisplayLabel,
  requirementTreeIndex,
  requirementTreeRowSecondaryLabel,
  subtreeExpandableRequirementTreeIds,
  tableRecordFocus,
  type RequirementTreeFilter,
} from "./requirementTreeModel";
import {
  geometryFocusRegionsForNode,
  requirementTreeFocusOverlays,
} from "./requirementTreeFocus";
import {
  draftRequirementPublicationSetId,
  requirementPublicationNotice,
  requirementPublicationSetId,
} from "./requirementPublication";
import { RequirementTreeStageRail } from "./RequirementTreeStageRail";
import {
  ParseDiagnosisLens,
  type ExtractionVisualReceiptSummary,
  type ExtractionVisualRouteIdentity,
} from "./ParseDiagnosisLens";
import {
  normalizeParsingDiagnostics,
  unavailableParsingDiagnostics,
} from "./parsingDiagnosticsModel";
import {
  BoundaryCalibrationWorkbench,
  type BoundaryCalibrationDraftVisual,
} from "./BoundaryCalibrationWorkbench";
import type {
  BoundaryCalibrationOperation,
  BoundaryCalibrationSelection,
} from "./boundaryCalibrationModel";
import { RequirementMatrixWorkbench } from "../matrix/RequirementMatrixWorkbench";
import { EntryClassificationInspector } from "./EntryClassificationInspector";
import { AtomizationScopeInspector } from "./AtomizationScopeInspector";
import { AgentStructureProposalDock } from "./AgentStructureProposalDock";
import {
  NativeStructureReferenceDock,
  type NativeStructureAdvancementState,
} from "./NativeStructureReferenceDock";
import { useDocumentAgent } from "../workspace/DocumentAgentContext";
import { DocumentTaskHome } from "../workspace/DocumentTaskHome";
import { SourceDocumentActions, startSourceDocumentGraph, sourceGraphJobActive, type SourceGraphJob, type SourceDocumentActivity } from "./SourceDocumentActions";
import {
  AgentWorkflowCockpit,
  type AgentWorkflowControl,
  type AgentWorkflowStep,
} from "./AgentWorkflowCockpit";
import {
  atomizationAttemptActionGate,
  atomizationScopeViewModel,
  buildApplyAtomizationScopeWireCommandFromPlan,
  buildAtomizationScopePlan,
  buildUndoAtomizationScopeWireCommand,
  reconcileSelectedAtomizationWorkflowIds,
  type AtomizationScope,
  type AtomizationScopePlan,
  type SelectedAtomizationWorkflowState,
} from "./atomizationScopeModel";
import {
  ENTRY_CLASSIFICATION_OPTIONS,
  assertEntryClassificationMatchesTree,
  assertPublicationReadyForActivation,
  classificationFilterNodeIds,
  classificationWorkflowIdsForScope,
  createEntryClassificationCommandId,
  effectiveEntryClassificationType,
  entryClassificationCommandPayload,
  entryClassificationForNode,
  entryClassificationLabel,
  type EntryClassificationBatchScope,
  type EntryClassificationFilter,
} from "./entryClassificationModel";
import {
  assertRequirementSemanticPublicationPreviewV2,
  assertRequirementSemanticPublishDraftV2,
  buildRequirementSemanticPublicationPreviewRequestV2,
  exactRequirementSemanticPublicationCapabilityV2,
  requirementSemanticPublicationAtomizationBinding,
  type RequirementSemanticPublicationAtomizationBinding,
} from "./semanticPublicationV2Model";

const TREE_ROW_HEIGHT = 48;
const TREE_OVERSCAN = 8;

export function availableDocumentPageCount(...values: unknown[]): number {
  return values.reduce<number>((current, value) => {
    const candidate = Math.floor(Number(value || 0));
    return Number.isFinite(candidate) && candidate > current ? candidate : current;
  }, 0);
}
const LOCAL_STRUCTURE_RUN_OVERRIDE: RunProfileOverride = {
  schema_version: "run-profile-override/v1",
  document: {
    primary_parser: "pymupdf",
    enhancement_chain: [],
    complex_vision: null,
    auto_escalation: false,
  },
  llm_provider: "none",
  force_visual_pages: [],
};

interface KeyedPayloadState {
  requestKey: string;
  status: "LOADING" | "READY" | "ERROR";
  payload: JsonRecord | null;
  error: string;
}

interface KeyedRestorationRunState {
  documentKey: string;
  payload: JsonRecord | null;
  error: string;
}

type PageEvidenceView = "STAGED_CURRENT" | "FORMAL_PREVIOUS";

interface KeyedPageEvidenceViewState {
  stagingKey: string;
  view: PageEvidenceView;
}

interface KeyedPageContentViewState {
  identityKey: string;
  view: PageContentView;
}

interface ReadyPageRange {
  startPage: number;
  endPage: number;
}

function normalizeReadyPageRanges(
  value: unknown,
  totalPages: number,
): ReadyPageRange[] {
  return asArray<JsonRecord>(value)
    .flatMap((candidate) => {
      const startPage = Math.floor(Number(candidate.start_page || 0));
      const endPage = Math.floor(Number(candidate.end_page || 0));
      if (startPage < 1 || endPage < startPage) return [];
      return [
        {
          startPage,
          endPage: totalPages > 0 ? Math.min(endPage, totalPages) : endPage,
        },
      ];
    })
    .filter((range) => range.endPage >= range.startPage);
}

function pageIsReadyInRanges(page: number, ranges: ReadyPageRange[]): boolean {
  return ranges.some(
    (range) => page >= range.startPage && page <= range.endPage,
  );
}

function readyPageRangesLabel(ranges: ReadyPageRange[]): string {
  return ranges
    .map((range) =>
      range.startPage === range.endPage
        ? `P${range.startPage}`
        : `P${range.startPage}–P${range.endPage}`,
    )
    .join("、");
}

export function extractionVisualReceiptSummary(
  result: unknown,
  fallbackProcessingPlan: unknown = null,
): ExtractionVisualReceiptSummary | null {
  const payload = asRecord(result);
  const pageReceipts = asArray<JsonRecord>(payload.page_processing_receipts);
  const receiptStates = new Map<number, "READY" | "DEGRADED">();
  const adoptedProviderIdByPage: Record<number, string> = {};
  pageReceipts.forEach((receipt) => {
    const pageNumber = Math.floor(Number(receipt.page_number || 0));
    const state = String(receipt.state || "").toUpperCase();
    if (pageNumber < 1 || !["READY", "DEGRADED"].includes(state)) return;
    receiptStates.set(pageNumber, state as "READY" | "DEGRADED");
    const adoptedProviderId = String(receipt.adopted_provider_id || "").trim();
    if (adoptedProviderId) {
      adoptedProviderIdByPage[pageNumber] = adoptedProviderId;
    }
  });
  if (receiptStates.size < 1) return null;

  const adoptedProviderIds = Array.from(
    new Set(Object.values(adoptedProviderIdByPage)),
  ).sort();
  const routeIdentity = extractionVisualRouteIdentity(
    payload,
    fallbackProcessingPlan,
    adoptedProviderIds,
  );

  const degradedPages = new Set<number>();
  receiptStates.forEach((state, pageNumber) => {
    if (state === "DEGRADED") degradedPages.add(pageNumber);
  });
  asArray(payload.degraded_page_numbers).forEach((candidate) => {
    const pageNumber = Math.floor(Number(candidate || 0));
    if (pageNumber > 0) degradedPages.add(pageNumber);
  });

  const attemptedPages = new Set(receiptStates.keys());
  degradedPages.forEach((pageNumber) => attemptedPages.add(pageNumber));
  const readyPageCount = Array.from(attemptedPages).filter(
    (pageNumber) =>
      receiptStates.get(pageNumber) === "READY" &&
      !degradedPages.has(pageNumber),
  ).length;
  const tableParsingReceipts = pageReceipts
    .map((receipt) => asRecord(receipt.table_parsing))
    .filter((receipt) => ["OFF", "NATIVE_TABLE_CROPS"].includes(String(receipt.mode || "")));
  const hasTableParsingWork = tableParsingReceipts.some(
    (receipt) =>
      receipt.mode === "NATIVE_TABLE_CROPS" ||
      Number(receipt.candidate_count || 0) > 0 ||
      Number(receipt.attempt_count || 0) > 0 ||
      Number(receipt.budget_skipped_count || 0) > 0,
  );
  const tableParsing = hasTableParsingWork
    ? {
        candidateCount: tableParsingReceipts.reduce(
          (total, receipt) => total + Math.max(0, Number(receipt.candidate_count || 0)),
          0,
        ),
        attemptCount: tableParsingReceipts.reduce(
          (total, receipt) => total + Math.max(0, Number(receipt.attempt_count || 0)),
          0,
        ),
        readyCount: tableParsingReceipts.reduce(
          (total, receipt) => total + Math.max(0, Number(receipt.ready_count || 0)),
          0,
        ),
        failedCount: tableParsingReceipts.reduce(
          (total, receipt) => total + Math.max(0, Number(receipt.failed_count || 0)),
          0,
        ),
        budgetSkippedCount: tableParsingReceipts.reduce(
          (total, receipt) => total + Math.max(0, Number(receipt.budget_skipped_count || 0)),
          0,
        ),
        requestCeilingPerPage: Math.max(
          ...tableParsingReceipts.map((receipt) =>
            Math.max(0, Number(receipt.request_ceiling_per_page || 0)),
          ),
        ),
        requestCeilingPerDocument: Math.max(
          ...tableParsingReceipts.map((receipt) =>
            Math.max(0, Number(receipt.request_ceiling_per_document || 0)),
          ),
        ),
      }
    : undefined;
  return {
    attemptedPageCount: attemptedPages.size,
    readyPageCount,
    degradedPageNumbers: Array.from(degradedPages).sort(
      (left, right) => left - right,
    ),
    routeIdentity,
    adoptedProviderIds,
    adoptedProviderIdByPage,
    tableParsing,
  };
}

function extractionVisualRouteIdentity(
  result: unknown,
  fallbackProcessingPlan: unknown,
  adoptedProviderIds: string[] = [],
): ExtractionVisualRouteIdentity {
  const embeddedPlan = asRecord(asRecord(result).processing_plan);
  const processingPlan = Object.keys(embeddedPlan).length
    ? embeddedPlan
    : asRecord(fallbackProcessingPlan);
  const document = asRecord(processingPlan.document);
  const primaryParser = String(document.primary_parser || "").toLowerCase();
  const enhancementChain = asArray(document.enhancement_chain).map((item) =>
    String(item).toLowerCase(),
  );

  // The frozen route is authoritative. In particular, the current Bailian
  // layout adapter can share a low-level provider identifier with OCR, so an
  // adopted-provider substring must never relabel a layout run as Qwen OCR.
  if (enhancementChain.includes("bailian_layout")) return "BAILIAN_LAYOUT";
  if (enhancementChain.includes("bailian_ocr")) return "QWEN_OCR";
  if (primaryParser === "azure_document_intelligence") {
    return "AZURE_DOCUMENT_INTELLIGENCE";
  }

  const normalizedProviderIds = adoptedProviderIds.map((providerId) =>
    providerId.toLowerCase(),
  );
  if (normalizedProviderIds.some((providerId) => providerId.includes("layout"))) {
    return "BAILIAN_LAYOUT";
  }
  if (
    normalizedProviderIds.some(
      (providerId) =>
        providerId.includes("qwen-ocr") || providerId.includes("bailian-ocr"),
    )
  ) {
    return "QWEN_OCR";
  }
  if (normalizedProviderIds.some((providerId) => providerId.includes("azure"))) {
    return "AZURE_DOCUMENT_INTELLIGENCE";
  }
  return "GENERIC_VISUAL";
}

function visualRepairEligibilitySummary(
  value: unknown,
  routeIdentity: ExtractionVisualRouteIdentity,
): ExtractionVisualReceiptSummary | null {
  const eligibility = asRecord(value);
  const state = String(eligibility.state || "").toUpperCase();
  if (
    routeIdentity !== "QWEN_OCR" ||
    !["AVAILABLE", "NOT_REQUIRED", "UNSUPPORTED"].includes(state)
  ) {
    return null;
  }
  const requiredPageCount = Math.max(
    0,
    Math.floor(Number(eligibility.required_page_count || 0)),
  );
  const readyPageCount = Math.max(
    0,
    Math.floor(Number(eligibility.ready_page_count || 0)),
  );
  const declaredFailedPageCount = Math.max(
    0,
    Math.floor(Number(eligibility.failed_page_count || 0)),
  );
  const degradedPageNumbers = Array.from(
    new Set(
      asArray(eligibility.failed_page_numbers)
        .map((pageNumber) => Math.floor(Number(pageNumber || 0)))
        .filter((pageNumber) => pageNumber > 0),
    ),
  ).sort((left, right) => left - right);
  if (
    requiredPageCount < 1 ||
    declaredFailedPageCount !== degradedPageNumbers.length ||
    readyPageCount + declaredFailedPageCount !== requiredPageCount
  ) {
    return null;
  }
  return {
    attemptedPageCount: requiredPageCount,
    readyPageCount,
    degradedPageNumbers,
    routeIdentity,
    adoptedProviderIds: [],
    adoptedProviderIdByPage: {},
  };
}

interface KeyedEntryClassificationState {
  requestKey: string;
  status: "LOADING" | "READY" | "ERROR";
  payload: EntryClassificationResponse | null;
  error: string;
}

interface KeyedRequirementEnrichmentState {
  requestKey: string;
  status: "LOADING" | "READY" | "ERROR";
  payload: RequirementEnrichmentState | null;
  error: string;
}

interface SemanticPublicationRequestState {
  generation: number;
  contextKey: string;
  identityKey: string;
  controller: AbortController;
}

interface ClassificationMutationRequestState {
  generation: number;
  requestKey: string;
  controller: AbortController;
}

interface PendingLocalDraftActivation {
  classificationRequestKey: string;
  classificationSnapshotId: string;
  classificationSnapshotSha256: string;
  requirementSetId: string;
  published: JsonRecord;
}

function assertSemanticActivationResponse(
  payload: JsonRecord | RequirementSetSemanticActivationResponseV1,
  expected: {
    projectId: string;
    documentId: string;
    requirementSetId: string;
    bindingId: string;
    bindingSha256: string;
  },
): RequirementSetSemanticActivationResponseV1 {
  const activeRequirementSet = asRecord(payload.active_requirement_set);
  const document = asRecord(payload.document);
  if (
    payload.project_id !== expected.projectId ||
    payload.document_id !== expected.documentId ||
    String(activeRequirementSet.id || "") !== expected.requirementSetId ||
    String(activeRequirementSet.enrichment_publication_binding_id || "") !==
      expected.bindingId ||
    String(activeRequirementSet.enrichment_publication_binding_sha256 || "") !==
      expected.bindingSha256 ||
    String(document.id || "") !== expected.documentId ||
    String(document.project_id || "") !== expected.projectId ||
    String(document.active_requirement_set_id || "") !==
      expected.requirementSetId
  ) {
    throw new Error(
      "启用结果无法验证为刚生成的确认清单，系统不会进入确认清单。",
    );
  }
  return payload as RequirementSetSemanticActivationResponseV1;
}

function validatedAtomizationScopeState(
  payload: RequirementEnrichmentState,
  expected: {
    projectId: string;
    documentId: string;
    sessionId: string;
    reviewRevision: number;
    classification: EntryClassificationResponse;
  },
): RequirementAtomizationScopeState {
  const scope = payload.atomization_scope;
  const snapshot = expected.classification.snapshot;
  if (
    payload.schema_version !== "requirement-enrichment-state/v1" ||
    payload.project_id !== expected.projectId ||
    payload.document_id !== expected.documentId ||
    payload.session_id !== expected.sessionId ||
    payload.review_revision !== expected.reviewRevision ||
    !scope ||
    scope.schema_version !== "requirement-atomization-scope-state/v1" ||
    scope.classification_snapshot_id !== snapshot.snapshot_id ||
    scope.classification_revision !== snapshot.revision ||
    scope.classification_sha256 !== snapshot.payload_sha256
  ) {
    throw new Error("原子化范围与当前清单身份不一致，请重新读取清单");
  }
  if (
    scope.capability.explicit_trigger_required !== true ||
    scope.capability.read_has_side_effects !== false ||
    scope.capability.unselected_policy !== "SOURCE_BOUND_PASSTHROUGH"
  ) {
    throw new Error("当前项目暂不能安全执行显式原子化，请刷新后重试");
  }
  return scope;
}

function atomizationMutationMessage(
  cause: unknown,
  operation: "APPLY" | "UNDO",
): string {
  const action = operation === "APPLY" ? "原子化" : "撤销原子化";
  const detail = cause instanceof Error ? cause.message.trim() : "";
  if (cause instanceof ApiError) {
    if (cause.status === 409) {
      return `${detail || "范围版本已变化"}。所选项已保留；范围成功重读后请核对并重试。`;
    }
    if (cause.status === 422) {
      return `${detail || "当前范围不再可执行"}。所选项已保留；请调整范围后重试。`;
    }
    if (cause.status === 503) {
      return `${detail || "原子化服务暂不可用"}。所选项已保留；请稍后重试。`;
    }
  }
  return `${detail || `${action}未完成`}。所选项已保留，请重新读取范围后重试。`;
}

export function parsingDiagnosisOverlays({
  pageSnapshot,
  focusedFactId,
  riskOnly,
  showSupportLayer,
  contentView = "EFFECTIVE",
  visualRecoveryFailed = false,
}: {
  pageSnapshot: PageSnapshot;
  focusedFactId: string;
  riskOnly: boolean;
  showSupportLayer: boolean;
  contentView?: PageContentView;
  visualRecoveryFailed?: boolean;
}) {
  const sourceBounds = pageSnapshot.bounds;
  const diagnosticBlocks = pageBlocksForEvidenceView(
    pageSnapshot,
    contentView,
    showSupportLayer,
    { visualRecoveryFailed },
  );
  const blockOverlays = (riskOnly ? [] : diagnosticBlocks)
    .filter(
      (block) =>
        (block.overlayDefault !== false ||
          (showSupportLayer && block.type === "PAGE_RASTER")) &&
        block.locatable &&
        block.bbox.length === 4,
    )
    .map((block) => ({
      id: block.id,
      type: block.type,
      label: block.label,
      bbox: block.bbox,
      sourceBounds,
      selected: focusedFactId === block.id,
      focus: focusedFactId === block.id,
      precision:
        pageBlockEvidenceChannel(block) === "NATIVE"
          ? "EXACT"
          : "APPROXIMATE",
      channel: pageBlockEvidenceChannel(block),
    }));
  const issueOverlays = pageSnapshot.issues
    .filter(
      (issue) => issue.overlay && issue.locatable && issue.bbox.length === 4,
    )
    .map((issue) => ({
      id: issue.id,
      type: "ISSUE",
      label: issue.label,
      bbox: issue.bbox,
      sourceBounds,
      selected: focusedFactId === issue.id,
      focus: focusedFactId === issue.id,
      precision: "EXACT",
      channel: "" as const,
    }));
  return [...blockOverlays, ...issueOverlays];
}

function setId(set: RequirementSet): string {
  return String(set.id || set.requirement_set_id || "");
}

function extractionStageLabel(stage: string): string {
  const labels: Record<string, string> = {
    queued: "等待后台任务",
    preflight: "校验文件与配置",
    source_ir: "读取逐页解析事实",
    visual_authorization: "确认视觉解析策略",
    visual_validation: "核对视觉解析结果",
    structure_build: "构建原文结构",
    structure_audit: "复核结构关系",
    agent_source_planning: "准备整文来源证据",
    agent_page_evidence: "准备全部授权页面",
    agent_document_understanding: "识别文档结构与原文条目边界",
    scope_planning: "确定需求范围",
    batch_planning: "准备文档条款分析",
    planned: "启动 Codex 文档分析",
    running: "分析文档条款",
    finalizing: "发布新的解析结果",
  };
  return labels[stage.toLowerCase()] || "处理当前文档";
}

export function extractionDecompositionSummary(value: unknown): {
  parents: JsonRecord[];
  parentCount: number;
  obligationCount: number;
} {
  const result = asRecord(value);
  const parents = asArray<JsonRecord>(result.requirement_decomposition);
  return {
    parents,
    parentCount: parents.length,
    obligationCount: parents.reduce(
      (count, parent) => count + asArray<JsonRecord>(parent.obligations).length,
      0,
    ),
  };
}

function safeUtcTimestampMs(value: unknown): number | null {
  const parsed = Date.parse(String(value || ""));
  return Number.isFinite(parsed) ? parsed : null;
}

function visualProviderLatencyLabel(value: unknown): string {
  const latencyMs = Math.max(0, Math.round(Number(value || 0)));
  if (latencyMs < 1000) return `${latencyMs} 毫秒`;
  return `${Number((latencyMs / 1000).toFixed(1))} 秒`;
}

function kindLabel(kind: string): string {
  const labels: Record<string, string> = {
    DOCUMENT: "文档",
    SECTION: "章节",
    CLAUSE: "条款",
    GROUP: "条款组",
    LIST_GROUP: "子条款",
    TABLE: "表格",
    TABLE_RECORD: "表格条目",
    REQUIREMENT: "需求",
  };
  return labels[kind] || "内容";
}

function nodeKindLabel(node: RequirementTreeNode): string {
  if (isPublishedRequirementNode(node)) return "需求";
  if (isRequirementTreeTocNode(node)) return "目录";
  if (isWholeTableReviewNode(node)) return "完整表格";
  if (
    node.kind === "TABLE_RECORD" ||
    node.projection_role === "TABLE_RECORD"
  ) {
    return "表格条目";
  }
  if (node.kind === "TABLE") return "完整表格";
  if (
    isDirectContentReviewNode(node) ||
    node.projection_role === "STRUCTURE_PROXY" ||
    node.kind === "DIRECT_CONTENT" ||
    (node.kind === "REQUIREMENT" && !isPublishedRequirementNode(node))
  ) {
    return "条款内容";
  }
  return kindLabel(node.kind);
}

function nodeIconName(
  node: RequirementTreeNode,
): "document" | "evidence" | "matrix" | "requirement" | "tree" {
  if (["TABLE", "TABLE_RECORD"].includes(node.kind)) return "matrix";
  if (
    node.projection_role === "STRUCTURE_PROXY" ||
    isDirectContentReviewNode(node)
  ) {
    return "evidence";
  }
  if (isPublishedRequirementNode(node)) return "requirement";
  if (node.kind === "DOCUMENT") return "document";
  return "tree";
}

function reviewItemCount(node: RequirementTreeNode): number {
  return Number.isFinite(node.review_item_count ?? Number.NaN)
    ? Number(node.review_item_count)
    : node.requirement_count;
}

function tableProjectionForNode(
  tree: RequirementTreeSnapshot | null,
  node: RequirementTreeNode | null,
): JsonRecord | null {
  if (!tree || !node || !["TABLE", "TABLE_RECORD"].includes(node.kind)) {
    return null;
  }
  const recordId = String(node.table_record_id || "");
  const structureNodeId = String(node.structure_node_id || "");
  const logicalTableIds = new Set(
    [
      ...asArray<JsonRecord>(node.table_refs),
      ...asArray<JsonRecord>(node.content_blocks),
    ]
      .map((item) =>
        String(item.logical_table_id || item.table_id || item.id || ""),
      )
      .filter(Boolean),
  );
  return (
    asArray<JsonRecord>(tree.table_record_projections).find((projection) => {
      if (
        recordId &&
        asArray<JsonRecord>(projection.records).some(
          (record) => String(record.record_id || "") === recordId,
        )
      ) {
        return true;
      }
      const tableFact = asRecord(projection.table_fact);
      return (
        (Boolean(structureNodeId) &&
          String(tableFact.owner_structure_node_id || "") ===
            structureNodeId) ||
        logicalTableIds.has(String(tableFact.logical_table_id || ""))
      );
    }) || null
  );
}

function tableProjectionExplanation(projection: JsonRecord): string {
  const decision = asRecord(projection.decision);
  const explicit = String(
    decision.message ||
      projection.decision_explanation ||
      projection.explanation ||
      projection.decision_reason ||
      "",
  ).trim();
  if (explicit) return explicit;
  const reasonCode = String(
    decision.reason_code || projection.reason_code || "",
  ).toUpperCase();
  const stableReasons: Record<string, string> = {
    STABLE_KEY_COLUMN:
      "首列形成稳定且唯一的业务键，当前按行拆解，并为每条保留共同表头。",
    CONTINUOUS_ORDINAL:
      "首列存在连续编号，行边界可以直接由原表证明，当前按行拆解。",
    EXPLICIT_STRUCTURAL_ROWS:
      "表内存在明确编号的结构行，当前按这些来源边界形成表格条目。",
    NO_PROVEN_RECORD_BOUNDARY:
      "没有找到足以证明业务行边界的稳定表头、编号或键列，因此保留完整原表，避免误拆。",
    MANUAL_SPLIT_ROWS:
      "当前按人工选择拆为表格条目；完整原表和共同表头仍然保留。",
    MANUAL_KEEP_WHOLE:
      "当前按人工选择保留完整原表，不向下生成行条目。",
  };
  if (stableReasons[reasonCode]) return stableReasons[reasonCode];
  const mode = String(projection.mode || "WHOLE_TABLE").toUpperCase();
  const records = asArray<JsonRecord>(projection.records);
  if (mode === "RECORDS") {
    const hasHeaders = records.some(
      (record) => asArray<JsonRecord>(record.header_bindings).length > 0,
    );
    return hasHeaders
      ? `检测到稳定表头与 ${records.length} 个可验证行边界，当前按行形成表格条目；每条仍保留原表头。`
      : `检测到 ${records.length} 个明确编号或稳定行边界，当前按行形成表格条目。`;
  }
  return "没有找到足以证明业务行边界的稳定表头、编号或键列，因此保留完整原表，避免误拆。";
}

function rawTableRows(table: JsonRecord): Array<Array<{
  text: string;
  rowSpan: number;
  colSpan: number;
}>> {
  const cells = asArray<JsonRecord>(table.cells);
  if (!cells.length) {
    return asArray<unknown[]>(table.rows).map((row) =>
      asArray<unknown>(row).map((value) => ({
        text: String(value ?? ""),
        rowSpan: 1,
        colSpan: 1,
      })),
    );
  }
  const rows = new Map<number, Array<{
    column: number;
    text: string;
    rowSpan: number;
    colSpan: number;
  }>>();
  cells.forEach((cell, index) => {
    const rowIndex = Number(cell.row ?? cell.row_index ?? 0);
    const row = rows.get(rowIndex) || [];
    row.push({
      column: Number(cell.column ?? cell.column_index ?? index),
      text: String(
        cell.text ?? cell.raw_text ?? cell.display_value ?? cell.value ?? "",
      ),
      rowSpan: Math.max(1, Number(cell.row_span ?? cell.rowSpan ?? 1)),
      colSpan: Math.max(
        1,
        Number(cell.column_span ?? cell.col_span ?? cell.columnSpan ?? 1),
      ),
    });
    rows.set(rowIndex, row);
  });
  return Array.from(rows.entries())
    .sort(([left], [right]) => left - right)
    .map(([, row]) =>
      row
        .sort((left, right) => left.column - right.column)
        .map(({ text, rowSpan, colSpan }) => ({ text, rowSpan, colSpan })),
    );
}

function ContentTable({ table }: { table: JsonRecord }) {
  const rows = rawTableRows(table);
  if (!rows.length) {
    return (
      <div className="tree-table-empty">
        <Icon name="matrix" />
        <span>已发现表格，但当前结果没有可验证的单元格网格。</span>
      </div>
    );
  }
  return (
    <div className="tree-table-scroll">
      <table className="tree-content-table">
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell, columnIndex) => {
                const Cell = rowIndex === 0 ? "th" : "td";
                return (
                  <Cell
                    key={columnIndex}
                    rowSpan={cell.rowSpan}
                    colSpan={cell.colSpan}
                  >
                    {cell.text}
                  </Cell>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const EVIDENCE_IDENTITY_KEYS = [
  "id",
  "source_id",
  "source_node_id",
  "table_id",
  "logical_table_id",
  "record_id",
  "cell_id",
  "reference_id",
] as const;

function evidenceIdentityValues(value: JsonRecord): string[] {
  const direct = EVIDENCE_IDENTITY_KEYS.map((key) =>
    String(value[key] || ""),
  ).filter(Boolean);
  const captionSources = asArray<JsonRecord>(value.caption_source_refs).flatMap(
    (reference) =>
      EVIDENCE_IDENTITY_KEYS.map((key) => String(reference[key] || "")).filter(
        Boolean,
      ),
  );
  return Array.from(new Set([...direct, ...captionSources]));
}

function normalizedEvidenceText(value: unknown): string {
  return String(value || "")
    .replace(/\s+/g, " ")
    .trim();
}

function compactEvidenceText(value: unknown): string {
  return normalizedEvidenceText(value).replace(/\s+/g, "");
}

function evidenceTextValues(value: JsonRecord): string[] {
  return [
    value.text,
    value.raw_text,
    value.summary,
    value.caption,
    ...asArray<JsonRecord>(value.caption_source_refs).flatMap((reference) => [
      reference.text,
      reference.raw_text,
      reference.summary,
    ]),
  ]
    .map(normalizedEvidenceText)
    .filter(Boolean);
}

export function requirementTreeReparentOperations({
  editableStructureNodeIds,
  targetStructureNodeId,
  structureNodeById,
  targetChildIds,
}: {
  editableStructureNodeIds: string[];
  targetStructureNodeId: string;
  structureNodeById: ReadonlyMap<string, JsonRecord>;
  targetChildIds: readonly string[];
}): JsonRecord[] {
  const targetId = targetStructureNodeId.trim();
  if (!targetId) return [];
  const orderedEditableIds = Array.from(
    new Set(
      editableStructureNodeIds
        .map((nodeId) => nodeId.trim())
        .filter(Boolean),
    ),
  );
  const movingIds = orderedEditableIds.filter((nodeId) => {
    const node = structureNodeById.get(nodeId);
    return node && String(node.parent_id || "") !== targetId;
  });
  const firstSiblingIndex = targetChildIds.length;
  return movingIds.map((nodeId, offset) => ({
    operation: "re_parent",
    structure_node_id: nodeId,
    new_parent_structure_node_id: targetId,
    new_sibling_index: firstSiblingIndex + offset,
  }));
}

function isStructuralReadingNode(node: RequirementTreeNode): boolean {
  return (
    node.projection_role === "STRUCTURE_PROXY" ||
    ["SECTION", "CLAUSE", "LIST_GROUP", "GROUP"].includes(node.kind)
  );
}

function childScopeLabel(
  node: RequirementTreeNode,
  index: ReadonlyMap<string, RequirementTreeNode>,
): string {
  const children = node.child_ids
    .map((childId) => index.get(childId))
    .filter((child): child is RequirementTreeNode => Boolean(child));
  if (!children.length) return "仅标题";
  const allStructural = children.every(isStructuralReadingNode);
  return `仅标题 · ${children.length} 个${allStructural ? "子条款" : "下级"}`;
}

function finiteJsonNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function contentBlockKey(block: JsonRecord): string {
  const kind = String(block.kind || block.type || "").toLowerCase();
  const readingEventId = String(block.reading_event_id || "").trim();
  if (readingEventId) return `event:${readingEventId}`;
  const continuation = asRecord(block.continuation);
  const stableIdentity = String(
    block.id ||
      block.table_id ||
      block.source_id ||
      block.source_node_id ||
      block.asset_id ||
      "",
  );
  if (stableIdentity) {
    const startOffset = finiteJsonNumber(block.start_offset);
    const endOffset = finiteJsonNumber(block.end_offset);
    const sourceOrder = finiteJsonNumber(block.source_order);
    const sourceOffset = finiteJsonNumber(block.source_offset);
    const spanIdentity =
      Number.isInteger(startOffset) && Number.isInteger(endOffset)
        ? `:${startOffset}:${endOffset}`
        : Number.isInteger(sourceOrder) && Number.isInteger(sourceOffset)
          ? `:at:${sourceOrder}:${sourceOffset}`
        : "";
    return `${kind}:${stableIdentity}${spanIdentity}`;
  }
  if (kind === "table") {
    return `table:${String(continuation.logical_table_id || "")}:${String(
      continuation.segment_index ?? block.source_order ?? "",
    )}`;
  }
  // Unbound legacy blocks have no source identity. Returning an empty key
  // preserves each occurrence through the owner/index fallback instead of
  // collapsing two distinct paragraphs merely because their text is equal.
  return "";
}

function contentBlockPage(block: JsonRecord): number {
  const location = asRecord(block.location);
  const page = [
    location.page_number,
    location.page,
    block.page_number,
    block.page_start,
    block.page,
  ]
    .map(Number)
    .find((candidate) => Number.isInteger(candidate) && candidate > 0);
  return page || 0;
}

interface NormalizedTocEntry {
  key: string;
  number: string;
  title: string;
  printedPageLabel: string;
  targetPage: number;
  targetStructureNodeId: string;
  sourceOrder: number;
}

function normalizedTocEntries(node: RequirementTreeNode): NormalizedTocEntry[] {
  return asArray<JsonRecord>(node.toc_entries)
    .map((entry, index) => {
      const number = String(
        entry.number ||
          entry.label_number ||
          entry.section_number ||
          entry.clause_number ||
          "",
      ).trim();
      const title = String(
        entry.title || entry.label || entry.heading || entry.text || "",
      ).trim();
      const targetStructureNodeId = String(
        entry.target_structure_node_id || "",
      ).trim();
      const physicalTargetPage = finiteJsonNumber(entry.target_page);
      const targetPage =
        targetStructureNodeId &&
        physicalTargetPage !== null &&
        Number.isInteger(physicalTargetPage) &&
        physicalTargetPage > 0
          ? physicalTargetPage
          : 0;
      const nativeOrder = finiteJsonNumber(entry.source_order);
      const sourceRefs = asArray<JsonRecord>(entry.source_refs);
      const sourceIdentity = sourceRefs
        .map((ref) =>
          String(
            ref.source_id || ref.semantic_unit_id || ref.cell_id || "",
          ).trim(),
        )
        .find(Boolean);
      return {
        key: String(
          entry.entry_id ||
            entry.id ||
            `${sourceIdentity || "unbound"}:${number}:${title}:${String(
              entry.printed_page_label || "",
            )}:${nativeOrder}:${index}`,
        ),
        number,
        title,
        printedPageLabel: String(entry.printed_page_label || "").trim(),
        targetPage,
        targetStructureNodeId,
        sourceOrder: nativeOrder ?? index,
      };
    })
    .filter((entry) => Boolean(entry.number || entry.title))
    .sort(
      (left, right) =>
        left.sourceOrder - right.sourceOrder ||
        left.key.localeCompare(right.key),
    );
}

export function preferredRequirementTreePage(
  node: RequirementTreeNode | null,
): number {
  if (!node) return 0;
  const contentBlocks = asArray<JsonRecord>(node.content_blocks);
  const mediaBlocks = [
    ...contentBlocks.filter((block) =>
      ["figure", "image"].includes(
        String(block.kind || block.type || "").toLowerCase(),
      ),
    ),
    ...asArray<JsonRecord>(node.figure_refs),
  ];
  const mediaPages = Array.from(
    new Set(mediaBlocks.map(contentBlockPage).filter((page) => page > 0)),
  ).sort((left, right) => left - right);
  const hasReadableNonMedia = contentBlocks.some((block) => {
    const kind = String(block.kind || block.type || "").toLowerCase();
    if (["figure", "image"].includes(kind)) return false;
    if (kind === "table") return true;
    return Boolean(String(block.text || block.raw_text || "").trim());
  });
  if (!hasReadableNonMedia && mediaPages.length > 0) return mediaPages[0];
  return Number(node.page_start) || mediaPages[0] || 0;
}

function structuralTitleCoveredBlocks(
  owner: RequirementTreeNode,
): ReadonlySet<JsonRecord> {
  if (!isStructuralReadingNode(owner)) return new Set();
  const label = compactEvidenceText(requirementTreeNodeDisplayLabel(owner));
  if (!label) return new Set();
  const textBlocks = asArray<JsonRecord>(owner.content_blocks).filter((block) => {
    const kind = String(block.kind || block.type || "").toLowerCase();
    return (
      !["table", "figure", "image"].includes(kind) &&
      Boolean(compactEvidenceText(block.text || block.raw_text))
    );
    },
  );
  if (!textBlocks.length) return new Set();
  for (let start = 0; start < textBlocks.length; start += 1) {
    let combinedText = "";
    for (let end = start; end < textBlocks.length; end += 1) {
      combinedText += compactEvidenceText(
        textBlocks[end].text || textBlocks[end].raw_text,
      );
      if (combinedText === label) {
        return new Set(textBlocks.slice(start, end + 1));
      }
      if (!label.startsWith(combinedText)) break;
    }
  }
  return new Set();
}

export function CompleteContent({
  node,
  selectedItem,
  tree = null,
  pageSnapshot,
  projectId,
  documentId,
  sourceSha256,
  onPageChange,
  onStructureNodeChange,
  recordFocus,
}: {
  node: RequirementTreeNode | null;
  selectedItem: RequirementTreeNode | null;
  tree?: RequirementTreeSnapshot | null;
  pageSnapshot: PageSnapshot;
  projectId: string;
  documentId: string;
  sourceSha256: string;
  onPageChange: (page: number) => void;
  onStructureNodeChange?: (structureNodeId: string) => void;
  recordFocus: ReturnType<typeof tableRecordFocus>;
}) {
  const articleRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!selectedItem?.id || selectedItem.id === node?.id) return;
    const target = articleRef.current?.querySelector<HTMLElement>(
      '[data-review-focus="true"]',
    );
    if (!target || typeof target.scrollIntoView !== "function") return;
    const frame = window.requestAnimationFrame(() => {
      target.scrollIntoView({ block: "center", inline: "nearest" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [node?.id, selectedItem?.id]);

  if (!node) {
    return (
      <EmptyState
        icon="tree"
        title="选择章节、条款、表格或条目"
        description="选择结构节点查看完整上下文；选择候选后保留父条款，并原位标出可证明的来源边界。"
      />
    );
  }
  if (isRequirementTreeTocNode(node)) {
    const tocEntries = normalizedTocEntries(node);
    const pageRange = node.page_start
      ? `原文第 ${node.page_start}${
          node.page_end && node.page_end !== node.page_start
            ? `–${node.page_end}`
            : ""
        } 页`
      : "原文范围待确认";
    return (
      <article ref={articleRef} className="tree-content-document tree-toc-document">
        <header>
          <span>目录</span>
          <h2>{requirementTreeNodeDisplayLabel(node)}</h2>
          <p>{pageRange}</p>
        </header>
        {tocEntries.length > 0 ? (
          <nav className="tree-toc-navigation" aria-label="目录条目">
            <div className="tree-toc-header" aria-hidden="true">
              <span>编号</span>
              <span>章节</span>
              <span>页码</span>
            </div>
            <ol className="tree-toc-list">
              {tocEntries.map((entry) => (
                <li key={entry.key}>
                  <span className="tree-toc-number">{entry.number || "—"}</span>
                  {entry.targetStructureNodeId && onStructureNodeChange ? (
                    <button
                      type="button"
                      className="tree-toc-title"
                      onClick={() =>
                        onStructureNodeChange(entry.targetStructureNodeId)
                      }
                    >
                      {entry.title || "未命名章节"}
                    </button>
                  ) : (
                    <span className="tree-toc-title">
                      {entry.title || "未命名章节"}
                    </span>
                  )}
                  {entry.targetPage > 0 ? (
                    <button
                      type="button"
                      className="tree-toc-page"
                      aria-label={
                        entry.printedPageLabel
                          ? `目录印刷页 ${entry.printedPageLabel}；打开 PDF 第 ${entry.targetPage} 页`
                          : `打开 PDF 第 ${entry.targetPage} 页`
                      }
                      title={
                        entry.printedPageLabel &&
                        entry.printedPageLabel !== String(entry.targetPage)
                          ? `对应 PDF 第 ${entry.targetPage} 页`
                          : undefined
                      }
                      onClick={() => onPageChange(entry.targetPage)}
                    >
                      {entry.printedPageLabel || entry.targetPage}
                      <Icon name="chevron" size={13} />
                    </button>
                  ) : (
                    <span
                      className="tree-toc-page is-missing"
                      title={
                        entry.printedPageLabel
                          ? "印刷页码尚未解析到物理 PDF 页"
                          : undefined
                      }
                    >
                      {entry.printedPageLabel || "—"}
                    </span>
                  )}
                </li>
              ))}
            </ol>
          </nav>
        ) : (
          <InlineNotice title="目录索引尚未生成" tone="info">
            当前目录仍可在右侧原 PDF 中核对；重新提取后会在这里显示编号、标题与可跳转页码。
          </InlineNotice>
        )}
      </article>
    );
  }
  const compactRootOverview =
    node.parent_id === null &&
    node.child_ids.length > 0 &&
    ["DOCUMENT", "GROUP"].includes(node.kind);
  if (compactRootOverview) {
    const pageRange = node.page_start
      ? `第 ${node.page_start}${
          node.page_end && node.page_end !== node.page_start
            ? `–${node.page_end}`
            : ""
        } 页`
      : "范围待确认";
    return (
      <article ref={articleRef} className="tree-content-document">
        <header>
          <span>{nodeKindLabel(node)}</span>
          <h2>{requirementTreeNodeDisplayLabel(node)}</h2>
          <p>{pageRange}</p>
        </header>
        <section className="tree-root-overview" aria-label="文档结构概览">
          <Icon name="tree" size={22} />
          <div className="tree-root-overview-copy">
            <strong>请选择具体章节或条款</strong>
            <p>
              文档根目录只显示结构概览，避免一次加载整份原文；进入下级后仍会完整显示所选章节或条款内容。
            </p>
          </div>
          <dl>
            <div>
              <dt>顶层结构</dt>
              <dd>{node.child_ids.length} 项</dd>
            </div>
            <div>
              <dt>待审阅候选</dt>
              <dd>{reviewItemCount(node)} 项</dd>
            </div>
            <div>
              <dt>原文范围</dt>
              <dd>{pageRange}</dd>
            </div>
          </dl>
        </section>
      </article>
    );
  }
  const exactContentLeaf = isExactRequirementTreeContentLeaf(node);
  const wholeTableReview = isWholeTableReviewNode(node);
  const isRenderableBlock = (block: JsonRecord): boolean => {
    const kind = String(block.kind || block.type || "").toLowerCase();
    if (["table", "figure", "image"].includes(kind)) {
      return !(
        exactContentLeaf &&
        !wholeTableReview &&
        kind === "table" &&
        !recordFocus
      );
    }
    return Boolean(String(block.text || block.raw_text || "").trim());
  };
  const contextNodeIndex = requirementTreeIndex(tree);
  const contextNodes = (() => {
    if (!tree || exactContentLeaf) return [node];
    const result: RequirementTreeNode[] = [];
    const pending = [node.id];
    const visited = new Set<string>();
    while (pending.length) {
      const currentId = pending.shift();
      if (!currentId || visited.has(currentId)) continue;
      visited.add(currentId);
      const current = contextNodeIndex.get(currentId);
      if (!current || current.include_in_view === false) continue;
      result.push(current);
      pending.push(...current.child_ids);
    }
    return result.sort(
      (left, right) =>
        left.source_order - right.source_order ||
        (finiteJsonNumber(left.source_offset) ?? 0) -
          (finiteJsonNumber(right.source_offset) ?? 0) ||
        left.id.localeCompare(right.id),
    );
  })();
  const contextBlockEntries = contextNodes.flatMap((contextNode) =>
    [
      ...asArray<JsonRecord>(contextNode.content_blocks),
      ...(isWholeTableReviewNode(contextNode)
        ? asArray<JsonRecord>(contextNode.table_refs)
        : []),
    ].map((block, index) => {
      const nativeOrder = finiteJsonNumber(block.source_order);
      const nativeOffset = finiteJsonNumber(block.source_offset);
      return {
        block,
        owner: contextNode,
        ownerDepth: contextNode.depth,
        nodeOrder: contextNode.source_order,
        nodeOffset: finiteJsonNumber(contextNode.source_offset) ?? 0,
        order: nativeOrder ?? contextNode.source_order + index / 1000,
        offset: nativeOffset ?? index,
        index,
      };
    }),
  );
  const titleCoveredBlocksByNodeId = new Map(
    contextNodes.map((contextNode) => [
      contextNode.id,
      structuralTitleCoveredBlocks(contextNode),
    ]),
  );
  const blockRepeatsStructuralTitle = (
    block: JsonRecord,
    owner: RequirementTreeNode,
  ): boolean => Boolean(titleCoveredBlocksByNodeId.get(owner.id)?.has(block));
  const tableCellIds = new Set<string>();
  contextBlockEntries.forEach(({ block }) => {
    if (String(block.kind || block.type || "").toLowerCase() !== "table") {
      return;
    }
    asArray<JsonRecord>(block.cells).forEach((cell) => {
      [cell.id, cell.cell_id, cell.source_id]
        .map((value) => String(value || ""))
        .filter(Boolean)
        .forEach((value) => tableCellIds.add(value));
    });
  });
  const titleOnlyContentNodeIds = new Set(
    contextBlockEntries
      .filter(({ block, owner }) => blockRepeatsStructuralTitle(block, owner))
      .map(({ owner }) => owner.id),
  );
  const preferredEntriesByKey = new Map<
    string,
    (typeof contextBlockEntries)[number]
  >();
  contextBlockEntries
    .filter(({ block, owner }) => {
      const kind = String(block.kind || block.type || "").toLowerCase();
      const identities = evidenceIdentityValues(block);
      return !(
        (kind !== "table" &&
          identities.some((identity) => tableCellIds.has(identity))) ||
        blockRepeatsStructuralTitle(block, owner)
      );
    })
    .forEach((entry) => {
      const key =
        contentBlockKey(entry.block) ||
        `unbound:${entry.owner.id}:${entry.index}`;
      const previous = preferredEntriesByKey.get(key);
      if (
        !previous ||
        entry.ownerDepth > previous.ownerDepth ||
        (entry.ownerDepth === previous.ownerDepth &&
          entry.nodeOrder < previous.nodeOrder)
      ) {
        preferredEntriesByKey.set(key, entry);
      }
    });
  const explicitContextEntries = Array.from(preferredEntriesByKey.values())
    .sort(
      (left, right) =>
        left.order - right.order ||
        left.offset - right.offset ||
        left.nodeOrder - right.nodeOrder ||
        left.nodeOffset - right.nodeOffset ||
        left.index - right.index,
    )
    .filter(
      ({ block }) =>
        !wholeTableReview ||
        String(block.kind || block.type || "").toLowerCase() === "table",
    )
    .filter(({ block }) => isRenderableBlock(block));
  const sourceIds = new Set(
    asArray<JsonRecord>(node.source_refs)
      .map((ref) => String(ref.source_id || ""))
      .filter(Boolean),
  );
  const restoredEntries = pageSnapshot.blocks
    .filter((block) => sourceIds.has(block.id))
    .map((block, index) => ({
      owner: node,
      block: {
        kind: block.type.toLowerCase(),
        source_id: block.id,
        table_id: block.tableId,
        text: block.text,
        source_order: block.sourceOrder,
        page_start: block.pageNumber,
        bbox: block.bbox,
      } as JsonRecord,
      ownerDepth: node.depth,
      nodeOrder: node.source_order,
      nodeOffset: finiteJsonNumber(node.source_offset) ?? 0,
      order: Number.isFinite(block.sourceOrder)
        ? block.sourceOrder
        : node.source_order + index / 1000,
      offset: index,
      index,
    }))
    .filter(({ block }) => isRenderableBlock(block));
  const pageFallbackSourceBlocks = pageSnapshot.blocks.filter(
    (block) => block.type !== "PAGE_RASTER",
  );
  const fallbackFigureCount = pageFallbackSourceBlocks.filter((block) =>
    ["FIGURE", "IMAGE"].includes(block.type),
  ).length;
  let visibleFallbackFigureCount = 0;
  const pageFallbackEntries = pageFallbackSourceBlocks
    .filter((block) => {
      if (!["FIGURE", "IMAGE"].includes(block.type)) return true;
      visibleFallbackFigureCount += 1;
      return visibleFallbackFigureCount <= 4;
    })
    .map((block, index) => ({
      owner: node,
      block: {
        kind: block.type.toLowerCase(),
        source_id: block.id,
        table_id: block.tableId,
        text: block.text,
        source_order: block.sourceOrder,
        page_start: block.pageNumber,
        bbox: block.bbox,
      } as JsonRecord,
      ownerDepth: node.depth,
      nodeOrder: node.source_order,
      nodeOffset: finiteJsonNumber(node.source_offset) ?? 0,
      order: Number.isFinite(block.sourceOrder)
        ? block.sourceOrder
        : node.source_order + index / 1000,
      offset: index,
      index,
    }));
  const hiddenFallbackFigureCount = Math.max(fallbackFigureCount - 4, 0);
  const showingPageFallback =
    !exactContentLeaf &&
    explicitContextEntries.length === 0 &&
    restoredEntries.length === 0 &&
    pageFallbackEntries.length > 0 &&
    (sourceIds.size === 0 || node.kind === "DOCUMENT");
  const entries = explicitContextEntries.length
    ? explicitContextEntries
    : restoredEntries.length
      ? restoredEntries
      : showingPageFallback
        ? pageFallbackEntries
        : [];
  const blockEntries = entries.map((entry, index) => ({ ...entry, index }));
  const contentTableByIndex = new Map<number, StructuredTableModel>();
  for (const { block, index } of blockEntries) {
    const kind = String(block.kind || block.type || "").toLowerCase();
    if (kind !== "table" || asArray<JsonRecord>(block.cells).length === 0) {
      continue;
    }
    contentTableByIndex.set(index, normalizeStructuredTable(block, index));
  }
  const tablesById = new Map<string, StructuredTableModel>();
  for (const table of [
    ...contentTableByIndex.values(),
    ...pageSnapshot.tables,
  ]) {
    if (!tablesById.has(table.id)) tablesById.set(table.id, table);
  }
  const availableTables = Array.from(tablesById.values());
  const tableByBlockIndex = new Map<number, StructuredTableModel>();
  for (const { block, index } of blockEntries) {
    const kind = String(block.kind || block.type || "").toLowerCase();
    if (kind !== "table") continue;
    const contentTable = contentTableByIndex.get(index);
    if (contentTable) {
      tableByBlockIndex.set(index, contentTable);
      continue;
    }
    const tableId = String(block.table_id || block.id || "");
    const sourceId = String(block.source_id || block.source_node_id || "");
    const exactTable =
      availableTables.find((table) => table.id === tableId) ||
      availableTables.find((table) => table.sourceNodeId === sourceId);
    if (exactTable) tableByBlockIndex.set(index, exactTable);
  }
  const focusTableIds = new Set(
    recordFocus
      ? [
          ...recordFocus.rowBindings.map((binding) => binding.tableId),
          ...recordFocus.columnBindings.map((binding) => binding.tableId),
        ]
      : [],
  );
  const displayBlocks = blockEntries;
  const tableContextPreview =
    node.kind !== "TABLE" && !isWholeTableReviewNode(node);
  const focusNode = selectedItem || node;
  const selectedEvidenceRecords = isRequirementTreeReviewItem(focusNode)
    ? [
        ...asArray<JsonRecord>(focusNode.source_refs),
        ...asArray<JsonRecord>(focusNode.content_blocks),
        ...asArray<JsonRecord>(focusNode.table_refs),
        ...asArray<JsonRecord>(focusNode.figure_refs),
      ]
    : [];
  const selectedEvidenceIds = new Set(
    selectedEvidenceRecords.flatMap(evidenceIdentityValues),
  );
  const selectedEvidenceTexts = new Set([
    normalizedEvidenceText(
      isRequirementTreeReviewItem(focusNode) ? focusNode.summary : "",
    ),
    ...selectedEvidenceRecords.flatMap(evidenceTextValues),
    ].filter(Boolean),
  );
  const blockIsFocused = (
    block: JsonRecord,
    index: number,
    owner: RequirementTreeNode,
  ): boolean => {
    if (!showingPageFallback && owner.id === focusNode.id) return true;
    if (!isRequirementTreeReviewItem(focusNode)) return false;
    if (
      evidenceIdentityValues(block).some((value) =>
        selectedEvidenceIds.has(value),
      ) ||
      evidenceTextValues(block).some((value) =>
        selectedEvidenceTexts.has(value),
      )
    ) {
      return true;
    }
    const kind = String(block.kind || block.type || "").toLowerCase();
    if (kind !== "table") return false;
    const structuredTable = tableByBlockIndex.get(index);
    if (!structuredTable) return false;
    const segments = tableSegments(structuredTable, availableTables);
    if (
      recordFocus &&
      (segments.some((segment) => focusTableIds.has(segment.id)) ||
        (Boolean(recordFocus.logicalTableId) &&
          segments.some(
            (segment) =>
              segment.logicalTableId === recordFocus.logicalTableId,
          )))
    ) {
      return true;
    }
    return segments.some((segment) =>
      [segment.id, segment.logicalTableId]
        .filter(Boolean)
        .some((value) => selectedEvidenceIds.has(value)),
    );
  };
  const focusedBlockIndexes = new Set(
    displayBlocks
      .filter(({ block, index, owner }) => blockIsFocused(block, index, owner))
      .map(({ index }) => index),
  );
  const tableRenderIndexes = new Set<number>();
  const renderedLogicalTableIds = new Set<string>();
  for (const { index } of displayBlocks) {
    const table = tableByBlockIndex.get(index);
    if (!table) continue;
    const logicalTableId = table.logicalTableId || table.id;
    if (renderedLogicalTableIds.has(logicalTableId)) continue;
    renderedLogicalTableIds.add(logicalTableId);
    tableRenderIndexes.add(index);
  }
  const nodeIsPureStructureContainer = (
    contextNode: RequirementTreeNode,
  ): boolean =>
    contextNode.projection_role === "STRUCTURE" &&
    contextNode.child_ids.length > 0 &&
    ![
      ...asArray<JsonRecord>(contextNode.content_blocks),
      ...asArray<JsonRecord>(contextNode.table_refs),
      ...asArray<JsonRecord>(contextNode.figure_refs),
    ].some(isRenderableBlock);
  const headingEvents = contextNodes
    .filter(
      (contextNode) =>
        contextNode.id !== node.id && isStructuralReadingNode(contextNode),
    )
    .map((contextNode, sequence) => ({
      kind: "heading" as const,
      node: contextNode,
      order: contextNode.source_order,
      offset: finiteJsonNumber(contextNode.source_offset) ?? 0,
      priority: 0,
      sequence,
      displayDepth: Math.min(Math.max(0, contextNode.depth - node.depth), 4),
    }));
  const captionEventId = (entry: (typeof displayBlocks)[number]): string => {
    const sourceOrder = finiteJsonNumber(entry.block.caption_source_order);
    const sourceOffset = finiteJsonNumber(entry.block.caption_source_offset);
    const base =
      contentBlockKey(entry.block) ||
      `unbound:${entry.owner.id}:${entry.index}`;
    return `caption:${base}:${sourceOrder ?? "unbound"}:${sourceOffset ?? 0}`;
  };
  const captionDomId = (entry: (typeof displayBlocks)[number]): string =>
    `reading-${captionEventId(entry).replace(/[^a-zA-Z0-9_-]+/g, "-")}`;
  const captionEvents = displayBlocks.flatMap((entry, sequence) => {
    const caption = String(entry.block.caption || "").trim();
    const order = finiteJsonNumber(entry.block.caption_source_order);
    if (!caption || order === null) return [];
    const offset = finiteJsonNumber(entry.block.caption_source_offset);
    return [
      {
        kind: "caption" as const,
        entry,
        caption,
        order,
        offset: offset ?? 0,
        priority: 1,
        sequence,
        displayDepth: Math.min(
          Math.max(0, entry.owner.depth - node.depth - 1),
          4,
        ),
        eventId: captionEventId(entry),
      },
    ];
  });
  const blockEvents = displayBlocks.map((entry, sequence) => ({
    kind: "block" as const,
    entry,
    order: entry.order,
    offset: entry.offset,
    priority: 2,
    sequence,
    displayDepth: Math.min(Math.max(0, entry.owner.depth - node.depth - 1), 4),
    eventId:
      contentBlockKey(entry.block) ||
      `unbound:${entry.owner.id}:${entry.index}`,
  }));
  // The middle pane is one source-ordered reading stream. Grouping all blocks
  // by their projected owner reorders media and continuation text whenever a
  // parent owns content that physically appears after a child heading.
  const readingEvents = [
    ...headingEvents,
    ...captionEvents,
    ...blockEvents,
  ].sort(
    (left, right) =>
      left.order - right.order ||
      left.offset - right.offset ||
      left.priority - right.priority ||
      left.sequence - right.sequence,
  );
  const rootIsPureContainer = nodeIsPureStructureContainer(node);
  const hasStructuredReadingContent =
    readingEvents.length > 0 ||
    titleOnlyContentNodeIds.has(node.id);
  const renderContentBlock = (entry: (typeof displayBlocks)[number]) => {
    const { block, index } = entry;
    const kind = String(block.kind || block.type || "").toLowerCase();
    const focused = focusedBlockIndexes.has(index);
    if (kind === "table") {
      const structuredTable = tableByBlockIndex.get(index) || null;
      if (structuredTable && !tableRenderIndexes.has(index)) return null;
      const segmentCount = structuredTable
        ? tableSegments(structuredTable, availableTables).length
        : 1;
      const crossPage = segmentCount > 1;
      return (
        <section
          className={cx(
            "tree-content-table-block",
            crossPage && "is-cross-page",
            tableContextPreview && "is-context-preview",
            focused && "is-review-focus",
          )}
          data-review-focus={focused ? "true" : undefined}
          aria-current={focused ? "true" : undefined}
          aria-label={crossPage ? "跨页表格完整内容" : undefined}
          key={String(
            block.table_id || block.id || block.source_id || `table-${index}`,
          )}
        >
          {Boolean(block.title) && <h4>{String(block.title)}</h4>}
          {crossPage && (
            <div className="tree-table-continuation-note">
              <Icon name="matrix" size={15} />
              <span>
                {tableContextPreview
                  ? "跨页表格已连续还原；章节中显示有界预览，使用“展开表格”查看完整内容。"
                  : "跨页内容已连续还原；可在表格内横向滚动、向下查看续表，或使用“展开表格”查看完整内容。"}
              </span>
            </div>
          )}
          {structuredTable ? (
            <StructuredTable
              table={structuredTable}
              tables={availableTables}
              fitContainer
              projectId={projectId}
              documentId={documentId}
              sourceSha256={sourceSha256}
              onPageChange={onPageChange}
              recordFocus={recordFocus}
              compactRecord={Boolean(
                recordFocus && selectedItem?.id === node.id,
              )}
            />
          ) : (
            <ContentTable table={block} />
          )}
        </section>
      );
    }
    if (kind === "figure" || kind === "image") {
      const rendering = asRecord(block.rendering);
      const mediaPage = contentBlockPage(block);
      const sourceGeometry = sourceGeometryForContentBlock(
        block,
        pageSnapshot.blocks,
      );
      const restoredSrc = sourceGeometry
        ? sourceCropUrl({
            projectId,
            documentId,
            sourceSha256,
            page: sourceGeometry.page,
            bbox: sourceGeometry.bbox,
          })
        : "";
      const src =
        restoredSrc ||
        String(
          block.url ||
            block.src ||
            rendering.url ||
            rendering.asset_url ||
            "",
        );
      const hasSeparateCaption =
        Boolean(String(block.caption || "").trim()) &&
        finiteJsonNumber(block.caption_source_order) !== null;
      const captionDescriptionId = hasSeparateCaption
        ? captionDomId(entry)
        : undefined;
      return src ? (
        <figure
          className={cx("tree-content-figure", focused && "is-review-focus")}
          data-review-focus={focused ? "true" : undefined}
          aria-current={focused ? "true" : undefined}
          aria-describedby={captionDescriptionId}
          key={String(block.source_id || block.id || `figure-${index}`)}
        >
          <img
            src={src}
            alt={String(block.alt || "条款中的来源图片")}
            aria-describedby={captionDescriptionId}
            loading="lazy"
            decoding="async"
          />
          {Boolean(block.caption) && !hasSeparateCaption && (
            <figcaption>{String(block.caption)}</figcaption>
          )}
        </figure>
      ) : mediaPage > 0 ? (
        <button
          type="button"
          className={cx(
            "tree-figure-placeholder",
            "is-actionable",
            focused && "is-review-focus",
          )}
          data-review-focus={focused ? "true" : undefined}
          aria-current={focused ? "true" : undefined}
          aria-describedby={captionDescriptionId}
          onClick={() => onPageChange(mediaPage)}
          key={String(block.source_id || block.id || `figure-${index}`)}
        >
          <Icon name="eye" />
          <span>查看原文第 {mediaPage} 页图片</span>
        </button>
      ) : (
        <div
          className={cx(
            "tree-figure-placeholder",
            focused && "is-review-focus",
          )}
          data-review-focus={focused ? "true" : undefined}
          aria-current={focused ? "true" : undefined}
          aria-describedby={captionDescriptionId}
          key={String(block.source_id || block.id || `figure-${index}`)}
        >
          <Icon name="eye" />
          <span>图片位置已保留，可在右侧原 PDF 中核对。</span>
        </div>
      );
    }
    const text = String(block.text || block.raw_text || "").trim();
    return text ? (
      <p
        className={cx(focused && "is-review-focus")}
        data-review-focus={focused ? "true" : undefined}
        aria-current={focused ? "true" : undefined}
        key={String(block.source_id || block.id || `text-${index}`)}
      >
        {text}
      </p>
    ) : null;
  };
  return (
    <article ref={articleRef} className="tree-content-document">
      <header>
        <span>{nodeKindLabel(node)}</span>
        <h2
          className={cx(focusNode.id === node.id && "tree-content-focus-title")}
        >
          {requirementTreeNodeDisplayLabel(node)}
        </h2>
        {node.page_start && (
          <p>
            原文第 {node.page_start}
            {node.page_end && node.page_end !== node.page_start
              ? `–${node.page_end}`
              : ""}{" "}
            页
          </p>
        )}
        {showingPageFallback && (
          <p>当前结构尚未覆盖全部内容，以下直接显示当前页解析事实。</p>
        )}
        {rootIsPureContainer && (
          <div className="tree-content-container-state">
            {childScopeLabel(node, contextNodeIndex)}
          </div>
        )}
        {selectedItem && selectedItem.id !== node.id && (
          <div className="tree-review-focus-strip">
            <span>当前聚焦</span>
            <strong>{nodeKindLabel(selectedItem)}</strong>
            <em>{statusLabel(selectedItem)}</em>
          </div>
        )}
      </header>
      {hasStructuredReadingContent ? (
        <div className="tree-content-blocks">
          {readingEvents.map((event) => {
            const style = {
              "--tree-content-depth": event.displayDepth,
            } as CSSProperties;
            if (event.kind === "heading") {
              const label = requirementTreeNodeDisplayLabel(event.node);
              const focusedHeading = focusNode.id === event.node.id;
              return (
                <div
                  className="tree-content-reading-event is-heading"
                  style={style}
                  data-reading-event="heading"
                  data-content-node-id={event.node.id}
                  key={`heading:${event.node.id}`}
                >
                  <header
                    className={cx(
                      "tree-content-node-heading",
                      focusedHeading && "is-focus-scope",
                    )}
                    data-review-focus={focusedHeading ? "true" : undefined}
                  >
                    <span>{nodeKindLabel(event.node)}</span>
                    <h3>{label}</h3>
                    {nodeIsPureStructureContainer(event.node) && (
                      <em>{childScopeLabel(event.node, contextNodeIndex)}</em>
                    )}
                  </header>
                </div>
              );
            }
            if (event.kind === "caption") {
              return (
                <div
                  className="tree-content-reading-event is-caption"
                  style={style}
                  data-reading-event="caption"
                  data-content-node-id={event.entry.owner.id}
                  key={event.eventId}
                >
                  <p
                    id={captionDomId(event.entry)}
                    className="tree-content-figure-caption"
                  >
                    {event.caption}
                  </p>
                </div>
              );
            }
            const renderedBlock = renderContentBlock(event.entry);
            if (!renderedBlock) return null;
            return (
              <div
                className="tree-content-reading-event is-block tree-content-node-body"
                style={style}
                data-reading-event="block"
                data-content-node-id={event.entry.owner.id}
                key={`block:${event.eventId}`}
              >
                {renderedBlock}
              </div>
            );
          })}
          {showingPageFallback && hiddenFallbackFigureCount > 0 && (
            <div className="tree-page-fallback-note">
              <Icon name="eye" />
              <span>
                本页另有 {hiddenFallbackFigureCount} 个图形框标，右侧原 PDF
                保留全部定位；选择相应结构节点时再展开。
              </span>
            </div>
          )}
        </div>
      ) : (
        <p className="tree-content-summary">
          {wholeTableReview
            ? "整表结构暂不可用，请在右侧原 PDF 中核对。"
            : node.summary ||
              "该节点保留结构身份；条款内容与 PDF 原文可在详情区核对。"}
        </p>
      )}
      {selectedItem &&
        selectedItem.id !== node.id &&
        focusedBlockIndexes.size === 0 && (
          <section
            className="tree-review-focus-fallback"
            data-review-focus="true"
            aria-label="当前候选来源内容降级显示"
          >
            <span>来源内容暂未原位匹配 · 降级显示</span>
            <p>
              {selectedItem.summary ||
                "当前候选已绑定原文范围；父条款未返回可内嵌的独立内容块，请在右侧 PDF 精确框选中核对。"}
            </p>
          </section>
        )}
    </article>
  );
}

function RequirementTreeOrganizePanel() {
  const agentAvailability = useSaasNativeAgentAvailability();
  const documentAgent = useDocumentAgent();
  const {
    route,
    updateRoute,
    selectedDocument,
    extraction,
    structure,
    structureOptimization,
    requirementSets,
    runtimeCapabilities,
    processingProfile,
    busyAction,
    refreshing,
    runAction,
    refreshDocument,
    refreshExtraction,
    refreshStructure,
    refreshStructureOptimization,
    setNotice,
  } = useWorkspace();
  const [extractionProgressClockMs, setExtractionProgressClockMs] = useState(
    () => Date.now(),
  );
  const workbench = asRecord(structure?.structure_workbench);
  const workbenchAgent = asRecord(workbench.agent);
  const structureAgent = asRecord(structure?.structure_agent);
  const structureArtifactReadOnlyReasonCode = String(
    structureAgent.read_only_reason || "",
  );
  const structureArtifactReadOnly = Boolean(
    workbenchAgent.editable === false ||
      (structureArtifactReadOnlyReasonCode && structureAgent.editable === false),
  );
  const recovery = asRecord(workbench.recovery);
  const recoveryReasonCode = String(recovery.reason_code || "").toUpperCase();
  const workbenchIssueCodes = new Set(
    asArray<JsonRecord>(workbench.issues).map((issue) =>
      String(issue.code || "").toUpperCase(),
    ),
  );
  const sourceCaptureRecovery =
    recoveryReasonCode === "SOURCE_CAPTURE_GAP" ||
    (!recoveryReasonCode &&
      workbenchIssueCodes.has("SOURCE_TEXT_RESTORATION_REQUIRED"));
  const sourcePages = asRecord(workbench.source_pages);
  const sourcePagesReady =
    String(sourcePages.state || "").toUpperCase() === "READY" &&
    Number(sourcePages.page_count || 0) > 0;
  const sourcePageSnapshotId = String(sourcePages.snapshot_id || "");
  const sourcePageSourceSha256 = String(sourcePages.source_sha256 || "");
  const visualRestorationOverride = useMemo(
    () =>
      buildVisualRestorationOverride(processingProfile, runtimeCapabilities),
    [processingProfile, runtimeCapabilities],
  );
  const visualRestorationMethodLabel = visualRestorationOverride?.document
    ?.enhancement_chain.includes("bailian_ocr")
    ? "Qwen OCR 结构恢复"
    : visualRestorationOverride?.document?.enhancement_chain.includes(
          "bailian_layout",
        )
      ? "页面布局识别"
      : visualRestorationOverride?.document?.primary_parser ===
          "azure_document_intelligence"
        ? "Azure 文档解析"
        : "视觉恢复";
  const workingTree =
    (workbench.requirement_tree as RequirementTreeSnapshot | undefined) || null;
  const sets = requirementSets?.requirement_sets || [];
  const activeSetId = String(requirementSets?.active_requirement_set_id || "");
  const defaultSetId =
    activeSetId ||
    setId(
      sets.find((item) => item.requirement_tree_state === "READY") ||
        sets[0] ||
        {},
    );
  const selectedSetId = route.requirementSetId || defaultSetId;
  const selectedSet = sets.find((item) => setId(item) === selectedSetId);
  const selectedSetVersion = Math.max(0, Number(selectedSet?.version || 0));
  const selectedSetVersionLabel =
    String(selectedSet?.version_label || "").trim() ||
    (selectedSetVersion > 0 ? `v${selectedSetVersion}` : "当前版本");
  const selectedSetRequirementCount = Math.max(
    0,
    Number(selectedSet?.requirement_count || 0),
  );
  const selectedSetTreeState = String(
    selectedSet?.requirement_tree_state || "",
  ).toUpperCase();
  const reviewAvailable = Boolean(
    selectedSetId &&
      (!selectedSetTreeState || selectedSetTreeState === "READY"),
  );
  const reviewStageUnavailable = !reviewAvailable;
  const rawRequestedCheckpoint: ExtractionCheckpoint = [
    "diagnose",
    "structure",
    "list",
    "review",
  ].includes(String(route.checkpoint || ""))
    ? (route.checkpoint as ExtractionCheckpoint)
    : "structure";
  const requestedCheckpoint: ExtractionCheckpoint =
    rawRequestedCheckpoint === "diagnose"
      ? "structure"
      : rawRequestedCheckpoint;
  const tree = workingTree;
  const treeState = tree?.state || "NOT_STARTED";
  const extractionJob = asRecord(extraction?.job);
  const extractionResult = asRecord(extraction?.result);
  const agentNativeExtractionReady =
    String(extractionResult.collection || "") ===
    "agent_native_requirement_document";
  const decompositionSummary = extractionDecompositionSummary(extraction?.result);
  const requirementDecomposition = decompositionSummary.parents;
  const decomposedObligationCount = decompositionSummary.obligationCount;
  const extractionJobId = String(extractionJob.job_id || "");
  const [extractionProgressDetailState, setExtractionProgressDetailState] =
    useState({ jobId: "", expanded: false });
  const extractionProgressDetailsExpanded = Boolean(
    extractionJobId &&
      extractionProgressDetailState.jobId === extractionJobId &&
      extractionProgressDetailState.expanded,
  );
  const resultVisualReceiptSummary = extractionVisualReceiptSummary(
    extraction?.result,
    extractionJob.processing_plan,
  );
  const visualRepair = asRecord(extraction?.visual_repair);
  const frozenVisualRouteIdentity = extractionVisualRouteIdentity(
    extraction?.result,
    extractionJob.processing_plan,
    resultVisualReceiptSummary?.adoptedProviderIds || [],
  );
  const visualReceiptSummary =
    resultVisualReceiptSummary ||
    visualRepairEligibilitySummary(visualRepair, frozenVisualRouteIdentity);
  const visualRepairState = String(visualRepair.state || "").toUpperCase();
  const visualRepairBaseJobId = String(visualRepair.base_job_id || "");
  const visualRepairBaseGeneration = Number(
    visualRepair.base_job_generation,
  );
  const visualRepairBaseArtifactSha256 = String(
    visualRepair.base_artifact_sha256 || "",
  );
  const visualRepairFailedPages = asArray(visualRepair.failed_page_numbers)
    .map((pageNumber) => Math.floor(Number(pageNumber || 0)))
    .filter((pageNumber) => pageNumber > 0)
    .sort((left, right) => left - right);
  const visualRepairReadyPageCount = Math.max(
    0,
    Math.floor(Number(visualRepair.ready_page_count || 0)),
  );
  const visualRepairRemoteRequestCeiling = Math.max(
    0,
    Math.floor(Number(visualRepair.remote_request_ceiling || 0)),
  );
  const visualRepairDeepLayoutRequestCeiling = Math.max(
    0,
    Math.floor(Number(visualRepair.deep_layout_request_ceiling || 0)),
  );
  const activeVisualRepairSummaryText =
    visualRepairState === "ACTIVE"
      ? `本次只重试正式回执中的 ${visualRepairFailedPages
          .map((pageNumber) => `P${pageNumber}`)
          .join("、")}；${visualRepairReadyPageCount} 页复用已验证结果。文字 OCR 请求上限 ${visualRepairRemoteRequestCeiling} 次；table_parsing 另受每页 8 次、整文 64 次上限；深度版面请求上限 ${visualRepairDeepLayoutRequestCeiling} 次。`
      : "";
  const visualReceiptFailedPages =
    visualReceiptSummary?.degradedPageNumbers || [];
  const visualRepairMatchesTerminalReceipt = Boolean(
    visualReceiptSummary &&
      visualRepairFailedPages.length === visualReceiptFailedPages.length &&
      visualRepairFailedPages.every(
        (pageNumber, index) => pageNumber === visualReceiptFailedPages[index],
      ),
  );
  const visualRepairAvailable = Boolean(
    visualReceiptSummary?.routeIdentity === "QWEN_OCR" &&
    visualRepairState === "AVAILABLE" &&
      visualRepairMatchesTerminalReceipt &&
      visualRepairBaseJobId &&
      Number.isInteger(visualRepairBaseGeneration) &&
      visualRepairBaseGeneration >= 0 &&
      /^[0-9a-f]{64}$/.test(visualRepairBaseArtifactSha256),
  );
  const extractionJobStatus = String(extractionJob.status || "").toUpperCase();
  const extractionQueue = asRecord(extractionJob.queue);
  const extractionQueueState = String(extractionQueue.state || "").toUpperCase();
  const extractionQueueWaitSeconds = Math.max(
    0,
    Number(extractionQueue.wait_seconds || 0),
  );
  const extractionQueueWaitLabel =
    extractionQueueWaitSeconds >= 60
      ? `${Math.max(1, Math.floor(extractionQueueWaitSeconds / 60))} 分钟`
      : extractionQueueWaitSeconds > 0
        ? `${Math.floor(extractionQueueWaitSeconds)} 秒`
        : "";
  const extractionProgress = asRecord(extractionJob.progress);
  const extractionProcessingPlan = asRecord(extractionJob.processing_plan);
  const extractionPlanDocument = asRecord(extractionProcessingPlan.document);
  const extractionPlanPrimaryParser = String(
    extractionPlanDocument.primary_parser || "",
  );
  const extractionPlanEnhancementChain = asArray(
    extractionPlanDocument.enhancement_chain,
  ).map((item) => String(item));
  const extractionPlanComplexVision = String(
    extractionPlanDocument.complex_vision || "",
  );
  const extractionPlanForcedVisualPages = asArray(
    extractionProcessingPlan.force_visual_pages,
  ).length;
  const extractionPlanProfileRevision = Math.max(
    0,
    Number(extractionProcessingPlan.project_profile_revision || 0),
  );
  const extractionPlanConnectionRevision = Math.max(
    0,
    Number(extractionProcessingPlan.connection_revision || 0),
  );
  const extractionPlanLlmProvider = String(
    extractionProcessingPlan.llm_provider || "",
  );
  const extractionPlanProviderBindings = asRecord(
    extractionProcessingPlan.provider_bindings,
  );
  const extractionPlanSemanticDeployment = String(
    extractionPlanProviderBindings.semantic_deployment || "",
  );
  const extractionPlanStructureDeployments = Array.from(
    new Set(
      [
        String(extractionPlanProviderBindings.structure_overview_deployment || ""),
        String(extractionPlanProviderBindings.structure_repair_deployment || ""),
      ].filter(Boolean),
    ),
  );
  const extractionPlanLlmSummary =
    extractionPlanLlmProvider === "none"
      ? "LLM 未启用"
      : extractionPlanLlmProvider
        ? [
            extractionPlanLlmProvider === "aliyun"
              ? "Aliyun LLM"
              : extractionPlanLlmProvider === "azure_openai"
                ? "Azure OpenAI"
                : extractionPlanLlmProvider,
            extractionPlanSemanticDeployment
              ? `语义模型 ${extractionPlanSemanticDeployment}`
              : "",
            extractionPlanStructureDeployments.length > 0
              ? `结构模型 ${extractionPlanStructureDeployments.join("/")}`
              : "",
          ]
            .filter(Boolean)
            .join(" · ")
        : "";
  const frozenExtractionPlanSummary = extractionPlanPrimaryParser
    ? [
        extractionPlanPrimaryParser === "pymupdf"
          ? "本地 PyMuPDF"
          : extractionPlanPrimaryParser === "azure_document_intelligence"
            ? "Azure Document Intelligence"
            : extractionPlanPrimaryParser,
        extractionPlanEnhancementChain.length === 0 &&
        !extractionPlanComplexVision &&
        extractionPlanForcedVisualPages === 0
          ? "无视觉增强"
          : [
              ...extractionPlanEnhancementChain.map((item) =>
                item === "bailian_layout"
                  ? "百炼版面增强"
                  : item === "bailian_ocr"
                    ? "Qwen OCR 结构恢复"
                  : item === "azure_document_intelligence"
                    ? "Azure 文档增强"
                    : item,
              ),
              ...(extractionPlanComplexVision ? ["复杂页面视觉"] : []),
              ...(extractionPlanForcedVisualPages > 0
                ? [`强制视觉 ${extractionPlanForcedVisualPages} 页`]
                : []),
            ].join("、"),
        extractionPlanLlmSummary,
        `项目方案修订 ${extractionPlanProfileRevision}`,
        `连接修订 ${extractionPlanConnectionRevision}`,
      ]
        .filter(Boolean)
        .join(" · ")
    : "";
  const extractionCompletedPages = Math.max(
    0,
    Number(extractionProgress.completed_pages || 0),
  );
  const extractionTotalPages = Math.max(
    0,
    Number(extractionProgress.total_pages || 0),
  );
  const extractionCurrentPage = Math.max(
    0,
    Number(extractionProgress.current_page || 0),
  );
  const latestVisualPageEvent = asRecord(
    extractionJob.latest_visual_page_event,
  );
  const latestVisualPageOutcome = String(
    latestVisualPageEvent.outcome || "",
  ).toUpperCase();
  const latestVisualPageNumber = Math.max(
    0,
    Math.floor(Number(latestVisualPageEvent.page_number || 0)),
  );
  const latestVisualPageRequestCount = Math.max(
    0,
    Math.floor(Number(latestVisualPageEvent.request_count || 0)),
  );
  const latestVisualPageAvailable = Boolean(
    latestVisualPageNumber > 0 &&
      ["READY", "CACHE_HIT", "FAILED"].includes(latestVisualPageOutcome),
  );
  const latestVisualPageTitle =
    latestVisualPageOutcome === "CACHE_HIT"
      ? "最近视觉页命中缓存"
      : latestVisualPageOutcome === "FAILED"
        ? "最近视觉页失败"
        : "最近视觉页成功";
  const latestExtractionQueued = ["PENDING", "QUEUED", "STARTING"].includes(
    extractionJobStatus,
  );
  const latestExtractionExecuting = extractionJobStatus === "RUNNING";
  const latestExtractionCancelling =
    extractionJobStatus === "CANCEL_REQUESTED";
  const extractionQueueStalled =
    extractionQueueState === "WORKER_NOT_CLAIMING";
  const extractionQueueWaitingForWorkerSync =
    extractionQueueState === "WAITING_FOR_WORKER_SYNC" ||
    String(
      asRecord(extraction?.execution_readiness).state || "",
    ).toUpperCase() === "WAITING_FOR_WORKER_SYNC";
  const extractionQueueBlockedNoLiveWorker =
    extractionQueueState === "BLOCKED_NO_LIVE_WORKER" ||
    String(
      asRecord(extraction?.execution_readiness).state || "",
    ).toUpperCase() === "BLOCKED_NO_LIVE_WORKER";
  const extractionRetryScheduled = extractionQueueState === "RETRY_SCHEDULED";
  const extractionRetryAvailableAt = String(
    extractionQueue.retry_available_at_utc || "",
  );
  const extractionCanCancel =
    extractionQueue.can_cancel === true && Boolean(extractionJob.job_id);
  const previousExtractionArtifact = asRecord(extraction?.previous_artifact);
  const previousArtifactAvailable =
    previousExtractionArtifact.available === true;
  const previousArtifactRuntimeCompatible =
    previousExtractionArtifact.runtime_compatible === true;
  const latestExtractionFailed = extractionJobStatus === "FAILED";
  const latestExtractionInterrupted = extractionJobStatus === "INTERRUPTED";
  const latestExtractionCancelled = extractionJobStatus === "CANCELLED";
  const latestExtractionUnsuccessful =
    latestExtractionFailed || latestExtractionInterrupted;
  const latestExtractionRunning = [
    "PENDING",
    "QUEUED",
    "STARTING",
    "RUNNING",
    "CANCEL_REQUESTED",
  ].includes(extractionJobStatus);
  const extractionHeartbeatUpdatedAtMs = safeUtcTimestampMs(
    extractionJob.updated_at_utc,
  );
  const extractionPageStartedAtMs = safeUtcTimestampMs(
    extractionProgress.page_started_at_utc,
  );
  const extractionHeartbeatAgeSeconds =
    extractionHeartbeatUpdatedAtMs === null
      ? null
      : Math.max(
          0,
          Math.floor(
            (extractionProgressClockMs - extractionHeartbeatUpdatedAtMs) / 1000,
          ),
        );
  const extractionPageElapsedSeconds =
    extractionPageStartedAtMs === null
      ? null
      : Math.max(
          0,
          Math.floor(
            (extractionProgressClockMs - extractionPageStartedAtMs) / 1000,
          ),
        );
  const extractionHeartbeatLabel =
    extractionHeartbeatAgeSeconds !== null
      ? extractionHeartbeatAgeSeconds <= 45
        ? [
            "后台心跳正常",
            extractionCurrentPage > 0 && extractionPageElapsedSeconds !== null
              ? `当前第 ${extractionCurrentPage} 页处理阶段已持续 ${extractionPageElapsedSeconds} 秒`
              : "",
            "仅表示运行时长，不是完成度估算",
          ]
            .filter(Boolean)
            .join(" · ")
        : `最近一次后台心跳在 ${extractionHeartbeatAgeSeconds} 秒前，正在继续确认任务状态。`
      : extractionCurrentPage > 0 && extractionPageElapsedSeconds !== null
        ? `当前第 ${extractionCurrentPage} 页处理阶段已持续 ${extractionPageElapsedSeconds} 秒 · 仅表示运行时长，不是完成度估算`
        : "";

  useEffect(() => {
    setExtractionProgressClockMs(Date.now());
    if (!latestExtractionExecuting || extractionCurrentPage < 1) return;
    const intervalId = window.setInterval(() => {
      setExtractionProgressClockMs(Date.now());
    }, 5000);
    return () => window.clearInterval(intervalId);
  }, [
    extractionCurrentPage,
    extractionJob.job_id,
    extractionProgress.page_started_at_utc,
    extractionJob.updated_at_utc,
    latestExtractionExecuting,
  ]);
  const previousStructureBrowseAllowed = Boolean(
    (latestExtractionRunning ||
      latestExtractionUnsuccessful ||
      latestExtractionCancelled) &&
      previousExtractionArtifact.can_browse_structure === true &&
      treeState === "READY" &&
      structureArtifactReadOnly,
  );
  const extractionFailureStage = String(extractionJob.error_stage || "");
  const extractionFailureCode = String(extractionJob.error_code || "");
  const extractionFailureCause = String(extractionJob.error_cause_code || "");
  const extractionExecutionReadiness = asRecord(
    extraction?.execution_readiness || extractionJob.execution_readiness,
  );
  const extractionErrorContext = asRecord(
    extraction?.error_context || extractionJob.error_context,
  );
  const extractionExecutionReadinessState = String(
    extractionExecutionReadiness.state || "NOT_APPLICABLE",
  ).toUpperCase();
  const extractionWorkerFleetState = String(
    extractionExecutionReadiness.worker_fleet_state || "UNAVAILABLE",
  ).toUpperCase();
  const connectionRevisionMismatch =
    extractionFailureCode ===
      "document_extraction_connection_revision_mismatch" ||
    extractionFailureCause ===
      "document_extraction_connection_revision_mismatch";
  const connectionMismatchFleetReady = Boolean(
    connectionRevisionMismatch &&
      extractionWorkerFleetState === "READY" &&
      ["READY", "CONFIGURATION_SUPERSEDED"].includes(
        extractionExecutionReadinessState,
      ) &&
      extractionExecutionReadiness.retryable !== false,
  );
  const connectionMismatchRetryBlocked = Boolean(
    connectionRevisionMismatch && !connectionMismatchFleetReady,
  );

  useEffect(() => {
    if (!connectionMismatchRetryBlocked) return;
    let active = true;
    let requestInFlight = false;
    let retryTimer: number | undefined;
    const pollExecutionReadiness = async () => {
      if (!active || requestInFlight) return;
      requestInFlight = true;
      try {
        await refreshExtraction();
      } catch {
        // The next bounded poll retries; the current server-projected state stays visible.
      } finally {
        requestInFlight = false;
        if (active) {
          retryTimer = window.setTimeout(
            () => void pollExecutionReadiness(),
            2500,
          );
        }
      }
    };
    void pollExecutionReadiness();
    return () => {
      active = false;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, [connectionMismatchRetryBlocked, refreshExtraction]);
  const submittedConnectionRevisionValue = Number(
    extractionErrorContext.submitted_connection_revision ??
      extractionProcessingPlan.connection_revision,
  );
  const submittedConnectionRevision = Number.isInteger(
    submittedConnectionRevisionValue,
  )
    ? submittedConnectionRevisionValue
    : null;
  const apiConnectionRevisionValue = Number(
    extractionErrorContext.api_connection_revision ??
      extractionExecutionReadiness.api_connection_revision,
  );
  const apiConnectionRevision = Number.isInteger(apiConnectionRevisionValue)
    ? apiConnectionRevisionValue
    : null;
  const workerConnectionRevisions = Array.from(
    new Set(
      asArray(extractionErrorContext.worker_connection_revisions)
        .map((revision) => Number(revision))
        .filter((revision) => Number.isInteger(revision) && revision >= 0),
    ),
  );
  const mismatchPagesStarted = Math.max(
    0,
    Number(extractionErrorContext.pages_started || 0),
  );
  const organizeUiScope = `requirement-tree:${route.projectId}:${route.documentId}`;
  const expansionScope = `${route.projectId}:${route.documentId}`;
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<RequirementTreeFilter>("all");
  const [classificationFilter, setClassificationFilter] =
    useState<EntryClassificationFilter>("ALL");
  const [entryClassificationState, setEntryClassificationState] =
    useState<KeyedEntryClassificationState | null>(null);
  const [classificationReloadToken, setClassificationReloadToken] =
    useState(0);
  const [classificationMutation, setClassificationMutation] = useState<
    "PATCH" | "ACCEPT" | ""
  >("");
  const [classificationMutationError, setClassificationMutationError] =
    useState("");
  const [classificationBatchOpen, setClassificationBatchOpen] =
    useState(false);
  const [classificationBatchScope, setClassificationBatchScope] =
    useState<EntryClassificationBatchScope>("BRANCH");
  const [classificationBatchType, setClassificationBatchType] =
    useState<EntryClassificationType>("TECHNICAL");
  const [requirementEnrichmentState, setRequirementEnrichmentState] =
    useState<KeyedRequirementEnrichmentState | null>(null);
  const [enrichmentReloadToken, setEnrichmentReloadToken] = useState(0);
  const [atomizationSelection, setAtomizationSelection] =
    useState<SelectedAtomizationWorkflowState>({
      identityKey: "",
      workflowIds: [],
    });
  const [atomizationScope, setAtomizationScope] =
    useState<AtomizationScope>("BRANCH");
  const [atomizationDialogOpen, setAtomizationDialogOpen] = useState(false);
  const [atomizationMutation, setAtomizationMutation] = useState<
    "APPLY" | "UNDO" | ""
  >("");
  const [atomizationMutationError, setAtomizationMutationError] = useState("");
  const [semanticPublicationMutation, setSemanticPublicationMutation] =
    useState(false);
  const [pendingLocalDraftActivation, setPendingLocalDraftActivation] =
    useState<PendingLocalDraftActivation | null>(null);
  const [expandedIdList, setExpandedIdList] = useScopedUiState<string[] | null>(
    `${organizeUiScope}:expanded`,
    null,
  );
  const expandedIds = useMemo(
    () => new Set(expandedIdList ?? initialExpandedIds(tree)),
    [expandedIdList, tree],
  );
  const setExpandedIds: Dispatch<SetStateAction<Set<string>>> = useCallback(
    (next) => {
      setExpandedIdList((current) => {
        const currentSet = new Set(current ?? initialExpandedIds(tree));
        const resolved =
          typeof next === "function" ? next(currentSet) : next;
        return Array.from(resolved);
      });
    },
    [setExpandedIdList, tree],
  );
  const [selectedId, setSelectedId] = useScopedUiState(
    `${organizeUiScope}:selected-node`,
    "",
  );
  const pdfDocumentKey = `${route.projectId}:${route.documentId}`;
  const [pdfLocation, setPdfLocation] = useState({
    documentKey: pdfDocumentKey,
    page: 1,
  });
  const storedPdfPage =
    pdfLocation.documentKey === pdfDocumentKey ? pdfLocation.page : 1;
  const formalPageLimit = sourcePagesReady
    ? Math.max(1, Number(sourcePages.page_count || 0))
    : 0;
  const setPdfPage = useCallback<Dispatch<SetStateAction<number>>>(
    (next) => {
      setPdfLocation((current) => {
        const currentPage =
          current.documentKey === pdfDocumentKey ? current.page : 1;
        const nextPage =
          typeof next === "function" ? next(currentPage) : next;
        return {
          documentKey: pdfDocumentKey,
          page: Math.max(1, Math.trunc(nextPage)),
        };
      });
    },
    [pdfDocumentKey],
  );
  const [compactDetail, setCompactDetail] = useState<"content" | "source">(
    "content",
  );
  const [organizeView, setOrganizeView] = useState<"tree" | "calibration">(
    "tree",
  );
  const [sourceEvidenceOpen, setSourceEvidenceOpen] = useState(
    rawRequestedCheckpoint === "diagnose",
  );
  const [nativeStructureReferenceAvailable, setNativeStructureReferenceAvailable] =
    useState(false);
  const [nativeReferenceLookupReady, setNativeReferenceLookupReady] = useState(false);
  const [sourceGraphJobs, setSourceGraphJobs] = useState<SourceGraphJob[]>([]);
  const [sourceDocumentActivity, setSourceDocumentActivity] = useState<SourceDocumentActivity | null>(null);
  const [sourceCollaborationRequest, setSourceCollaborationRequest] = useState(0);
  const currentSourceActivity = sourceDocumentActivity?.projectId === route.projectId
    && sourceDocumentActivity.documentId === route.documentId
    && sourceDocumentActivity.sourceSha256 === String(selectedDocument?.source_sha256 || sourcePages.source_sha256 || "")
    ? sourceDocumentActivity : null;
  const sourceGraphBusy = !!currentSourceActivity && (currentSourceActivity.checking || currentSourceActivity.statusUnavailable
    || currentSourceActivity.submitting || currentSourceActivity.jobs.some(sourceGraphJobActive));
  const sourceDocumentTask = currentSourceActivity?.jobs.find(job => job.kind === "agent.source-item.extract-loop");
  const showSourceDocumentTask = !nativeStructureReferenceAvailable && !!currentSourceActivity
    && (currentSourceActivity.checking || currentSourceActivity.statusUnavailable || currentSourceActivity.submitting || !!sourceDocumentTask);
  const sourceDocumentTaskTitle = currentSourceActivity?.checking ? "正在同步当前任务"
    : currentSourceActivity?.statusUnavailable ? "当前任务状态待同步"
    : currentSourceActivity?.submitting ? "正在提交原文任务"
    : ({ PENDING: "原文任务已排队", RUNNING: "Agent 正在处理原文", CANCEL_REQUESTED: "正在暂停当前任务",
        CANCELLED: "当前任务已暂停", FAILED: "本轮任务未完成", SUCCEEDED: "本轮任务已结束" } as Record<string, string>)[sourceDocumentTask?.status || ""] || "当前原文任务";
  const [sourceGraphRefresh, setSourceGraphRefresh] = useState(0);
  useEffect(() => { setNativeReferenceLookupReady(false); setSourceGraphJobs([]); }, [route.projectId, route.documentId]);
  const [nativeStructureReferencePageCount, setNativeStructureReferencePageCount] =
    useState(0);
  const [nativeStructureAdvancement, setNativeStructureAdvancement] =
    useState<NativeStructureAdvancementState>({
      available: false,
      advancementAllowed: false,
      sourceSha256: "",
      artifactId: "",
      continuationStatus: "IDLE",
    });
  const [nativeBaselineStatus, setNativeBaselineStatus] =
    useState<NativeStructureBaselineStatusV1 | null>(null);
  const [nativeParentStatus, setNativeParentStatus] =
    useState<NativeParentProvisionStatusV1 | null>(null);
  const [sourceRequirementStatus, setSourceRequirementStatus] =
    useState<SourceRequirementBaselineStatusV1 | null>(null);
  const [nativeAdvanceError, setNativeAdvanceError] = useState("");
  const [boundaryCalibrationPlan, setBoundaryCalibrationPlan] =
    useState<JsonRecord | null>(null);
  const [boundaryCalibrationLoading, setBoundaryCalibrationLoading] =
    useState(false);
  const [boundaryCalibrationError, setBoundaryCalibrationError] =
    useState("");
  const [boundaryCalibrationPreview, setBoundaryCalibrationPreview] =
    useState<JsonRecord | null>(null);
  const [boundaryCalibrationDraft, setBoundaryCalibrationDraft] =
    useState<BoundaryCalibrationDraftVisual | null>(null);
  const [treeScrollTop, setTreeScrollTop] = useScopedUiState(
    `${organizeUiScope}:tree-scroll-top`,
    0,
  );
  const [treeViewportHeight, setTreeViewportHeight] = useState(560);
  const [review, setReview] = useState<ReviewSessionView | null>(null);
  const [reviewSaveState, setReviewSaveState] = useState("");
  const [pendingWorkflowFocusId, setPendingWorkflowFocusId] = useState("");
  const [reviewUndo, setReviewUndo] = useState<{
    nodes: ReviewNode[];
    confirmedIds: string[];
    revision: number;
    label: string;
  } | null>(null);
  const [reorganizeDialogOpen, setReorganizeDialogOpen] = useState(false);
  const [publicationDialogOpen, setPublicationDialogOpen] = useState(false);
  const [tableRowGroupDialogOpen, setTableRowGroupDialogOpen] =
    useState(false);
  const [tableRowGroupReview, setTableRowGroupReview] =
    useState<TableRecordProjectionRowGroupReview | null>(null);
  const [tableRowGroupDraft, setTableRowGroupDraft] = useState<
    TableRecordProjectionRowCoordinate[][]
  >([]);
  const [tableRowGroupLoading, setTableRowGroupLoading] = useState(false);
  const [tableRowGroupSaving, setTableRowGroupSaving] = useState(false);
  const [tableRowGroupError, setTableRowGroupError] = useState("");
  const [structureDrawerOpen, setStructureDrawerOpen] = useState(false);
  const [structureAction, setStructureAction] = useState<
    "reparent" | "reorder" | "merge" | "split"
  >("reparent");
  const [structureTargetId, setStructureTargetId] = useState("");
  const [structureParentQuery, setStructureParentQuery] = useState("");
  const [structureParentPickerOpen, setStructureParentPickerOpen] =
    useState(false);
  const [structureParentActiveIndex, setStructureParentActiveIndex] =
    useState(0);
  const [mergeTargetId, setMergeTargetId] = useState("");
  const [splitOffset, setSplitOffset] = useState("");
  const [restorationRunState, setRestorationRunState] =
    useState<KeyedRestorationRunState | null>(null);
  const currentRestorationRunState =
    restorationRunState?.documentKey === pdfDocumentKey
      ? restorationRunState
      : null;
  const restorationRun = currentRestorationRunState?.payload || null;
  const restorationRunError = currentRestorationRunState?.error || "";
  const [pageEvidenceState, setPageEvidenceState] =
    useState<KeyedPayloadState | null>(null);
  const [pageEvidenceViewState, setPageEvidenceViewState] =
    useState<KeyedPageEvidenceViewState | null>(null);
  const [parsingDiagnosticsState, setParsingDiagnosticsState] =
    useState<KeyedPayloadState | null>(null);
  const [parsingRiskDialogOpen, setParsingRiskDialogOpen] = useState(false);
  const [parsingRerunDialogOpen, setParsingRerunDialogOpen] = useState(false);
  const [diagnosticFocusId, setDiagnosticFocusId] = useState("");
  const [diagnosticRiskOnly, setDiagnosticRiskOnly] = useState(false);
  const [diagnosticShowSupportLayer, setDiagnosticShowSupportLayer] =
    useState(false);
  const [diagnosticContentViewState, setDiagnosticContentViewState] =
    useState<KeyedPageContentViewState | null>(null);
  const [geometryFocusState, setGeometryFocusState] = useState<{
    requestKey: string;
    status: "LOADING" | "READY" | "ERROR";
    payload: JsonRecord;
    error: string;
  } | null>(null);
  const treeViewportRef = useRef<HTMLDivElement>(null);
  const treeScrollTopRef = useRef(treeScrollTop);
  const treeScrollUpdateFrameRef = useRef<number | null>(null);
  const treeExpandMenuRef = useRef<HTMLDetailsElement>(null);
  const classificationBatchErrorRef = useRef<HTMLDivElement>(null);
  const classificationPublicationErrorRef = useRef<HTMLDivElement>(null);
  const pdfFrameRef = useRef<HTMLIFrameElement>(null);
  const pagePayloadCacheRef = useRef(new Map<string, JsonRecord>());
  const parsingDiagnosticsRequestKeyRef = useRef("");
  const correctionTextRef = useRef<HTMLTextAreaElement>(null);
  const boundaryCalibrationRequestRef = useRef(0);
  const tableRowGroupRequestRef = useRef<AbortController | null>(null);
  const expansionScopeRef = useRef("");
  const filterExpansionSnapshotRef = useRef<Set<string> | null>(null);
  const pendingClassificationCommandRef = useRef<{
    key: string;
    commandId: string;
  } | null>(null);
  const classificationRequestKeyRef = useRef("");
  const classificationMutationGenerationRef = useRef(0);
  const classificationMutationRequestRef =
    useRef<ClassificationMutationRequestState | null>(null);
  const pendingAtomizationCommandRef = useRef<{
    key: string;
    commandId: string;
  } | null>(null);
  const enrichmentRequestKeyRef = useRef("");
  const atomizationMutationGenerationRef = useRef(0);
  const atomizationMutationRequestRef = useRef<{
    generation: number;
    requestKey: string;
    controller: AbortController;
  } | null>(null);
  const atomizationLastAttemptRef = useRef<
    RequirementAtomizationScopeState["last_attempt"]
  >(null);
  const semanticPublicationGenerationRef = useRef(0);
  const semanticPublicationContextKeyRef = useRef("");
  const semanticPublicationIdentityKeyRef = useRef("");
  const publicationDialogContextKeyRef = useRef("");
  const semanticPublicationRequestRef =
    useRef<SemanticPublicationRequestState | null>(null);
  const parsingDiagnosticsRequestKey = JSON.stringify([
    route.projectId,
    route.documentId,
    sourcePageSnapshotId,
    sourcePageSourceSha256,
  ]);
  const currentParsingDiagnosticsState =
    parsingDiagnosticsState?.requestKey === parsingDiagnosticsRequestKey
      ? parsingDiagnosticsState
      : null;
  const parsingDiagnosticsPayload =
    currentParsingDiagnosticsState?.payload || null;
  const parsingDiagnostics = useMemo(
    () =>
      parsingDiagnosticsPayload
        ? normalizeParsingDiagnostics(parsingDiagnosticsPayload)
        : unavailableParsingDiagnostics(),
    [parsingDiagnosticsPayload],
  );
  const parsingDiagnosticsCurrent = Boolean(
    currentParsingDiagnosticsState?.status === "READY" &&
    parsingDiagnostics.sourceSha256 === sourcePageSourceSha256 &&
    parsingDiagnostics.activeParseArtifactId === sourcePageSnapshotId,
  );
  const parsingGatePassed = Boolean(
    treeState === "READY" &&
    !latestExtractionRunning &&
    sourcePagesReady &&
    parsingDiagnosticsCurrent &&
    ["READY", "READY_WITH_RISKS"].includes(parsingDiagnostics.status) &&
    parsingDiagnostics.hardBlockerCount === 0,
  );
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (!document.hidden) {
        try {
          const value = await api.sourceRequirementBaselineStatus(route.projectId, route.documentId, controller.signal);
          if (!controller.signal.aborted) setSourceRequirementStatus(previous =>
            previous?.baseline_artifact_sha256 === value.baseline_artifact_sha256 && previous?.status === value.status ? previous : value);
        } catch { if (!controller.signal.aborted) setSourceRequirementStatus(null); }
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 5000);
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [route.documentId, route.projectId, sourceGraphRefresh]);
  const sourceRequirementReady =
    sourceRequirementStatus?.baseline_ready === true;
  const activeCheckpoint: ExtractionCheckpoint = requestedCheckpoint;
  const showSourceEvidenceLens = Boolean(
    activeCheckpoint === "structure" && sourceEvidenceOpen,
  );
  useEffect(() => {
    setSourceEvidenceOpen(rawRequestedCheckpoint === "diagnose");
  }, [rawRequestedCheckpoint, route.documentId, route.projectId]);
  useEffect(() => {
    setNativeStructureReferenceAvailable(false);
  }, [route.documentId, route.projectId]);
  const nativeStructurePageLimit = availableDocumentPageCount(
    formalPageLimit,
    Number(selectedDocument?.page_count || 0),
    nativeStructureReferencePageCount,
  );
  const activePdfPageLimit =
    ["structure", "list"].includes(activeCheckpoint) &&
    nativeStructureReferenceAvailable
      ? nativeStructurePageLimit
      : formalPageLimit;
  const pdfPage = activePdfPageLimit
    ? Math.min(activePdfPageLimit, storedPdfPage)
    : storedPdfPage;
  useEffect(() => {
    if (!nativeStructureAdvancement.available) {
      setNativeBaselineStatus(null);
      setNativeParentStatus(null);
      setNativeAdvanceError("");
      return;
    }
    const controller = new AbortController();
    let timer: number | null = null;
    const poll = async () => {
      try {
        const [baseline, parent] = await Promise.all([
          api.nativeStructureBaselineStatus(
            route.projectId,
            route.documentId,
            controller.signal,
          ),
          api.nativeParentProvisionStatus(
            route.projectId,
            route.documentId,
            controller.signal,
          ),
        ]);
        if (controller.signal.aborted) return;
        setNativeBaselineStatus(baseline);
        setNativeParentStatus(parent);
        setNativeAdvanceError("");
        if (
          [baseline.status, parent.status].some((status) =>
            ["PENDING", "RUNNING", "CANCEL_REQUESTED"].includes(status),
          )
        ) {
          timer = window.setTimeout(poll, 1500);
        }
      } catch (caught) {
        if (controller.signal.aborted) return;
        setNativeAdvanceError(
          caught instanceof ApiError
            ? caught.message
            : "Agent 基线状态暂时无法同步。",
        );
        timer = window.setTimeout(poll, 3000);
      }
    };
    void poll();
    return () => {
      controller.abort();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [
    nativeStructureAdvancement.artifactId,
    nativeStructureAdvancement.available,
    nativeStructureAdvancement.sourceSha256,
    route.documentId,
    route.projectId,
  ]);
  const parsingDiagnosticsLoading = Boolean(
    showSourceEvidenceLens &&
    sourcePagesReady &&
    (!currentParsingDiagnosticsState ||
      currentParsingDiagnosticsState.status === "LOADING"),
  );
  const parsingDiagnosticsError = currentParsingDiagnosticsState?.error || "";
  if (treeScrollUpdateFrameRef.current === null) {
    treeScrollTopRef.current = treeScrollTop;
  }

  useEffect(() => {
    if (activeCheckpoint !== "structure") {
      setOrganizeView("tree");
      setReorganizeDialogOpen(false);
      setStructureDrawerOpen(false);
      tableRowGroupRequestRef.current?.abort();
      tableRowGroupRequestRef.current = null;
      setTableRowGroupDialogOpen(false);
    }
    if (activeCheckpoint === "list") {
      setCompactDetail("content");
    }
    if (activeCheckpoint !== "list") {
      classificationMutationGenerationRef.current += 1;
      classificationMutationRequestRef.current?.controller.abort();
      classificationMutationRequestRef.current = null;
      atomizationMutationGenerationRef.current += 1;
      atomizationMutationRequestRef.current?.controller.abort();
      atomizationMutationRequestRef.current = null;
      semanticPublicationGenerationRef.current += 1;
      semanticPublicationRequestRef.current?.controller.abort();
      semanticPublicationRequestRef.current = null;
      setAtomizationMutation("");
      setSemanticPublicationMutation(false);
      setClassificationMutation("");
      setPublicationDialogOpen(false);
      setClassificationBatchOpen(false);
      setAtomizationDialogOpen(false);
      setClassificationFilter("ALL");
      setClassificationMutationError("");
      setPendingLocalDraftActivation(null);
      setAtomizationMutationError("");
      pendingClassificationCommandRef.current = null;
      pendingAtomizationCommandRef.current = null;
    }
    if (!showSourceEvidenceLens) {
      setParsingRiskDialogOpen(false);
      setParsingRerunDialogOpen(false);
      setDiagnosticFocusId("");
      setDiagnosticRiskOnly(false);
      setDiagnosticShowSupportLayer(false);
    }
  }, [activeCheckpoint, showSourceEvidenceLens]);

  useEffect(() => {
    if (!structureArtifactReadOnly) return;
    classificationMutationGenerationRef.current += 1;
    classificationMutationRequestRef.current?.controller.abort();
    classificationMutationRequestRef.current = null;
    atomizationMutationGenerationRef.current += 1;
    atomizationMutationRequestRef.current?.controller.abort();
    atomizationMutationRequestRef.current = null;
    semanticPublicationGenerationRef.current += 1;
    semanticPublicationRequestRef.current?.controller.abort();
    semanticPublicationRequestRef.current = null;
    setClassificationMutation("");
    setAtomizationMutation("");
    setSemanticPublicationMutation(false);
    setPendingLocalDraftActivation(null);
    setClassificationBatchOpen(false);
    setAtomizationDialogOpen(false);
    setPublicationDialogOpen(false);
    setStructureDrawerOpen(false);
    setOrganizeView("tree");
  }, [structureArtifactReadOnly]);

  useEffect(() => {
    if (
      !classificationMutationError ||
      (!classificationBatchOpen && !publicationDialogOpen)
    ) {
      return;
    }
    const frame = requestAnimationFrame(() => {
      const target = classificationBatchOpen
        ? classificationBatchErrorRef.current
        : classificationPublicationErrorRef.current;
      target?.focus();
    });
    return () => cancelAnimationFrame(frame);
  }, [
    classificationBatchOpen,
    classificationMutationError,
    publicationDialogOpen,
  ]);

  const workflow = asRecord(workbench.workflow);
  const workflowReview = asRecord(workflow.review);
  const geometryIndex = asRecord(sourcePages.geometry_index);
  const geometryIndexReady =
    String(geometryIndex.state || "").toUpperCase() === "READY" &&
    Boolean(String(geometryIndex.geometry_index_id || "")) &&
    Boolean(String(geometryIndex.geometry_index_sha256 || ""));
  const sessionId = String(workflowReview.session_id || "");
  const reviewRevision = Number(workflowReview.revision || 0);
  const classificationRequestKey = JSON.stringify([
    route.projectId,
    route.documentId,
    String(tree?.source_identity.source_sha256 || ""),
    String(tree?.structure_identity.effective_snapshot_id || ""),
    String(tree?.structure_identity.effective_tree_sha256 || ""),
  ]);
  classificationRequestKeyRef.current = classificationRequestKey;
  const currentEntryClassificationState =
    entryClassificationState?.requestKey === classificationRequestKey
      ? entryClassificationState
      : null;
  const entryClassification =
    currentEntryClassificationState?.status === "READY"
      ? currentEntryClassificationState.payload
      : null;
  const entryClassificationLoading = Boolean(
    activeCheckpoint === "list" &&
    tree?.state === "READY" &&
      (!currentEntryClassificationState ||
        currentEntryClassificationState.status === "LOADING"),
  );
  const entryClassificationError =
    currentEntryClassificationState?.error || "";
  const enrichmentRequestKey = JSON.stringify([
    route.projectId,
    route.documentId,
    sessionId,
    reviewRevision,
    String(tree?.source_identity.source_sha256 || ""),
    String(tree?.structure_identity.effective_snapshot_id || ""),
    String(tree?.structure_identity.effective_tree_sha256 || ""),
    String(entryClassification?.snapshot.snapshot_id || ""),
    Number(entryClassification?.snapshot.revision || 0),
    String(entryClassification?.snapshot.payload_sha256 || ""),
    String(entryClassification?.snapshot.working_tree_id || ""),
    String(entryClassification?.snapshot.working_tree_sha256 || ""),
  ]);
  enrichmentRequestKeyRef.current = enrichmentRequestKey;
  const currentRequirementEnrichmentState =
    requirementEnrichmentState?.requestKey === enrichmentRequestKey
      ? requirementEnrichmentState
      : null;
  const requirementEnrichment =
    currentRequirementEnrichmentState?.payload || null;
  const requirementEnrichmentReady =
    currentRequirementEnrichmentState?.status === "READY";
  const atomizationScopeState = requirementEnrichment?.atomization_scope || null;
  atomizationLastAttemptRef.current = atomizationScopeState?.last_attempt || null;
  const semanticPublicationCapability = requirementEnrichmentReady
    ? exactRequirementSemanticPublicationCapabilityV2(
        requirementEnrichment?.semantic_publication_capability,
      )
    : null;
  const semanticPublicationReady = Boolean(
    sessionId && requirementEnrichmentReady && semanticPublicationCapability,
  );
  const semanticPublicationUnbound = Boolean(
    sessionId && !semanticPublicationReady,
  );
  let semanticPublicationAtomizationBinding: RequirementSemanticPublicationAtomizationBinding | null =
    null;
  let semanticPublicationAtomizationError = "";
  if (semanticPublicationReady && atomizationScopeState) {
    try {
      semanticPublicationAtomizationBinding =
        requirementSemanticPublicationAtomizationBinding(atomizationScopeState);
    } catch (cause) {
      semanticPublicationAtomizationError =
        cause instanceof Error
          ? cause.message
          : "当前原子化范围无法安全发布，请重新读取后再生成。";
    }
  }
  const semanticPublicationContextKey = JSON.stringify([
    activeCheckpoint,
    route.task,
    route.mode,
    route.projectId,
    route.documentId,
    route.requirementSetId,
    activeSetId,
    sessionId,
    reviewRevision,
    String(tree?.source_identity.source_sha256 || ""),
    String(tree?.structure_identity.effective_snapshot_id || ""),
    String(tree?.structure_identity.effective_tree_sha256 || ""),
    structureArtifactReadOnly,
    structureArtifactReadOnlyReasonCode,
  ]);
  const semanticPublicationIdentityFor = (
    classification: EntryClassificationResponse | null,
  ) =>
    JSON.stringify([
      semanticPublicationContextKey,
      String(classification?.snapshot.snapshot_id || ""),
      Number(classification?.snapshot.revision || 0),
      String(classification?.snapshot.payload_sha256 || ""),
      String(classification?.snapshot.working_tree_id || ""),
      String(classification?.snapshot.working_tree_sha256 || ""),
    ]);
  const semanticPublicationIdentityKey =
    semanticPublicationIdentityFor(entryClassification);
  semanticPublicationContextKeyRef.current = semanticPublicationContextKey;
  semanticPublicationIdentityKeyRef.current = semanticPublicationIdentityKey;
  const requirementEnrichmentLoading = Boolean(
    activeCheckpoint === "list" &&
      sessionId &&
      entryClassification &&
      (!currentRequirementEnrichmentState ||
        currentRequirementEnrichmentState.status === "LOADING"),
  );
  const requirementEnrichmentError =
    currentRequirementEnrichmentState?.error || "";

  useEffect(() => {
    const pendingRequest = classificationMutationRequestRef.current;
    if (pendingRequest && pendingRequest.requestKey !== classificationRequestKey) {
      classificationMutationGenerationRef.current += 1;
      pendingRequest.controller.abort();
      classificationMutationRequestRef.current = null;
      setClassificationMutation("");
    }
    setClassificationMutationError("");
    setPendingLocalDraftActivation(null);
    pendingClassificationCommandRef.current = null;
  }, [classificationRequestKey]);
  const restorationRunId = String(
    restorationRun?.run_id || restorationRun?.id || "",
  );
  const restorationRunStatus = String(
    restorationRun?.status || "",
  ).toUpperCase();
  const effectiveRestorationRunStatus = String(
    restorationRun?.run_status || restorationRunStatus,
  ).toUpperCase();
  const parsingPageCount = sourcePagesReady
    ? Math.max(
        1,
        Number(sourcePages.page_count || 0),
        Number(asRecord(extractionJob.page_staging).total_pages || 0),
      )
    : Math.max(
        Number(asRecord(extractionJob.page_staging).total_pages || 0),
        Number(asRecord(restorationRun?.progress).page_count || 0),
        Number(selectedDocument?.page_count || 0),
        1,
      );
  useEffect(() => {
    setPdfPage((current) => Math.min(parsingPageCount, Math.max(1, current)));
    setDiagnosticFocusId("");
  }, [parsingPageCount, pdfDocumentKey, setPdfPage]);
  const formalPageReady = sourcePagesReady && Boolean(sourcePageSnapshotId);
  const legacyPageReady =
    effectiveRestorationRunStatus === "READY" && Boolean(restorationRunId);
  const pageStaging = asRecord(extractionJob.page_staging);
  const stagedJobId = String(pageStaging.job_id || "");
  const stagedAttemptGeneration = Math.floor(
    Number(pageStaging.attempt_generation || 0),
  );
  const stagedTotalPages = Math.max(
    0,
    Math.floor(Number(pageStaging.total_pages || 0)),
  );
  const stagedReadyPageCountRaw = Math.max(
    0,
    Math.floor(Number(pageStaging.ready_page_count || 0)),
  );
  const stagedReadyPageCount = stagedTotalPages > 0
    ? Math.min(stagedReadyPageCountRaw, stagedTotalPages)
    : stagedReadyPageCountRaw;
  const stagedPageUrlTemplate = String(pageStaging.page_url_template || "");
  const stagedReadyPageRanges = normalizeReadyPageRanges(
    pageStaging.ready_page_ranges,
    stagedTotalPages,
  );
  const stagedPageFactsActive = Boolean(
    latestExtractionRunning &&
      stagedReadyPageCount > 0 &&
      stagedJobId &&
      stagedJobId === String(extractionJob.job_id || "") &&
      stagedAttemptGeneration > 0 &&
      stagedReadyPageRanges.length > 0,
  );
  const stagedPageFactsKey = stagedPageFactsActive
    ? JSON.stringify([
        route.projectId,
        route.documentId,
        stagedJobId,
        stagedAttemptGeneration,
      ])
    : "";
  const currentPageEvidenceViewState =
    pageEvidenceViewState?.stagingKey === stagedPageFactsKey
      ? pageEvidenceViewState
      : null;
  const pageEvidenceView: PageEvidenceView = stagedPageFactsActive
    ? currentPageEvidenceViewState?.view === "FORMAL_PREVIOUS" && formalPageReady
      ? "FORMAL_PREVIOUS"
      : "STAGED_CURRENT"
    : "FORMAL_PREVIOUS";
  const stagedPageViewActive =
    stagedPageFactsActive && pageEvidenceView === "STAGED_CURRENT";
  const stagedSelectedPageReady = Boolean(
    stagedPageViewActive &&
      pageIsReadyInRanges(pdfPage, stagedReadyPageRanges),
  );
  const pageEvidenceArtifactId = stagedPageViewActive
    ? `staged:${stagedJobId}:${stagedAttemptGeneration}`
    : formalPageReady
      ? sourcePageSnapshotId
      : legacyPageReady
        ? restorationRunId
        : "";
  const diagnosticContentViewIdentityKey = JSON.stringify([
    route.projectId,
    route.documentId,
    pageEvidenceArtifactId,
    pageEvidenceView,
    pdfPage,
  ]);
  const diagnosticContentView: PageContentView =
    diagnosticContentViewState?.identityKey ===
    diagnosticContentViewIdentityKey
      ? diagnosticContentViewState.view
      : "EFFECTIVE";
  const diagnosticVisualRecoveryFailed = Boolean(
    !stagedPageViewActive &&
      visualReceiptSummary?.degradedPageNumbers.includes(pdfPage),
  );
  const pageEvidenceRequestAvailable = stagedPageViewActive
    ? stagedSelectedPageReady
    : formalPageReady || legacyPageReady;
  const pageEvidenceRequestKey = JSON.stringify([
    route.projectId,
    route.documentId,
    pageEvidenceArtifactId,
    pageEvidenceView,
    pdfPage,
  ]);
  const currentPageEvidenceState =
    pageEvidenceState?.requestKey === pageEvidenceRequestKey
      ? pageEvidenceState
      : null;
  const restorationPagePayload = currentPageEvidenceState?.payload || null;
  const pageEvidenceError =
    currentPageEvidenceState?.error ||
    (!stagedPageViewActive && !formalPageReady ? restorationRunError : "");
  const pageEvidenceLoading = Boolean(
    pageEvidenceRequestAvailable &&
      (!currentPageEvidenceState || currentPageEvidenceState.status === "LOADING"),
  );
  useLayoutEffect(() => {
    parsingDiagnosticsRequestKeyRef.current = parsingDiagnosticsRequestKey;
  }, [parsingDiagnosticsRequestKey]);
  const restorationPageComparison = asRecord(
    restorationPagePayload?.page_comparison ??
      restorationPagePayload?.pageComparison ??
      asRecord(restorationPagePayload?.result).page_comparison,
  );
  const restorationPayloadPage =
    asArray<JsonRecord>(restorationPageComparison.pages)[0] ||
    asRecord(restorationPagePayload?.page);
  const restorationPayloadPageNumber = Number(
    restorationPagePayload?.page_number ||
      restorationPayloadPage.page_number ||
      restorationPayloadPage.page ||
      0,
  );
  const restorationPayloadProjectId = String(
    restorationPagePayload?.project_id || "",
  );
  const restorationPayloadDocumentId = String(
    restorationPagePayload?.document_id || "",
  );
  const currentRestorationPagePayload =
    restorationPagePayload &&
    restorationPayloadPageNumber === pdfPage &&
    (!restorationPayloadProjectId ||
      restorationPayloadProjectId === route.projectId) &&
    (!restorationPayloadDocumentId ||
      restorationPayloadDocumentId === route.documentId)
      ? restorationPagePayload
      : null;
  const pageSnapshot = useMemo(
    () => normalizeRestorationPage(currentRestorationPagePayload, pdfPage),
    [currentRestorationPagePayload, pdfPage],
  );
  useEffect(() => {
    setDiagnosticFocusId("");
    setDiagnosticRiskOnly(false);
    setDiagnosticShowSupportLayer(false);
  }, [diagnosticContentViewIdentityKey]);
  useEffect(() => {
    if (!route.projectId || !route.documentId) {
      setRestorationRunState(null);
      setPageEvidenceState(null);
      setPageEvidenceViewState(null);
      return;
    }
    if (sourcePagesReady) {
      setRestorationRunState(null);
      return;
    }
    let active = true;
    const documentKey = pdfDocumentKey;
    setRestorationRunState({ documentKey, payload: null, error: "" });
    void api
      .latestRestoration(route.projectId, route.documentId)
      .then((payload) => {
        if (active) {
          setRestorationRunState({ documentKey, payload, error: "" });
        }
      })
      .catch((cause) => {
        if (active) {
          setRestorationRunState({
            documentKey,
            payload: null,
            error:
              cause instanceof Error
                ? cause.message
                : "逐页解析事实暂时无法建立。",
          });
        }
      });
    return () => {
      active = false;
    };
  }, [pdfDocumentKey, route.documentId, route.projectId, sourcePagesReady]);

  useEffect(() => {
    if (!sourcePagesReady || !route.projectId || !route.documentId) {
      setParsingDiagnosticsState(null);
      return;
    }
    let active = true;
    const requestKey = parsingDiagnosticsRequestKey;
    setParsingDiagnosticsState({
      requestKey,
      status: "LOADING",
      payload: null,
      error: "",
    });
    void api
      .parsingDiagnostics(route.projectId, route.documentId)
      .then((payload) => {
        if (!active) return;
        const normalized = normalizeParsingDiagnostics(payload);
        if (
          normalized.status === "UNAVAILABLE" ||
          normalized.activeParseArtifactId !== sourcePageSnapshotId ||
          normalized.sourceSha256 !== sourcePageSourceSha256
        ) {
          throw new Error("解析风险与当前解析结果不一致，请同步后重试");
        }
        setParsingDiagnosticsState({
          requestKey,
          status: "READY",
          payload,
          error: "",
        });
      })
      .catch((cause) => {
        if (!active) return;
        setParsingDiagnosticsState({
          requestKey,
          status: "ERROR",
          payload: null,
          error:
            cause instanceof Error ? cause.message : "解析风险摘要读取失败。",
        });
      });
    return () => {
      active = false;
    };
  }, [
    route.documentId,
    route.projectId,
    parsingDiagnosticsRequestKey,
    sourcePageSnapshotId,
    sourcePageSourceSha256,
    sourcePagesReady,
  ]);

  useEffect(() => {
    if (
      activeCheckpoint !== "list" ||
      !tree ||
      tree.state !== "READY" ||
      !route.projectId ||
      !route.documentId
    ) {
      setEntryClassificationState(null);
      return;
    }
    let active = true;
    const requestKey = classificationRequestKey;
    setEntryClassificationState({
      requestKey,
      status: "LOADING",
      payload: null,
      error: "",
    });
    setClassificationMutationError("");
    pendingClassificationCommandRef.current = null;
    void api
      .entryClassifications(route.projectId, route.documentId)
      .then((payload) => {
        if (!active) return;
        const verified = assertEntryClassificationMatchesTree(payload, tree);
        setEntryClassificationState({
          requestKey,
          status: "READY",
          payload: verified,
          error: "",
        });
      })
      .catch((cause) => {
        if (!active) return;
        setEntryClassificationState({
          requestKey,
          status: "ERROR",
          payload: null,
          error:
            cause instanceof Error
              ? cause.message
              : "当前清单分类读取失败。",
        });
      });
    return () => {
      active = false;
    };
  }, [
    activeCheckpoint,
    classificationReloadToken,
    classificationRequestKey,
    route.documentId,
    route.projectId,
    tree,
  ]);

  useEffect(() => {
    if (
      activeCheckpoint !== "list" ||
      !requirementEnrichmentReady ||
      atomizationScopeState?.last_attempt?.status !== "RUNNING"
    ) {
      return;
    }
    const timer = window.setTimeout(() => {
      setEnrichmentReloadToken((value) => value + 1);
    }, 2_500);
    return () => window.clearTimeout(timer);
  }, [
    activeCheckpoint,
    enrichmentRequestKey,
    requirementEnrichmentReady,
    atomizationScopeState?.last_attempt?.status,
  ]);

  useEffect(() => {
    if (!tree || !entryClassification) return;
    setAtomizationSelection((current) => {
      const next = reconcileSelectedAtomizationWorkflowIds(
        current,
        tree,
        entryClassification,
      );
      return next.identityKey === current.identityKey &&
        next.workflowIds.length === current.workflowIds.length &&
        next.workflowIds.every(
          (workflowId, index) => workflowId === current.workflowIds[index],
        )
        ? current
        : next;
    });
  }, [entryClassification, tree]);

  useEffect(() => {
    const pendingRequest = atomizationMutationRequestRef.current;
    if (
      pendingRequest &&
      pendingRequest.requestKey !== enrichmentRequestKey
    ) {
      pendingRequest.controller.abort();
      atomizationMutationRequestRef.current = null;
      setAtomizationMutation("");
    }
    setAtomizationMutationError("");
    setAtomizationDialogOpen(false);
    pendingAtomizationCommandRef.current = null;
  }, [enrichmentRequestKey]);

  useEffect(() => {
    const pendingRequest = semanticPublicationRequestRef.current;
    if (
      pendingRequest &&
      (pendingRequest.contextKey !== semanticPublicationContextKey ||
        pendingRequest.identityKey !== semanticPublicationIdentityKey)
    ) {
      pendingRequest.controller.abort();
      semanticPublicationRequestRef.current = null;
      setSemanticPublicationMutation(false);
      setClassificationMutation((current) =>
        current === "ACCEPT" ? "" : current,
      );
      setPublicationDialogOpen(false);
    }
    if (
      publicationDialogOpen &&
      publicationDialogContextKeyRef.current &&
      publicationDialogContextKeyRef.current !==
        semanticPublicationContextKey
    ) {
      publicationDialogContextKeyRef.current = "";
      setPublicationDialogOpen(false);
    }
  }, [
    publicationDialogOpen,
    semanticPublicationContextKey,
    semanticPublicationIdentityKey,
  ]);

  useEffect(
    () => () => {
      classificationMutationGenerationRef.current += 1;
      classificationMutationRequestRef.current?.controller.abort();
      classificationMutationRequestRef.current = null;
      atomizationMutationGenerationRef.current += 1;
      atomizationMutationRequestRef.current?.controller.abort();
      atomizationMutationRequestRef.current = null;
      semanticPublicationGenerationRef.current += 1;
      semanticPublicationRequestRef.current?.controller.abort();
      semanticPublicationRequestRef.current = null;
    },
    [],
  );

  useEffect(() => {
    if (
      activeCheckpoint !== "list" ||
      !sessionId ||
      !tree ||
      tree.state !== "READY" ||
      !entryClassification ||
      !route.projectId ||
      !route.documentId
    ) {
      setRequirementEnrichmentState(null);
      return;
    }
    const controller = new AbortController();
    const requestKey = enrichmentRequestKey;
    setRequirementEnrichmentState((current) => ({
      requestKey,
      status: "LOADING",
      payload: current?.requestKey === requestKey ? current.payload : null,
      error: "",
    }));
    void api
      .requirementEnrichment(
        route.projectId,
        route.documentId,
        sessionId,
        controller.signal,
      )
      .then((payload) => {
        if (controller.signal.aborted) return;
        validatedAtomizationScopeState(payload, {
          projectId: route.projectId,
          documentId: route.documentId,
          sessionId,
          reviewRevision,
          classification: entryClassification,
        });
        setAtomizationMutationError("");
        setRequirementEnrichmentState({
          requestKey,
          status: "READY",
          payload,
          error: "",
        });
      })
      .catch((cause) => {
        if (
          controller.signal.aborted ||
          (cause instanceof DOMException && cause.name === "AbortError")
        ) {
          return;
        }
        setRequirementEnrichmentState((current) => ({
          requestKey,
          status: "ERROR",
          payload: current?.requestKey === requestKey ? current.payload : null,
          error:
            cause instanceof Error
              ? `原子化范围读取失败：${cause.message}`
              : "原子化范围读取失败，请重试。",
        }));
      });
    return () => controller.abort();
  }, [
    activeCheckpoint,
    enrichmentReloadToken,
    enrichmentRequestKey,
    entryClassification,
    reviewRevision,
    route.documentId,
    route.projectId,
    sessionId,
    tree,
  ]);

  useEffect(() => {
    if (
      sourcePagesReady ||
      !restorationRunId ||
      ["READY", "FAILED", "CANCELLED", "STALE"].includes(
        effectiveRestorationRunStatus,
      ) ||
      !route.projectId ||
      !route.documentId
    ) {
      return;
    }
    let active = true;
    const documentKey = pdfDocumentKey;
    const timer = window.setTimeout(() => {
      void api
        .restorationRun(route.projectId, route.documentId, restorationRunId)
        .then((payload) => {
          if (active) {
            setRestorationRunState({ documentKey, payload, error: "" });
          }
        })
        .catch((cause) => {
          if (active) {
            setRestorationRunState((current) => ({
              documentKey,
              payload:
                current?.documentKey === documentKey
                  ? current.payload
                  : restorationRun,
              error:
                cause instanceof Error
                  ? cause.message
                  : "逐页解析进度读取失败。",
            }));
          }
        });
    }, 700);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [
    effectiveRestorationRunStatus,
    pdfDocumentKey,
    restorationRun,
    restorationRunId,
    route.documentId,
    route.projectId,
    sourcePagesReady,
  ]);

  useEffect(() => {
    if (
      !pageEvidenceRequestAvailable ||
      !route.projectId ||
      !route.documentId
    ) {
      setPageEvidenceState(null);
      return;
    }
    const requestKey = pageEvidenceRequestKey;
    const cacheKey = pageEvidenceRequestKey;
    const cached = pagePayloadCacheRef.current.get(cacheKey);
    if (cached) {
      pagePayloadCacheRef.current.delete(cacheKey);
      pagePayloadCacheRef.current.set(cacheKey, cached);
      setPageEvidenceState({
        requestKey,
        status: "READY",
        payload: cached,
        error: "",
      });
      return;
    }
    let active = true;
    setPageEvidenceState({
      requestKey,
      status: "LOADING",
      payload: null,
      error: "",
    });
    const pageRequest = stagedPageViewActive
      ? api.stagedSourcePage(
          stagedJobId,
          stagedAttemptGeneration,
          pdfPage,
          stagedPageUrlTemplate,
        )
      : formalPageReady
        ? api.sourcePage(route.projectId, route.documentId, pdfPage)
        : api.restorationPage(
            route.projectId,
            route.documentId,
            restorationRunId,
            pdfPage,
          );
    void pageRequest
      .then((payload) => {
        if (!active) return;
        if (stagedPageViewActive) {
          const attempt = asRecord(payload.attempt);
          const responseJobId = String(payload.job_id || attempt.job_id || "");
          const responseGeneration = Math.floor(
            Number(
              payload.attempt_generation || attempt.attempt_generation || 0,
            ),
          );
          if (responseJobId && responseJobId !== stagedJobId) {
            throw new Error("本轮暂存页与当前解析任务不一致，已拒绝显示。");
          }
          if (
            responseGeneration > 0 &&
            responseGeneration !== stagedAttemptGeneration
          ) {
            throw new Error("本轮暂存页与当前解析代际不一致，已拒绝显示。");
          }
        }
        pagePayloadCacheRef.current.set(cacheKey, payload);
        while (pagePayloadCacheRef.current.size > 5) {
          const oldestKey = pagePayloadCacheRef.current.keys().next().value;
          if (typeof oldestKey !== "string") break;
          pagePayloadCacheRef.current.delete(oldestKey);
        }
        setPageEvidenceState({
          requestKey,
          status: "READY",
          payload,
          error: "",
        });
      })
      .catch((cause) => {
        if (active) {
          setPageEvidenceState({
            requestKey,
            status: "ERROR",
            payload: null,
            error:
              cause instanceof Error ? cause.message : "本页解析事实载入失败。",
          });
        }
      });
    return () => {
      active = false;
    };
  }, [
    pdfPage,
    restorationRunId,
    effectiveRestorationRunStatus,
    formalPageReady,
    legacyPageReady,
    pageEvidenceRequestKey,
    pageEvidenceRequestAvailable,
    route.documentId,
    route.projectId,
    sourcePageSnapshotId,
    sourcePagesReady,
    stagedAttemptGeneration,
    stagedJobId,
    stagedPageUrlTemplate,
    stagedPageViewActive,
  ]);

  useEffect(() => {
    if (!sessionId || activeCheckpoint !== "structure") {
      setReview(null);
      return;
    }
    let active = true;
    void api
      .reviewSession(route.projectId, route.documentId, sessionId)
      .then((payload) => {
        if (active) setReview(reviewSessionView(payload));
      })
      .catch(() => {
        if (active) setReview(null);
      });
    return () => {
      active = false;
    };
  }, [activeCheckpoint, route.documentId, route.projectId, sessionId]);

  useEffect(() => {
    const element = treeViewportRef.current;
    if (!element) return;
    const observer = new ResizeObserver(() =>
      setTreeViewportHeight(element.clientHeight),
    );
    observer.observe(element);
    setTreeViewportHeight(element.clientHeight);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const element = treeViewportRef.current;
    if (!element) return;
    const frame = requestAnimationFrame(() => {
      const restoredScrollTop = treeScrollTopRef.current;
      if (Math.abs(element.scrollTop - restoredScrollTop) > 1) {
        element.scrollTop = restoredScrollTop;
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [activeCheckpoint, expansionScope, tree?.nodes.length]);

  useEffect(
    () => () => {
      if (treeScrollUpdateFrameRef.current !== null) {
        cancelAnimationFrame(treeScrollUpdateFrameRef.current);
        treeScrollUpdateFrameRef.current = null;
      }
    },
    [organizeUiScope],
  );

  useEffect(() => {
    if (expansionScopeRef.current === expansionScope) return;
    expansionScopeRef.current = expansionScope;
    filterExpansionSnapshotRef.current = null;
  }, [expansionScope]);

  useEffect(() => {
    if (!tree || expandedIdList !== null) return;
    setExpandedIdList(Array.from(initialExpandedIds(tree)));
  }, [expandedIdList, setExpandedIdList, tree]);

  const classificationProjectedNodeIds = useMemo(
    () =>
      activeCheckpoint === "list" && tree && entryClassification
        ? classificationFilterNodeIds(
            tree,
            entryClassification,
            classificationFilter,
          )
        : new Set(tree?.nodes.map((node) => node.id) || []),
    [activeCheckpoint, classificationFilter, entryClassification, tree],
  );
  const classificationFilterActive =
    activeCheckpoint === "list" && classificationFilter !== "ALL";
  const effectiveTreeFilter: RequirementTreeFilter =
    activeCheckpoint === "list" ? "all" : filter;
  const filterActive =
    Boolean(query.trim()) ||
    effectiveTreeFilter !== "all" ||
    classificationFilterActive;

  useEffect(() => {
    if (!tree) return;
    if (filterActive) {
      setExpandedIds((current) => {
        if (!filterExpansionSnapshotRef.current) {
          filterExpansionSnapshotRef.current = new Set(current);
        }
        const next = new Set(current);
        expandableRequirementTreeIds(tree, query, effectiveTreeFilter).forEach(
          (nodeId) => next.add(nodeId),
        );
        if (classificationFilterActive) {
          classificationProjectedNodeIds.forEach((nodeId) => next.add(nodeId));
        }
        return next;
      });
      return;
    }
    if (filterExpansionSnapshotRef.current) {
      const restored = filterExpansionSnapshotRef.current;
      filterExpansionSnapshotRef.current = null;
      setExpandedIds(restored);
    }
  }, [
    classificationFilterActive,
    classificationProjectedNodeIds,
    effectiveTreeFilter,
    filterActive,
    query,
    tree,
  ]);

  useEffect(() => {
    const closeTreeMenu = (event: MouseEvent | KeyboardEvent) => {
      const menu = treeExpandMenuRef.current;
      if (!menu?.open) return;
      if (event instanceof KeyboardEvent && event.key === "Escape") {
        menu.open = false;
        menu.querySelector<HTMLElement>("summary")?.focus();
        return;
      }
      if (
        event instanceof MouseEvent &&
        event.target instanceof Node &&
        !menu.contains(event.target)
      ) {
        menu.open = false;
      }
    };
    document.addEventListener("click", closeTreeMenu);
    document.addEventListener("keydown", closeTreeMenu);
    return () => {
      document.removeEventListener("click", closeTreeMenu);
      document.removeEventListener("keydown", closeTreeMenu);
    };
  }, []);

  const visibleRows = useMemo(
    () =>
      flattenRequirementTree(
        tree,
        expandedIds,
        query,
        effectiveTreeFilter,
      ).filter((row) => classificationProjectedNodeIds.has(row.node.id)),
    [
      classificationProjectedNodeIds,
      effectiveTreeFilter,
      expandedIds,
      query,
      tree,
    ],
  );
  const navigationRows = visibleRows;
  const treeIndex = useMemo(() => requirementTreeIndex(tree), [tree]);
  const projectedNodeIds = useMemo(
    () =>
      new Set(
        flattenRequirementTree(
          tree,
          expandableRequirementTreeIds(tree, query, effectiveTreeFilter),
          query,
          effectiveTreeFilter,
        )
          .filter((row) => classificationProjectedNodeIds.has(row.node.id))
          .map((row) => row.node.id),
      ),
    [classificationProjectedNodeIds, effectiveTreeFilter, query, tree],
  );

  useEffect(() => {
    if (!tree) return;
    if (!navigationRows.length) {
      setSelectedId("");
      return;
    }
    if (!selectedId) {
      setSelectedId(initialVisibleSelectionId(navigationRows));
      return;
    }
    if (
      !navigationRows.some((row) => row.node.id === selectedId) &&
      !projectedNodeIds.has(selectedId)
    ) {
      const firstItem =
        navigationRows.find((row) => isRequirementTreeReviewItem(row.node)) ||
        navigationRows[0];
      setSelectedId(firstItem.node.id);
    }
  }, [navigationRows, projectedNodeIds, selectedId, setSelectedId, tree]);

  useEffect(() => {
    if (!pendingWorkflowFocusId || !tree) return;
    const focused = tree.nodes.find(
      (node) => node.workflow_node_id === pendingWorkflowFocusId,
    );
    if (!focused) return;
    setSelectedId(focused.id);
    setPendingWorkflowFocusId("");
  }, [pendingWorkflowFocusId, tree]);

  const selectedNode = treeIndex.get(selectedId) || null;
  const selectedClassificationEntry = entryClassificationForNode(
    entryClassification,
    selectedNode?.id || "",
  );
  const atomizationPlanResult = useMemo(() => {
    if (!tree || !entryClassification || !atomizationScopeState) {
      return { plan: null as AtomizationScopePlan | null, error: "" };
    }
    try {
      return {
        plan: buildAtomizationScopePlan({
          tree,
          classification: entryClassification,
          scope: atomizationScope,
          currentTreeNodeId: selectedId,
          selectedWorkflowIds: atomizationSelection.workflowIds,
          eligibleReviewNodeIds:
            atomizationScopeState.eligible_review_node_ids,
        }),
        error: "",
      };
    } catch (cause) {
      return {
        plan: null,
        error:
          cause instanceof Error
            ? cause.message
            : "当前原子化范围无法验证。",
      };
    }
  }, [
    atomizationScope,
    atomizationScopeState,
    atomizationSelection.workflowIds,
    entryClassification,
    selectedId,
    tree,
  ]);
  const atomizationApplyAvailable = Boolean(
    !structureArtifactReadOnly &&
    requirementEnrichmentReady &&
      atomizationScopeState?.capability.available &&
      atomizationScopeState.capability.operations.includes("APPLY") &&
      atomizationScopeState.capability.scopes.includes(atomizationScope),
  );
  const atomizationSelectionAvailable = Boolean(
    sessionId &&
      atomizationScopeState?.capability.available &&
      atomizationScopeState.capability.operations.includes("APPLY") &&
      atomizationScopeState.capability.scopes.includes("SELECTED"),
  );
  const atomizationUnavailableReason = !requirementEnrichmentReady
    ? "范围状态必须成功重读后才能执行，当前原条目保持不变"
    : "当前服务未开放这个原子化范围，原条目保持不变";
  const baseAtomizationViewModel = atomizationPlanResult.plan
    ? atomizationScopeViewModel(atomizationPlanResult.plan)
    : null;
  const atomizationAttemptGate = atomizationPlanResult.plan
    ? atomizationAttemptActionGate(
        atomizationPlanResult.plan,
        atomizationScopeState?.last_attempt || null,
      )
    : null;
  const atomizationViewModel = baseAtomizationViewModel
    ? {
        ...baseAtomizationViewModel,
        canApply:
          baseAtomizationViewModel.canApply &&
          atomizationApplyAvailable &&
          !atomizationAttemptGate?.applyBlocked,
        actionLabel: atomizationAttemptGate?.retryingApply
          ? `重试原子化 ${baseAtomizationViewModel.targetCount} 项技术需求`
          : baseAtomizationViewModel.actionLabel,
        emptyReason: atomizationApplyAvailable
          ? baseAtomizationViewModel.emptyReason
          : atomizationUnavailableReason,
      }
    : null;
  const atomizationMutationBusy = Boolean(
    atomizationMutation ||
      classificationMutation ||
      semanticPublicationMutation ||
      busyAction,
  );
  const atomizationCanOpenDialog = Boolean(
    !structureArtifactReadOnly &&
    requirementEnrichmentReady &&
      atomizationScopeState?.capability.available &&
      atomizationScopeState.capability.operations.includes("APPLY") &&
      atomizationScopeState.capability.scopes.length > 0 &&
      !atomizationAttemptGate?.openScopeBlocked,
  );
  const atomizationCanUndo = Boolean(
    !structureArtifactReadOnly &&
    requirementEnrichmentReady &&
      atomizationScopeState?.capability.available &&
      atomizationScopeState.capability.operations.includes("UNDO") &&
      atomizationScopeState.can_undo &&
      !atomizationAttemptGate?.undoBlocked,
  );
  const activeAtomizationTargetCount =
    atomizationScopeState?.current_receipt?.cumulative_review_node_ids.length ||
    0;
  const atomizationServerStateLabel = atomizationAttemptGate?.serverRunning
    ? atomizationScopeState?.last_attempt?.operation === "UNDO"
      ? "正在撤销原子化 · 每 2.5 秒自动更新"
      : "正在执行原子化 · 每 2.5 秒自动更新"
    : atomizationAttemptGate?.retryingApply
      ? "上次执行中断 · 可重试当前范围"
      : atomizationAttemptGate?.applyBlocked
        ? "上次执行不可重试 · 请调整范围"
        : atomizationAttemptGate?.undoBlocked
          ? "最近一次撤销不可重试"
          : atomizationScopeState?.last_attempt?.status === "INTERRUPTED"
            ? "上次操作中断 · 当前范围已调整"
            : atomizationScopeState?.can_undo
              ? `已应用 ${activeAtomizationTargetCount} 项 · 可撤销最近一次操作`
              : atomizationScopeState
                ? "尚未执行 · 当前保留原条目"
                : requirementEnrichmentLoading
                  ? "正在读取可执行范围"
                  : "范围状态不可用";
  const classificationBatchWorkflowIds = useMemo(
    () =>
      tree && entryClassification
        ? classificationWorkflowIdsForScope(tree, entryClassification, {
            scope: classificationBatchScope,
            selectedNodeId: selectedId,
            visibleNodeIds: projectedNodeIds,
          })
        : [],
    [
      classificationBatchScope,
      entryClassification,
      projectedNodeIds,
      selectedId,
      tree,
    ],
  );
  const classificationBatchScopeCounts = useMemo(() => {
    if (!tree || !entryClassification) {
      return { BRANCH: 0, FILTERED: 0, ALL: 0 };
    }
    const count = (scope: EntryClassificationBatchScope) =>
      classificationWorkflowIdsForScope(tree, entryClassification, {
        scope,
        selectedNodeId: selectedId,
        visibleNodeIds: projectedNodeIds,
      }).length;
    return {
      BRANCH: count("BRANCH"),
      FILTERED: count("FILTERED"),
      ALL: count("ALL"),
    };
  }, [entryClassification, projectedNodeIds, selectedId, tree]);
  const contextNode = parentClause(selectedNode, tree);
  const selectedItem = isRequirementTreeReviewItem(selectedNode)
    ? selectedNode
    : null;
  const selectedRequirement =
    isPublishedRequirementNode(selectedNode) ? selectedNode : null;
  const selectedTableContextNode = useMemo(
    () => nearestRequirementTreeTableNode(selectedNode, tree),
    [selectedNode, tree],
  );
  const selectedTableRecordFocus = useMemo(
    () => tableRecordFocus(selectedTableContextNode, tree),
    [selectedTableContextNode, tree],
  );
  const selectedTableProjection = useMemo(
    () => tableProjectionForNode(tree, selectedTableContextNode),
    [selectedTableContextNode, tree],
  );
  const treeSourceIdentity = asRecord(tree?.source_identity);
  const contentSourceSha256 = String(
    treeSourceIdentity.source_sha256 || pageSnapshot.sourceSha256 || "",
  );
  const treeGeometryIndexId = String(
    treeSourceIdentity.geometry_index_id || "",
  );
  const treeGeometryIndexSha256 = String(
    treeSourceIdentity.geometry_index_sha256 || "",
  );
  const treeStructureSha256 = String(
    tree?.structure_identity.effective_tree_sha256 || "",
  );
  const requirementTreeProjectionRuntimeVersion = String(
    asRecord(workbench.runtime).requirement_tree_projection_runtime_version ||
      "",
  );
  const geometryFocusRequestKey = JSON.stringify([
    route.projectId,
    route.documentId,
    selectedNode?.id || "",
    treeStructureSha256,
    requirementTreeProjectionRuntimeVersion,
    treeGeometryIndexId,
    treeGeometryIndexSha256,
  ]);
  const currentGeometryFocusState =
    geometryFocusState?.requestKey === geometryFocusRequestKey
      ? geometryFocusState
      : null;
  const geometryFocusRegions = geometryFocusRegionsForNode(
    currentGeometryFocusState?.payload || null,
    selectedNode?.id || "",
    {
      geometryIndexId: treeGeometryIndexId,
      geometryIndexSha256: treeGeometryIndexSha256,
    },
  );
  const geometryFocusError = currentGeometryFocusState?.error || "";
  const geometryFocusLoading = currentGeometryFocusState?.status === "LOADING";

  useEffect(() => {
    if (
      activeCheckpoint !== "structure" ||
      !selectedNode ||
      !route.projectId ||
      !route.documentId ||
      !geometryIndexReady
    ) {
      setGeometryFocusState(null);
      return;
    }
    if (!treeGeometryIndexId || !treeGeometryIndexSha256) {
      setGeometryFocusState({
        requestKey: geometryFocusRequestKey,
        status: "ERROR",
        payload: { node_id: selectedNode.id, regions: [] },
        error: "当前需求树没有记录分页几何身份，请重新提取后再使用精确框选。",
      });
      return;
    }
    let active = true;
    const requestKey = geometryFocusRequestKey;
    const emptyPayload = {
      node_id: selectedNode.id,
      geometry_identity: {
        geometry_index_id: treeGeometryIndexId,
        geometry_index_sha256: treeGeometryIndexSha256,
      },
      regions: [],
    };
    setGeometryFocusState({
      requestKey,
      status: "LOADING",
      payload: emptyPayload,
      error: "",
    });
    void api
      .geometryFocus(route.projectId, route.documentId, selectedNode.id, "")
      .then((payload) => {
        if (!active) return;
        if (
          geometryFocusRegionsForNode(payload, selectedNode.id, {
            geometryIndexId: treeGeometryIndexId,
            geometryIndexSha256: treeGeometryIndexSha256,
          }) === null
        ) {
          throw new Error("来源定位结果与当前所选节点不一致，请重试。");
        }
        setGeometryFocusState({
          requestKey,
          status: "READY",
          payload,
          error: "",
        });
        const verifiedPages = asArray<unknown>(payload.page_numbers)
          .map(Number)
          .filter((page) => Number.isInteger(page) && page > 0)
          .sort((left, right) => left - right);
        if (verifiedPages.length) {
          setPdfPage((currentPage) =>
            verifiedPages.includes(currentPage)
              ? currentPage
              : Math.min(parsingPageCount, verifiedPages[0]),
          );
        }
      })
      .catch((cause) => {
        if (!active) return;
        setGeometryFocusState({
          requestKey,
          status: "ERROR",
          payload: emptyPayload,
          error:
            cause instanceof Error
              ? cause.message
              : "当前节点的精确来源范围无法验证。",
        });
      });
    return () => {
      active = false;
    };
  }, [
    activeCheckpoint,
    geometryIndexReady,
    geometryFocusRequestKey,
    parsingPageCount,
    route.documentId,
    route.projectId,
    selectedNode?.id,
    treeGeometryIndexId,
    treeGeometryIndexSha256,
    treeStructureSha256,
  ]);

  const rawStructureNodes = useMemo(
    () => asArray<JsonRecord>(asRecord(workbench.structure).nodes),
    [structure],
  );
  const structureNodeById = useMemo(
    () =>
      new Map(
        rawStructureNodes
          .map((node) => [String(node.id || ""), node] as const)
          .filter(([nodeId]) => Boolean(nodeId)),
      ),
    [rawStructureNodes],
  );
  const selectedStructureIds = useMemo(
    () => requirementTreeEditableStructureNodeIds(selectedNode || contextNode),
    [contextNode, selectedNode],
  );
  const selectedStructureId = selectedStructureIds[0] || "";
  const selectedStructureNodes = selectedStructureIds
    .map((nodeId) => structureNodeById.get(nodeId))
    .filter((node): node is JsonRecord => Boolean(node));
  const selectedStructureNode =
    structureNodeById.get(selectedStructureId) || null;
  const workbenchCapabilities = asRecord(workbench.capabilities);
  const canEditStructure =
    !structureArtifactReadOnly &&
    activeCheckpoint === "structure" &&
    workbenchCapabilities.can_edit_structure === true &&
    selectedStructureIds.length > 0 &&
    selectedStructureNodes.length === selectedStructureIds.length;
  const localRulesReady = capabilityReady(
    runtimeCapabilities,
    CAPABILITY_IDS.pymupdf,
  );
  const codexBaselineReady = Boolean(
    !isSaasAccountMode() && localRulesReady &&
      processingProfile?.llm_provider === "codex" &&
      capabilityReady(runtimeCapabilities, CAPABILITY_IDS.codexAgent),
  );
  const structureAiReady =
    processingProfile?.llm_provider === "aliyun"
      ? capabilityReady(runtimeCapabilities, CAPABILITY_IDS.aliyunLlm)
      : processingProfile?.llm_provider === "azure_openai"
        ? capabilityReady(runtimeCapabilities, CAPABILITY_IDS.azureSol) &&
          capabilityReady(runtimeCapabilities, CAPABILITY_IDS.azureTerra)
        : false;
  const canOptimizeStructure =
    !structureArtifactReadOnly &&
    workbenchCapabilities.can_optimize_structure === true && structureAiReady;
  const canCalibrateStructureBoundaries = Boolean(
    !structureArtifactReadOnly &&
      workbenchCapabilities.can_calibrate_structure_boundaries === true,
  );
  const effectiveStructureSnapshotId = String(
    asRecord(workbench.structure).effective_snapshot_id || "",
  );

  useEffect(() => {
    if (
      organizeView !== "calibration" ||
      !route.projectId ||
      !route.documentId ||
      !sourcePagesReady ||
      !geometryIndexReady
    ) {
      boundaryCalibrationRequestRef.current += 1;
      setBoundaryCalibrationPlan(null);
      setBoundaryCalibrationDraft(null);
      setBoundaryCalibrationLoading(false);
      if (organizeView === "calibration" && !geometryIndexReady) {
        setBoundaryCalibrationError(
          "当前提取没有可验证的 PDF 行几何，版面校准保持只读。",
        );
      } else {
        setBoundaryCalibrationError("");
      }
      return;
    }
    let active = true;
    const requestId = ++boundaryCalibrationRequestRef.current;
    setBoundaryCalibrationLoading(true);
    setBoundaryCalibrationError("");
    setBoundaryCalibrationPreview(null);
    setBoundaryCalibrationDraft(null);
    void api
      .boundaryCalibrationPage(
        route.projectId,
        route.documentId,
        pdfPage,
        selectedStructureId,
      )
      .then((payload) => {
        if (!active || boundaryCalibrationRequestRef.current !== requestId) {
          return;
        }
        setBoundaryCalibrationPlan(payload);
        setBoundaryCalibrationError("");
      })
      .catch((cause) => {
        if (!active || boundaryCalibrationRequestRef.current !== requestId) {
          return;
        }
        setBoundaryCalibrationPlan(null);
        setBoundaryCalibrationError(
          cause instanceof Error
            ? cause.message
            : "当前页边界校准计划载入失败。",
        );
      })
      .finally(() => {
        if (
          active &&
          boundaryCalibrationRequestRef.current === requestId
        ) {
          setBoundaryCalibrationLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [
    effectiveStructureSnapshotId,
    geometryIndexReady,
    organizeView,
    pdfPage,
    route.documentId,
    route.projectId,
    selectedStructureId,
    sourcePagesReady,
  ]);

  const selectBoundaryCalibrationSegment = useCallback(
    async (nodeId: string) => {
      if (
        !nodeId ||
        organizeView !== "calibration" ||
        !route.projectId ||
        !route.documentId ||
        !sourcePagesReady ||
        !geometryIndexReady
      ) {
        return;
      }
      const requestId = ++boundaryCalibrationRequestRef.current;
      setBoundaryCalibrationLoading(true);
      setBoundaryCalibrationError("");
      setBoundaryCalibrationPreview(null);
      setBoundaryCalibrationDraft(null);
      try {
        const payload = await api.boundaryCalibrationPage(
          route.projectId,
          route.documentId,
          pdfPage,
          nodeId,
        );
        if (boundaryCalibrationRequestRef.current !== requestId) return;
        setBoundaryCalibrationPlan(payload);
      } catch (cause) {
        if (boundaryCalibrationRequestRef.current !== requestId) return;
        setBoundaryCalibrationError(
          cause instanceof Error
            ? cause.message
            : "所选条目的边界校准计划载入失败。",
        );
      } finally {
        if (boundaryCalibrationRequestRef.current === requestId) {
          setBoundaryCalibrationLoading(false);
        }
      }
    },
    [
      geometryIndexReady,
      organizeView,
      pdfPage,
      route.documentId,
      route.projectId,
      sourcePagesReady,
    ],
  );

  const structureChildrenByParent = useMemo(() => {
    const grouped = new Map<string, string[]>();
    rawStructureNodes.forEach((node) => {
      const parentId = String(node.parent_id || "");
      const nodeId = String(node.id || "");
      if (!nodeId) return;
      grouped.set(parentId, [...(grouped.get(parentId) || []), nodeId]);
    });
    grouped.forEach((nodeIds) =>
      nodeIds.sort((left, right) => {
        const leftNode = structureNodeById.get(left);
        const rightNode = structureNodeById.get(right);
        return (
          Number(leftNode?.order || 0) - Number(rightNode?.order || 0) ||
          left.localeCompare(right)
        );
      }),
    );
    return grouped;
  }, [rawStructureNodes, structureNodeById]);
  const selectedDescendantIds = useMemo(() => {
    const result = new Set<string>();
    const pending = selectedStructureIds.flatMap(
      (nodeId) => structureChildrenByParent.get(nodeId) || [],
    );
    while (pending.length) {
      const nodeId = pending.shift()!;
      if (result.has(nodeId)) continue;
      result.add(nodeId);
      pending.push(...(structureChildrenByParent.get(nodeId) || []));
    }
    return result;
  }, [selectedStructureIds, structureChildrenByParent]);
  const selectedStructureIdSet = useMemo(
    () => new Set(selectedStructureIds),
    [selectedStructureIds],
  );
  const structureParentCandidates = useMemo(
    () =>
      rawStructureNodes.filter((node) => {
        const nodeId = String(node.id || "");
        return (
          nodeId &&
          !selectedStructureIdSet.has(nodeId) &&
          !selectedDescendantIds.has(nodeId) &&
          node.allows_children !== false
        );
      }),
    [rawStructureNodes, selectedDescendantIds, selectedStructureIdSet],
  );
  const structureParentPathById = useMemo(() => {
    const paths = new Map<string, string>();
    const resolving = new Set<string>();
    const resolvePath = (nodeId: string): string => {
      const cached = paths.get(nodeId);
      if (cached) return cached;
      const node = structureNodeById.get(nodeId);
      if (!node) return "";
      const label = String(node.label || "未命名节点");
      if (resolving.has(nodeId)) return label;
      resolving.add(nodeId);
      const parentId = String(node.parent_id || "");
      const parentPath = parentId ? resolvePath(parentId) : "";
      resolving.delete(nodeId);
      const path = parentPath ? `${parentPath} › ${label}` : label;
      paths.set(nodeId, path);
      return path;
    };
    rawStructureNodes.forEach((node) => {
      const nodeId = String(node.id || "");
      if (nodeId) resolvePath(nodeId);
    });
    return paths;
  }, [rawStructureNodes, structureNodeById]);
  const structureParentSearchResult = useMemo(() => {
    const query = compactText(structureParentQuery, "").toLocaleLowerCase();
    const visible: JsonRecord[] = [];
    let total = 0;
    for (const node of structureParentCandidates) {
      const nodeId = String(node.id || "");
      const path = structureParentPathById.get(nodeId) || "";
      if (query && !path.toLocaleLowerCase().includes(query)) continue;
      total += 1;
      if (visible.length < 8) visible.push(node);
    }
    return { visible, total };
  }, [
    structureParentQuery,
    structureParentCandidates,
    structureParentPathById,
  ]);
  const selectedStructureTargetPath = structureTargetId
    ? structureParentPathById.get(structureTargetId) || ""
    : "";
  const chooseStructureParent = (node: JsonRecord) => {
    const nodeId = String(node.id || "");
    if (!nodeId) return;
    setStructureTargetId(nodeId);
    setStructureParentQuery(structureParentPathById.get(nodeId) || "");
    setStructureParentPickerOpen(false);
  };
  const handleStructureParentSearchKeyDown = (
    event: ReactKeyboardEvent<HTMLInputElement>,
  ) => {
    const visible = structureParentSearchResult.visible;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setStructureParentPickerOpen(true);
      if (visible.length < 1) return;
      setStructureParentActiveIndex((current) =>
        event.key === "ArrowDown"
          ? (current + 1) % visible.length
          : (current - 1 + visible.length) % visible.length,
      );
      return;
    }
    if (event.key === "Enter" && structureParentPickerOpen) {
      const target = visible[structureParentActiveIndex];
      if (!target) return;
      event.preventDefault();
      chooseStructureParent(target);
      return;
    }
    if (event.key === "Escape") {
      event.stopPropagation();
      setStructureParentPickerOpen(false);
    }
  };
  const selectedCurrentParentIds = Array.from(
    new Set(selectedStructureNodes.map((node) => String(node.parent_id || ""))),
  );
  const currentParentId =
    selectedCurrentParentIds.length === 1 ? selectedCurrentParentIds[0] : "";
  const selectedSiblingIds = structureChildrenByParent.get(currentParentId) || [];
  const mergeCandidates = selectedSiblingIds
    .filter((nodeId) => nodeId !== selectedStructureId)
    .map((nodeId) => structureNodeById.get(nodeId))
    .filter((node): node is JsonRecord => Boolean(node));
  const selectedSourceSpans = asArray<JsonRecord>(
    selectedStructureNode?.source_spans,
  );
  const boundarySpan =
    selectedSourceSpans.length === 1 ? selectedSourceSpans[0] : null;
  const boundaryStart = Number(boundarySpan?.start_offset ?? 0);
  const boundaryEnd = Number(boundarySpan?.end_offset ?? 0);
  const canBoundarySplit =
    selectedStructureIds.length === 1 &&
    Boolean(boundarySpan) &&
    Number.isInteger(boundaryStart) &&
    Number.isInteger(boundaryEnd) &&
    boundaryEnd - boundaryStart >= 2;
  const reparentDisabledReason = (() => {
    if (busyAction) return "正在处理上一项操作。";
    if (workbenchCapabilities.can_edit_structure !== true) {
      return "当前结构版本不可编辑。";
    }
    if (!selectedStructureIds.length) {
      return "当前展示节点缺少可编辑结构绑定，请重新提取后再调整。";
    }
    if (selectedStructureNodes.length !== selectedStructureIds.length) {
      return "当前结构版本已变化，请刷新后重试。";
    }
    if (!structureTargetId) return "请选择新的父级。";
    if (
      selectedStructureNodes.every(
        (structureNode) =>
          String(structureNode.parent_id || "") === structureTargetId,
      )
    ) {
      return "所选内容已经属于这个父级。";
    }
    return "";
  })();
  const selectedPreferredPage = preferredRequirementTreePage(selectedNode);
  useEffect(() => {
    if (activeCheckpoint !== "structure") return;
    if (selectedPreferredPage > 0) setPdfPage(selectedPreferredPage);
  }, [activeCheckpoint, selectedNode?.id, selectedPreferredPage]);

  useEffect(() => {
    setStructureTargetId("");
    setStructureParentQuery("");
    setStructureParentActiveIndex(0);
    setMergeTargetId("");
    if (selectedStructureIds.length > 1) setStructureAction("reparent");
    setSplitOffset(
      canBoundarySplit
        ? String(boundaryStart + Math.floor((boundaryEnd - boundaryStart) / 2))
        : "",
    );
  }, [
    boundaryEnd,
    boundaryStart,
    canBoundarySplit,
    selectedStructureIds.length,
    selectedStructureId,
  ]);

  useEffect(() => {
    if (!structureDrawerOpen) {
      setStructureParentPickerOpen(false);
      return;
    }
    setStructureParentQuery("");
    setStructureParentActiveIndex(0);
    setStructureParentPickerOpen(true);
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setStructureDrawerOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [structureDrawerOpen]);

  const selectedReviewNodeId = String(
    selectedRequirement?.workflow_node_id || "",
  );
  const selectedReviewNode =
    review?.nodes.find(
      (node) => node.review_node_id === selectedReviewNodeId,
    ) || null;
  const selectedReviewText =
    selectedReviewNode && review
      ? reviewNodeText(selectedReviewNode, review.modelByUnit)
      : selectedRequirement?.summary || "";

  const rowStart = Math.max(
    0,
    Math.floor(treeScrollTop / TREE_ROW_HEIGHT) - TREE_OVERSCAN,
  );
  const rowEnd = Math.min(
    navigationRows.length,
    Math.ceil((treeScrollTop + treeViewportHeight) / TREE_ROW_HEIGHT) +
      TREE_OVERSCAN,
  );
  const virtualRows = navigationRows.slice(rowStart, rowEnd);

  const selectNode = useCallback((node: RequirementTreeNode) => {
    setSelectedId(node.id);
    const nextPage = preferredRequirementTreePage(node);
    if (nextPage > 0) setPdfPage(nextPage);
    },
    [setSelectedId],
  );

  const handleTreeScroll = useCallback(
    (element: HTMLDivElement) => {
      treeScrollTopRef.current = element.scrollTop;
      if (treeScrollUpdateFrameRef.current !== null) return;
      treeScrollUpdateFrameRef.current = requestAnimationFrame(() => {
        treeScrollUpdateFrameRef.current = null;
        setTreeScrollTop(treeScrollTopRef.current);
      });
    },
    [setTreeScrollTop],
  );

  const toggleNode = (nodeId: string, expanded?: boolean) => {
    setExpandedIds((current) => {
      const next = new Set(current);
      const shouldExpand = expanded ?? !next.has(nodeId);
      if (shouldExpand) next.add(nodeId);
      else next.delete(nodeId);
      return next;
    });
  };

  const closeTreeExpandMenu = () => {
    if (treeExpandMenuRef.current) treeExpandMenuRef.current.open = false;
  };

  const expandCurrentBranch = () => {
    if (!tree || !selectedId) return;
    const next = new Set(expandedIds);
    subtreeExpandableRequirementTreeIds(
      tree,
      selectedId,
      query,
      effectiveTreeFilter,
    ).forEach((nodeId) => next.add(nodeId));
    setExpandedIds(next);
    closeTreeExpandMenu();
  };

  const collapseCurrentBranch = () => {
    if (!tree || !selectedId) return;
    const next = new Set(expandedIds);
    subtreeExpandableRequirementTreeIds(
      tree,
      selectedId,
      query,
      effectiveTreeFilter,
    ).forEach((nodeId) => next.delete(nodeId));
    setExpandedIds(next);
  };

  const expandToClauses = () => {
    setExpandedIds(clauseLevelExpandedIds(tree, query, effectiveTreeFilter));
    closeTreeExpandMenu();
  };

  const expandAll = () => {
    setExpandedIds(
      expandableRequirementTreeIds(tree, query, effectiveTreeFilter),
    );
    closeTreeExpandMenu();
  };

  const collapseAll = () => {
    setExpandedIds(new Set());
    closeTreeExpandMenu();
  };

  const collapseToCurrentPath = () => {
    setExpandedIds(requirementTreeAncestorIds(tree, selectedId));
    closeTreeExpandMenu();
  };

  const revealTreeNode = (
    nodeId: string,
    options: {
      query?: string;
      filter?: RequirementTreeFilter;
      focus?: boolean;
    } = {},
  ) => {
    if (!tree || !nodeId) return;
    const visibleQuery = options.query ?? query;
    const visibleFilter = options.filter ?? effectiveTreeFilter;
    const next = new Set(expandedIds);
    requirementTreeAncestorIds(tree, nodeId).forEach((ancestorId) =>
      next.add(ancestorId),
    );
    setExpandedIds(next);
    const nextRows = flattenRequirementTree(
      tree,
      next,
      visibleQuery,
      visibleFilter,
    );
    const index = nextRows.findIndex((row) => row.node.id === nodeId);
    if (index < 0) return;
    const nextScrollTop = Math.max(
      0,
      index * TREE_ROW_HEIGHT - TREE_ROW_HEIGHT * 2,
    );
    treeScrollTopRef.current = nextScrollTop;
    setTreeScrollTop(nextScrollTop);
    requestAnimationFrame(() => {
      treeViewportRef.current?.scrollTo({ top: nextScrollTop });
      if (options.focus === false) return;
      treeViewportRef.current
        ?.querySelector<HTMLElement>(`[data-tree-node="${CSS.escape(nodeId)}"]`)
        ?.focus();
    });
  };

  const revealCurrentNode = () => {
    revealTreeNode(selectedId);
  };

  const focusVisibleRow = (index: number) => {
    const safeIndex = Math.max(0, Math.min(navigationRows.length - 1, index));
    const row = navigationRows[safeIndex];
    if (!row) return;
    selectNode(row.node);
    treeViewportRef.current?.scrollTo({
      top: Math.max(0, safeIndex * TREE_ROW_HEIGHT - TREE_ROW_HEIGHT * 2),
    });
    requestAnimationFrame(() => {
      treeViewportRef.current
        ?.querySelector<HTMLElement>(
          `[data-tree-node="${CSS.escape(row.node.id)}"]`,
        )
        ?.focus();
    });
  };

  const handleTreeKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
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
      navigationRows.findIndex((row) => row.node.id === selectedId),
    );
    const row = navigationRows[index];
    if (event.key === "Home") return focusVisibleRow(0);
    if (event.key === "End") return focusVisibleRow(navigationRows.length - 1);
    if (event.key === "ArrowDown") return focusVisibleRow(index + 1);
    if (event.key === "ArrowUp") return focusVisibleRow(index - 1);
    if (event.shiftKey && event.key === "ArrowRight") {
      expandCurrentBranch();
      return;
    }
    if (event.shiftKey && event.key === "ArrowLeft") {
      collapseCurrentBranch();
      return;
    }
    if (event.key === "ArrowRight" && row?.hasChildren) {
      if (!row.expanded) toggleNode(row.node.id, true);
      else focusVisibleRow(index + 1);
      return;
    }
    if (event.key === "ArrowLeft") {
      if (row?.expanded && row.hasChildren) {
        toggleNode(row.node.id, false);
        return;
      }
      if (row?.node.parent_id) {
        const parentIndex = navigationRows.findIndex(
          (candidate) => candidate.node.id === row.node.parent_id,
        );
        if (parentIndex >= 0) focusVisibleRow(parentIndex);
      }
    }
  };

  const boundaryCalibrationOverlays = useMemo(() => {
    if (
      organizeView !== "calibration" ||
      !boundaryCalibrationPlan
    ) {
      return [];
    }
    const planPage = asRecord(boundaryCalibrationPlan.page);
    if (Number(planPage.number || 0) !== pdfPage) return [];
    const width = Number(planPage.width || 0);
    const height = Number(planPage.height || 0);
    if (!(width > 0 && height > 0)) return [];
    return asArray<JsonRecord>(boundaryCalibrationPlan.boundaries)
      .map((boundary, index) => {
        const boundaryId = String(
          boundary.boundary_id || `boundary-overlay-${index + 1}`,
        );
        const draft =
          boundaryCalibrationDraft?.boundaryId === boundaryId
            ? boundaryCalibrationDraft
            : null;
        const currentOffset = Number(boundary.current_offset);
        const anchor = draft
          ? { bbox: draft.bbox }
          : asArray<JsonRecord>(boundary.anchors).find(
              (candidate) => Number(candidate.offset) === currentOffset,
            );
        const bbox = asArray<unknown>(anchor?.bbox).map(Number);
        if (
          bbox.length !== 4 ||
          !bbox.every(Number.isFinite) ||
          bbox[2] <= bbox[0] ||
          bbox[3] <= bbox[1]
        ) {
          return null;
        }
        return {
          id: boundaryId,
          type: draft
            ? "BOUNDARY-DRAFT"
            : boundary.editable === true
              ? "BOUNDARY"
              : "BOUNDARY-LOCKED",
          label: draft
            ? `${draft.label} · 草稿`
            : "",
          bbox,
          sourceBounds: [0, 0, width, height],
          selected: Boolean(draft),
          focus: Boolean(draft),
          precision: "EXACT_LINE",
        };
      })
      .filter((item): item is NonNullable<typeof item> => item !== null);
  }, [
    boundaryCalibrationDraft,
    boundaryCalibrationPlan,
    organizeView,
    pdfPage,
  ]);

  const selectedOverlays = useMemo(() => {
    if (showSourceEvidenceLens) {
      return parsingDiagnosisOverlays({
        pageSnapshot,
        focusedFactId: diagnosticFocusId,
        riskOnly: diagnosticRiskOnly,
        showSupportLayer: diagnosticShowSupportLayer,
        contentView: diagnosticContentView,
        visualRecoveryFailed: diagnosticVisualRecoveryFailed,
      });
    }
    const focusOverlays = requirementTreeFocusOverlays({
      selectedNode,
      contextNode,
      pageSnapshot,
      pdfPage,
      focusRegions: geometryFocusRegions,
    });
    return organizeView === "calibration"
      ? boundaryCalibrationOverlays
      : focusOverlays;
  }, [
    showSourceEvidenceLens,
    boundaryCalibrationOverlays,
    contextNode,
    diagnosticFocusId,
    diagnosticRiskOnly,
    diagnosticShowSupportLayer,
    diagnosticContentView,
    diagnosticVisualRecoveryFailed,
    geometryFocusRegions,
    organizeView,
    pageSnapshot,
    pdfPage,
    selectedNode,
  ]);

  const syncPdf = useCallback(() => {
    pdfFrameRef.current?.contentWindow?.postMessage(
      {
        type: "REQPILOTX_PDF_NAVIGATE",
        page: pdfPage,
        overlays: selectedOverlays,
      },
      window.location.origin,
    );
  }, [pdfPage, selectedOverlays]);

  useEffect(() => {
    syncPdf();
  }, [syncPdf]);

  useEffect(() => {
    const listener = (event: MessageEvent) => {
      if (
        event.origin !== window.location.origin ||
        event.source !== pdfFrameRef.current?.contentWindow
      ) {
        return;
      }
      const message = asRecord(event.data);
      if (message.type === "pdf-viewer-ready") syncPdf();
      if (message.type === "pdf-page-changed") {
        const next = Number(message.page || pdfPage);
        if (Number.isFinite(next) && next > 0) {
          setPdfPage(Math.min(parsingPageCount, Math.trunc(next)));
        }
      }
    };
    window.addEventListener("message", listener);
    return () => window.removeEventListener("message", listener);
  }, [parsingPageCount, pdfPage, setPdfPage, syncPdf]);

  const startExtraction = async () => {
    if (sourceGraphBusy || !agentAvailability.ready || isSaasAccountMode() && nativeStructureAdvancement.artifactId) return;
    const result = await runAction(
      "Agent 提取原文",
      () => startSourceDocumentGraph(route.projectId, route.documentId,
        String(selectedDocument?.source_sha256 || ""), nativeStructureAdvancement.artifactId || ""),
      {
        refresh: "document",
        successNotice: "需求提取已提交，正在生成",
      },
    );
    if (result) setSourceGraphJobs(result.jobs || [result]);
  };

  const regenerateWithLocalRules = async () => {
    const started = await runAction(
      "本地规则重新生成",
      () =>
        api.startExtraction(
          route.projectId,
          route.documentId,
          LOCAL_STRUCTURE_RUN_OVERRIDE,
        ),
      {
        refresh: "document",
        successNotice: "本地规则重新生成已提交，正在生成",
      },
    );
    if (started) setReorganizeDialogOpen(false);
  };

  const rerunParsingWithCodexAgent = async () => {
    if (isSaasAccountMode()) return;
    const started = await runAction(
      "Codex Agent 原文基线重新生成",
      () => api.startExtraction(route.projectId, route.documentId),
      {
        refresh: "document",
        successNotice: "Codex Agent 原文基线任务已提交，正在生成",
      },
    );
    if (started) setParsingRerunDialogOpen(false);
  };

  const restoreSourceAndExtract = async () => {
    if (!visualRestorationOverride) return;
    const started = await runAction(
      `${visualRestorationMethodLabel}并重新提取`,
      () =>
        api.startExtraction(
          route.projectId,
          route.documentId,
          visualRestorationOverride,
        ),
      {
        refresh: "document",
        successNotice: `${visualRestorationMethodLabel}任务已提交，正在生成全文结果`,
      },
    );
    if (started) setParsingRerunDialogOpen(false);
  };

  const retryFailedVisualPages = async () => {
    if (!visualRepairAvailable) return;
    const failedPageCount = visualRepairFailedPages.length;
    await runAction(
      `只重试降级的 ${failedPageCount} 页`,
      () =>
        api.repairExtractionVisualPages(route.projectId, route.documentId, {
          schema_version: "document-extraction-visual-repair-request/v1",
          expected_base_job_id: visualRepairBaseJobId,
          expected_base_job_generation: visualRepairBaseGeneration,
          expected_base_artifact_sha256: visualRepairBaseArtifactSha256,
        }),
      {
        refresh: "document",
        successNotice: `Qwen OCR 结构恢复降级页重试已提交；其余 ${
          visualReceiptSummary?.readyPageCount || 0
        } 页将复用已验证结果`,
      },
    );
  };

  const cancelCurrentExtraction = async () => {
    const jobId = String(extractionJob.job_id || "");
    if (!jobId || !extractionCanCancel) return;
    await runAction(
      "取消本次解析",
      () => api.cancelExtraction(route.projectId, route.documentId, jobId),
      {
        refresh: "document",
        successNotice: "已提交取消请求；已发布结果和原文件不会删除",
        failureNotice: "取消请求未完成，请刷新任务状态后重试",
      },
    );
  };

  const navigateToCheckpoint = (checkpoint: ExtractionCheckpoint) =>
    updateRoute({
      task: "extraction",
      mode: checkpoint === "review" ? "review" : "organize",
      checkpoint,
      requirementSetId: checkpoint === "review" ? selectedSetId : "",
    });

  const closeSourceEvidenceLens = () => {
    setSourceEvidenceOpen(false);
    if (rawRequestedCheckpoint === "diagnose") {
      navigateToCheckpoint("structure");
    }
  };

  const currentChecklistButton =
    reviewAvailable && selectedSetId ? (
      <button
        type="button"
        className="button ghost compact"
        onClick={() => navigateToCheckpoint("review")}
      >
        查看当前确认清单（{selectedSetVersionLabel}
        {selectedSetRequirementCount > 0
          ? `，${selectedSetRequirementCount} 条`
          : ""}
        ）
      </button>
    ) : null;
  const previousStructureButton = previousStructureBrowseAllowed ? (
    <button
      type="button"
      className="button ghost compact"
      onClick={() => navigateToCheckpoint("structure")}
    >
      查看上一版结构（只读）
    </button>
  ) : null;
  const activeExtractionActionRow =
    extractionCanCancel || currentChecklistButton || previousStructureButton ? (
      <span className="extraction-run-actions">
        {extractionCanCancel && (
          <button
            type="button"
            className="button secondary compact"
            disabled={Boolean(busyAction)}
            onClick={() => void cancelCurrentExtraction()}
          >
            取消本次解析
          </button>
        )}
        {previousStructureButton}
        {currentChecklistButton}
      </span>
    ) : null;

  const acknowledgeParsingRisksAndContinue = async () => {
    if (
      parsingDiagnostics.status !== "READY_WITH_RISKS" ||
      parsingDiagnostics.hardBlockerCount > 0 ||
      parsingDiagnostics.severeWarningCount < 1
    ) {
      return;
    }
    const expectedDiagnostics = parsingDiagnostics;
    const expectedRequestKey = parsingDiagnosticsRequestKey;
    const acknowledged = await runAction(
      `知晓 ${expectedDiagnostics.severeWarningCount} 项解析风险`,
      () =>
        api.acknowledgeParsingRisks(route.projectId, route.documentId, {
          expected_source_sha256: expectedDiagnostics.sourceSha256,
          expected_parse_artifact_id:
            expectedDiagnostics.activeParseArtifactId,
          expected_parse_identity_sha256:
            expectedDiagnostics.parseIdentitySha256,
        }),
      { refresh: "none" },
    );
    if (!acknowledged) return;
    const normalized = normalizeParsingDiagnostics(acknowledged);
    if (
      parsingDiagnosticsRequestKeyRef.current !== expectedRequestKey ||
      !normalized.acknowledged ||
      normalized.sourceSha256 !== expectedDiagnostics.sourceSha256 ||
      normalized.activeParseArtifactId !==
        expectedDiagnostics.activeParseArtifactId ||
      normalized.parseIdentitySha256 !==
        expectedDiagnostics.parseIdentitySha256
    ) {
      setNotice("风险知晓回执未绑定当前解析结果，请刷新后重试。");
      return;
    }
    setParsingDiagnosticsState({
      requestKey: expectedRequestKey,
      status: "READY",
      payload: acknowledged,
      error: "",
    });
    setParsingRiskDialogOpen(false);
    navigateToCheckpoint("structure");
  };

  const requestCheckpoint = (checkpoint: ExtractionCheckpoint) => {
    if (checkpoint === "diagnose") {
      setSourceEvidenceOpen(true);
      return;
    }
    if (checkpoint === "review") {
      navigateToCheckpoint(checkpoint);
      return;
    }
    if (checkpoint === "structure") {
      setSourceEvidenceOpen(false);
      navigateToCheckpoint(checkpoint);
      return;
    }
    navigateToCheckpoint(checkpoint);
  };

  const saveReviewNodes = async (
    label: string,
    nodes: ReviewNode[],
    focusId: string,
    prior?: {
      nodes: ReviewNode[];
      confirmedIds: string[];
      revision: number;
      label: string;
    },
  ) => {
    if (!review || structureArtifactReadOnly) return;
    setReviewSaveState("保存中");
    const knownIds = new Set(nodes.map((node) => node.review_node_id));
    const response = await runAction(
      label,
      () =>
        api.saveReviewSession(
          route.projectId,
          route.documentId,
          review.sessionId,
          {
            expected_revision: review.revision,
            status: "DRAFT",
            reviewer_label: review.reviewerLabel,
            confirmed_review_node_ids: review.confirmedIds.filter((id) =>
              knownIds.has(id),
            ),
            nodes,
          },
        ),
      { refresh: "none" },
    );
    if (!response) {
      setReviewSaveState("保存失败");
      return;
    }
    const next = reviewSessionView(response);
    if (!next) {
      setReviewSaveState("保存失败");
      return;
    }
    setReview(next);
    setReviewUndo(prior || null);
    setPendingWorkflowFocusId(focusId);
    setReviewSaveState("已保存");
    await refreshStructure();
  };

  const splitSelected = async () => {
    if (!review || !selectedReviewNode || !correctionTextRef.current) return;
    const field = correctionTextRef.current;
    try {
      const result = splitReviewNode(
        review.nodes,
        selectedReviewNode.review_node_id,
        codePointOffset(selectedReviewText, field.selectionStart),
        codePointOffset(selectedReviewText, field.selectionEnd),
        review.revision,
      );
      await saveReviewNodes(
        "拆分需求",
        result.nodes,
        result.focusId,
        {
          nodes: review.nodes,
          confirmedIds: review.confirmedIds,
          revision: review.revision,
          label: "拆分需求",
      });
    } catch (cause) {
      setReviewSaveState(
        cause instanceof Error ? cause.message : String(cause),
      );
    }
  };

  const mergeSelected = async () => {
    if (!review || !selectedReviewNode) return;
    try {
      const nodes = mergeReviewNode(
        review.nodes,
        selectedReviewNode.review_node_id,
      );
      await saveReviewNodes(
        "合并相邻需求",
        nodes,
        selectedReviewNode.review_node_id,
        {
          nodes: review.nodes,
          confirmedIds: review.confirmedIds,
          revision: review.revision,
          label: "合并相邻需求",
        },
      );
    } catch (cause) {
      setReviewSaveState(
        cause instanceof Error ? cause.message : String(cause),
      );
    }
  };

  const excludeSelected = async () => {
    if (!review || !selectedReviewNode) return;
    try {
      const nodes = excludeReviewNode(
        review.nodes,
        selectedReviewNode.review_node_id,
      );
      const nextFocus =
        nodes.find((node) => node.include_in_view)?.review_node_id || "";
      await saveReviewNodes(
        "移出需求",
        nodes,
        nextFocus,
        {
          nodes: review.nodes,
          confirmedIds: review.confirmedIds,
          revision: review.revision,
          label: "移出需求",
      });
    } catch (cause) {
      setReviewSaveState(
        cause instanceof Error ? cause.message : String(cause),
      );
    }
  };

  const undoReview = async () => {
    if (
      !review ||
      !reviewUndo ||
      reviewUndo.revision + 1 !== review.revision
    ) {
      setReviewSaveState("只能撤销刚刚保存的一次修正");
      return;
    }
    await saveReviewNodes(
      `撤销${reviewUndo.label}`,
      reviewUndo.nodes,
      reviewUndo.nodes[0]?.review_node_id || "",
    );
    setReviewUndo(null);
  };

  const classificationCommandId = (key: string) => {
    if (pendingClassificationCommandRef.current?.key === key) {
      return pendingClassificationCommandRef.current.commandId;
    }
    const commandId = createEntryClassificationCommandId();
    pendingClassificationCommandRef.current = { key, commandId };
    return commandId;
  };

  const beginSemanticPublicationRequest = (
    classification: EntryClassificationResponse,
  ) => {
    if (
      structureArtifactReadOnly ||
      semanticPublicationContextKeyRef.current !== semanticPublicationContextKey
    ) {
      return null;
    }
    semanticPublicationRequestRef.current?.controller.abort();
    const identityKey = semanticPublicationIdentityFor(classification);
    const requestState: SemanticPublicationRequestState = {
      generation: semanticPublicationGenerationRef.current + 1,
      contextKey: semanticPublicationContextKey,
      identityKey,
      controller: new AbortController(),
    };
    semanticPublicationGenerationRef.current = requestState.generation;
    semanticPublicationIdentityKeyRef.current = identityKey;
    semanticPublicationRequestRef.current = requestState;
    return requestState;
  };

  const semanticPublicationRequestIsCurrent = (
    requestState: SemanticPublicationRequestState,
  ) =>
    !requestState.controller.signal.aborted &&
    semanticPublicationContextKeyRef.current === requestState.contextKey &&
    semanticPublicationIdentityKeyRef.current === requestState.identityKey &&
    semanticPublicationRequestRef.current?.generation ===
      requestState.generation;

  const advanceSemanticPublicationIdentity = (
    requestState: SemanticPublicationRequestState,
    classification: EntryClassificationResponse,
  ) => {
    if (!semanticPublicationRequestIsCurrent(requestState)) return false;
    const identityKey = semanticPublicationIdentityFor(classification);
    requestState.identityKey = identityKey;
    semanticPublicationIdentityKeyRef.current = identityKey;
    return true;
  };

  const storeClassificationResponse = (
    payload: EntryClassificationResponse,
  ) => {
    if (!tree) throw new Error("当前需求树不可用");
    const verified = assertEntryClassificationMatchesTree(payload, tree);
    setEntryClassificationState({
      requestKey: classificationRequestKey,
      status: "READY",
      payload: verified,
      error: "",
    });
    setPendingLocalDraftActivation(null);
    setEnrichmentReloadToken((value) => value + 1);
    return verified;
  };

  const beginClassificationMutationRequest = () => {
    classificationMutationRequestRef.current?.controller.abort();
    const requestState: ClassificationMutationRequestState = {
      generation: classificationMutationGenerationRef.current + 1,
      requestKey: classificationRequestKey,
      controller: new AbortController(),
    };
    classificationMutationGenerationRef.current = requestState.generation;
    classificationMutationRequestRef.current = requestState;
    return requestState;
  };

  const classificationMutationRequestIsCurrent = (
    requestState: ClassificationMutationRequestState,
  ) =>
    !requestState.controller.signal.aborted &&
    classificationRequestKeyRef.current === requestState.requestKey &&
    classificationMutationRequestRef.current?.generation ===
      requestState.generation;

  const commitClassificationOverrides = async (
    overrides: Record<string, EntryClassificationType | null>,
  ) => {
    if (
      structureArtifactReadOnly ||
      atomizationMutation ||
      !entryClassification ||
      entryClassification.snapshot.status !== "WORKING" ||
      !Object.keys(overrides).length
    ) {
      return null;
    }
    const commandKey = JSON.stringify([
      "PATCH",
      entryClassification.snapshot.snapshot_id,
      Object.entries(overrides).sort(([left], [right]) =>
        left.localeCompare(right),
      ),
    ]);
    const payload: EntryClassificationPatchCommand = {
      ...entryClassificationCommandPayload(
        entryClassification.snapshot,
        classificationCommandId(commandKey),
      ),
      overrides,
    };
    const mutationRequest = beginClassificationMutationRequest();
    setClassificationMutation("PATCH");
    setClassificationMutationError("");
    try {
      const response = await api.updateEntryClassifications(
        route.projectId,
        route.documentId,
        payload,
      );
      if (!classificationMutationRequestIsCurrent(mutationRequest)) return null;
      const verified = storeClassificationResponse(response);
      pendingClassificationCommandRef.current = null;
      return verified;
    } catch (cause) {
      if (!classificationMutationRequestIsCurrent(mutationRequest)) return null;
      setClassificationMutationError(
        cause instanceof Error ? cause.message : "清单分类保存失败。",
      );
      return null;
    } finally {
      if (
        classificationMutationRequestRef.current?.generation ===
        mutationRequest.generation
      ) {
        classificationMutationRequestRef.current = null;
        setClassificationMutation("");
      }
    }
  };

  const applyClassificationBatch = async () => {
    const overrides = Object.fromEntries(
      classificationBatchWorkflowIds.map((workflowNodeId) => [
        workflowNodeId,
        classificationBatchType,
      ]),
    );
    const saved = await commitClassificationOverrides(overrides);
    if (saved) setClassificationBatchOpen(false);
  };

  const acceptCurrentClassification = async (
    publicationRequest: SemanticPublicationRequestState | null = null,
  ) => {
    if (!entryClassification || structureArtifactReadOnly) return null;
    if (entryClassification.snapshot.status === "ACCEPTED") {
      return entryClassification;
    }
    if (!entryClassification.can_accept) {
      setClassificationMutationError("仍有未分类条目，不能接受当前清单。");
      return null;
    }
    const commandKey = JSON.stringify([
      "ACCEPT",
      entryClassification.snapshot.snapshot_id,
      entryClassification.snapshot.payload_sha256,
    ]);
    const payload = entryClassificationCommandPayload(
      entryClassification.snapshot,
      classificationCommandId(commandKey),
    );
    setClassificationMutation("ACCEPT");
    setClassificationMutationError("");
    try {
      const response = await api.acceptEntryClassifications(
        route.projectId,
        route.documentId,
        payload,
        publicationRequest?.controller.signal,
      );
      if (
        publicationRequest &&
        !semanticPublicationRequestIsCurrent(publicationRequest)
      ) {
        return null;
      }
      const verified = storeClassificationResponse(response);
      if (!verified.can_publish || verified.snapshot.status !== "ACCEPTED") {
        throw new Error("接受后的分类结果不完整，请重新载入当前清单。");
      }
      if (
        publicationRequest &&
        !advanceSemanticPublicationIdentity(publicationRequest, verified)
      ) {
        return null;
      }
      pendingClassificationCommandRef.current = null;
      return verified;
    } catch (cause) {
      if (
        !publicationRequest ||
        (semanticPublicationRequestIsCurrent(publicationRequest) &&
          !(cause instanceof DOMException && cause.name === "AbortError"))
      ) {
        setClassificationMutationError(
          cause instanceof Error ? cause.message : "清单分类接受失败。",
        );
      }
      return null;
    } finally {
      if (
        !publicationRequest ||
        semanticPublicationRequestRef.current?.generation ===
          publicationRequest.generation
      ) {
        setClassificationMutation("");
      }
    }
  };

  const atomizationCommandId = (key: string) => {
    if (pendingAtomizationCommandRef.current?.key === key) {
      return pendingAtomizationCommandRef.current.commandId;
    }
    const commandId = createEntryClassificationCommandId();
    pendingAtomizationCommandRef.current = { key, commandId };
    return commandId;
  };

  const beginAtomizationMutationRequest = () => {
    atomizationMutationRequestRef.current?.controller.abort();
    const requestState = {
      generation: atomizationMutationGenerationRef.current + 1,
      requestKey: enrichmentRequestKey,
      controller: new AbortController(),
    };
    atomizationMutationGenerationRef.current = requestState.generation;
    atomizationMutationRequestRef.current = requestState;
    return requestState;
  };

  const atomizationMutationRequestIsCurrent = (requestState: {
    generation: number;
    requestKey: string;
    controller: AbortController;
  }) =>
    !requestState.controller.signal.aborted &&
    enrichmentRequestKeyRef.current === requestState.requestKey &&
    atomizationMutationRequestRef.current?.generation ===
      requestState.generation;

  const reloadRequirementEnrichment = (clearMutationError = true) => {
    if (clearMutationError) setAtomizationMutationError("");
    setEnrichmentReloadToken((value) => value + 1);
  };

  const toggleSelectedAtomizationWorkflow = (workflowNodeId: string) => {
    if (
      structureArtifactReadOnly ||
      !workflowNodeId ||
      atomizationMutationBusy ||
      !requirementEnrichmentReady ||
      atomizationAttemptGate?.serverRunning
    ) {
      return;
    }
    setAtomizationSelection((current) => {
      const selected = new Set(current.workflowIds);
      if (selected.has(workflowNodeId)) selected.delete(workflowNodeId);
      else selected.add(workflowNodeId);
      return { ...current, workflowIds: Array.from(selected) };
    });
  };

  const applyAtomizationPlan = async (plan: AtomizationScopePlan) => {
    const attemptGate = atomizationAttemptActionGate(
      plan,
      atomizationLastAttemptRef.current,
    );
    if (
      structureArtifactReadOnly ||
      atomizationMutationBusy ||
      attemptGate.applyBlocked ||
      !requirementEnrichmentReady ||
      !requirementEnrichment ||
      !atomizationScopeState ||
      !atomizationScopeState.capability.available ||
      !atomizationScopeState.capability.operations.includes("APPLY") ||
      !atomizationScopeState.capability.scopes.includes(plan.scope) ||
      !sessionId
    ) {
      return;
    }
    const commandKey = JSON.stringify([
      "APPLY",
      enrichmentRequestKey,
      atomizationScopeState.current_scope_revision,
      plan.scope,
      plan.anchorTreeNodeId,
      plan.requestedWorkflowIds,
    ]);
    const mutationRequest = beginAtomizationMutationRequest();
    setAtomizationMutation("APPLY");
    setAtomizationMutationError("");
    try {
      const command = await buildApplyAtomizationScopeWireCommandFromPlan({
        plan,
        session_id: sessionId,
        command_id: atomizationCommandId(commandKey),
        expected_review_revision: requirementEnrichment.review_revision,
        expected_scope_revision: atomizationScopeState.current_scope_revision,
        expected_entry_classification_snapshot_id:
          atomizationScopeState.classification_snapshot_id,
        expected_entry_classification_revision:
          atomizationScopeState.classification_revision,
        expected_entry_classification_sha256:
          atomizationScopeState.classification_sha256,
      });
      if (
        !atomizationMutationRequestIsCurrent(mutationRequest) ||
        atomizationAttemptActionGate(plan, atomizationLastAttemptRef.current)
          .applyBlocked
      ) {
        return;
      }
      await api.runRequirementAtomizationScope(
        route.projectId,
        route.documentId,
        sessionId,
        command,
        mutationRequest.controller.signal,
      );
      if (!atomizationMutationRequestIsCurrent(mutationRequest)) return;
      pendingAtomizationCommandRef.current = null;
      setAtomizationDialogOpen(false);
      setNotice(
        `已应用原子化范围：${plan.targets.length} 项技术需求；未选项继续保留原条目。`,
      );
      reloadRequirementEnrichment();
    } catch (cause) {
      if (!atomizationMutationRequestIsCurrent(mutationRequest)) return;
      setAtomizationMutationError(atomizationMutationMessage(cause, "APPLY"));
      reloadRequirementEnrichment(false);
    } finally {
      if (
        atomizationMutationRequestRef.current?.generation ===
        mutationRequest.generation
      ) {
        atomizationMutationRequestRef.current = null;
        setAtomizationMutation("");
      }
    }
  };

  const undoAtomizationScope = async () => {
    const plan = atomizationPlanResult.plan;
    const attemptGate = plan
      ? atomizationAttemptActionGate(plan, atomizationLastAttemptRef.current)
      : null;
    if (
      structureArtifactReadOnly ||
      atomizationMutationBusy ||
      attemptGate?.undoBlocked ||
      !requirementEnrichmentReady ||
      !plan ||
      !requirementEnrichment ||
      !atomizationScopeState?.capability.available ||
      !atomizationScopeState.capability.operations.includes("UNDO") ||
      !atomizationScopeState?.can_undo ||
      !sessionId
    ) {
      return;
    }
    const commandKey = JSON.stringify([
      "UNDO",
      enrichmentRequestKey,
      atomizationScopeState.current_scope_revision,
    ]);
    const mutationRequest = beginAtomizationMutationRequest();
    setAtomizationMutation("UNDO");
    setAtomizationMutationError("");
    try {
      const command = await buildUndoAtomizationScopeWireCommand({
        identity: plan.identity,
        session_id: sessionId,
        command_id: atomizationCommandId(commandKey),
        expected_review_revision: requirementEnrichment.review_revision,
        expected_scope_revision: atomizationScopeState.current_scope_revision,
        expected_entry_classification_snapshot_id:
          atomizationScopeState.classification_snapshot_id,
        expected_entry_classification_revision:
          atomizationScopeState.classification_revision,
        expected_entry_classification_sha256:
          atomizationScopeState.classification_sha256,
      });
      if (
        !atomizationMutationRequestIsCurrent(mutationRequest) ||
        atomizationAttemptActionGate(plan, atomizationLastAttemptRef.current)
          .undoBlocked
      ) {
        return;
      }
      await api.runRequirementAtomizationScope(
        route.projectId,
        route.documentId,
        sessionId,
        command,
        mutationRequest.controller.signal,
      );
      if (!atomizationMutationRequestIsCurrent(mutationRequest)) return;
      pendingAtomizationCommandRef.current = null;
      setNotice("已撤销最近一次原子化；原条目和历史操作记录均已保留。");
      reloadRequirementEnrichment();
    } catch (cause) {
      if (!atomizationMutationRequestIsCurrent(mutationRequest)) return;
      setAtomizationMutationError(atomizationMutationMessage(cause, "UNDO"));
      reloadRequirementEnrichment(false);
    } finally {
      if (
        atomizationMutationRequestRef.current?.generation ===
        mutationRequest.generation
      ) {
        atomizationMutationRequestRef.current = null;
        setAtomizationMutation("");
      }
    }
  };

  const prepareSemanticConfirmationList = async (
    acceptedClassification: EntryClassificationResponse,
    publicationRequest: SemanticPublicationRequestState,
  ) => {
    if (structureArtifactReadOnly) return false;
    const workingTree = tree;
    if (!workingTree) {
      setClassificationMutationError(
        "当前需求树不可用，系统不会生成无法验证的版本。",
      );
      return false;
    }
    setClassificationMutationError("");
    try {
      const result = await runAction(
        "生成确认清单",
        async () => {
          try {
            const assertCurrentRequest = () => {
              if (!semanticPublicationRequestIsCurrent(publicationRequest)) {
                throw new DOMException(
                  "当前文档或分类身份已变化",
                  "AbortError",
                );
              }
            };
            const enrichment = await api.requirementEnrichment(
              route.projectId,
              route.documentId,
              sessionId,
              publicationRequest.controller.signal,
            );
            assertCurrentRequest();
            const scopeState = validatedAtomizationScopeState(enrichment, {
              projectId: route.projectId,
              documentId: route.documentId,
              sessionId,
              reviewRevision,
              classification: acceptedClassification,
            });
            if (
              !exactRequirementSemanticPublicationCapabilityV2(
                enrichment.semantic_publication_capability,
              )
            ) {
              throw new Error("当前项目暂不能安全生成确认清单，请刷新后重试。");
            }
            const previewRequest =
              buildRequirementSemanticPublicationPreviewRequestV2({
                reviewRevision,
                classification: acceptedClassification,
                scopeState,
                projectId: route.projectId,
                documentId: route.documentId,
                sessionId,
              });
            const previewResponse =
              await api.previewRequirementSemanticPublication(
                route.projectId,
                route.documentId,
                sessionId,
                previewRequest,
                publicationRequest.controller.signal,
              );
            assertCurrentRequest();
            const preview =
              assertRequirementSemanticPublicationPreviewV2(
                previewResponse,
                {
                  projectId: route.projectId,
                  documentId: route.documentId,
                  sessionId,
                  reviewRevision,
                  baseActiveRequirementSetId: activeSetId,
                  classification: acceptedClassification,
                  scopeState,
                  request: previewRequest,
                },
              );
            const publishResponse = await api.publishRequirementSemanticDraft(
              route.projectId,
              route.documentId,
              sessionId,
              {
                schema_version:
                  "requirement-enrichment-publish-draft-request/v2",
                expected_review_revision: reviewRevision,
                binding_preview_id: preview.bindingPreviewId,
                binding_preview_sha256: preview.bindingPreviewSha256,
              },
              publicationRequest.controller.signal,
            );
            assertCurrentRequest();
            const published = assertRequirementSemanticPublishDraftV2(
              publishResponse,
              {
                projectId: route.projectId,
                documentId: route.documentId,
                sessionId,
                reviewRevision,
                classification: acceptedClassification,
                preview,
                workingTree,
              },
            );
            assertCurrentRequest();
            const activationRequest: RequirementSetSemanticActivationRequestV1 = {
              schema_version: "requirement-set-semantic-activation-request/v1",
              expected_active_requirement_set_id: activeSetId,
              expected_enrichment_publication_binding_id:
                published.bindingId,
              expected_enrichment_publication_binding_sha256:
                published.response.binding.sha256,
            };
            const activationResponse = await api.activateRequirementSet(
              route.projectId,
              route.documentId,
              published.requirementSetId,
              activationRequest,
              publicationRequest.controller.signal,
            );
            assertCurrentRequest();
            assertSemanticActivationResponse(activationResponse, {
              projectId: route.projectId,
              documentId: route.documentId,
              requirementSetId: published.requirementSetId,
              bindingId: published.bindingId,
              bindingSha256: published.response.binding.sha256,
            });
            assertCurrentRequest();
            return published;
          } catch (cause) {
            if (
              semanticPublicationRequestIsCurrent(publicationRequest) &&
              !(cause instanceof DOMException && cause.name === "AbortError")
            ) {
              setClassificationMutationError(
                cause instanceof Error
                  ? cause.message
                  : "确认清单发布状态无法验证，系统尚未激活该版本。",
              );
            }
            throw cause;
          }
        },
        // Navigation to the verified set refreshes its scoped workspace. A
        // pre-navigation refresh would observe our own active-set switch as
        // unrelated context drift and could suppress the hand-off.
        { refresh: "none" },
      );
      if (
        !result ||
        !semanticPublicationRequestIsCurrent(publicationRequest)
      ) {
        return false;
      }
      setNotice("确认清单已按当前分类与原子化结果生成，并设为当前版本。");
      updateRoute({
        task: "extraction",
        mode: "review",
        checkpoint: "review",
        requirementSetId: result.requirementSetId,
      });
      return true;
    } catch (cause) {
      if (
        semanticPublicationRequestIsCurrent(publicationRequest) &&
        !(cause instanceof DOMException && cause.name === "AbortError")
      ) {
        setClassificationMutationError(
          cause instanceof Error
            ? cause.message
            : "确认清单发布状态无法验证，系统尚未激活该版本。",
        );
      }
      return false;
    }
  };

  const prepareConfirmationList = async (
    acceptedClassification: EntryClassificationResponse,
    publicationRequest: SemanticPublicationRequestState | null,
  ) => {
    if (structureArtifactReadOnly) return false;
    if (semanticPublicationUnbound) {
      setClassificationMutationError(
        "已接受的分类结果尚未同步完成，请重新读取后再生成确认清单。",
      );
      return false;
    }
    if (sessionId) {
      if (!publicationRequest) return false;
      return prepareSemanticConfirmationList(
        acceptedClassification,
        publicationRequest,
      );
    }
    const pendingActivation =
      pendingLocalDraftActivation?.classificationRequestKey ===
        classificationRequestKey &&
      pendingLocalDraftActivation.classificationSnapshotId ===
        acceptedClassification.snapshot.snapshot_id &&
      pendingLocalDraftActivation.classificationSnapshotSha256 ===
        acceptedClassification.snapshot.payload_sha256
        ? pendingLocalDraftActivation
        : null;
    if (pendingLocalDraftActivation && !pendingActivation) {
      setPendingLocalDraftActivation(null);
    }
    setClassificationMutationError("");
    const result = await runAction(
      pendingActivation ? "设为当前版本" : "生成确认清单",
      async () => {
        try {
          const published =
            pendingActivation?.published ||
            (await request<JsonRecord>(
              `/api/projects/${encodeURIComponent(route.projectId)}` +
                `/sor-documents/${encodeURIComponent(route.documentId)}` +
                "/extraction/requirement-tree/publish-draft",
              { method: "POST" },
            ));
          assertPublicationReadyForActivation(
            published,
            acceptedClassification,
          );
          const draftId =
            pendingActivation?.requirementSetId ||
            draftRequirementPublicationSetId(published);
          if (draftId) {
            if (!pendingActivation) {
              setPendingLocalDraftActivation({
                classificationRequestKey,
                classificationSnapshotId:
                  acceptedClassification.snapshot.snapshot_id,
                classificationSnapshotSha256:
                  acceptedClassification.snapshot.payload_sha256,
                requirementSetId: draftId,
                published,
              });
            }
            await api.activateRequirementSet(
              route.projectId,
              route.documentId,
              draftId,
            );
            setPendingLocalDraftActivation(null);
          }
          return published;
        } catch (cause) {
          const message =
            cause instanceof Error
              ? cause.message
              : "确认清单发布状态无法验证，系统尚未激活该版本";
          setClassificationMutationError(message);
          throw cause;
        }
      },
      { refresh: "document" },
    );
    const requirementSetId = requirementPublicationSetId(result);
    if (!requirementSetId) return false;
    setNotice(
      `${requirementPublicationNotice(result)}${
        draftRequirementPublicationSetId(result) ? "；已设为当前版本" : ""
      }`,
    );
    updateRoute({
      task: "extraction",
      mode: "review",
      checkpoint: "review",
      requirementSetId,
    });
    return true;
  };

  const acceptAndPrepareConfirmationList = async () => {
    if (!entryClassification || structureArtifactReadOnly) return;
    const publicationRequest = sessionId
      ? beginSemanticPublicationRequest(entryClassification)
      : null;
    if (sessionId && !publicationRequest) return;
    if (publicationRequest) setSemanticPublicationMutation(true);
    try {
      const accepted = await acceptCurrentClassification(publicationRequest);
      if (
        !accepted ||
        (publicationRequest &&
          !semanticPublicationRequestIsCurrent(publicationRequest))
      ) {
        return;
      }
      const prepared = await prepareConfirmationList(
        accepted,
        publicationRequest,
      );
      if (
        prepared &&
        (!publicationRequest ||
          semanticPublicationRequestIsCurrent(publicationRequest))
      ) {
        setPublicationDialogOpen(false);
      }
    } finally {
      if (
        publicationRequest &&
        semanticPublicationRequestRef.current?.generation ===
          publicationRequest.generation
      ) {
        semanticPublicationRequestRef.current = null;
        setSemanticPublicationMutation(false);
      }
    }
  };

  const reviseSelectedTableProjection = async (
    action:
      | "SPLIT_ROWS"
      | "MERGE_PREVIOUS"
      | "MERGE_NEXT"
      | "KEEP_WHOLE"
      | "RESTORE_AUTO",
  ) => {
    if (!selectedTableProjection || structureArtifactReadOnly) return;
    const projectionId = String(selectedTableProjection.projection_id || "");
    if (!projectionId) return;
    const recordId = String(selectedTableContextNode?.table_record_id || "");
    const labels = {
      SPLIT_ROWS: "按行拆分表格",
      MERGE_PREVIOUS: "与上一条合并",
      MERGE_NEXT: "与下一条合并",
      KEEP_WHOLE: "恢复为完整原表",
      RESTORE_AUTO: "恢复自动判断",
    } as const;
    const result = await runAction(
      labels[action],
      () =>
        api.reviseTableRecordProjection(
          route.projectId,
          route.documentId,
          projectionId,
          {
            schema_version: "table-record-projection-revision-request/v1",
            action,
            expected_projection_revision: Number(
              selectedTableProjection.projection_revision || 0,
            ),
            ...(recordId ? { record_id: recordId } : {}),
          },
        ),
      { refresh: "none" },
    );
    if (result) await refreshStructure();
  };

  const closeTableRowGroupEditor = () => {
    if (tableRowGroupSaving) return;
    tableRowGroupRequestRef.current?.abort();
    tableRowGroupRequestRef.current = null;
    setTableRowGroupLoading(false);
    setTableRowGroupDialogOpen(false);
  };

  const openTableRowGroupEditor = async () => {
    if (!selectedTableProjection) return;
    const projectionId = String(selectedTableProjection.projection_id || "");
    if (!projectionId) return;
    tableRowGroupRequestRef.current?.abort();
    const controller = new AbortController();
    tableRowGroupRequestRef.current = controller;
    setTableRowGroupDialogOpen(true);
    setTableRowGroupReview(null);
    setTableRowGroupDraft([]);
    setTableRowGroupError("");
    setTableRowGroupLoading(true);
    try {
      const review = await api.tableRecordProjectionRowGroups(
        route.projectId,
        route.documentId,
        projectionId,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      const eligibleCoordinates = review.eligible_rows.map(
        (row) => `${row.table_id}:${row.row_index}`,
      );
      const currentCoordinates = review.current_row_groups.flatMap((group) =>
        group.rows.map((row) => `${row.table_id}:${row.row_index}`),
      );
      const currentIsExact =
        currentCoordinates.length === eligibleCoordinates.length &&
        currentCoordinates.every(
          (coordinate, index) => coordinate === eligibleCoordinates[index],
        );
      const initialGroups = currentIsExact
        ? review.current_row_groups
        : review.suggested_row_groups;
      setTableRowGroupReview(review);
      setTableRowGroupDraft(
        initialGroups.map((group) =>
          group.rows.map((row) => ({
            table_id: row.table_id,
            row_index: row.row_index,
          })),
        ),
      );
    } catch (error) {
      if (controller.signal.aborted) return;
      setTableRowGroupError(
        error instanceof Error
          ? error.message
          : "读取行分组失败，请稍后重试。",
      );
    } finally {
      if (tableRowGroupRequestRef.current === controller) {
        tableRowGroupRequestRef.current = null;
        setTableRowGroupLoading(false);
      }
    }
  };

  const toggleTableRowGroupBoundary = (
    groupIndex: number,
    rowIndex: number,
  ) => {
    setTableRowGroupDraft((current) => {
      const groups = current.map((group) => [...group]);
      const group = groups[groupIndex];
      if (!group) return current;
      if (rowIndex < group.length - 1) {
        const left = group.slice(0, rowIndex + 1);
        const right = group.slice(rowIndex + 1);
        groups.splice(groupIndex, 1, left, right);
        return groups;
      }
      const next = groups[groupIndex + 1];
      if (!next) return current;
      groups.splice(groupIndex, 2, [...group, ...next]);
      return groups;
    });
    setTableRowGroupError("");
  };

  const saveTableRowGroups = async () => {
    if (
      !tableRowGroupReview ||
      !tableRowGroupReview.editable ||
      !tableRowGroupDraft.length ||
      structureArtifactReadOnly ||
      tableRowGroupSaving
    ) {
      return;
    }
    setTableRowGroupSaving(true);
    setTableRowGroupError("");
    try {
      await api.reviseTableRecordProjection(
        route.projectId,
        route.documentId,
        tableRowGroupReview.projection_id,
        {
          schema_version: "table-record-projection-revision-request/v2",
          action: "SPLIT_ROWS",
          expected_projection_revision:
            tableRowGroupReview.expected_projection_revision,
          expected_source_sha256:
            tableRowGroupReview.expected_source_sha256,
          expected_structure_snapshot_id:
            tableRowGroupReview.expected_structure_snapshot_id,
          row_groups: tableRowGroupDraft.map((rows) => ({ rows })),
        },
      );
      await refreshStructure();
      setTableRowGroupDialogOpen(false);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setTableRowGroupError(
          "表格或结构状态已变化。请关闭后重新打开行分组，读取最新版本。",
        );
      } else {
        setTableRowGroupError(
          error instanceof Error
            ? error.message
            : "保存行分组失败，请稍后重试。",
        );
      }
    } finally {
      setTableRowGroupSaving(false);
    }
  };

  const undoStructurePayload = () => {
    const source = asRecord(workbench.source);
    const runtime = asRecord(workbench.runtime);
    const structureIdentity = asRecord(workbench.structure);
    return {
      schema_version: "structure-undo-request/v1",
      source_ir_id: String(source.source_ir_id || ""),
      source_sha256: String(source.source_sha256 || ""),
      source_ir_adapter_version: String(
        source.source_ir_adapter_version || runtime.source_ir_adapter_version || "",
      ),
      base_structure_snapshot_id: String(
        structureIdentity.base_snapshot_id || "",
      ),
      base_structure_canonical_sha256: String(
        structureIdentity.base_canonical_sha256 || "",
      ),
      structure_resolver_version: String(
        runtime.structure_resolver_version || structureIdentity.resolver_version || "",
      ),
      expected_revision_number: Number(structureIdentity.revision || 0),
      expected_effective_snapshot_id: String(
        structureIdentity.effective_snapshot_id || "",
      ),
    };
  };

  const structurePatchPayload = (operations: JsonRecord[]) => {
    const binding = undoStructurePayload();
    return {
      ...binding,
      schema_version: "structure-patch-request/v1",
      operations,
    };
  };

  const boundaryCalibrationPreviewPayload = (
    operation: BoundaryCalibrationOperation,
  ) => ({
    ...undoStructurePayload(),
    schema_version: "boundary-calibration-preview-request/v1",
    page_number: pdfPage,
    geometry_index_id: String(geometryIndex.geometry_index_id || ""),
    geometry_index_sha256: String(geometryIndex.geometry_index_sha256 || ""),
    operations: [operation],
  });

  const previewBoundaryCalibration = async (
    operation: BoundaryCalibrationOperation,
    selection: BoundaryCalibrationSelection,
  ) => {
    if (structureArtifactReadOnly) {
      throw new Error("当前展示的是只读结果，不能预览新的边界修订。");
    }
    const structureIdentity = asRecord(workbench.structure);
    const planBinding = asRecord(boundaryCalibrationPlan?.binding);
    if (
      selection.binding.revision_number !==
        Number(structureIdentity.revision || 0) ||
      selection.binding.effective_snapshot_id !==
        String(structureIdentity.effective_snapshot_id || "") ||
      selection.binding.geometry_index_sha256 !==
        String(geometryIndex.geometry_index_sha256 || "") ||
      String(planBinding.effective_snapshot_id || "") !==
        selection.binding.effective_snapshot_id
    ) {
      throw new Error("结构或 PDF 几何已更新，请重新载入当前页后再预览。");
    }
    const result = await api.previewBoundaryCalibration(
      route.projectId,
      route.documentId,
      boundaryCalibrationPreviewPayload(operation),
    );
    if (result.accepted !== true || result.persisted !== false) {
      throw new Error("没有生成可安全提交的边界预览，请重新预览后再试。");
    }
    setBoundaryCalibrationPreview(result);
    return result;
  };

  const applyBoundaryCalibration = async (
    operation: BoundaryCalibrationOperation,
    selection: BoundaryCalibrationSelection,
  ) => {
    if (structureArtifactReadOnly) {
      throw new Error("当前展示的是只读结果，不能提交边界修订。");
    }
    const previewOperation = asArray<JsonRecord>(
      boundaryCalibrationPreview?.operations,
    )[0];
    if (
      !boundaryCalibrationPreview ||
      selection.binding.effective_snapshot_id !==
        String(asRecord(workbench.structure).effective_snapshot_id || "") ||
      JSON.stringify(previewOperation) !== JSON.stringify(operation)
    ) {
      throw new Error("预览已过期，请重新预览这一边界。");
    }
    const result = await runAction(
      "应用版面边界修订",
      () =>
        api.patchStructure(
          route.projectId,
          route.documentId,
          structurePatchPayload([operation]),
        ),
      { refresh: "document" },
    );
    if (!result) {
      throw new Error("边界修订未提交，当前结构版本保持不变。");
    }
    setBoundaryCalibrationPreview(null);
    setBoundaryCalibrationDraft(null);
    setBoundaryCalibrationPlan(null);
    return result;
  };

  const applyManualStructurePatch = async (
    label: string,
    operations: JsonRecord[],
  ) => {
    if (
      structureArtifactReadOnly ||
      !selectedStructureId ||
      !operations.length
    ) {
      return;
    }
    const result = await runAction(
      label,
      () =>
        api.patchStructure(
          route.projectId,
          route.documentId,
          structurePatchPayload(operations),
        ),
      { refresh: "document" },
    );
    if (result) setStructureDrawerOpen(false);
  };

  const reparentSelectedStructure = async () => {
    const operations = requirementTreeReparentOperations({
      editableStructureNodeIds: selectedStructureIds,
      targetStructureNodeId: structureTargetId,
      structureNodeById,
      targetChildIds: structureChildrenByParent.get(structureTargetId) || [],
    });
    if (!operations.length) return;
    await applyManualStructurePatch(
      operations.length > 1 ? `重挂 ${operations.length} 个结构节点` : "重挂结构节点",
      operations,
    );
  };

  const reorderSelectedStructure = async (direction: -1 | 1) => {
    const currentIndex = selectedSiblingIds.indexOf(selectedStructureId);
    const targetIndex = currentIndex + direction;
    if (currentIndex < 0 || targetIndex < 0 || targetIndex >= selectedSiblingIds.length) {
      return;
    }
    const orderedChildIds = [...selectedSiblingIds];
    [orderedChildIds[currentIndex], orderedChildIds[targetIndex]] = [
      orderedChildIds[targetIndex],
      orderedChildIds[currentIndex],
    ];
    await applyManualStructurePatch("调整同级顺序", [
      {
        operation: "logical_reorder",
        parent_structure_node_id: currentParentId,
        ordered_child_structure_node_ids: orderedChildIds,
      },
    ]);
  };

  const mergeSelectedStructure = async () => {
    if (!mergeTargetId) return;
    await applyManualStructurePatch("合并续接内容", [
      {
        operation: "continuation_merge",
        primary_structure_node_id: selectedStructureId,
        continuation_structure_node_id: mergeTargetId,
      },
    ]);
  };

  const splitSelectedStructure = async () => {
    const offset = Number(splitOffset);
    if (
      !boundarySpan ||
      !Number.isInteger(offset) ||
      offset <= boundaryStart ||
      offset >= boundaryEnd
    ) {
      return;
    }
    const sourceRef = asRecord(boundarySpan.source_ref);
    const normalizedSourceRef = {
      kind: String(sourceRef.kind || ""),
      source_id: String(sourceRef.source_id || ""),
    };
    await applyManualStructurePatch("拆分结构边界", [
      {
        operation: "boundary_split",
        structure_node_id: selectedStructureId,
        parts: [
          {
            source_spans: [
              {
                source_ref: normalizedSourceRef,
                start_offset: boundaryStart,
                end_offset: offset,
              },
            ],
          },
          {
            source_spans: [
              {
                source_ref: normalizedSourceRef,
                start_offset: offset,
                end_offset: boundaryEnd,
              },
            ],
          },
        ],
      },
    ]);
  };

  const nativeBaselineJobStatus = String(
    nativeBaselineStatus?.job?.status || nativeBaselineStatus?.status || "IDLE",
  ).toUpperCase();
  const nativeParentJobStatus = String(
    nativeParentStatus?.job?.status || nativeParentStatus?.status || "IDLE",
  ).toUpperCase();
  const nativeAdvanceRunning = [nativeBaselineJobStatus, nativeParentJobStatus].some(
    (status) => ["PENDING", "RUNNING", "CANCEL_REQUESTED"].includes(status),
  );
  const nativeAdvanceLabel = sourceRequirementReady
    ? "进入原文需求清单"
    : nativeParentJobStatus === "RUNNING"
      ? "旧诊断运行中 · 不作为新基线"
      : nativeBaselineJobStatus === "RUNNING"
        ? "正在验证原文基线"
        : "原文需求清单切片尚未发布";
  const nativeParentProgress = asRecord(nativeParentStatus?.job?.progress);
  const nativeBaselineProgress = asRecord(nativeBaselineStatus?.job?.progress);
  const nativeActiveProgress = Object.keys(nativeParentProgress).length
    ? nativeParentProgress
    : nativeBaselineProgress;
  const nativeCurrentWindow = Math.max(
    0,
    Number(nativeActiveProgress.current_window || 0),
  );
  const nativeTotalWindows = Math.max(
    0,
    Number(nativeActiveProgress.total_windows || 0),
  );
  const nativeCheckpointCount = Math.max(
    0,
    Number(
      nativeActiveProgress.checkpoint_count ||
        nativeActiveProgress.completed_window_count ||
        0,
    ),
  );
  const nativeReconciliationCheckpointCount = Math.max(
    0,
    Number(nativeActiveProgress.reconciliation_checkpoint_count || 0),
  );
  const nativeActiveStage = String(nativeActiveProgress.stage || "");
  const nativeReconciliationActive = nativeActiveStage.includes("RECONCIL");
  const nativeValidatedBatchCount = Math.max(
    0,
    Number(nativeActiveProgress.validated_batch_count || 0),
  );
  const nativeTotalModelBatchCount = Math.max(
    0,
    Number(nativeActiveProgress.total_model_batch_count || 0),
  );
  const nativeOverallTotalWindows = Math.max(
    0,
    Number(
      nativeActiveProgress.overall_total_windows ||
        (nativeCheckpointCount > 0 ? nativeTotalWindows : 0),
    ),
  );
  const nativeRemainingWindowTotal = Math.max(
    0,
    nativeReconciliationActive
      ? nativeTotalWindows
      : nativeActiveProgress.overall_total_windows
        ? nativeTotalWindows
        : nativeOverallTotalWindows - nativeCheckpointCount,
  );
  const nativeAgentCallCount = Math.max(
    0,
    Number(nativeActiveProgress.agent_call_count || 0),
  );
  const nativeActiveTargetCount = Math.max(
    0,
    Number(nativeActiveProgress.active_target_count || 0),
  );
  const nativeAttempt = Math.max(1, Number(nativeActiveProgress.attempt || 1));
  const nativeWindowElapsedSeconds = Math.max(
    0,
    Number(nativeActiveProgress.elapsed_seconds || 0),
  );
  const optimizeStructure = async () => {
    if (structureArtifactReadOnly) return;
    const started = await runAction(
      "优化文档结构",
      () => api.startStructureOptimization(route.projectId, route.documentId),
      { refresh: "none" },
    );
    if (started) {
      setReorganizeDialogOpen(false);
      await refreshStructureOptimization();
    }
  };

  const tableRowDescriptorByCoordinate = useMemo(
    () =>
      new Map(
        (tableRowGroupReview?.eligible_rows || []).map((row) => [
          `${row.table_id}:${row.row_index}`,
          row,
        ]),
      ),
    [tableRowGroupReview],
  );

  if (!selectedDocument) {
    return (
      <EmptyState
        icon="document"
        title="先在项目管理中选择客户文件"
        description="需求提取严格绑定项目、文件和来源身份。"
        action={
          <button
            className="button primary"
            type="button"
            onClick={() => updateRoute({ task: "projects" })}
          >
            打开项目管理
          </button>
        }
      />
    );
  }

  const workingTreeTemporarilyUnavailable =
    treeState === "NOT_STARTED" &&
    (latestExtractionUnsuccessful ||
      latestExtractionCancelled ||
      reviewAvailable);
  const extractionAgentStatus = String(
    asRecord(workbench.agent).status || "",
  ).toUpperCase();
  const completedWithoutReviewItems =
    treeState === "RUNNING_PARTIAL" &&
    extractionAgentStatus === "COMPLETED" &&
    !tree?.nodes.some((node) => isRequirementTreeReviewItem(node));
  const structureLoading = Boolean(refreshing && !workingTree);
  const extractionFailureCodes = Array.from(
    new Set(
      [extractionFailureCode, extractionFailureCause].filter(
        (code): code is string => Boolean(code),
      ),
    ),
  );
  const extractionFailureDetails = [
    extractionFailureStage ? `阶段 ${extractionFailureStage}` : "",
    ...extractionFailureCodes.map((code, index) =>
      index === 0 ? `错误码 ${code}` : `原因码 ${code}`,
    ),
  ];
  const currentParseArtifactAvailable = Boolean(
    treeState === "READY" &&
    sourcePagesReady &&
    parsingDiagnosticsCurrent &&
    ["READY", "READY_WITH_RISKS", "BLOCKED"].includes(
      parsingDiagnostics.status,
    ),
  );
  const parsingWorkflowBlockerMessage = latestExtractionQueued
    ? extractionQueueWaitingForWorkerSync
      ? "任务已保留，但后台解析服务正在同步当前连接配置；同步完成前不会领取任务。"
      : extractionQueueBlockedNoLiveWorker
        ? "任务已保留，但当前没有在线后台解析服务；服务恢复前不会开始逐页解析。"
    : extractionQueueStalled
      ? "解析任务仍未被后台服务领取；当前没有在生成新结果。可以安全取消本次任务，确认服务恢复后再重试。"
      : "解析任务已进入后台队列，尚未开始生成新结果；上一版事实按一致性合同保留或标记为历史版本。"
    : latestExtractionExecuting
      ? "新的解析结果正在生成；完成前保留通过一致性校验的旧事实只读浏览。"
      : latestExtractionCancelling
        ? "正在安全停止本次解析；已发布结果和原文件保持不变。"
    : connectionRevisionMismatch
      ? connectionMismatchFleetReady
        ? "后台解析服务已同步当前连接配置；失败任务不会原地复用，请按当前方案重新提交全文解析。"
        : extractionExecutionReadinessState === "BLOCKED_NO_LIVE_WORKER"
          ? "当前没有在线后台解析服务，任务尚未开始逐页解析。请等待服务自动恢复后再重新提交。"
          : "后台解析服务尚未同步当前连接配置，任务尚未开始逐页解析。系统正在自动同步；完成前不要重复提交。"
    : latestExtractionUnsuccessful && !currentParseArtifactAvailable
      ? `最近一次解析${latestExtractionInterrupted ? "中断" : "失败"}${
          extractionFailureDetails.length
            ? `（${extractionFailureDetails.join(" · ")}）`
            : ""
        }。原文件和已有可用结果保持不变，请重新解析后再进入下一步。`
      : latestExtractionCancelled && !currentParseArtifactAvailable
        ? "最近一次解析已取消，未生成可进入下一步的新结果。原文件和已有业务数据保持不变，请重新解析后再继续。"
    : treeState === "REQUIRES_REEXTRACTION"
      ? sourceCaptureRecovery
        ? visualRepairAvailable
          ? `本次 Qwen OCR 结构恢复有 ${visualRepairFailedPages.length} 页降级；只需重试这些降级页，已就绪页将原样复用。`
          : `当前正文事实不足以生成结构，需要使用已授权的${visualRestorationMethodLabel}重新解析整个文档。`
        : "当前解析结果与运行版本或结果合同不兼容，需要使用当前配置重新生成。"
      : treeState === "FAILED"
        ? "最近一次提取没有形成可进入下一步的结构结果，请显式重新解析。"
        : treeState === "NOT_STARTED"
              ? sourcePagesReady
                ? "逐页解析事实已可核对，但尚未形成可进入结构整理的当前结构结果；请重新解析全文生成完整结果。"
                : legacyPageReady
                  ? "已有旧版逐页预览和解析事实可供核对，但尚未生成当前结构整理所需的正式解析工件；请使用当前配置重新解析。"
                  : "当前文件尚未形成解析与结构结果，请先显式开始解析。"
          : treeState === "RUNNING_PARTIAL"
            ? "解析与结构结果尚未完成；当前只读事实可先浏览，完成后再进入结构整理。"
            : "";
  const sourceFileUrl =
    `/api/projects/${encodeURIComponent(route.projectId)}` +
    `/sor-documents/${encodeURIComponent(route.documentId)}/file`;
  const pdfUrl =
    `/pdf-viewer?embedded=1&keyboard=parent&fit=page` +
    `&pdf=${encodeURIComponent(sourceFileUrl)}`;
  const selectedTableProjectionMode = String(
    selectedTableProjection?.mode || "WHOLE_TABLE",
  ).toUpperCase();
  const selectedTableProjectionRecords = asArray<JsonRecord>(
    selectedTableProjection?.records,
  );
  const tableRowGroupSelectionChanged = Boolean(
    tableRowGroupReview &&
      String(selectedTableProjection?.projection_id || "") !==
        tableRowGroupReview.projection_id,
  );
  const selectedTableRecordIndex = selectedTableProjectionRecords.findIndex(
    (record) =>
      String(record.record_id || "") ===
      String(selectedTableContextNode?.table_record_id || ""),
  );
  const boundaryCalibrationActive =
    activeCheckpoint === "structure" && organizeView === "calibration";
  const currentStructureIdentity = asRecord(workbench.structure);
  const currentStructureRevision = Number(currentStructureIdentity.revision || 0);
  const currentEffectiveStructureId = String(
    currentStructureIdentity.effective_snapshot_id || "",
  );
  const classificationUnclassifiedCount =
    entryClassification?.counts.unclassified || 0;
  const acceptedNonSemanticPublicationRetry =
    !sessionId &&
    Boolean(classificationMutationError) &&
    entryClassification?.snapshot.status === "ACCEPTED";
  const localDraftActivationRetry = Boolean(
    acceptedNonSemanticPublicationRetry &&
      pendingLocalDraftActivation?.classificationRequestKey ===
        classificationRequestKey &&
      pendingLocalDraftActivation.classificationSnapshotId ===
        entryClassification?.snapshot.snapshot_id &&
      pendingLocalDraftActivation.classificationSnapshotSha256 ===
        entryClassification?.snapshot.payload_sha256,
  );
  let listPrimaryLabel = "生成确认清单";
  if (structureArtifactReadOnly) {
    listPrimaryLabel = "当前结果只读";
  } else if (sessionId && requirementEnrichmentLoading) {
    listPrimaryLabel = "正在核对发布范围";
  } else if (semanticPublicationUnbound) {
    listPrimaryLabel = "正在同步已接受的分类结果";
  } else if (semanticPublicationMutation) {
    listPrimaryLabel = "正在生成确认清单";
  } else if (entryClassificationLoading) {
    listPrimaryLabel = "正在读取分类";
  } else if (entryClassificationError) {
    listPrimaryLabel = "分类不可用";
  } else if (classificationUnclassifiedCount > 0) {
    listPrimaryLabel = `还有 ${classificationUnclassifiedCount} 条未分类`;
  }
  const semanticPublicationScopeTitle = semanticPublicationAtomizationError
    ? "当前原子化范围不能发布"
    : !semanticPublicationReady
      ? "正在核对发布范围"
      : semanticPublicationAtomizationBinding?.atomizationState === "APPLIED"
        ? "采用已应用的原子化范围"
        : "保留当前原条目";
  const semanticPublicationScopeDescription =
    semanticPublicationAtomizationError ||
    (!semanticPublicationReady
      ? "正在重新读取刚接受的分类与原子化范围，请稍候。"
      : `当前 ${entryClassification?.counts.total || 0} 条内容将先接受分类，再生成并校验待确认版本；本动作不调用原子化，也不会启动需求分析。`);
  const focusFirstUnclassifiedEntry = () => {
    if (!tree || !entryClassification) return;
    const entry = entryClassification.snapshot.entries.find(
      (candidate) => effectiveEntryClassificationType(candidate) === null,
    );
    if (!entry) return;
    const node = tree.nodes.find(
      (candidate) => candidate.id === entry.tree_node_id,
    );
    if (!node) return;
    filterExpansionSnapshotRef.current = null;
    setQuery("");
    setFilter("all");
    setClassificationFilter("UNCLASSIFIED");
    selectNode(node);
    revealTreeNode(node.id, { query: "", filter: "all" });
  };
  const reloadEntryClassifications = () => {
    setClassificationBatchOpen(false);
    setPublicationDialogOpen(false);
    setClassificationMutationError("");
    setPendingLocalDraftActivation(null);
    setClassificationReloadToken((value) => value + 1);
  };

  const sourceEvidenceHardBlocked = Boolean(
    parsingDiagnostics.hardBlockerCount > 0 ||
      (!sourcePagesReady && parsingWorkflowBlockerMessage),
  );
  const workflowSteps: AgentWorkflowStep[] = [
    {
      id: "source-evidence",
      label: "准备文件与页面证据",
      state:
        sourcePagesReady && !sourceEvidenceHardBlocked
          ? "complete"
          : sourceEvidenceHardBlocked
              ? "blocked"
              : "current",
    },
    {
      id: "document-orientation",
      label: "理解全文并识别主要文档范围",
      state:
        nativeStructureReferenceAvailable || treeState === "READY"
          ? "complete"
          : sourceEvidenceHardBlocked
              ? "blocked"
              : "current",
    },
    {
      id: "structure-growth",
      label: "逐层还原标题、正文、表格与图片归属",
      state:
        activeCheckpoint === "list" ||
        nativeStructureAdvancement.advancementAllowed
          ? "complete"
          : nativeStructureReferenceAvailable || treeState === "READY"
            ? "current"
            : "pending",
    },
    {
      id: "structure-verification",
      label: "复验跨页关系与来源覆盖",
      state:
        activeCheckpoint === "list" || sourceRequirementReady
          ? "complete"
          : nativeStructureReferenceAvailable
            ? "current"
            : "pending",
    },
  ];
  const workflowStatusTone = latestExtractionRunning
    ? "working"
    : parsingWorkflowBlockerMessage ||
        parsingDiagnostics.hardBlockerCount > 0
      ? "blocked"
      : parsingDiagnostics.severeWarningCount > 0 &&
          !parsingDiagnostics.acknowledged
        ? "attention"
        : "ready";
  const workflowStatusLabel = latestExtractionRunning
    ? "Agent 正在理解并恢复原文结构"
    : parsingWorkflowBlockerMessage
      ? sourcePagesReady
        ? "来源证据需要局部恢复"
        : "正在准备来源证据"
      : showSourceEvidenceLens
        ? "来源证据可按需核对"
        : activeCheckpoint === "structure"
          ? nativeStructureReferenceAvailable
            ? "原文结构正在生长或等待反馈"
            : "正在建立原文结构"
          : classificationUnclassifiedCount > 0
            ? "需求基线待判断"
            : "需求基线可确认";
  const workflowCurrentAction = latestExtractionRunning
    ? `正在${extractionStageLabel(String(extractionJob.stage || ""))}${
        extractionTotalPages > 0
          ? `，页面证据已覆盖 ${extractionCompletedPages}/${extractionTotalPages} 页`
          : ""
      }。`
    : parsingWorkflowBlockerMessage
      ? sourcePagesReady
        ? "正在定位受影响的页面或对象；已验证结构仍可浏览，恢复只影响新的相关范围。"
        : "正在建立可供结构恢复使用的页面证据；请查看下方具体原因和可执行动作。"
      : showSourceEvidenceLens
        ? "正在显示原文结构使用的页面证据；返回结构不会重新运行任务。"
        : activeCheckpoint === "structure"
          ? selectedNode
            ? `正在核对「${truncate(requirementTreeNodeDisplayLabel(selectedNode), 80)}」的层级与父条目边界。`
            : "Agent 正在从全文概览向标题、编号、跨页内容和表格边界逐层深入。"
          : entryClassificationLoading
            ? "正在读取原文条目的专业判断结果。"
            : classificationUnclassifiedCount > 0
              ? `正在逐条判断 ${entryClassification?.counts.total || 0} 条原文内容，尚有 ${classificationUnclassifiedCount} 条待判断。`
              : "原文条目判断已完成，正在等待生成可逐条对照的基线确认清单。";
  const workflowFindings = [
    parsingWorkflowBlockerMessage
      ? `来源状态：${parsingWorkflowBlockerMessage}`
      : "",
    parsingDiagnostics.hardBlockerCount > 0
      ? `发现 ${parsingDiagnostics.hardBlockerCount} 项真实来源证据阻断；只限制受影响的新结构发布。`
      : "",
    parsingDiagnostics.severeWarningCount > 0
      ? `发现 ${parsingDiagnostics.severeWarningCount} 项非阻断来源风险；默认继续结构工作，可在来源证据中定位。`
      : "",
    requirementDecomposition.length > 0
      ? `检测到旧结果携带 ${decomposedObligationCount} 条自动原子化候选；它们不作为当前原文基线，也不会自动进入需求分析。`
      : "",
    activeCheckpoint === "list" && classificationUnclassifiedCount > 0
      ? `当前仍有 ${classificationUnclassifiedCount} 条原文内容待判断是否纳入需求基线。`
      : "",
  ].filter(Boolean);
  if (workflowFindings.length === 0) {
    workflowFindings.push(
      "当前未发现需要单独提示的异常；仍需通过原文与页面证据逐条确认准确性。",
    );
  }
  const workflowControls: AgentWorkflowControl[] = [];
  if (extractionCanCancel) {
    workflowControls.push({
      id: "terminate-extraction",
      label: "终止本次解析",
      tone: "danger",
      disabled: Boolean(busyAction),
      onClick: () => void cancelCurrentExtraction(),
    });
  }
  if (
    parsingWorkflowBlockerMessage &&
    !latestExtractionRunning &&
    !latestExtractionCancelling
  ) {
    workflowControls.push({
      id: "rerun-extraction",
      label: "查看重新解析方案",
      tone: "primary",
      disabled: Boolean(busyAction) || connectionMismatchRetryBlocked,
      title: connectionMismatchRetryBlocked
        ? "后台解析服务正在同步当前连接配置"
        : "打开解析方案确认，不会直接提交任务",
      onClick: () => setParsingRerunDialogOpen(true),
    });
  }
  if (showSourceEvidenceLens && nativeStructureReferenceAvailable) {
    workflowControls.push({
      id: "return-to-source-structure",
      label: "返回原文结构",
      tone: "primary",
      disabled: Boolean(busyAction),
      onClick: closeSourceEvidenceLens,
    });
  } else if (
    activeCheckpoint === "list" &&
    classificationUnclassifiedCount > 0
  ) {
    workflowControls.push({
      id: "focus-unclassified",
      label: "定位首条待判断",
      tone: "primary",
      disabled: Boolean(busyAction) || entryClassificationLoading,
      onClick: focusFirstUnclassifiedEntry,
    });
  } else if (activeCheckpoint === "list" && entryClassification) {
    workflowControls.push({
      id: "prepare-baseline-confirmation",
      label: "生成基线确认清单",
      tone: "primary",
      disabled:
        Boolean(busyAction) ||
        Boolean(classificationMutation) ||
        Boolean(atomizationMutation) ||
        semanticPublicationMutation ||
        structureArtifactReadOnly ||
        latestExtractionRunning ||
        semanticPublicationUnbound ||
        Boolean(entryClassificationError),
      onClick: () => {
        setClassificationMutationError("");
        publicationDialogContextKeyRef.current = semanticPublicationContextKey;
        setPublicationDialogOpen(true);
      },
    });
  }

  return (
    <section className="requirement-tree-page">
      <header className="requirement-tree-commandbar">
        <div className="requirement-tree-title">
          <span>需求提取</span>
          <strong title={selectedDocument.file_name}>
            {selectedDocument.file_name}
          </strong>
        </div>
        <RequirementTreeStageRail
          value={activeCheckpoint}
          reviewAvailable={!reviewStageUnavailable}
          workingLedger={Boolean(documentAgent) || sourceRequirementReady && requestedCheckpoint !== "review"}
          onChange={requestCheckpoint}
        />
        {treeState === "READY" && activeCheckpoint === "structure" && (
          <Tabs
            className="boundary-view-switch"
            label="结构整理查看方式"
            value={organizeView}
            onChange={setOrganizeView}
            items={[
              { value: "tree", label: "树形整理" },
              {
                value: "calibration",
                label: "版面校准",
                disabled: !canCalibrateStructureBoundaries,
                title: canCalibrateStructureBoundaries
                  ? undefined
                  : "当前结构结果只读，不能校准版面边界",
              },
            ]}
          />
        )}
        <div className="tree-command-actions" hidden={Boolean(documentAgent) && !nativeStructureReferenceAvailable && !showSourceEvidenceLens}>
          {activeCheckpoint === "structure" && (
            <button
              className={cx(
                "button ghost source-evidence-toggle",
                showSourceEvidenceLens && "is-active",
              )}
              type="button"
              aria-pressed={showSourceEvidenceLens}
              title="查看原文结构使用的页面事实、恢复状态与技术详情"
              onClick={() => {
                if (showSourceEvidenceLens) {
                  closeSourceEvidenceLens();
                  return;
                }
                setSourceEvidenceOpen(true);
              }}
            >
              <Icon name={showSourceEvidenceLens ? "tree" : "evidence"} />
              {showSourceEvidenceLens ? "返回结构" : "来源证据"}
            </button>
          )}
          {(showSourceEvidenceLens ||
            (activeCheckpoint === "structure" &&
              Boolean(parsingWorkflowBlockerMessage) &&
              !nativeStructureReferenceAvailable)) && (
            <button
              className="button ghost"
              type="button"
              disabled={
                Boolean(busyAction) ||
                latestExtractionRunning ||
                connectionMismatchRetryBlocked
              }
              title={
                connectionMismatchRetryBlocked
                  ? "后台解析服务正在同步当前连接配置；恢复前不能重复提交"
                  : "查看本次全文来源恢复方案；不会直接提交任务"
              }
              onClick={() => setParsingRerunDialogOpen(true)}
            >
              <Icon name="refresh" />
              重新解析全文…
            </button>
          )}
          {treeState === "READY" &&
            activeCheckpoint === "structure" &&
            !showSourceEvidenceLens &&
            !structureArtifactReadOnly && (
            <button
              className="button ghost"
              type="button"
              disabled={Boolean(busyAction)}
              onClick={() => setReorganizeDialogOpen(true)}
            >
              <Icon name="refresh" />
              重新整理…
            </button>
          )}
          {activeCheckpoint === "structure" && !showSourceEvidenceLens && (
            <button
              className="button primary"
              type="button"
              disabled={
                nativeStructureReferenceAvailable
                  ? !sourceRequirementReady
                  : Boolean(busyAction) ||
                    latestExtractionRunning ||
                    treeState !== "READY"
              }
              onClick={() => {
                if (nativeStructureReferenceAvailable) {
                  if (sourceRequirementReady) requestCheckpoint("list");
                  return;
                }
                requestCheckpoint("list");
              }}
              title={
                nativeStructureReferenceAvailable
                  ? sourceRequirementReady
                    ? "进入基于同一 Document Map 的原文需求清单"
                    : "当前仅有旧515条诊断结果；等待Agent-owned Source Requirement Record切片"
                  : undefined
              }
            >
              {nativeStructureReferenceAvailable
                ? nativeAdvanceLabel
                : "进入原文需求清单"}
              <Icon name="arrow" />
            </button>
          )}
          {activeCheckpoint === "list" && !sourceRequirementReady && (
            <button
              className="button primary"
              type="button"
              disabled={
                Boolean(busyAction) ||
                Boolean(classificationMutation) ||
                Boolean(atomizationMutation) ||
                semanticPublicationMutation ||
                structureArtifactReadOnly ||
                latestExtractionRunning ||
                treeState !== "READY" ||
                semanticPublicationUnbound ||
                entryClassificationLoading ||
                Boolean(entryClassificationError) ||
                !entryClassification
              }
              onClick={() => {
                if (classificationUnclassifiedCount > 0) {
                  focusFirstUnclassifiedEntry();
                  return;
                }
                setClassificationMutationError("");
                publicationDialogContextKeyRef.current =
                  semanticPublicationContextKey;
                setPublicationDialogOpen(true);
              }}
              title={
                structureArtifactReadOnly
                  ? "当前展示的是上一次可用结果；新解析成功前只能浏览"
                  : requirementEnrichmentLoading
                  ? "正在核对当前分类与原子化结果"
                  : semanticPublicationUnbound
                    ? "正在同步已接受的分类结果，请稍候再生成"
                    : classificationUnclassifiedCount > 0
                      ? "定位第一条未分类内容"
                      : "接受当前三类分类并冻结为确认清单；不会启动需求分析"
              }
            >
              {listPrimaryLabel}
              <Icon name="arrow" />
            </button>
          )}
        </div>
      </header>

      <div className="requirement-tree-status-stack">
        {isSaasAccountMode() && (nativeStructureAdvancement.artifactId ? <p role="status">{SAAS_KIT_UNAVAILABLE_MESSAGE}</p> : agentAvailability.message && <p role="status">{agentAvailability.message} <button type="button" className="button ghost compact" onClick={agentAvailability.refresh}>刷新 Agent 配置</button></p>)}
        {(activeCheckpoint === "structure" || !sourceRequirementReady)
          && !(documentAgent && nativeReferenceLookupReady && !nativeStructureReferenceAvailable && !showSourceEvidenceLens) && <SourceDocumentActions
          projectId={route.projectId} documentId={route.documentId}
          sourceSha256={String(selectedDocument.source_sha256 || sourcePages.source_sha256 || "")}
          mapArtifactId={nativeStructureAdvancement.artifactId || ""}
          ready={nativeReferenceLookupReady && (!nativeStructureReferenceAvailable || !!nativeStructureAdvancement.artifactId)}
          externalJobs={sourceGraphJobs}
          onActivityChange={setSourceDocumentActivity}
          collaborationRequest={sourceCollaborationRequest}
          onUpdated={() => setSourceGraphRefresh(value => value + 1)} />}
        {!documentAgent && !showSourceDocumentTask && (showSourceEvidenceLens ||
          !nativeStructureReferenceAvailable ||
          !(
            activeCheckpoint === "structure" ||
            activeCheckpoint === "list"
          )) ? (
          <AgentWorkflowCockpit
            goal={
              activeCheckpoint === "structure"
                ? "将授权原文逐层还原为可定位、可修订的文档结构"
                : "在已接受的原文结构下生长可逐条对照的原文需求清单"
            }
            statusLabel={workflowStatusLabel}
            statusTone={workflowStatusTone}
            steps={workflowSteps}
            currentAction={workflowCurrentAction}
            findings={workflowFindings}
            controls={workflowControls}
            defaultExpanded={false}
          />
        ) : null}
        {!showSourceEvidenceLens &&
        (requestedCheckpoint === "structure" ||
          requestedCheckpoint === "list") ? (
          <NativeStructureReferenceDock
            projectId={route.projectId}
            onChangePurpose={() => updateRoute({ task: "analysis" })}
            documentId={route.documentId}
            refreshToken={sourceGraphRefresh}
            baselineRefreshSha={sourceRequirementStatus?.baseline_artifact_sha256 || ""}
            sourceSha256={String(
              selectedDocument.source_sha256 || sourcePages.source_sha256 || "",
            )}
            onPageChange={(page) => {
              setPdfPage(
                Math.min(nativeStructurePageLimit, Math.max(1, page)),
              );
              setCompactDetail("source");
            }}
            onAvailabilityChange={(available) => {
              setNativeStructureReferenceAvailable(available);
              setNativeReferenceLookupReady(true);
            }}
            onAdvancementChange={setNativeStructureAdvancement}
            onPageCountChange={setNativeStructureReferencePageCount}
            sourceFileUrl={sourceFileUrl}
            activePage={pdfPage}
            pageCount={nativeStructurePageLimit}
            showSourcePreview
            mode={
              requestedCheckpoint === "list" && sourceRequirementReady
                ? "SOURCE_REQUIREMENTS"
                : "STRUCTURE"
            }
          />
        ) : null}
        {!showSourceEvidenceLens && activeCheckpoint === "list" &&
        nativeStructureReferenceAvailable && !sourceRequirementReady ? (
          <InlineNotice title="原文条目尚未生成">
            当前先展示同一份已恢复的原文结构、正文与表格。可以继续核对或返回原文结构；切换步骤不会重新解析文档。
          </InlineNotice>
        ) : null}
        {!showSourceEvidenceLens &&
        activeCheckpoint === "structure" &&
        nativeStructureReferenceAvailable &&
        (sourceRequirementReady || nativeAdvanceRunning || nativeAdvanceError) ? (
          <InlineNotice
            title={
              sourceRequirementReady
                ? "原文需求清单代表性切片已就绪"
                : nativeAdvanceError
                  ? "Agent 基线状态同步异常"
                  : "旧父条款诊断仍在运行"
            }
            tone={sourceRequirementReady ? "success" : nativeAdvanceError ? "warning" : "info"}
          >
            <span>
              {sourceRequirementReady
                ? sourceRequirementStatus?.message
                : nativeAdvanceError ||
                  "旧515条结果只保留为诊断，不会进入原子化、分析或正式导出。"}
            </span>
            {!sourceRequirementReady &&
            nativeCurrentWindow > 0 &&
            nativeRemainingWindowTotal > 0 ? (
              <span>
                {nativeReconciliationActive ? "跨对象协调" : "待补来源窗口"}{" "}
                {nativeCurrentWindow}/{nativeRemainingWindowTotal}
                {nativeActiveTargetCount > 0
                  ? ` · 当前 ${nativeActiveTargetCount} 个对象`
                  : ""}
                {nativeValidatedBatchCount > 0 && nativeTotalModelBatchCount > 0
                  ? ` · 已验证对象 ${nativeValidatedBatchCount}/${nativeTotalModelBatchCount}`
                  : ""}
                {nativeCheckpointCount > 0
                  ? ` · 对象检查点 ${nativeCheckpointCount}`
                  : ""}
                {nativeReconciliationCheckpointCount > 0
                  ? ` · 协调检查点 ${nativeReconciliationCheckpointCount}`
                  : ""}
                {nativeAttempt > 1 ? ` · 第 ${nativeAttempt} 次尝试` : ""}
                {nativeAgentCallCount > 0
                  ? ` · 本轮 Agent 调用 ${nativeAgentCallCount} 次`
                  : ""}
                {nativeWindowElapsedSeconds > 0
                  ? ` · 本次 ${Math.round(nativeWindowElapsedSeconds)} 秒`
                  : ""}
              </span>
            ) : null}
            {sourceRequirementReady ? (
              <span>
                当前覆盖 {sourceRequirementStatus?.covered_scope_ids?.length || 0} 个范围、
                {sourceRequirementStatus?.record_count || 0} 条Source Requirement Records；
                结果基于同一Document Map，可反馈、可撤销且不阻断其他范围。
              </span>
            ) : null}
          </InlineNotice>
        ) : null}
        {!showSourceEvidenceLens &&
        activeCheckpoint === "structure" &&
        treeState === "READY" ? (
          <AgentStructureProposalDock
            projectId={route.projectId}
            documentId={route.documentId}
            sourceSha256={String(
              sourcePages.source_sha256 || selectedDocument.source_sha256 || "",
            )}
            revisionNumber={currentStructureRevision}
            effectiveSnapshotId={currentEffectiveStructureId}
            selectedStructureNodeId={
              selectedNode?.id.startsWith("structure-node-")
                ? selectedNode.id
                : ""
            }
            disabled={structureArtifactReadOnly || latestExtractionRunning}
            onFocusNode={(nodeId) => {
              const node = treeIndex.get(nodeId);
              if (node) selectNode(node);
              revealTreeNode(nodeId);
            }}
            onApplied={refreshStructure}
          />
        ) : null}
        {structureArtifactReadOnly &&
          !showSourceEvidenceLens &&
          !nativeStructureReferenceAvailable && (
          <InlineNotice
            title="当前展示上一次可用结果（只读）"
            tone="warning"
          >
            <span>
              {structureArtifactReadOnlyReasonCode ===
              "structure_derivation_rerun_not_complete"
                ? "最新一次重新解析未形成可替换的新结果，系统继续提供上一次成功结果供核对。"
                : structureArtifactReadOnlyReasonCode ===
                    "structure_derivation_rerun_in_progress"
                  ? "新的解析任务仍在排队或运行；系统继续提供上一次成功结构供只读核对。"
                : "服务端已将当前结构结果标记为只读，系统继续保留内容供核对。"}
            </span>
            <span>
              结构修正、分类、原子化和生成确认清单均已停用；筛选、原文和解析事实仍可浏览。
            </span>
            <button
              type="button"
              className="text-button"
              onClick={() => navigateToCheckpoint("diagnose")}
            >
              查看来源证据
            </button>
          </InlineNotice>
        )}
        {requirementDecomposition.length > 0 && (
          <InlineNotice
            title={`检测到历史原子化候选：${requirementDecomposition.length} 个父条目、${decomposedObligationCount} 个派生条目`}
            tone="warning"
          >
            <span>
              这些派生内容不会应用到当前来源忠实基线，也不会自动进入需求分析。完整父条目仍是当前可核对对象；如需采用，必须在需求基线形成后由用户显式发起原子化并复核拆分结果。
            </span>
            <details className="extraction-technical-details">
              <summary>仅查看历史候选</summary>
              {requirementDecomposition.map((parent, parentIndex) => (
                <section
                  key={
                    String(
                      parent.parent_provision_id ||
                        parent.parent_requirement_view_node_id ||
                        "",
                    ) ||
                    `decomposition-${parentIndex}`
                  }
                >
                  <strong>完整父条目 {parentIndex + 1}</strong>
                  <ol>
                    {asArray<JsonRecord>(parent.obligations).map(
                      (obligation, obligationIndex) => (
                        <li key={`${parentIndex}-${obligationIndex}`}>
                          {truncate(
                            String(
                                obligation.text ||
                                obligation.atomic_statement ||
                                "原子化候选",
                            ),
                            260,
                          )}
                        </li>
                      ),
                    )}
                  </ol>
                </section>
              ))}
            </details>
          </InlineNotice>
        )}
        {showSourceEvidenceLens &&
          (latestExtractionUnsuccessful || latestExtractionCancelled) && (
            <InlineNotice
              title={
                connectionRevisionMismatch
                  ? connectionMismatchFleetReady
                    ? "后台已恢复，可按当前方案重新提交"
                    : extractionExecutionReadinessState ===
                        "CONFIGURATION_SUPERSEDED"
                      ? "AI 连接配置已更新"
                      : "后台解析服务未同步当前配置"
                : latestExtractionCancelled
                  ? currentParseArtifactAvailable
                    ? "最新重新解析已取消"
                    : "最近一次解析已取消"
                  : currentParseArtifactAvailable
                    ? `最新重新解析${
                        latestExtractionInterrupted ? "中断" : "失败"
                      }`
                    : `最近一次解析${
                        latestExtractionInterrupted ? "中断" : "失败"
                      }`
              }
              tone={
                connectionRevisionMismatch
                  ? connectionMismatchFleetReady
                    ? "warning"
                    : "danger"
                : latestExtractionCancelled
                  ? "warning"
                  : currentParseArtifactAvailable
                    ? "warning"
                    : "danger"
              }
            >
              {connectionRevisionMismatch ? (
                <>
                  <span>
                    {mismatchPagesStarted === 0
                      ? "本次任务在开始逐页解析前已停止，没有发布任何新解析结果。"
                      : `本次任务在配置不一致时已开始 ${mismatchPagesStarted} 页；这些临时结果没有发布。`}
                  </span>
                  {(submittedConnectionRevision !== null ||
                    apiConnectionRevision !== null ||
                    workerConnectionRevisions.length > 0) && (
                    <span className="extraction-revision-summary">
                      {[
                        submittedConnectionRevision !== null
                          ? `任务方案 revision ${submittedConnectionRevision}`
                          : "",
                        apiConnectionRevision !== null
                          ? `当前 API revision ${apiConnectionRevision}`
                          : "",
                        workerConnectionRevisions.length > 0
                          ? `后台执行器 revision ${workerConnectionRevisions.join("/")}`
                          : "后台执行器 revision 待确认",
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  )}
                  <span>
                    {connectionMismatchFleetReady
                      ? "后台已加载当前连接配置。失败任务不会原地复用，现在可以按当前项目方案重新提交全文解析。"
                      : extractionExecutionReadinessState ===
                          "BLOCKED_NO_LIVE_WORKER"
                        ? "当前没有在线后台解析服务。请等待服务自动启动并显示就绪，恢复前不要重复提交。"
                        : "后台正在自动同步当前连接配置。同步完成前普通重跑已停用，避免再次提交必然失败的任务。"}
                  </span>
                  <span>
                    {previousArtifactAvailable
                      ? previousExtractionArtifact.can_browse_structure === true
                        ? "上一成功结构仍可只读浏览；结构修正、分类和发布保持停用。"
                        : "上一成功结果已保留为历史版本，但与当前运行合同不兼容，不能作为当前结构继续。"
                      : "原 PDF、历史确认清单和已有业务数据保持不变。"}
                  </span>
                  {extractionFailureDetails.length > 0 && (
                    <details className="extraction-technical-details">
                      <summary>技术详情</summary>
                      <span>{extractionFailureDetails.join(" · ")}</span>
                    </details>
                  )}
                  {activeExtractionActionRow}
                </>
              ) : latestExtractionCancelled ? (
                <span>任务已按请求结束，没有发布新结果。</span>
              ) : extractionFailureDetails.length > 0 ? (
                <span>{extractionFailureDetails.join(" · ")}</span>
              ) : (
                <span>任务没有返回可公开的稳定错误码。</span>
              )}
              {!connectionRevisionMismatch && (
                <span>
                  {currentParseArtifactAvailable
                    ? "旧结果未被替换，当前解析事实、结构和风险回执仍保持有效；可以继续核对，也可以重试生成新结果。"
                    : "原文件和已有业务数据保持不变；请使用“重新解析全文”生成可进入下一步的当前结果。"}
                </span>
              )}
            </InlineNotice>
          )}
        {showSourceEvidenceLens &&
          treeState === "REQUIRES_REEXTRACTION" &&
          !connectionRevisionMismatch && (
            <InlineNotice
              title={
                sourceCaptureRecovery
                  ? "正文需要视觉恢复"
                  : "当前解析结果需要重新生成"
              }
              tone="warning"
            >
              {sourceCaptureRecovery
                ? visualRepairAvailable
                  ? `原 PDF 保持不变；本次只需重试 Qwen OCR 结构恢复降级的 ${visualRepairFailedPages.length} 页，其余已就绪页不会重复调用远程服务。`
                  : `原 PDF 保持不变；请使用已授权的${visualRestorationMethodLabel}生成当前逐页事实。`
                : "原 PDF 和历史确认清单保持不变；请按当前配置重新生成解析结果。"}
            </InlineNotice>
          )}
        {latestExtractionQueued && (
          <InlineNotice
            title={
              visualRepairState === "ACTIVE"
                ? "Qwen OCR 结构恢复降级页重试正在排队"
                : extractionQueueWaitingForWorkerSync
                  ? "等待后台配置同步"
                  : extractionQueueBlockedNoLiveWorker
                    ? "等待后台解析服务启动"
                : extractionQueueStalled
                ? "解析排队异常"
                : extractionRetryScheduled
                  ? "解析将在稍后自动重试"
                  : "重新解析正在排队"
            }
            tone={extractionQueueStalled ? "warning" : "info"}
          >
            <span>
              {extractionRetryScheduled
                ? "上一次后台尝试未完成；到达重试时间前不会有页面正在解析。"
                : extractionQueueWaitingForWorkerSync
                  ? "任务已保留，但兼容槽位正在加载当前连接配置；同步完成前不会开始解析。"
                  : extractionQueueBlockedNoLiveWorker
                    ? "当前没有在线兼容槽位；服务恢复前任务不会开始逐页解析。"
                : extractionQueueStalled
                ? "任务已提交，但后台服务尚未领取；此时并没有页面正在解析。"
                : Number(extractionQueue.ahead_count || 0) > 0
                  ? `按当前可见队列估计，前方约有 ${Number(extractionQueue.ahead_count)} 个任务；可用解析槽位释放后会自动开始逐页解析。`
                  : Number(extractionQueue.running_count || 0) > 0
                    ? "当前解析槽位均在工作；任一兼容槽位释放后会自动开始当前文档。"
                    : "任务已提交，正在等待后台服务领取。"}
            </span>
            {extractionRetryScheduled && extractionRetryAvailableAt && (
              <span>
                计划重试时间：
                <time dateTime={extractionRetryAvailableAt}>
                  {new Date(extractionRetryAvailableAt).toLocaleString("zh-CN", {
                    hour12: false,
                  })}
                </time>
                。
              </span>
            )}
            {extractionQueueWaitLabel && (
              <span>当前任务已等待 {extractionQueueWaitLabel}。</span>
            )}
            {frozenExtractionPlanSummary && (
              <span>
                本次冻结方案：{frozenExtractionPlanSummary}。任务提交后不会随页面选择或设置变更。
              </span>
            )}
            {visualRepairState === "ACTIVE" && (
              <span>{activeVisualRepairSummaryText}</span>
            )}
            {previousArtifactAvailable && (
              <span>
                {previousArtifactRuntimeCompatible
                  ? "上一次成功解析仍可只读浏览；新结果成功后才会替换它。"
                  : "上一版成功结果仍保留，但当前运行版本已变化，不能冒充当前可编辑结构；已发布确认清单和原文件不受影响。"}
              </span>
            )}
            {activeExtractionActionRow}
          </InlineNotice>
        )}
        {latestExtractionExecuting && (
          <section
            className={cx(
              "extraction-progress-drawer",
              latestVisualPageOutcome === "FAILED" && "has-visual-failure",
            )}
            aria-label="全文解析运行进度"
          >
            <div className="extraction-progress-drawer-summary">
              <span className="notice-pip" aria-hidden="true" />
              <div className="extraction-progress-drawer-heading">
                <strong>
                  {visualRepairState === "ACTIVE"
                    ? "正在重试 Qwen OCR 结构恢复降级页"
                    : `正在${extractionStageLabel(String(extractionJob.stage || ""))}`}
                </strong>
                {extractionTotalPages > 0 ? (
                  <span className="extraction-progress-drawer-count">
                    {extractionCompletedPages}/{extractionTotalPages} 页
                    {extractionCurrentPage > 0
                      ? ` · 当前 P${extractionCurrentPage}`
                      : ""}
                  </span>
                ) : (
                  <span>
                    {String(extractionJob.last_progress || "") ||
                      "后台已领取任务，正在准备第一项可验证进度。"}
                  </span>
                )}
              </div>
              <div className="extraction-progress-drawer-signals">
                {stagedPageFactsActive && (
                  <span>{stagedReadyPageCount} 页可浏览</span>
                )}
                {latestVisualPageAvailable &&
                  latestVisualPageOutcome === "FAILED" && (
                    <span className="is-failed">
                      P{latestVisualPageNumber} 视觉失败
                    </span>
                  )}
                {extractionHeartbeatAgeSeconds !== null &&
                  extractionHeartbeatAgeSeconds > 45 && (
                    <span className="is-warning">
                      后台响应延迟 · {extractionHeartbeatAgeSeconds} 秒
                    </span>
                  )}
              </div>
              <button
                type="button"
                className="extraction-progress-drawer-toggle"
                aria-controls="extraction-progress-running-details"
                aria-expanded={extractionProgressDetailsExpanded}
                aria-label={
                  extractionProgressDetailsExpanded
                    ? "收起解析运行详情"
                    : "展开解析运行详情"
                }
                onClick={() =>
                  setExtractionProgressDetailState((current) => ({
                    jobId: extractionJobId,
                    expanded:
                      current.jobId === extractionJobId
                        ? !current.expanded
                        : true,
                  }))
                }
              >
                <span>{extractionProgressDetailsExpanded ? "收起" : "详情"}</span>
                <Icon
                  name={
                    extractionProgressDetailsExpanded
                      ? "chevron-down"
                      : "chevron"
                  }
                  size={14}
                />
              </button>
            </div>
            <span
              className="sr-only"
              role="status"
              aria-live="polite"
              aria-atomic="true"
            >
              {extractionTotalPages > 0
                ? `全文解析进度：已处理 ${extractionCompletedPages} 页，共 ${extractionTotalPages} 页${extractionCurrentPage > 0 ? `，当前第 ${extractionCurrentPage} 页` : ""}`
                : String(extractionJob.last_progress || "后台已领取解析任务")}
              {latestVisualPageAvailable &&
              latestVisualPageOutcome === "FAILED"
                ? `；第 ${latestVisualPageNumber} 页视觉处理失败`
                : ""}
              {extractionHeartbeatAgeSeconds !== null &&
              extractionHeartbeatAgeSeconds > 45
                ? "；后台响应延迟"
                : ""}
            </span>
            {extractionTotalPages > 0 && (
              <div className="extraction-progress-drawer-track">
                <progress
                  max={Math.max(extractionTotalPages, 1)}
                  value={Math.min(extractionCompletedPages, extractionTotalPages)}
                  aria-label={`全文解析页进度：已处理 ${extractionCompletedPages} 页，共 ${extractionTotalPages} 页`}
                />
              </div>
            )}
            <div
              id="extraction-progress-running-details"
              className="extraction-progress-drawer-details"
              role="region"
              aria-label="解析运行详情"
              hidden={!extractionProgressDetailsExpanded}
            >
              {stagedPageFactsActive && (
                <div className="extraction-page-progress">
                  <small>
                    本轮已有 {stagedReadyPageCount} 页可浏览
                    {readyPageRangesLabel(stagedReadyPageRanges)
                      ? `（${readyPageRangesLabel(stagedReadyPageRanges)}）`
                      : ""}
                    ；未完成页不会混入上一正式结果。
                  </small>
                </div>
              )}
              {latestVisualPageAvailable && (
                <section
                  className={cx(
                    "extraction-visual-page-status",
                    latestVisualPageOutcome === "FAILED" && "is-failed",
                    latestVisualPageOutcome === "CACHE_HIT" && "is-cache-hit",
                  )}
                  aria-label="最近视觉页状态"
                >
                  <div>
                    <strong>{latestVisualPageTitle}</strong>
                    <span>
                      第 {latestVisualPageNumber} 页 · {visualProviderLatencyLabel(
                        latestVisualPageEvent.latency_ms,
                      )} · {latestVisualPageRequestCount} 次请求
                    </span>
                  </div>
                  <small>“已处理”只表示流程已推进，不等于视觉识别成功。</small>
                </section>
              )}
              {extractionHeartbeatLabel && <span>{extractionHeartbeatLabel}</span>}
              {frozenExtractionPlanSummary && (
                <span>本次冻结方案：{frozenExtractionPlanSummary}。</span>
              )}
              {visualRepairState === "ACTIVE" && (
                <span>{activeVisualRepairSummaryText}</span>
              )}
              {previousArtifactAvailable && (
                <span>
                  {previousArtifactRuntimeCompatible
                    ? "上一次成功解析仍可只读浏览；本次完成前不会替换。"
                    : "上一版成功结果仍作为历史记录保留，但不满足当前运行版本的一致性合同。"}
                </span>
              )}
              {activeExtractionActionRow}
            </div>
          </section>
        )}
        {latestExtractionCancelling && (
          <InlineNotice title="正在安全停止解析" tone="warning">
            <span>
              当前任务不会再发布新结果；已发布确认清单、历史结果和原文件保持不变。
            </span>
            {(previousStructureButton || currentChecklistButton) && (
              <span className="extraction-run-actions">
                {previousStructureButton}
                {currentChecklistButton}
              </span>
            )}
          </InlineNotice>
        )}
        {treeState === "RUNNING_PARTIAL" &&
          !latestExtractionExecuting &&
          !agentNativeExtractionReady && (
          <InlineNotice
            title={
              completedWithoutReviewItems
                ? "原文已还原，结构条目待完善"
                : "正在生成需求结构树"
            }
            tone={completedWithoutReviewItems ? "warning" : "info"}
          >
            {completedWithoutReviewItems
              ? "页面文字、框标和表格仍可逐页核对；当前结构尚未形成可审阅条目，本地规则只保留为后续语义判断的参考。"
              : "已完成的章节可浏览；最终结果就绪前保持只读，不会发布不完整版本。"}
          </InlineNotice>
        )}
        {pageEvidenceError && !showSourceEvidenceLens && (
          <InlineNotice title="逐页解析事实暂不可用" tone="warning">
            {pageEvidenceError} 原 PDF 不受影响，可继续查看。
          </InlineNotice>
        )}
        {geometryFocusError && activeCheckpoint === "structure" && (
          <InlineNotice title="精确框选无法验证" tone="warning">
            {geometryFocusError} 系统不会退回全文搜索或整页框选。
          </InlineNotice>
        )}
        {activeCheckpoint === "list" &&
          !sourceRequirementReady &&
          entryClassificationLoading && (
          <InlineNotice title="正在读取清单分类" tone="info">
            结构、内容和原 PDF 仍可浏览；分类身份验证完成后才可操作。
          </InlineNotice>
        )}
        {activeCheckpoint === "list" &&
          !sourceRequirementReady &&
          entryClassificationError && (
          <InlineNotice title="清单分类暂不可用" tone="warning">
            {entryClassificationError}
            <button
              type="button"
              className="text-button"
              onClick={reloadEntryClassifications}
            >
              重新读取
            </button>
          </InlineNotice>
        )}
        {activeCheckpoint === "list" &&
          !sourceRequirementReady &&
          classificationMutationError && (
          <InlineNotice title="分类操作未完成" tone="warning">
            {classificationMutationError}{" "}
            当前已读取结果仍保留；发生并发冲突时请重新读取。
            <button
              type="button"
              className="text-button"
              onClick={reloadEntryClassifications}
            >
              重新读取分类
            </button>
          </InlineNotice>
        )}
        {activeCheckpoint === "list" &&
          !sourceRequirementReady &&
          requirementEnrichmentLoading && (
          <InlineNotice title="正在核对发布范围" tone="info">
            系统正在读取当前分类、原子化结果和安全生成条件；完成前可以继续浏览清单。
          </InlineNotice>
        )}
        {activeCheckpoint === "list" &&
          !sourceRequirementReady &&
          semanticPublicationUnbound &&
          !requirementEnrichmentLoading && (
            <InlineNotice title="确认清单生成暂不可用" tone="warning">
              {requirementEnrichmentError ||
                "已接受的分类结果尚未同步完成。"}
              为避免切换到无法追溯的版本，本轮只能继续浏览和整理分类，不能生成确认清单。
            </InlineNotice>
          )}
        {activeCheckpoint === "list" &&
          !sourceRequirementReady &&
          entryClassification && (
          <section
            className="entry-classification-summary"
            aria-label="清单分类统计"
          >
            <div>
              <strong>{entryClassification.counts.total} 条待整理内容</strong>
              <span>技术 {entryClassification.counts.technical}</span>
              <span>非技术 {entryClassification.counts.non_technical}</span>
              <span>信息参考 {entryClassification.counts.information}</span>
              <span>人工修改 {entryClassification.counts.human_override}</span>
              <span className={classificationUnclassifiedCount ? "has-risk" : ""}>
                未分类 {classificationUnclassifiedCount}
              </span>
            </div>
            <div>
              <StatusBadge
                tone={
                  entryClassification.snapshot.status === "ACCEPTED"
                    ? "success"
                    : classificationUnclassifiedCount
                      ? "warning"
                      : "neutral"
                }
              >
                {entryClassification.snapshot.status === "ACCEPTED"
                  ? "已接受"
                  : classificationUnclassifiedCount
                    ? "整理中"
                    : "可接受"}
              </StatusBadge>
              <button
                className="button secondary"
                type="button"
                aria-label="批量分类"
                disabled={
                  structureArtifactReadOnly ||
                  Boolean(classificationMutation) ||
                  Boolean(atomizationMutation) ||
                  entryClassification.snapshot.status === "ACCEPTED"
                }
                onClick={() => {
                  if (structureArtifactReadOnly) return;
                  setClassificationMutationError("");
                  setClassificationBatchOpen(true);
                }}
              >
                批量分类…
              </button>
            </div>
          </section>
        )}
      </div>

      {documentAgent && nativeReferenceLookupReady && !nativeStructureReferenceAvailable && !showSourceEvidenceLens ? (
        <DocumentTaskHome fileName={selectedDocument.file_name} sourceFileUrl={sourceFileUrl} />
      ) : showSourceDocumentTask && !showSourceEvidenceLens ? (
        <EmptyState
          icon="spark"
          title={sourceDocumentTaskTitle}
          description={currentSourceActivity?.checking ? "正在读取后台已有任务，请稍候。"
            : currentSourceActivity?.statusUnavailable ? "暂时无法确认后台状态，请在上方重新同步任务。"
            : sourceDocumentTask && sourceGraphJobActive(sourceDocumentTask) || currentSourceActivity?.submitting
              ? "当前任务会继续处理这份原文件，工作版将在校验后显示。可查看实际处理动作，或补充本轮意见。"
              : "在 Agent 协作中查看本轮结果、未决项，或接续当前任务。"}
          action={<button type="button" className="button primary" onClick={() => setSourceCollaborationRequest(value => value + 1)}>查看 Agent 协作</button>}
        />
      ) : showSourceEvidenceLens ? (
        <ParseDiagnosisLens
          projectId={route.projectId}
          documentId={route.documentId}
          sourceSha256={String(
            sourcePages.source_sha256 || selectedDocument.source_sha256 || "",
          )}
          sourceFileUrl={sourceFileUrl}
          pdfUrl={pdfUrl}
          pdfFrameRef={pdfFrameRef}
          page={pdfPage}
          pageCount={parsingPageCount}
          pageSnapshot={pageSnapshot}
          pageLoading={pageEvidenceLoading}
          pageAvailable={Boolean(currentRestorationPagePayload)}
          pageError={pageEvidenceError}
          pageEvidenceView={pageEvidenceView}
          hasPreviousFormalPageFacts={stagedPageFactsActive && formalPageReady}
          stagedPagePending={stagedPageViewActive && !stagedSelectedPageReady}
          stagedReadyPageCount={stagedReadyPageCount}
          stagedTotalPages={stagedTotalPages}
          stagedReadyPageRanges={readyPageRangesLabel(stagedReadyPageRanges)}
          diagnostics={parsingDiagnostics}
          diagnosticsLoading={parsingDiagnosticsLoading}
          diagnosticsError={parsingDiagnosticsError}
          workflowBlockerMessage={parsingWorkflowBlockerMessage}
          visualReceiptSummary={visualReceiptSummary}
          onRetryFailedVisualPages={
            visualRepairAvailable ? () => void retryFailedVisualPages() : null
          }
          retryFailedVisualPagesDisabled={
            Boolean(busyAction) || latestExtractionRunning
          }
          focusedFactId={diagnosticFocusId}
          riskOnly={diagnosticRiskOnly}
          showSupportLayer={diagnosticShowSupportLayer}
          contentView={diagnosticContentView}
          visualRecoveryFailed={diagnosticVisualRecoveryFailed}
          onRiskOnlyChange={(value) => {
            setDiagnosticFocusId("");
            setDiagnosticRiskOnly(value);
          }}
          onShowSupportLayerChange={setDiagnosticShowSupportLayer}
          onContentViewChange={(view) => {
            setDiagnosticFocusId("");
            setDiagnosticRiskOnly(false);
            setDiagnosticContentViewState({
              identityKey: diagnosticContentViewIdentityKey,
              view,
            });
          }}
          onPageEvidenceViewChange={(view) => {
            if (!stagedPageFactsKey) return;
            setDiagnosticFocusId("");
            setDiagnosticRiskOnly(false);
            setPageEvidenceViewState({ stagingKey: stagedPageFactsKey, view });
          }}
          onPageChange={(page) => {
            setDiagnosticFocusId("");
            setPdfPage(Math.min(parsingPageCount, Math.max(1, page)));
          }}
          onFactFocus={setDiagnosticFocusId}
        />
      ) : activeCheckpoint === "structure" &&
        nativeStructureReferenceAvailable &&
        (workingTreeTemporarilyUnavailable ||
          structureArtifactReadOnly ||
          treeState !== "READY") ? null
      : activeCheckpoint === "list" &&
          nativeStructureReferenceAvailable ? null
      : structureLoading ? (
        <LoadingBlock label="正在载入需求结构树" />
      ) : workingTreeTemporarilyUnavailable ? (
        <EmptyState
          icon="tree"
          title={reviewAvailable ? "当前整理结果暂不可用" : "最近一次提取未完成"}
          description={
            reviewAvailable
              ? "已冻结的确认清单仍完整保留。可以继续确认或分析；重新提取只会生成新的工作结果。"
              : "最近一次尝试没有生成可用结构，不代表此前业务数据被删除。可以安全重试。"
          }
          action={
            <div className="empty-state-actions">
              {reviewAvailable && (
                <button
                  className="button primary"
                  type="button"
                  onClick={() =>
                    updateRoute({
                      task: "extraction",
                      mode: "review",
                      checkpoint: "review",
                      requirementSetId: selectedSetId,
                    })
                  }
                >
                  查看最近确认清单
                </button>
              )}
              <button
                className="button ghost"
                type="button"
                disabled={Boolean(busyAction) || connectionMismatchRetryBlocked || sourceGraphBusy || !agentAvailability.ready || Boolean(isSaasAccountMode() && nativeStructureAdvancement.artifactId)}
                onClick={() => void startExtraction()}
              >
                重新提取
              </button>
            </div>
          }
        />
      ) : treeState === "NOT_STARTED" ? (
        <EmptyState
          icon="tree"
          title="结构尚未生成"
          description="从当前客户文件生成章节、条款、表格和可审阅结构条目。无需先进入技术工作台。"
          action={
            <button
              className="button primary"
              type="button"
              disabled={Boolean(busyAction) || connectionMismatchRetryBlocked || sourceGraphBusy || !agentAvailability.ready || Boolean(isSaasAccountMode() && nativeStructureAdvancement.artifactId)}
              onClick={() => void startExtraction()}
            >
              <Icon name="play" />
              开始需求提取
            </button>
          }
        />
      ) : treeState === "FAILED" ? (
        <EmptyState
          icon="tree"
          title="需求结构树生成失败"
          description="既有来源没有被修改。修复处理配置后可安全重试。"
          action={
            <button
              className="button primary"
              type="button"
              disabled={Boolean(busyAction) || connectionMismatchRetryBlocked || sourceGraphBusy || !agentAvailability.ready || Boolean(isSaasAccountMode() && nativeStructureAdvancement.artifactId)}
              onClick={() => void startExtraction()}
            >
              重新提取
            </button>
          }
        />
      ) : treeState === "REQUIRES_REEXTRACTION" ? (
        <EmptyState
          icon="tree"
          title={
            connectionRevisionMismatch
              ? connectionMismatchFleetReady
                ? "后台已恢复，可以重新提交"
                : "等待后台解析服务同步"
            : sourceCaptureRecovery
              ? "正文需要视觉恢复"
              : "当前解析结果需要重新生成"
          }
          description={
            connectionRevisionMismatch
              ? connectionMismatchFleetReady
                ? "失败任务不会原地复用；请按当前项目方案创建一项新的全文解析任务。"
                : "当前连接配置尚未被兼容槽位确认。系统正在自动同步，完成前不会让旧配置继续接单。"
            : sourceCaptureRecovery
              ? visualRepairAvailable || visualRestorationOverride
                ? visualRepairAvailable
                  ? `Qwen OCR 结构恢复已有 ${visualReceiptSummary?.readyPageCount || 0} 页就绪，另有 ${visualRepairFailedPages.length} 页降级；重试时只请求降级页，本地重建完整结果。`
                  : `原生 PDF 没有提供可靠正文。将只启用${visualRestorationMethodLabel}；本地规则只提供结构与分类参考，不启用语义 LLM。`
                : "原生 PDF 没有提供可靠正文，且当前没有可用的页面视觉恢复能力。请先在设置中完成连接检查。"
              : "原文文件仍然可用，但当前结构结果与运行版本不兼容或结果合同不完整。请使用当前处理配置重新生成解析结果。"
          }
          action={
            <div className="empty-state-actions">
              <a
                className="button ghost"
                href={`/pdf-viewer?fit=page&pdf=${encodeURIComponent(sourceFileUrl)}&page=1`}
                target="_blank"
                rel="noreferrer"
              >
                <Icon name="external" />
                打开原 PDF
              </a>
              <button
                className="button primary"
                type="button"
                disabled={
                  (sourceCaptureRecovery &&
                    !visualRepairAvailable &&
                    !visualRestorationOverride) ||
                  Boolean(busyAction) ||
                  connectionMismatchRetryBlocked
                }
                onClick={() =>
                  void (
                    sourceCaptureRecovery
                      ? visualRepairAvailable
                        ? retryFailedVisualPages()
                        : restoreSourceAndExtract()
                      : startExtraction()
                  )
                }
              >
                <Icon name="refresh" />
                {sourceCaptureRecovery
                  ? visualRepairAvailable
                    ? `只重试降级的 ${visualRepairFailedPages.length} 页`
                    : `${visualRestorationMethodLabel}并重新提取`
                  : connectionRevisionMismatch && connectionMismatchFleetReady
                    ? "按当前方案重新提交"
                  : "重新生成解析结果"}
              </button>
            </div>
          }
        />
      ) : (
        <div
          className={cx(
            "requirement-tree-workbench",
            organizeView === "calibration" &&
              "is-boundary-calibration",
          )}
          data-compact-detail={compactDetail}
        >
            <aside className="requirement-tree-pane">
              <div className="tree-tools">
                <label className="search-field">
                  <Icon name="search" />
                  <input
                    type="search"
                    aria-label={
                      activeCheckpoint === "list"
                        ? "搜索清单内容"
                        : "搜索结构内容"
                    }
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="搜索章节、条款、表格或候选"
                  />
                </label>
                <div className="tree-tool-row">
                    <div className="tree-filter-chips">
                    {activeCheckpoint === "list"
                      ? ([
                          ["ALL", "全部", "全部分类筛选"],
                          ["UNCLASSIFIED", "未分类", "未分类筛选"],
                          ["TECHNICAL", "技术", "技术需求筛选"],
                          ["NON_TECHNICAL", "非技术", "非技术需求筛选"],
                          ["INFORMATION", "信息参考", "信息参考筛选"],
                          ["HUMAN_OVERRIDE", "人工修改", "人工修改筛选"],
                        ] as const).map(([value, label, ariaLabel]) => (
                          <button
                            key={value}
                            type="button"
                            aria-label={ariaLabel}
                            aria-pressed={classificationFilter === value}
                            className={
                              classificationFilter === value ? "is-active" : ""
                            }
                            onClick={() => setClassificationFilter(value)}
                          >
                            {label}
                          </button>
                        ))
                      : ([
                          ["all", "全部"],
                          ["attention", "需关注"],
                          ["items", "候选"],
                        ] as const).map(([value, label]) => (
                          <button
                            key={value}
                            type="button"
                            aria-pressed={filter === value}
                            className={filter === value ? "is-active" : ""}
                            onClick={() => setFilter(value)}
                          >
                            {label}
                          </button>
                        ))}
                    </div>
                    <div className="tree-view-actions">
                    <details
                      ref={treeExpandMenuRef}
                      className="tree-expand-menu"
                    >
                      <summary title="选择结构树展开范围">
                        展开
                        <Icon name="chevron-down" size={14} />
                      </summary>
                      <div className="tree-expand-menu-popover">
                        <button type="button" onClick={expandCurrentBranch}>
                          展开当前分支
                        </button>
                        <button type="button" onClick={expandToClauses}>
                          展开到条款
                        </button>
                        <button type="button" onClick={expandAll}>
                          {filterActive ? "展开全部筛选结果" : "全部展开"}
                        </button>
                        <button type="button" onClick={collapseToCurrentPath}>
                          只保留当前路径
                        </button>
                      </div>
                    </details>
                    <button
                      type="button"
                      className="tree-view-action"
                      onClick={collapseAll}
                      title="将结构树收起到根节点"
                    >
                      全部收起
                    </button>
                    <button
                      type="button"
                      className="tree-view-action is-locate"
                      onClick={revealCurrentNode}
                      disabled={!selectedId}
                      title="重新展开并定位当前内容"
                    >
                      定位当前
                    </button>
                    </div>
                  </div>
              </div>
              <div className="tree-keyboard-hint">
                <span>{navigationRows.length} 项</span>
                <span>↑↓ 选择 · ←→ 层级 · ⇧←→ 分支</span>
              </div>
              <div
                ref={treeViewportRef}
                className="requirement-tree-viewport"
                role={navigationRows.length ? "tree" : "region"}
                aria-label="需求结构树"
                onScroll={(event) => handleTreeScroll(event.currentTarget)}
                onKeyDown={handleTreeKeyDown}
              >
                {navigationRows.length === 0 ? (
                  <div className="tree-filter-empty" role="status">
                    <Icon name="search" size={20} />
                    <strong>当前筛选没有匹配内容</strong>
                    <span>原清单没有改变，只是当前视图没有命中项。</span>
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => {
                        setQuery("");
                        setFilter("all");
                        setClassificationFilter("ALL");
                      }}
                    >
                      清除筛选
                    </button>
                  </div>
                ) : (
                  <div
                    className="requirement-tree-virtual-space"
                    style={{ height: navigationRows.length * TREE_ROW_HEIGHT }}
                  >
                    <div
                      style={{
                        transform: `translateY(${rowStart * TREE_ROW_HEIGHT}px)`,
                      }}
                    >
                      {virtualRows.map((row, virtualIndex) => {
                        const absoluteIndex = rowStart + virtualIndex;
                        const node = row.node;
                        const classificationEntry = entryClassificationForNode(
                          entryClassification,
                          node.id,
                        );
                        const classificationStateLabel = classificationEntry
                          ? `${entryClassificationLabel(
                              effectiveEntryClassificationType(
                                classificationEntry,
                              ),
                            )}${
                              classificationEntry.human_override
                                ? " · 人工"
                                : ""
                            }`
                          : "";
                        return (
                        <button
                          key={node.id}
                          type="button"
                          role="treeitem"
                          data-tree-node={node.id}
                          aria-level={row.depth + 1}
                          aria-posinset={absoluteIndex + 1}
                          aria-setsize={navigationRows.length}
                          aria-expanded={
                            row.hasChildren ? row.expanded : undefined
                          }
                          aria-selected={node.id === selectedId}
                          tabIndex={node.id === selectedId ? 0 : -1}
                          className={cx(
                            "requirement-tree-row",
                            node.id === selectedId && "is-active",
                            isPublishedRequirementNode(node) &&
                              "is-requirement",
                            node.attention_count > 0 && "needs-attention",
                          )}
                          style={
                            {
                              "--tree-depth": Math.min(row.depth, 8),
                              height: TREE_ROW_HEIGHT,
                            } as CSSProperties
                          }
                          onClick={() => selectNode(node)}
                          onDoubleClick={() =>
                            row.hasChildren && toggleNode(node.id)
                          }
                        >
                          <span
                            className="tree-expander"
                            onClick={(event) => {
                              if (!row.hasChildren) return;
                              event.stopPropagation();
                              toggleNode(node.id);
                            }}
                          >
                            {row.hasChildren ? (
                              <Icon
                                name={
                                  row.expanded ? "chevron-down" : "chevron"
                                }
                                size={15}
                              />
                            ) : (
                              <span />
                            )}
                          </span>
                          <Icon
                            className="tree-kind-icon"
                            name={nodeIconName(node)}
                            size={17}
                          />
                          <span className="tree-row-copy">
                            <strong>
                              {truncate(
                                requirementTreeNodeDisplayLabel(node),
                                64,
                              )}
                            </strong>
                            {requirementTreeRowSecondaryLabel(node) ? (
                              <small>
                                {requirementTreeRowSecondaryLabel(node)}
                              </small>
                            ) : null}
                          </span>
                          <span className="tree-row-state">
                            {reviewItemCount(node) > 0 &&
                              !isRequirementTreeReviewItem(node) && (
                                <span
                                  className="tree-node-count is-requirement-count"
                                  aria-label={`此节点及下级共 ${reviewItemCount(node)} 个候选`}
                                  title={`此节点及下级共 ${reviewItemCount(node)} 个候选`}
                                >
                                  <span>候选</span>
                                  <strong>{reviewItemCount(node)}</strong>
                                </span>
                              )}
                            {node.attention_count > 0 && (
                              <span
                                className="tree-node-count is-attention-count"
                                aria-label={`此节点及下级共 ${node.attention_count} 项需关注`}
                                title={`此节点及下级共 ${node.attention_count} 项需关注`}
                              >
                                <span>关注</span>
                                <strong>{node.attention_count}</strong>
                              </span>
                            )}
                            {activeCheckpoint === "list" &&
                              classificationEntry && (
                                <span
                                  className="tree-classification-state"
                                  title={classificationStateLabel}
                                >
                                  {classificationStateLabel}
                                </span>
                              )}
                          </span>
                        </button>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>
            </aside>

            <div className="tree-compact-detail-bar">
              <span>查看当前节点</span>
              <Tabs
                className="tree-compact-detail-switch"
                label="当前节点查看方式"
                value={compactDetail}
                onChange={setCompactDetail}
                items={[
                  {
                    value: "content",
                    label:
                      activeCheckpoint === "list"
                        ? "内容与分类"
                        : "条款内容",
                  },
                  { value: "source", label: "PDF 原文" },
                ]}
              />
            </div>

            <section className="requirement-content-pane">
              {!boundaryCalibrationActive && (
                <div className="content-pane-toolbar">
                  <div>
                    <span>
                      {selectedNode
                        ? nodeKindLabel(selectedNode)
                        : "条款内容"}
                    </span>
                    <strong>
                      {selectedNode ? statusLabel(selectedNode) : "选择树节点"}
                    </strong>
                  </div>
                  {canEditStructure ? (
                    <button
                      className="button secondary"
                      type="button"
                      onClick={() => setStructureDrawerOpen(true)}
                    >
                      <Icon name="wrench" />
                      调整结构
                    </button>
                  ) : null}
                </div>
              )}

              {!boundaryCalibrationActive &&
                selectedNode?.bilingual_context?.mismatch_review_required ===
                  true && (
                  <InlineNotice title="双语内容存在不一致" tone="warning">
                    系统已保留两种语言的精确原文，且未自动选择或改写其中一侧；请对照原文复核后再确认该条款。
                  </InlineNotice>
                )}

              {boundaryCalibrationActive ? (
                boundaryCalibrationLoading && !boundaryCalibrationPlan ? (
                  <LoadingBlock label="正在建立当前页的来源边界校准计划" />
                ) : boundaryCalibrationError ? (
                  <div className="boundary-calibration-unavailable">
                    <InlineNotice title="当前页暂不能校准" tone="warning">
                      {boundaryCalibrationError}
                    </InlineNotice>
                    <p>
                      原 PDF 和现有结构均保持不变；系统不会退回到自由像素拖动。
                    </p>
                  </div>
                ) : (
                  <BoundaryCalibrationWorkbench
                    plan={boundaryCalibrationPlan}
                    pageSnapshot={pageSnapshot}
                    busy={
                    structureArtifactReadOnly ||
                    Boolean(busyAction) ||
                    boundaryCalibrationLoading
                    }
                    onPreview={previewBoundaryCalibration}
                    onApply={applyBoundaryCalibration}
                    onCancel={() => {
                      setBoundaryCalibrationPreview(null);
                      setBoundaryCalibrationDraft(null);
                    }}
                    onDraftChange={setBoundaryCalibrationDraft}
                    onSegmentSelect={(nodeId) => {
                      void selectBoundaryCalibrationSegment(nodeId);
                    }}
                  />
                )
              ) : (
                <CompleteContent
                  node={contextNode}
                  selectedItem={selectedItem}
                  tree={tree}
                  pageSnapshot={pageSnapshot}
                  projectId={route.projectId}
                  documentId={route.documentId}
                  sourceSha256={contentSourceSha256}
                  onPageChange={setPdfPage}
                  onStructureNodeChange={(structureNodeId) => {
                    const target = tree?.nodes.find(
                      (candidate) => candidate.id === structureNodeId,
                    );
                    if (target && tree) {
                      // A TOC target is an explicit navigation command. Search
                      // and review filters must not immediately hide and
                      // deselect the proven target after the jump.
                      filterExpansionSnapshotRef.current = null;
                      setQuery("");
                      setFilter("all");
                      setClassificationFilter("ALL");
                      selectNode(target);
                      revealTreeNode(target.id, {
                        query: "",
                        filter: "all",
                      });
                    }
                  }}
                  recordFocus={selectedTableRecordFocus}
                />
              )}

              {!boundaryCalibrationActive && activeCheckpoint === "list" && (
                <div className="list-inspector-stack">
                  <EntryClassificationInspector
                    node={selectedNode}
                    classification={entryClassification}
                  busy={
                    structureArtifactReadOnly ||
                    Boolean(classificationMutation || atomizationMutation)
                  }
                    onChange={(workflowNodeId, entryType) => {
                    if (structureArtifactReadOnly) return;
                      void commitClassificationOverrides({
                        [workflowNodeId]: entryType,
                      });
                    }}
                  />

                  {atomizationSelectionAvailable && selectedClassificationEntry ? (
                    <label className="atomization-selection-control">
                      <input
                        type="checkbox"
                        aria-label="加入“所选项”原子化范围"
                        checked={atomizationSelection.workflowIds.includes(
                          selectedClassificationEntry.workflow_node_id,
                        )}
                        disabled={
                        structureArtifactReadOnly ||
                          atomizationMutationBusy ||
                          !requirementEnrichmentReady ||
                          Boolean(atomizationAttemptGate?.serverRunning)
                        }
                        onChange={() =>
                          toggleSelectedAtomizationWorkflow(
                            selectedClassificationEntry.workflow_node_id,
                          )
                        }
                      />
                      <span>
                        <strong>加入“所选项”原子化范围</strong>
                        <small>独立于当前高亮与筛选；勾选不会启动任务</small>
                      </span>
                      <strong>
                        已选 {atomizationSelection.workflowIds.length} 项
                      </strong>
                    </label>
                  ) : null}

                  {sessionId && atomizationViewModel ? (
                    <AtomizationScopeInspector
                      viewModel={atomizationViewModel}
                      dialogOpen={atomizationDialogOpen}
                    busy={structureArtifactReadOnly || atomizationMutationBusy}
                      rangeLoading={requirementEnrichmentLoading}
                    canOpenDialog={
                      !structureArtifactReadOnly && atomizationCanOpenDialog
                    }
                      actionGuidance={atomizationAttemptGate?.guidance || ""}
                      serverStateLabel={atomizationServerStateLabel}
                    canUndo={!structureArtifactReadOnly && atomizationCanUndo}
                      error={
                        requirementEnrichmentError ||
                        atomizationMutationError ||
                        atomizationPlanResult.error
                      }
                      currentReceipt={
                        atomizationScopeState?.current_receipt || null
                      }
                      lastAttempt={atomizationScopeState?.last_attempt || null}
                      onOpenDialog={() => {
                      if (
                        !structureArtifactReadOnly &&
                        atomizationCanOpenDialog
                      ) {
                          setAtomizationDialogOpen(true);
                        }
                      }}
                      onCloseDialog={() => setAtomizationDialogOpen(false)}
                      onScopeChange={(scope) => {
                      if (
                        !structureArtifactReadOnly &&
                        atomizationCanOpenDialog
                      ) {
                          setAtomizationScope(scope);
                        }
                      }}
                      onApply={(plan) => void applyAtomizationPlan(plan)}
                      onRetry={() => reloadRequirementEnrichment()}
                      onUndo={() => void undoAtomizationScope()}
                    />
                  ) : sessionId ? (
                    <section
                      className="atomization-scope-inline-state"
                      aria-label="原子化范围状态"
                      aria-live="polite"
                    >
                      <strong>
                        {requirementEnrichmentLoading
                          ? "正在读取可执行范围"
                          : "原子化范围暂不可用"}
                      </strong>
                      <span>
                        {requirementEnrichmentError ||
                          atomizationPlanResult.error ||
                          "当前内容仍可浏览，读取完成前不会启动任何任务。"}
                      </span>
                      {!requirementEnrichmentLoading ? (
                        <button
                          className="text-button"
                          type="button"
                          onClick={() => reloadRequirementEnrichment()}
                        >
                          重新读取范围
                        </button>
                      ) : null}
                    </section>
                  ) : null}
                </div>
              )}

              {activeCheckpoint === "structure" &&
                !boundaryCalibrationActive &&
                selectedTableProjection && (
                  <section className="table-projection-controls">
                    <header>
                      <div>
                        <span>表格拆解</span>
                        <strong>
                          {selectedTableProjectionMode === "RECORDS"
                            ? `已拆为 ${selectedTableProjectionRecords.length} 个行条目`
                            : "当前保留完整原表"}
                        </strong>
                      </div>
                      <StatusBadge
                        tone={
                          selectedTableProjectionMode === "RECORDS"
                            ? "success"
                            : "neutral"
                        }
                      >
                        {selectedTableProjectionMode === "RECORDS"
                          ? "按行"
                          : "整表"}
                      </StatusBadge>
                    </header>
                    <p>
                      {tableProjectionExplanation(selectedTableProjection)}
                    </p>
                    <div className="table-projection-actions">
                      <button
                        className="button secondary"
                        type="button"
                        disabled={Boolean(busyAction)}
                        onClick={() => void openTableRowGroupEditor()}
                      >
                        <Icon name="split" />
                        {structureArtifactReadOnly
                          ? "查看行组（只读）"
                          : "人工调整行组"}
                      </button>
                      {selectedTableProjectionMode === "RECORDS" ? (
                        <>
                          {selectedNode?.kind === "TABLE_RECORD" && (
                            <>
                              <button
                                className="button secondary"
                                type="button"
                                disabled={
                                structureArtifactReadOnly ||
                                  Boolean(busyAction) ||
                                  selectedTableRecordIndex <= 0
                                }
                                onClick={() =>
                                  void reviseSelectedTableProjection(
                                    "MERGE_PREVIOUS",
                                  )
                                }
                              >
                                与上一条合并
                              </button>
                              <button
                                className="button secondary"
                                type="button"
                                disabled={
                                structureArtifactReadOnly ||
                                  Boolean(busyAction) ||
                                  selectedTableRecordIndex < 0 ||
                                  selectedTableRecordIndex >=
                                    selectedTableProjectionRecords.length - 1
                                }
                                onClick={() =>
                                void reviseSelectedTableProjection("MERGE_NEXT")
                                }
                              >
                                与下一条合并
                              </button>
                            </>
                          )}
                          <button
                            className="button ghost"
                            type="button"
                          disabled={
                            structureArtifactReadOnly || Boolean(busyAction)
                          }
                            onClick={() =>
                              void reviseSelectedTableProjection("KEEP_WHOLE")
                            }
                          >
                            保留完整原表
                          </button>
                        </>
                      ) : null}
                      <button
                        className="button ghost"
                        type="button"
                      disabled={
                        structureArtifactReadOnly || Boolean(busyAction)
                      }
                        onClick={() =>
                          void reviseSelectedTableProjection("RESTORE_AUTO")
                        }
                      >
                        <Icon name="undo" />
                        恢复自动判断
                      </button>
                    </div>
                  </section>
                )}

              {!boundaryCalibrationActive &&
                activeCheckpoint === "structure" &&
                selectedRequirement &&
                selectedReviewNode &&
                review && (
                  <section className="requirement-correction-tray">
                    <header>
                      <div>
                        <span>需求边界修正</span>
                        <strong>只影响当前选中的需求</strong>
                      </div>
                      <span role="status" aria-live="polite">
                        {reviewSaveState || "已保存"}
                      </span>
                    </header>
                    <textarea
                      ref={correctionTextRef}
                      value={selectedReviewText}
                      readOnly
                      aria-label="选择需要拆分的来源文字"
                    />
                    <div>
                      <button
                        className="button secondary"
                        type="button"
                      disabled={
                        structureArtifactReadOnly || Boolean(busyAction)
                      }
                        onClick={() => void splitSelected()}
                      >
                        拆分所选文字
                      </button>
                      <button
                        className="button ghost"
                        type="button"
                      disabled={
                        structureArtifactReadOnly || Boolean(busyAction)
                      }
                        onClick={() => void mergeSelected()}
                      >
                        与下一项合并
                      </button>
                      <button
                        className="button ghost"
                        type="button"
                      disabled={
                        structureArtifactReadOnly || Boolean(busyAction)
                      }
                        onClick={() => void excludeSelected()}
                      >
                        移出需求
                      </button>
                      <button
                        className="button ghost"
                        type="button"
                      disabled={
                        structureArtifactReadOnly ||
                        !reviewUndo ||
                        Boolean(busyAction)
                      }
                        onClick={() => void undoReview()}
                      >
                        <Icon name="undo" />
                        撤销
                      </button>
                    </div>
                  </section>
                )}
            </section>

            <aside className="requirement-pdf-pane">
              <header>
                <div>
                  <span>
                    {boundaryCalibrationActive
                      ? "PDF 原文 · 同步边界"
                      : "原 PDF"}
                  </span>
                  <strong>第 {pdfPage} 页</strong>
                  <small>
                    {boundaryCalibrationActive
                      ? boundaryCalibrationLoading
                        ? "正在同步来源边界"
                        : `${selectedOverlays.length} 条已验证边界`
                      : geometryFocusLoading
                      ? "正在定位当前条款"
                      : geometryFocusRegions !== null
                        ? `${selectedOverlays.length} 个已验证来源范围`
                        : "兼容定位"}
                  </small>
                </div>
                <div className="requirement-pdf-header-actions">
                  {selectedOverlays.length > 0 && (
                    <div
                      className="source-precision-legend"
                      aria-label="来源范围精度图例"
                    >
                      <span>
                        <span
                          className="source-precision-swatch is-exact"
                          aria-hidden="true"
                        />
                        精确
                      </span>
                      <span>
                        <span
                          className="source-precision-swatch is-approximate"
                          aria-hidden="true"
                        />
                        近似
                      </span>
                    </div>
                  )}
                  <a
                    className="button ghost"
                    href={`/pdf-viewer?fit=page&pdf=${encodeURIComponent(sourceFileUrl)}&page=${pdfPage}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    新窗口
                    <Icon name="external" />
                  </a>
                </div>
              </header>
              <iframe
                ref={pdfFrameRef}
                className="requirement-pdf-frame"
                title={`原 PDF · 第 ${pdfPage} 页`}
                src={pdfUrl}
                loading="eager"
              />
            </aside>
        </div>
      )}

      <Dialog
        open={tableRowGroupDialogOpen}
        title="人工调整表格行组"
        onClose={closeTableRowGroupEditor}
        busy={tableRowGroupSaving}
        className="table-row-group-dialog"
      >
        <header className="modal-header">
          <div>
            <p className="section-label">表格拆解</p>
            <h2>人工调整行组</h2>
            <p>
              每个行组生成一个表格条目。点击两行之间的边界可拆分或合并；表头、表注和完整原表不会被改写。
            </p>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭人工行分组"
            disabled={tableRowGroupSaving}
            onClick={closeTableRowGroupEditor}
          >
            <Icon name="close" />
          </button>
        </header>

        {tableRowGroupLoading ? (
          <LoadingBlock label="正在读取当前行分组…" />
        ) : null}
        {structureArtifactReadOnly ? (
          <InlineNotice title="当前结果只读" tone="warning">
            行分组仍可查看，但不能保存。请回到最新可编辑结果后重新打开。
          </InlineNotice>
        ) : null}
        {tableRowGroupSelectionChanged ? (
          <InlineNotice title="已切换到另一张表" tone="warning">
            当前草稿仍绑定打开时的表格。请关闭后在目标表格重新打开，避免误改。
          </InlineNotice>
        ) : null}
        {tableRowGroupReview && !tableRowGroupReview.editable ? (
          <InlineNotice title="当前表格不能按行调整" tone="warning">
            {tableRowGroupReview.read_only_reason ||
              "没有形成可安全调整的业务行。"}
          </InlineNotice>
        ) : null}
        {tableRowGroupError ? (
          <InlineNotice title="行分组未完成" tone="warning">
            {tableRowGroupError}
          </InlineNotice>
        ) : null}

        {tableRowGroupReview?.editable && tableRowGroupDraft.length ? (
          <div className="table-row-group-editor">
            <p>
              共 {tableRowGroupReview.eligible_rows.length} 个业务行，当前分为{" "}
              <strong>{tableRowGroupDraft.length} 组</strong>。
            </p>
            <ol className="table-row-group-list">
              {tableRowGroupDraft.map((group, groupIndex) => (
                <li key={`${group[0]?.table_id}:${group[0]?.row_index}`}>
                  <header>
                    <strong>行组 {groupIndex + 1}</strong>
                    <span>{group.length} 行</span>
                  </header>
                  <ol>
                    {group.map((row, rowIndex) => {
                      const descriptor = tableRowDescriptorByCoordinate.get(
                        `${row.table_id}:${row.row_index}`,
                      );
                      const isGroupEnd = rowIndex === group.length - 1;
                      const hasFollowingGroup =
                        groupIndex < tableRowGroupDraft.length - 1;
                      return (
                        <li key={`${row.table_id}:${row.row_index}`}>
                          <div>
                            <span>
                              {descriptor?.page_number
                                ? `第 ${descriptor.page_number} 页 · `
                                : ""}
                              原表第 {row.row_index + 1} 行
                            </span>
                            <p>
                              {descriptor?.display_text || "[图片或图形事实行]"}
                            </p>
                          </div>
                          {!isGroupEnd || hasFollowingGroup ? (
                            <button
                              className="text-button"
                              type="button"
                              disabled={
                                tableRowGroupSaving ||
                                structureArtifactReadOnly ||
                                tableRowGroupSelectionChanged
                              }
                              aria-label={
                                isGroupEnd
                                  ? `合并行组 ${groupIndex + 1} 和 ${groupIndex + 2}`
                                  : `在原表第 ${row.row_index + 1} 行后拆分`
                              }
                              onClick={() =>
                                toggleTableRowGroupBoundary(
                                  groupIndex,
                                  rowIndex,
                                )
                              }
                            >
                              {isGroupEnd ? "取消此分组边界" : "在此拆分"}
                            </button>
                          ) : null}
                        </li>
                      );
                    })}
                  </ol>
                </li>
              ))}
            </ol>
          </div>
        ) : null}

        <footer className="modal-actions">
          <button
            className="button ghost"
            type="button"
            disabled={tableRowGroupSaving}
            onClick={closeTableRowGroupEditor}
          >
            取消
          </button>
          <button
            className="button primary"
            type="button"
            disabled={
              tableRowGroupLoading ||
              tableRowGroupSaving ||
              structureArtifactReadOnly ||
              tableRowGroupSelectionChanged ||
              !tableRowGroupReview?.editable ||
              !tableRowGroupDraft.length
            }
            onClick={() => void saveTableRowGroups()}
          >
            {tableRowGroupSaving
              ? "正在保存…"
              : `保存 ${tableRowGroupDraft.length || 0} 个行组`}
          </button>
        </footer>
      </Dialog>

      <Dialog
        open={parsingRiskDialogOpen}
        title="知晓解析风险并继续"
        onClose={() => setParsingRiskDialogOpen(false)}
        className="confirm-action-dialog parsing-risk-dialog"
      >
        <header>
          <div>
            <p className="section-label">解析诊断</p>
            <h2>带已知风险进入结构整理</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭风险知晓确认"
            onClick={() => setParsingRiskDialogOpen(false)}
          >
            <Icon name="close" />
          </button>
        </header>
        <div className="parsing-risk-dialog-copy">
          <p>
            当前解析结果标记了
            <strong>
              {" "}
              {parsingDiagnostics.severeWarningCount} 项非阻断严重风险
            </strong>
            ，涉及
            <strong> {parsingDiagnostics.riskPages.length} 页</strong>
            {parsingDiagnostics.unassignedRiskCount > 0
              ? `，另有 ${parsingDiagnostics.unassignedRiskCount} 项没有可靠页码`
              : ""}
            。你可以带风险继续，并在后续发现问题时回到这里定位解析层。
          </p>
          <dl>
            <div>
              <dt>影响</dt>
              <dd>只记录你已知晓风险，不修改原文、解析内容或结构规则。</dd>
            </div>
            <div>
              <dt>有效范围</dt>
              <dd>本次确认只适用于当前文件和解析结果；重新解析后需要再次确认。</dd>
            </div>
          </dl>
        </div>
        <footer>
          <button
            className="button ghost"
            type="button"
            onClick={() => setParsingRiskDialogOpen(false)}
          >
            返回核对
          </button>
          <button
            className="button primary"
            type="button"
            disabled={Boolean(busyAction)}
            onClick={() => void acknowledgeParsingRisksAndContinue()}
          >
            已知晓 {parsingDiagnostics.severeWarningCount} 项风险，继续
          </button>
        </footer>
      </Dialog>

      <Dialog
        open={parsingRerunDialogOpen}
        title="重新解析整个文档"
        onClose={() => setParsingRerunDialogOpen(false)}
        className="reorganize-tree-dialog parsing-rerun-dialog"
      >
        <header className="modal-header">
          <div>
            <p className="section-label">解析诊断</p>
            <h2>生成新的全文解析结果</h2>
            <p>
              {visualRepairAvailable
                ? `服务端已从正式回执确定 ${visualRepairFailedPages.length} 个失败页；重试只为这些页面发起文字 OCR 请求，并复用其余成功页重建整份结果。table_parsing 仍单独受每页 8 次、整文 64 次上限。历史确认清单和导出仍会保留。`
                : "系统目前只支持整份文档重新解析，不能安全替换单页。新结果完成后会替换当前解析结果；历史确认清单和导出仍会保留。"}
            </p>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭重新解析"
            onClick={() => setParsingRerunDialogOpen(false)}
          >
            <Icon name="close" />
          </button>
        </header>
        {connectionMismatchRetryBlocked && (
          <InlineNotice title="后台执行器正在同步" tone="warning">
            当前连接配置尚未被兼容解析槽位确认。系统会自动同步；状态恢复前不能重复提交全文解析。
          </InlineNotice>
        )}
        <div className="reorganize-tree-options">
          <section>
            <div className="reorganize-tree-option-copy">
              <Icon name="refresh" />
              <div>
                <h3>Codex Agent 原文基线</h3>
                <p>
                  重新读取当前文件，建立原文结构、完整条目边界与需求属性判断；默认不执行原子化。Parser/Resolver 只提供来源事实和建议。
                </p>
              </div>
            </div>
            <button
              className="button secondary"
              type="button"
              disabled={
                !codexBaselineReady ||
                Boolean(busyAction) ||
                connectionMismatchRetryBlocked
              }
              onClick={() => void rerunParsingWithCodexAgent()}
            >
              启动 Agent 原文基线
            </button>
            {!codexBaselineReady && (
              <small>{isSaasAccountMode() ? SAAS_KIT_UNAVAILABLE_MESSAGE : "当前 Codex Agent 或本地来源读取能力尚未就绪。"}</small>
            )}
          </section>
          <section>
            <div className="reorganize-tree-option-copy">
              <Icon name="spark" />
              <div>
                <h3>
                  {visualRepairAvailable
                    ? `只重试 Qwen OCR 结构恢复降级的 ${visualRepairFailedPages.length} 页`
                    : `使用${visualRestorationMethodLabel}`}
                </h3>
                <p>
                  {visualRepairAvailable
                    ? `已就绪的 ${visualReceiptSummary?.readyPageCount || 0} 页从精确缓存复用；客户端不提交页码，降级范围由服务端正式回执决定。`
                    : `仅使用项目已授权、与当前来源精确绑定的${visualRestorationMethodLabel}；不会伪装成局部页替换。`}
                </p>
              </div>
            </div>
            <button
              className="button primary"
              type="button"
              disabled={
                (!visualRepairAvailable && !visualRestorationOverride) ||
                Boolean(busyAction) ||
                connectionMismatchRetryBlocked
              }
              onClick={() =>
                void (
                  visualRepairAvailable
                    ? retryFailedVisualPages()
                    : restoreSourceAndExtract()
                )
              }
            >
              {visualRepairAvailable
                ? `只重试降级的 ${visualRepairFailedPages.length} 页`
                : `使用${visualRestorationMethodLabel}解析整个文档`}
            </button>
            {!visualRepairAvailable && !visualRestorationOverride && (
              <small>当前来源没有可用且已授权的更高视觉能力。</small>
            )}
          </section>
        </div>
        <footer className="modal-actions">
          <button
            className="button ghost"
            type="button"
            onClick={() => setParsingRerunDialogOpen(false)}
          >
            取消
          </button>
        </footer>
      </Dialog>

      <Dialog
        open={classificationBatchOpen && !structureArtifactReadOnly}
        title="批量分类"
        onClose={() => setClassificationBatchOpen(false)}
        className="confirm-action-dialog entry-classification-batch-dialog"
      >
        <header>
          <div>
            <p className="section-label">清单整理</p>
            <h2>批量分类</h2>
            <p>只修改所选清单内容；标题仅用于显示，不会被分类。</p>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭批量分类"
            onClick={() => setClassificationBatchOpen(false)}
          >
            <Icon name="close" />
          </button>
        </header>
        {classificationMutationError && (
          <div
            ref={classificationBatchErrorRef}
            className="classification-dialog-error"
            role="alert"
            tabIndex={-1}
          >
            <InlineNotice title="批量分类未完成" tone="warning">
              {classificationMutationError}
              <button
                type="button"
                className="text-button"
                onClick={reloadEntryClassifications}
              >
                重新读取分类
              </button>
            </InlineNotice>
          </div>
        )}
        <div className="entry-classification-batch-body">
          <fieldset>
            <legend>应用范围</legend>
            {(
              [
                ["BRANCH", "当前分支"],
                ["FILTERED", "当前筛选"],
                ["ALL", "全部条目"],
              ] as const
            ).map(([scope, label]) => (
              <label key={scope}>
                <input
                  type="radio"
                  name="classification-batch-scope"
                  checked={classificationBatchScope === scope}
                  disabled={structureArtifactReadOnly}
                  onChange={() => setClassificationBatchScope(scope)}
                />
                <span>{label}</span>
                <strong>{classificationBatchScopeCounts[scope]} 项</strong>
              </label>
            ))}
          </fieldset>
          <div className="entry-classification-batch-types">
            {ENTRY_CLASSIFICATION_OPTIONS.map((option) => (
              <button
                key={option.value}
                type="button"
                aria-label={`批量设为${option.label}`}
                aria-pressed={classificationBatchType === option.value}
                disabled={structureArtifactReadOnly}
                onClick={() => setClassificationBatchType(option.value)}
              >
                <strong>{option.label}</strong>
                <small>{option.description}</small>
              </button>
            ))}
          </div>
        </div>
        <footer className="confirm-action-footer">
          <button
            className="button ghost"
            type="button"
            onClick={() => setClassificationBatchOpen(false)}
          >
            取消
          </button>
          <button
            className="button primary"
            type="button"
            disabled={
              structureArtifactReadOnly ||
              Boolean(classificationMutation) ||
              Boolean(atomizationMutation) ||
              classificationBatchWorkflowIds.length === 0
            }
            onClick={() => void applyClassificationBatch()}
          >
            应用到 {classificationBatchWorkflowIds.length} 项
          </button>
        </footer>
      </Dialog>

      <Dialog
        open={publicationDialogOpen && !structureArtifactReadOnly}
        title="生成确认清单"
        onClose={() => {
          if (!semanticPublicationMutation) {
            setPublicationDialogOpen(false);
          }
        }}
        className="confirm-action-dialog publication-confirmation-dialog"
      >
        <header>
          <div>
            <p className="section-label">清单整理</p>
            <h2>接受分类并生成确认清单</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭生成确认清单"
            disabled={semanticPublicationMutation}
            onClick={() => setPublicationDialogOpen(false)}
          >
            <Icon name="close" />
          </button>
        </header>
        {classificationMutationError && (
          <div
            ref={classificationPublicationErrorRef}
            className="classification-dialog-error"
            role="alert"
            tabIndex={-1}
          >
            <InlineNotice
              title={
                sessionId || acceptedNonSemanticPublicationRetry
                  ? "生成确认清单未完成"
                  : "接受分类未完成"
              }
              tone="warning"
            >
              {localDraftActivationRetry
                ? "确认清单已生成，但尚未设为当前版本。"
                : acceptedNonSemanticPublicationRetry
                  ? "确认清单尚未生成；当前分类结果仍保持已接受。"
                : classificationMutationError}
              {localDraftActivationRetry ? (
                <p>
                  当前分类结果和已生成的确认清单均已保留。重试只会再次设为当前版本，不会重复生成确认清单。
                </p>
              ) : acceptedNonSemanticPublicationRetry ? (
                <p>
                  当前分类结果已接受，可直接重试生成确认清单，无需重新分类。
                </p>
              ) : null}
              <button
                type="button"
                className="text-button"
                onClick={reloadEntryClassifications}
              >
                重新读取分类
              </button>
            </InlineNotice>
          </div>
        )}
        <div className="confirm-action-copy publication-confirmation-copy">
          {sessionId ? (
            <>
              <InlineNotice
                tone={
                  semanticPublicationAtomizationError ? "warning" : "info"
                }
                title={semanticPublicationScopeTitle}
              >
                {semanticPublicationScopeDescription}
              </InlineNotice>
              <p>
                技术需求默认进入分析；非技术内容默认保留；信息参考作为参考保留。
                原标题和全部正文仍完整保留。
              </p>
            </>
          ) : (
            <>
              <InlineNotice tone="info" title="本次只冻结当前已整理结果">
                当前 {entryClassification?.counts.total || 0}{" "}
                条内容已经完成三类整理。
                接受后才会生成待确认版本；本动作不调用原子化模型，也不会启动需求分析。
              </InlineNotice>
              <p>
                原标题和全部正文仍完整保留。本次只随版本保存三类结果，不改变当前分析资格；
                后续阶段再据此决定哪些内容需要分析。
              </p>
            </>
          )}
        </div>
        <footer className="confirm-action-footer">
          <button
            className="button ghost"
            type="button"
            disabled={semanticPublicationMutation}
            onClick={() => setPublicationDialogOpen(false)}
          >
            返回检查
          </button>
          <button
            className="button primary"
            type="button"
            disabled={
              structureArtifactReadOnly ||
              Boolean(busyAction) ||
              Boolean(classificationMutation) ||
              Boolean(atomizationMutation) ||
              semanticPublicationMutation ||
              semanticPublicationUnbound ||
              Boolean(semanticPublicationAtomizationError)
            }
            onClick={() => void acceptAndPrepareConfirmationList()}
          >
            {entryClassification?.snapshot.status === "ACCEPTED"
              ? localDraftActivationRetry
                ? "重试设为当前版本"
                : acceptedNonSemanticPublicationRetry
                ? "按已接受结果重试生成"
                : "继续生成确认清单"
              : "接受分类并生成清单"}
          </button>
        </footer>
      </Dialog>

      <Dialog
        open={reorganizeDialogOpen}
        title="重新整理结构"
        onClose={() => setReorganizeDialogOpen(false)}
        className="reorganize-tree-dialog"
      >
        <header className="modal-header">
          <div>
            <p className="section-label">整理结构</p>
            <h2>重新整理结构</h2>
            <p>刷新显示、本地重新生成和结构修正是三种不同操作。</p>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭重新整理"
            onClick={() => setReorganizeDialogOpen(false)}
          >
            <Icon name="close" />
          </button>
        </header>

        <div className="reorganize-tree-options">
          <section>
            <div className="reorganize-tree-option-copy">
              <Icon name="refresh" />
              <div>
                <h3>本地规则重新生成</h3>
                <p>
                  重新读取当前文件并生成原文事实、结构和本地规则参考，不调用语义
                  LLM；既有需求版本和导出不会删除。
                </p>
              </div>
            </div>
            <button
              className="button primary"
              type="button"
              disabled={Boolean(busyAction) || !localRulesReady}
              onClick={() => void regenerateWithLocalRules()}
            >
              <Icon name="play" />
              开始重新生成
            </button>
            {!localRulesReady && (
              <small>本地 PDF 解析能力尚未就绪，暂不能启动。</small>
            )}
          </section>

          <section>
            <div className="reorganize-tree-option-copy">
              <Icon name="wrench" />
              <div>
                <h3>手动调整当前结构</h3>
                <p>
                  对当前节点执行重挂、排序、续接合并或边界拆分；每次修改都会形成可撤销的新版本。
                </p>
              </div>
            </div>
            <button
              className="button secondary"
              type="button"
              disabled={!canEditStructure || Boolean(busyAction)}
              onClick={() => {
                setReorganizeDialogOpen(false);
                setStructureDrawerOpen(true);
              }}
            >
              调整当前节点
            </button>
            {!selectedStructureNode && (
              <small>先在左侧选择一个结构节点。</small>
            )}
          </section>

          <section>
            <div className="reorganize-tree-option-copy">
              <Icon name="spark" />
              <div>
                <h3>AI 优化当前结构</h3>
                <p>
                  可选能力。仅在项目已选择并连接可用模型时运行，不影响本地整理和人工修正。
                </p>
              </div>
            </div>
            <button
              className="button ghost"
              type="button"
              disabled={!canOptimizeStructure || Boolean(busyAction)}
              onClick={() => void optimizeStructure()}
            >
              启动 AI 优化
            </button>
            {!structureAiReady && (
              <small>当前项目未启用可用的结构优化模型。</small>
            )}
          </section>
        </div>

        <footer className="modal-actions">
          <button
            className="button ghost"
            type="button"
            onClick={() => setReorganizeDialogOpen(false)}
          >
            取消
          </button>
        </footer>
      </Dialog>

      {activeCheckpoint === "structure" &&
        structureDrawerOpen &&
        !structureArtifactReadOnly && (
        <div className="structure-drawer-layer">
          <button
            className="structure-drawer-backdrop"
            type="button"
            aria-label="关闭结构修正"
            onClick={() => setStructureDrawerOpen(false)}
          />
          <aside
            className="structure-correction-drawer"
            role="dialog"
            aria-modal="true"
            aria-label="结构修正"
          >
            <header>
              <div>
                <span>高级操作</span>
                <h2>修正结构</h2>
              </div>
              <button
                className="icon-button"
                type="button"
                aria-label="关闭"
                onClick={() => setStructureDrawerOpen(false)}
              >
                <Icon name="close" />
              </button>
            </header>
            <p>
              所有结构修正只创建新的结构版本，不修改原文事实。
              系统会先核对修改对象，避免把调整应用到过期内容。
            </p>
            <dl>
              <div>
                <dt>当前节点</dt>
                <dd>{selectedNode?.label || "未选择"}</dd>
              </div>
              {selectedStructureIds.length > 1 && (
                <div>
                  <dt>结构范围</dt>
                  <dd>{selectedStructureIds.length} 个连续内容节点</dd>
                </div>
              )}
              <div>
                <dt>影响范围</dt>
                <dd>
                  {selectedNode ? reviewItemCount(selectedNode) : 0} 个候选
                </dd>
              </div>
            </dl>
            <button
              className="button primary full"
              type="button"
              disabled={
                Boolean(busyAction) ||
                !canOptimizeStructure ||
                ["PENDING", "RUNNING"].includes(
                  String(
                    structureOptimization?.job?.status || "",
                  ).toUpperCase(),
                )
              }
              onClick={() => void optimizeStructure()}
            >
              <Icon name="spark" />
              AI 优化当前结构
            </button>
            <button
              className="button secondary full"
              type="button"
              disabled={
                Boolean(busyAction) ||
                asRecord(workbench.capabilities).can_undo_revision !== true
              }
              onClick={() =>
                void runAction(
                  "撤销结构修正",
                  () =>
                    api.undoStructure(
                      route.projectId,
                      route.documentId,
                      undoStructurePayload(),
                    ),
                  { refresh: "document" },
                )
              }
            >
              <Icon name="undo" />
              撤销最新修正
            </button>
            <section className="manual-structure-tools">
              <header>
                <div>
                  <span>人工精确修正</span>
                  <strong>一次只执行一种结构变更</strong>
                </div>
                <small>提交前会校验来源与当前版本</small>
              </header>
              <div
                className="structure-action-tabs"
                role="tablist"
                aria-label="结构修正类型"
              >
                {[
                  ["reparent", "重挂"],
                  ["reorder", "排序"],
                  ["merge", "续接合并"],
                  ["split", "边界拆分"],
                ].map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    role="tab"
                    disabled={
                      selectedStructureIds.length > 1 && value !== "reparent"
                    }
                    title={
                      selectedStructureIds.length > 1 && value !== "reparent"
                        ? "连续内容节点只能整体重挂；其他修正需先选择单一结构节点。"
                        : undefined
                    }
                    aria-selected={structureAction === value}
                    className={structureAction === value ? "is-active" : ""}
                    onClick={() =>
                      setStructureAction(
                        value as "reparent" | "reorder" | "merge" | "split",
                      )
                    }
                  >
                    {label}
                  </button>
                ))}
              </div>

              {structureAction === "reparent" && (
                <div className="structure-action-form" role="tabpanel">
                  <label htmlFor="structure-parent-search">
                    <span>新的父级</span>
                    <input
                      id="structure-parent-search"
                      type="search"
                      role="combobox"
                      aria-label="搜索新的父级"
                      aria-autocomplete="list"
                      aria-expanded={structureParentPickerOpen}
                      aria-controls="structure-parent-search-results"
                      aria-activedescendant={
                        structureParentPickerOpen &&
                        structureParentSearchResult.visible[
                          structureParentActiveIndex
                        ]
                          ? `structure-parent-option-${String(
                              structureParentSearchResult.visible[
                                structureParentActiveIndex
                              ].id,
                            )}`
                          : undefined
                      }
                      value={structureParentQuery}
                      placeholder="搜索章节、条款或完整路径"
                      autoComplete="off"
                      onFocus={() => setStructureParentPickerOpen(true)}
                      onChange={(event) => {
                        setStructureParentQuery(event.target.value);
                        setStructureTargetId("");
                        setStructureParentActiveIndex(0);
                        setStructureParentPickerOpen(true);
                      }}
                      onKeyDown={handleStructureParentSearchKeyDown}
                    />
                  </label>
                  {structureParentPickerOpen && (
                    <div
                      id="structure-parent-search-results"
                      className="project-rail-list"
                      role="listbox"
                      aria-label="可选父级"
                    >
                      {structureParentSearchResult.visible.map(
                        (node, index) => {
                          const nodeId = String(node.id || "");
                          const path =
                            structureParentPathById.get(nodeId) ||
                            String(node.label || "未命名节点");
                          return (
                            <button
                              id={`structure-parent-option-${nodeId}`}
                              key={nodeId}
                              className={cx(
                                "project-rail-item",
                                index === structureParentActiveIndex &&
                                  "is-active",
                              )}
                              type="button"
                              role="option"
                              tabIndex={-1}
                              aria-label={path}
                              aria-selected={structureTargetId === nodeId}
                              aria-setsize={structureParentSearchResult.total}
                              aria-posinset={index + 1}
                              title={path}
                              onMouseDown={(event) => event.preventDefault()}
                              onClick={() => chooseStructureParent(node)}
                            >
                              <span
                                className="project-monogram"
                                aria-hidden="true"
                              >
                                §
                              </span>
                              <span>
                                <strong>{path}</strong>
                                <small>
                                  {index + 1} /{" "}
                                  {structureParentSearchResult.total}
                                </small>
                              </span>
                            </button>
                          );
                        },
                      )}
                      {structureParentSearchResult.visible.length < 1 && (
                        <small role="status">
                          没有匹配且允许承载子节点的父级。
                        </small>
                      )}
                      {structureParentSearchResult.total >
                        structureParentSearchResult.visible.length && (
                        <small>
                          仅显示前 {structureParentSearchResult.visible.length}
                          项；继续输入章节号、标题或祖先路径可缩小范围。
                        </small>
                      )}
                    </div>
                  )}
                  {selectedStructureTargetPath && (
                    <small role="status">
                      已选择：{selectedStructureTargetPath}
                    </small>
                  )}
                  <button
                    className="button primary full"
                    type="button"
                    disabled={Boolean(reparentDisabledReason)}
                    aria-describedby={
                      reparentDisabledReason
                        ? "structure-reparent-reason"
                        : undefined
                    }
                    onClick={() => void reparentSelectedStructure()}
                  >
                    确认重挂
                  </button>
                  {reparentDisabledReason && (
                    <small id="structure-reparent-reason" role="status">
                      {reparentDisabledReason}
                    </small>
                  )}
                </div>
              )}

              {structureAction === "reorder" && (
                <div className="structure-action-form" role="tabpanel">
                  <p>仅调整当前节点在同级条款中的逻辑顺序，不改动原文位置。</p>
                  <div className="structure-order-actions">
                    <button
                      className="button secondary"
                      type="button"
                      disabled={
                        Boolean(busyAction) ||
                        !currentParentId ||
                        selectedSiblingIds.indexOf(selectedStructureId) <= 0
                      }
                      onClick={() => void reorderSelectedStructure(-1)}
                    >
                      <Icon name="chevron" className="icon-rotate-up" />
                      上移
                    </button>
                    <button
                      className="button secondary"
                      type="button"
                      disabled={
                        Boolean(busyAction) ||
                        !currentParentId ||
                        selectedSiblingIds.indexOf(selectedStructureId) < 0 ||
                        selectedSiblingIds.indexOf(selectedStructureId) >=
                          selectedSiblingIds.length - 1
                      }
                      onClick={() => void reorderSelectedStructure(1)}
                    >
                      <Icon name="chevron-down" />
                      下移
                    </button>
                  </div>
                </div>
              )}

              {structureAction === "merge" && (
                <div className="structure-action-form" role="tabpanel">
                  <label>
                    <span>作为续接内容合并</span>
                    <select
                      value={mergeTargetId}
                      onChange={(event) => setMergeTargetId(event.target.value)}
                    >
                      <option value="">选择同级节点</option>
                      {mergeCandidates.map((node) => (
                        <option key={String(node.id)} value={String(node.id)}>
                          {String(node.label || "未命名节点")}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    className="button primary full"
                    type="button"
                    disabled={Boolean(busyAction) || !mergeTargetId}
                    onClick={() => void mergeSelectedStructure()}
                  >
                    确认续接合并
                  </button>
                </div>
              )}

              {structureAction === "split" && (
                <div className="structure-action-form" role="tabpanel">
                  {canBoundarySplit ? (
                    <>
                      <label>
                        <span>拆分位置（字符偏移）</span>
                        <input
                          type="number"
                          min={boundaryStart + 1}
                          max={boundaryEnd - 1}
                          value={splitOffset}
                          onChange={(event) => setSplitOffset(event.target.value)}
                        />
                      </label>
                      <small>
                        可拆范围 {boundaryStart + 1}–{boundaryEnd - 1}；仅作用于
                        当前唯一、可验证的来源跨度。
                      </small>
                      <button
                        className="button primary full"
                        type="button"
                        disabled={
                          Boolean(busyAction) ||
                          Number(splitOffset) <= boundaryStart ||
                          Number(splitOffset) >= boundaryEnd
                        }
                        onClick={() => void splitSelectedStructure()}
                      >
                        确认边界拆分
                      </button>
                    </>
                  ) : (
                    <InlineNotice title="当前节点不能安全拆分" tone="warning">
                      只有具备单一、完整来源跨度的节点才开放边界拆分，避免猜测原文边界。
                    </InlineNotice>
                  )}
                </div>
              )}
            </section>
          </aside>
        </div>
      )}
    </section>
  );
}

export function RequirementTreePanel() {
  const { route, updateRoute, selectedDocument } = useWorkspace();
  if (route.mode === "review") {
    return (
      <section className="requirement-tree-page requirement-tree-review-page">
        <header className="requirement-tree-commandbar">
          <div className="requirement-tree-title">
            <span>需求提取</span>
            <strong title={selectedDocument?.file_name || "确认清单"}>
              {selectedDocument?.file_name || "确认清单"}
            </strong>
          </div>
          {route.reviewCase ? <span className="status-badge">原文清单 · 候选核对</span> : <RequirementTreeStageRail
            value="review"
            reviewAvailable
            onChange={(checkpoint) =>
              updateRoute({
                task: "extraction",
                mode: checkpoint === "review" ? "review" : "organize",
                checkpoint,
                requirementSetId:
                  checkpoint === "review" ? route.requirementSetId : "",
              })
            }
          />}
        </header>
        <RequirementMatrixWorkbench lens="review" />
      </section>
    );
  }
  return <RequirementTreeOrganizePanel />;
}
