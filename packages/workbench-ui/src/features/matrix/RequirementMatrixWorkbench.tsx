import { CandidateReviewInspector } from "../extraction/CandidateReviewInspector";
import { CLASSIFICATION_OPTIONS, classificationLabels } from "./classificationFacets";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MutableRefObject,
} from "react";
import { ApiError, api, asArray, asRecord, request } from "../../shared/api/client";
import type {
  AiProfile,
  JsonRecord,
  RequirementAnalysisExecutionCounts,
  RequirementAnalysisNodeExecution,
  RequirementAnalysisNodeExecutionStatus,
  RequirementAnalysisScope,
  RequirementAnalysisScopePreview,
  RequirementAnalysisScopeRequest,
  RequirementMatrixLogicalTable,
  RequirementMatrixNode,
  RequirementMatrixNodeDetail,
  RequirementMatrixNodeDisposition,
  RequirementMatrixSnapshot,
  RequirementSet,
  RequirementSetLockReadiness,
  RequirementSetLockReadinessItem,
} from "../../shared/api/contracts";
import { normalizeAiProfiles } from "../../shared/api/normalize";
import { useScopedUiState } from "../../shared/hooks/useScopedUiState";
import { compactText, cx, truncate } from "../../shared/lib/format";
import { registerNavigationGuard } from "../../shared/navigation";
import {
  EmptyState,
  ErrorBlock,
  InlineNotice,
  LoadingBlock,
  StatusBadge,
} from "../../shared/ui/Feedback";
import { Dialog } from "../../shared/ui/Dialog";
import { Icon } from "../../shared/ui/Icon";
import { normalizeRestorationPage } from "../restoration/restorationModel";
import { useWorkspace } from "../workspace/WorkspaceContext";
import { MatrixSourceContent } from "./MatrixSourceContent";
import {
  firstMatrixSelectionId,
  flattenRequirementMatrix,
  initialMatrixExpandedIds,
  isAiAnalysisActiveStatus,
  matrixAncestorIds,
  matrixAnalysisCapable,
  matrixAnalysisEligible,
  matrixAnalysisStatus,
  matrixAnalysisTone,
  matrixConclusionContract,
  matrixDisplayPreview,
  matrixHierarchyLabel,
  matrixNeedsAnalysis,
  matrixNodeMatchesView,
  matchingCandidateExportIds,
  matrixNodeLabel,
  matrixRowAccessibleName,
  matrixSourceNumber,
  requirementMatrixMatchCounts,
  suggestedMatrixExpandedIds,
  type RequirementMatrixFilter,
  type RequirementMatrixLens,
  type RequirementMatrixRow,
} from "./requirementMatrixModel";

const MATRIX_ROW_HEIGHT = 64;
const MATRIX_OVERSCAN = 8;
const EMPTY_EXPANDED: string[] | null = null;
const EMPTY_ANALYSIS_SELECTION: string[] = [];
const COMPACT_MATRIX_PANE_WIDTH = 720;
const COMPACT_REVIEW_WORKBENCH_WIDTH = 1160;

function useMeasuredElementWidth<T extends HTMLElement>() {
  const [width, setWidth] = useState(0);
  const elementRef = useRef<T | null>(null);
  const observerRef = useRef<ResizeObserver | null>(null);

  const setElement = useCallback((element: T | null) => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    elementRef.current = element;
    if (!element) return;

    setWidth(element.getBoundingClientRect().width);
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      setWidth(
        entry?.contentRect.width ?? element.getBoundingClientRect().width,
      );
    });
    observer.observe(element);
    observerRef.current = observer;
  }, []);

  useEffect(
    () => () => {
      observerRef.current?.disconnect();
    },
    [],
  );

  return [setElement, width, elementRef] as const;
}

type DetailPane = "source" | "pdf" | "inspector";
type BatchScope = "branch" | "filter" | "all";
type MatrixWorkflowDialog = "lock" | "unlock" | "analysis" | "stop" | null;
type MatrixWorkflowPending = "lock" | "unlock" | "analysis" | "stop" | null;
type AnalysisRetryKind = "" | "preview" | "start";
type SaveState =
  "" | "未保存" | "保存中…" | "保存失败，请重试" | `已保存 ${string}`;
type MatrixMutationKind = "single" | "batch";
export type MatrixMutationRequest = {
  generation: number;
  kind: MatrixMutationKind;
  uiScope: string;
};

function nonNegativeInteger(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(Math.trunc(parsed), 0) : 0;
}

function optionalNonNegativeInteger(...values: unknown[]): number | null {
  for (const value of values) {
    if (value === null || value === undefined || value === "") continue;
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed >= 0) return Math.trunc(parsed);
  }
  return null;
}

function lockReadinessItemMessage(
  item: RequirementSetLockReadinessItem,
): string {
  const recoveryMessages: Record<string, string> = {
    REQUIREMENT_SET_SELECTION_CHANGED:
      "当前确认清单已不是活动版本，请关闭对话框并刷新后重新选择。",
    REQUIREMENT_MATRIX_REQUIRES_REEXTRACTION:
      "当前需求矩阵无法验证，请返回需求提取重新提取后再锁定。",
    REQUIREMENT_MATRIX_COUNT_MISMATCH:
      "清单统计与冻结分析范围不一致，请重新读取摘要；若仍出现，请重新提取。",
    SOURCE_CLASSIFICATION_CONFLICT:
      "同一原文条目的内容分类不一致，建议返回清单整理核对；你也可以带风险继续。",
  };
  if (typeof item === "string") {
    const message = item.trim();
    const recovery = recoveryMessages[message.toUpperCase()];
    if (recovery) return recovery;
    if (!message || /^[A-Z0-9_.:-]+$/.test(message)) {
      return "清单存在需要处理的问题，请返回清单检查后重试。";
    }
    return message;
  }
  const code = String(item.code || "").trim().toUpperCase();
  const message = compactText(item.message || item.detail, "");
  if (message && !/^[A-Z0-9_.:-]+$/.test(message)) return message;
  return (
    recoveryMessages[code] ||
    "清单存在需要处理的问题，请返回清单检查后重试。"
  );
}

function workflowErrorMessage(
  cause: unknown,
  action: "lock" | "unlock" | "readiness",
): { message: string; conflict: boolean } {
  if (cause instanceof ApiError && cause.status === 409) {
    return {
      message:
        action === "unlock"
          ? "当前清单已发生变化。请关闭提示并重新检查后再解锁。"
          : "清单内容已发生变化，请刷新摘要后重新核对锁定范围。",
      conflict: true,
    };
  }
  if (action === "readiness") {
    return {
      message: "暂时无法读取锁定摘要。请检查连接后重新读取。",
      conflict: false,
    };
  }
  return {
    message:
      action === "unlock"
        ? "清单未能解锁，当前已锁定清单没有改变。请稍后重试。"
        : "清单未能锁定，当前内容没有改变。请重新读取摘要后重试。",
    conflict: false,
  };
}

function apiErrorCode(cause: unknown): string {
  if (!(cause instanceof ApiError)) return "";
  const payload = asRecord(cause.payload);
  const detail = asRecord(payload.detail);
  return String(detail.code || payload.code || "").trim().toLowerCase();
}

function aiStageLabel(stage: string): string {
  return (
    {
      starting: "正在启动",
      queued: "等待开始",
      pending: "等待开始",
      screening: "正在筛选",
      assessment: "正在分析",
      publishing: "正在发布结果",
      cancelling: "正在停止",
      cancelled: "已停止",
      completed: "已完成",
      completed_with_warnings: "部分项目未完成",
      succeeded: "已完成",
      failed: "运行失败",
    }[stage.toLowerCase()] || "任务状态更新中"
  );
}

function analysisScopeLabel(scope: RequirementAnalysisScope): string {
  return {
    CURRENT: "当前项",
    SELECTED: "所选项",
    ALL: "全部分析项",
    FAILED_ONLY: "仅重试失败项",
  }[scope];
}

const ANALYSIS_NODE_EXECUTION_STATUSES = new Set<RequirementAnalysisNodeExecutionStatus>([
  "WAITING",
  "RUNNING",
  "PUBLISHING",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
]);

function strictNonNegativeInteger(
  record: JsonRecord,
  key: string,
): number | null {
  if (!Object.prototype.hasOwnProperty.call(record, key)) return null;
  const value = record[key];
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value
    : null;
}

function normalizeAnalysisExecutionCounts(
  value: unknown,
): RequirementAnalysisExecutionCounts | null {
  const record = asRecord(value);
  const total = strictNonNegativeInteger(record, "total");
  const waiting = strictNonNegativeInteger(record, "waiting");
  const running = strictNonNegativeInteger(record, "running");
  const publishing = strictNonNegativeInteger(record, "publishing");
  const succeeded = strictNonNegativeInteger(record, "succeeded");
  const failed = strictNonNegativeInteger(record, "failed");
  const cancelled = strictNonNegativeInteger(record, "cancelled");
  const retryableFailed = strictNonNegativeInteger(record, "retryable_failed");
  const settled = strictNonNegativeInteger(record, "settled");
  if (
    total === null ||
    waiting === null ||
    running === null ||
    publishing === null ||
    succeeded === null ||
    failed === null ||
    cancelled === null ||
    retryableFailed === null ||
    settled === null ||
    settled !== succeeded + failed + cancelled ||
    total !== waiting + running + publishing + settled ||
    retryableFailed > failed
  ) {
    return null;
  }
  return {
    ...record,
    total,
    waiting,
    running,
    publishing,
    succeeded,
    failed,
    cancelled,
    retryable_failed: retryableFailed,
    settled,
  } as RequirementAnalysisExecutionCounts;
}

function nullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function normalizeAnalysisNodeExecutions(
  value: unknown,
  expectedJobId: string,
): RequirementAnalysisNodeExecution[] | null {
  if (!Array.isArray(value)) return null;
  const normalized: RequirementAnalysisNodeExecution[] = [];
  for (const candidate of value) {
    const item = asRecord(candidate);
    const status = String(item.status || "").toUpperCase() as
      RequirementAnalysisNodeExecutionStatus | "";
    const sourceOrder = item.source_order;
    const attemptNo = item.attempt_no;
    if (
      !String(item.execution_id || "") ||
      !String(item.target_id || "") ||
      String(item.job_id || "") !== expectedJobId ||
      !String(item.node_id || "") ||
      !String(item.requirement_id || "") ||
      typeof item.requirement_code !== "string" ||
      !Number.isSafeInteger(sourceOrder) ||
      Number(sourceOrder) < 0 ||
      !Number.isSafeInteger(attemptNo) ||
      Number(attemptNo) < 0 ||
      !ANALYSIS_NODE_EXECUTION_STATUSES.has(
        status as RequirementAnalysisNodeExecutionStatus,
      ) ||
      typeof item.stage !== "string" ||
      typeof item.published !== "boolean" ||
      typeof item.retryable !== "boolean" ||
      !nullableString(item.error_code) ||
      !nullableString(item.error_message) ||
      !nullableString(item.started_at) ||
      !nullableString(item.finished_at)
    ) {
      return null;
    }
    normalized.push({
      ...item,
      execution_id: String(item.execution_id),
      target_id: String(item.target_id),
      job_id: expectedJobId,
      node_id: String(item.node_id),
      requirement_id: String(item.requirement_id),
      requirement_code: item.requirement_code,
      source_order: Number(sourceOrder),
      attempt_no: Number(attemptNo),
      status: status as RequirementAnalysisNodeExecutionStatus,
      stage: item.stage,
      published: item.published,
      retryable: item.retryable,
      error_code: item.error_code,
      error_message: item.error_message,
      started_at: item.started_at,
      finished_at: item.finished_at,
    } as RequirementAnalysisNodeExecution);
  }
  return normalized;
}

function analysisExecutionsMatchCounts(
  executions: readonly RequirementAnalysisNodeExecution[],
  counts: RequirementAnalysisExecutionCounts,
): boolean {
  if (executions.length !== counts.total) return false;
  const statusCounts = {
    WAITING: 0,
    RUNNING: 0,
    PUBLISHING: 0,
    SUCCEEDED: 0,
    FAILED: 0,
    CANCELLED: 0,
  };
  let retryableFailed = 0;
  const nodeIds = new Set<string>();
  for (const [index, execution] of executions.entries()) {
    if (
      nodeIds.has(execution.node_id) ||
      (index > 0 && executions[index - 1].source_order > execution.source_order) ||
      execution.published !== (execution.status === "SUCCEEDED")
    ) {
      return false;
    }
    nodeIds.add(execution.node_id);
    statusCounts[execution.status] += 1;
    if (execution.status === "FAILED" && execution.retryable) {
      retryableFailed += 1;
    }
  }
  return (
    statusCounts.WAITING === counts.waiting &&
    statusCounts.RUNNING === counts.running &&
    statusCounts.PUBLISHING === counts.publishing &&
    statusCounts.SUCCEEDED === counts.succeeded &&
    statusCounts.FAILED === counts.failed &&
    statusCounts.CANCELLED === counts.cancelled &&
    retryableFailed === counts.retryable_failed
  );
}

function analysisExecutionStatusLabel(
  execution: RequirementAnalysisNodeExecution | null,
  fallback: string,
): string {
  if (!execution) return fallback;
  if (execution.status === "WAITING") return "等待分析";
  if (["RUNNING", "PUBLISHING"].includes(execution.status)) return "分析中";
  if (execution.status === "FAILED") return "分析失败";
  if (execution.status === "CANCELLED") return "未完成";
  return execution.published ? "AI 已分析" : "未完成";
}

function analysisScopePreviewMatchesRequest(
  preview: RequirementAnalysisScopePreview,
  request: RequirementAnalysisScopeRequest,
): boolean {
  const record = preview as unknown as JsonRecord;
  const requirementSetVersion = strictNonNegativeInteger(
    record,
    "requirement_set_version",
  );
  const targetCount = strictNonNegativeInteger(record, "target_count");
  const skippedCount = strictNonNegativeInteger(record, "skipped_count");
  if (
    record.command_id !== request.command_id ||
    record.requirement_set_id !== request.requirement_set_id ||
    record.tree_sha256 !== request.expected_tree_sha256 ||
    record.scope !== request.scope ||
    typeof record.scope_sha256 !== "string" ||
    !record.scope_sha256 ||
    !Array.isArray(record.requested_node_ids) ||
    !Array.isArray(record.target_node_ids) ||
    !Array.isArray(record.target_requirement_ids) ||
    !Array.isArray(record.target_examples) ||
    !Array.isArray(record.skipped_examples) ||
    typeof record.skipped_reason_counts !== "object" ||
    record.skipped_reason_counts === null ||
    Array.isArray(record.skipped_reason_counts) ||
    requirementSetVersion === null ||
    strictNonNegativeInteger(record, "requested_count") === null ||
    targetCount === null ||
    skippedCount === null ||
    record.target_node_ids.length !== targetCount ||
    record.target_requirement_ids.length !== targetCount
  ) {
    return false;
  }
  if (request.schema_version === "requirement-analysis-scope-request/v1") {
    return (
      record.schema_version === "requirement-analysis-scope-preview/v1" &&
      record.publication_mode === "ATOMIC_BATCH"
    );
  }
  if (
    record.schema_version !== "requirement-analysis-scope-preview/v2" ||
    record.publication_mode !== "PER_NODE_RECOVERABLE" ||
    strictNonNegativeInteger(record, "source_failed_count") === null ||
    strictNonNegativeInteger(record, "already_current_count") === null ||
    strictNonNegativeInteger(record, "retry_target_count") === null ||
    strictNonNegativeInteger(record, "retry_already_current_count") === null ||
    strictNonNegativeInteger(record, "retry_skipped_count") === null
  ) {
    return false;
  }
  if (request.scope !== "FAILED_ONLY") return true;
  const failedSnapshotSha256 = String(record.failed_snapshot_sha256 || "");
  const sourceFailedCount = strictNonNegativeInteger(
    record,
    "source_failed_count",
  );
  const alreadyCurrentCount = strictNonNegativeInteger(
    record,
    "already_current_count",
  );
  const retryTargetCount = strictNonNegativeInteger(
    record,
    "retry_target_count",
  );
  const retryAlreadyCurrentCount = strictNonNegativeInteger(
    record,
    "retry_already_current_count",
  );
  const retrySkippedCount = strictNonNegativeInteger(
    record,
    "retry_skipped_count",
  );
  return (
    record.source_job_id === request.source_job_id &&
    /^[a-f0-9]{64}$/i.test(failedSnapshotSha256) &&
    sourceFailedCount !== null &&
    alreadyCurrentCount !== null &&
    retryTargetCount === targetCount &&
    retryAlreadyCurrentCount === alreadyCurrentCount &&
    retrySkippedCount !== null &&
    sourceFailedCount ===
      retryTargetCount + retryAlreadyCurrentCount + retrySkippedCount
  );
}

function analysisScopeCommandId(): string {
  const randomUuid = globalThis.crypto?.randomUUID?.();
  if (randomUuid) return randomUuid;
  return `analysis-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function analysisSkipReasonLabel(reason: string): string {
  return (
    {
      SYSTEM_NON_BUSINESS: "系统内容不进入分析",
      NOT_VISIBLE: "当前不在可分析范围",
      NOT_MARKED_FOR_ANALYSIS: "已设为仅保留或排除分析",
      NOT_ELIGIBLE: "章节、标题或上下文不单独分析",
      MISSING_REQUIREMENT_BINDING: "未绑定可分析需求",
    }[reason.toUpperCase()] || "不满足当前分析条件"
  );
}

function logicalTableContainsRecord(
  table: RequirementMatrixLogicalTable,
  recordId: string,
): boolean {
  if (!recordId) return false;
  const projection = asRecord(table.projection);
  return asArray<JsonRecord>(projection.records).some(
    (record) =>
      String(record.record_id || record.id || "").trim() === recordId,
  );
}

function logicalTableSegmentFocusRegions(
  node: RequirementMatrixNode | null,
  logicalTables: readonly RequirementMatrixLogicalTable[],
): JsonRecord[] {
  if (node?.display_role !== "TABLE" || !node.logical_table_ids.length) {
    return [];
  }
  const selectedLogicalTableIds = new Set(node.logical_table_ids);
  const seen = new Set<string>();
  const regions: JsonRecord[] = [];
  logicalTables.forEach((logicalTable) => {
    if (!selectedLogicalTableIds.has(logicalTable.logical_table_id)) return;
    asArray<JsonRecord>(logicalTable.structured_segments).forEach(
      (segment, segmentIndex) => {
        const location = asRecord(segment.location);
        const geometry = asRecord(segment.geometry);
        const page = Number(
          location.page_number ||
            segment.page_number ||
            segment.page_start ||
            node.page_start ||
            0,
        );
        const bbox = asArray<unknown>(
          location.bbox || segment.bbox || geometry.bbox,
        ).map(Number);
        if (
          page < 1 ||
          bbox.length !== 4 ||
          !bbox.every(Number.isFinite) ||
          bbox[2] <= bbox[0] ||
          bbox[3] <= bbox[1]
        ) {
          return;
        }
        const segmentId = String(
          segment.id ||
            segment.table_id ||
            `${logicalTable.logical_table_id}-${segmentIndex + 1}`,
        );
        const identity = `${page}:${bbox.join(":")}:${segmentId}`;
        if (seen.has(identity)) return;
        seen.add(identity);
        regions.push({
          id: `matrix-table-segment-${segmentId}`,
          page_number: page,
          bbox,
          role: "TABLE",
          precision: "TABLE_SEGMENT_EXACT",
        });
      },
    );
  });
  return regions;
}

function focusBbox(value: unknown): number[] {
  const bbox = asArray<unknown>(value).map(Number);
  return bbox.length === 4 &&
    bbox.every(Number.isFinite) &&
    bbox[2] > bbox[0] &&
    bbox[3] > bbox[1]
    ? bbox
    : [];
}

function mergedFocusBbox(boxes: readonly number[][]): number[] {
  if (!boxes.length) return [];
  return [
    Math.min(...boxes.map((bbox) => bbox[0])),
    Math.min(...boxes.map((bbox) => bbox[1])),
    Math.max(...boxes.map((bbox) => bbox[2])),
    Math.max(...boxes.map((bbox) => bbox[3])),
  ];
}

/**
 * Resolve a frozen Matrix record back to the physical continuation segment that
 * owns its row. The Requirement Set's broad page range remains immutable; this
 * is a read-only presentation projection proven by row/cell bindings.
 */
function logicalTableRecordFocusRegion(
  node: RequirementMatrixNode | null,
  logicalTables: readonly RequirementMatrixLogicalTable[],
): JsonRecord | null {
  if (
    !node?.table_record_id ||
    !["TABLE_RECORD", "ATOMIC_REQUIREMENT"].includes(node.display_role)
  ) {
    return null;
  }
  const logicalIds = new Set(node.logical_table_ids);
  const logicalTable =
    logicalTables.find((table) => logicalIds.has(table.logical_table_id)) ||
    logicalTables.find((table) => table.node_ids.includes(node.node_id));
  if (!logicalTable) return null;

  const projection = asRecord(logicalTable.projection);
  const record = asArray<JsonRecord>(projection.records).find(
    (candidate) =>
      String(candidate.record_id || candidate.id || "").trim() ===
      node.table_record_id,
  );
  if (!record) return null;

  const projectionSegments = [
    ...asArray<JsonRecord>(logicalTable.structured_segments),
    ...asArray<JsonRecord>(projection.tables),
    ...asArray<JsonRecord>(projection.table_segments),
  ];
  const segments = new Map<string, JsonRecord>();
  projectionSegments.forEach((segment) => {
    const segmentId = String(
      segment.id || segment.table_id || segment.source_node_id || "",
    ).trim();
    if (segmentId && !segments.has(segmentId)) segments.set(segmentId, segment);
  });

  for (const rowBinding of asArray<JsonRecord>(record.row_bindings)) {
    const tableId = String(rowBinding.table_id || "").trim();
    const segment = segments.get(tableId);
    if (!segment) continue;
    const segmentLocation = asRecord(segment.location);
    const segmentPage = Number(
      segmentLocation.page_number ||
        segment.page_number ||
        segment.page_start ||
        0,
    );
    const boundCellIds = new Set(
      asArray<unknown>(rowBinding.cell_ids).map(String).filter(Boolean),
    );
    const rowIndex = Number(rowBinding.row_index);
    const segmentCells = asArray<JsonRecord>(segment.cells);
    let boundCells = segmentCells.filter((cell) =>
      boundCellIds.has(String(cell.id || cell.cell_id || "")),
    );
    if (!boundCells.length && Number.isInteger(rowIndex) && rowIndex >= 0) {
      boundCells = segmentCells.filter(
        (cell) => Number(cell.row ?? cell.row_index) === rowIndex,
      );
    }

    const pageCellBoxes = new Map<number, number[][]>();
    boundCells.forEach((cell) => {
      const cellLocation = asRecord(cell.location);
      const page = Number(
        cellLocation.page_number ||
          cell.page_number ||
          cell.page ||
          segmentPage ||
          0,
      );
      const bbox = focusBbox(cellLocation.bbox || cell.bbox);
      if (page < 1 || !bbox.length) return;
      pageCellBoxes.set(page, [...(pageCellBoxes.get(page) || []), bbox]);
    });
    const exactPage =
      (segmentPage > 0 && pageCellBoxes.has(segmentPage) && segmentPage) ||
      pageCellBoxes.keys().next().value ||
      0;
    if (exactPage > 0) {
      const bbox = mergedFocusBbox(pageCellBoxes.get(exactPage) || []);
      if (bbox.length) {
        return {
          id: `matrix-table-record-${node.table_record_id}`,
          page_number: exactPage,
          bbox,
          role: "TABLE_RECORD",
          precision: "TABLE_RECORD_ROW_EXACT",
        };
      }
    }

    const segmentBbox = focusBbox(
      segmentLocation.bbox || segment.bbox || asRecord(segment.geometry).bbox,
    );
    if (segmentPage > 0 && segmentBbox.length) {
      return {
        id: `matrix-table-record-segment-${tableId}`,
        page_number: segmentPage,
        bbox: segmentBbox,
        role: "TABLE_RECORD",
        precision: "TABLE_SEGMENT_EXACT",
      };
    }
  }
  return null;
}

const savedTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const primaryFilterOptions: Array<{
  value: RequirementMatrixFilter;
  label: string;
}> = [
  { value: "all", label: "全部内容" },
  { value: "analysis", label: "仅分析项" },
  { value: "pending", label: "待处理" },
  { value: "attention", label: "不符合与需关注" },
];

const secondaryFilterOptions: Array<{
  value: RequirementMatrixFilter;
  label: string;
}> = [
  { value: "tables", label: "表格" },
  { value: "excluded", label: "已排除" },
];

const dispositionOptions: Array<{
  value: RequirementMatrixNodeDisposition;
  label: string;
  description: string;
}> = [
  {
    value: "ANALYZE",
    label: "需分析",
    description: "进入 AI 分析和人工结论范围。",
  },
  {
    value: "RETAIN",
    label: "仅保留",
    description: "保留层级、正文与导出，不单独判定。",
  },
  {
    value: "EXCLUDE",
    label: "排除",
    description: "保留来源与版本记录，可在“已排除”筛选中查看；不进入分析。",
  },
];

const decisionOptions = [
  ["ACCEPT", "接受"],
  ["DEVIATION_ACCEPT", "偏差接受"],
  ["NEED_CLARIFICATION", "需澄清"],
  ["REJECT", "拒绝"],
] as const;

function setId(set: RequirementSet): string {
  return String(set.id || set.requirement_set_id || "");
}

function requirementSetLabel(set: RequirementSet): string {
  const explicitLabel = compactText(set.version_label, "");
  if (explicitLabel) return explicitLabel;
  const version = compactText(set.version ?? set.version_number, "");
  return version ? `版本 ${version}` : "未标号版本";
}

function derivedRequirementSetId(payload: JsonRecord): string {
  const requirementSet = asRecord(payload.requirement_set);
  return compactText(
    requirementSet.id || requirementSet.requirement_set_id,
    "",
  );
}

function impactStaleResultNodeIds(payload: unknown): string[] {
  const response = asRecord(payload);
  const receipt = asRecord(response.impact_receipt);
  return Array.from(
    new Set(
      asArray<unknown>(receipt.stale_result_node_ids)
        .map((value) => String(value || "").trim())
        .filter(Boolean),
    ),
  );
}

function objectText(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return compactText(value, "");
  }
  const record = value as JsonRecord;
  return compactText(
    record.label ||
      record.summary ||
      record.decision ||
      record.status ||
      record.reason ||
      record.rationale,
    "",
  );
}

function replaceMatrixNode(
  matrix: RequirementMatrixSnapshot,
  node: RequirementMatrixNode,
  counts?: JsonRecord,
): RequirementMatrixSnapshot {
  return {
    ...matrix,
    nodes: matrix.nodes.map((candidate) =>
      candidate.node_id === node.node_id ? node : candidate,
    ),
    counts: counts
      ? ({ ...matrix.counts, ...counts } as RequirementMatrixSnapshot["counts"])
      : matrix.counts,
  };
}

function replaceMatrixNodes(
  matrix: RequirementMatrixSnapshot,
  nodes: readonly RequirementMatrixNode[],
  counts?: JsonRecord,
): RequirementMatrixSnapshot {
  const replacements = new Map(nodes.map((node) => [node.node_id, node]));
  return {
    ...matrix,
    nodes: matrix.nodes.map((candidate) =>
      replacements.get(candidate.node_id) || candidate,
    ),
    counts: counts
      ? ({ ...matrix.counts, ...counts } as RequirementMatrixSnapshot["counts"])
      : matrix.counts,
  };
}

function dispositionLabel(value: string): string {
  return (
    dispositionOptions.find((option) => option.value === value)?.label ||
    "仅保留"
  );
}

function decisionLabel(value: string): string {
  return decisionOptions.find(([key]) => key === value)?.[1] || "待确认";
}

function assessmentModeLabel(value: unknown): string {
  const labels: Record<string, string> = {
    EVIDENCE: "证据辅助分析",
    EVIDENCE_DRIVEN: "证据辅助分析",
    LLM: "智能辅助分析",
    AI: "智能辅助分析",
    RULE: "本地规则预判",
    LOCAL_RULE: "本地规则预判",
    MANUAL: "人工录入",
  };
  return labels[String(value || "").toUpperCase()] || "辅助分析";
}

export function profileReady(profile: AiProfile | null): boolean {
  if (typeof profile?.ready === "boolean") return profile.ready;
  return Boolean(profile?.llm_ready && profile?.retrieval_ready);
}

function MatrixInspector({
  lens,
  projectId,
  documentId,
  requirementSetId,
  node,
  detail,
  detailLoading,
  detailError,
  canEditDisposition,
  canWriteDecision,
  resetSignal,
  saveRef,
  saveState,
  onSaveState,
  beginMatrixMutation,
  isMatrixMutationCurrent,
  onNodeUpdated,
  onImpactChanged,
  onRetryDetail,
  analysisExecution,
}: {
  lens: RequirementMatrixLens;
  projectId: string;
  documentId: string;
  requirementSetId: string;
  node: RequirementMatrixNode | null;
  detail: RequirementMatrixNodeDetail | null;
  detailLoading: boolean;
  detailError: string;
  canEditDisposition: boolean;
  canWriteDecision: boolean;
  resetSignal: number;
  saveRef: MutableRefObject<(() => Promise<boolean>) | null>;
  saveState: SaveState;
  onSaveState: (state: SaveState) => void;
  beginMatrixMutation: () => MatrixMutationRequest;
  isMatrixMutationCurrent: (request: MatrixMutationRequest) => boolean;
  onNodeUpdated: (node: RequirementMatrixNode, counts?: JsonRecord) => void;
  onImpactChanged: (payload: JsonRecord) => void;
  onRetryDetail: () => void;
  analysisExecution: RequirementAnalysisNodeExecution | null;
}) {
  const [disposition, setDisposition] =
    useState<RequirementMatrixNodeDisposition>(node?.disposition || "RETAIN");
  const [dispositionReason, setDispositionReason] = useState(
    String(node?.disposition_reason || ""),
  );
  const [decision, setDecision] = useState(
    String(node?.manual_decision?.decision || ""),
  );
  const [decisionReason, setDecisionReason] = useState(
    String(node?.manual_decision?.reason || ""),
  );
  const isContainer = Boolean(node?.child_ids.length);

  useEffect(() => {
    setDisposition(node?.disposition || "RETAIN");
    setDispositionReason(String(node?.disposition_reason || ""));
    setDecision(String(node?.manual_decision?.decision || ""));
    setDecisionReason(String(node?.manual_decision?.reason || ""));
  }, [
    node?.disposition,
    node?.disposition_reason,
    node?.manual_decision?.decision,
    node?.manual_decision?.reason,
    node?.node_id,
    node?.save_revision,
    resetSignal,
  ]);

  useEffect(() => {
    onSaveState("");
  }, [node?.node_id, onSaveState, resetSignal]);

  const save = useCallback(async (): Promise<boolean> => {
    if (!node || saveState === "保存中…") return false;
    if (
      (lens === "review" &&
        (!canEditDisposition ||
          (isContainer && disposition !== "RETAIN") ||
          (disposition === "ANALYZE" && !matrixAnalysisCapable(node)))) ||
      (lens === "analysis" && (!canWriteDecision || !decision))
    ) {
      return false;
    }
    const mutationRequest = beginMatrixMutation();
    onSaveState("保存中…");
    try {
      if (lens === "review") {
        const response = await api.updateRequirementMatrixDisposition(
          projectId,
          documentId,
          requirementSetId,
          node.node_id,
          {
            schema_version: "requirement-matrix-disposition-patch/v1",
            disposition,
            reason: dispositionReason.trim(),
            expected_revision: node.save_revision,
          },
        );
        if (!isMatrixMutationCurrent(mutationRequest)) return false;
        const responseNode = asRecord(response).node as RequirementMatrixNode;
        onNodeUpdated(responseNode, asRecord(asRecord(response).counts));
        onImpactChanged(asRecord(response));
      } else {
        const response = await api.updateRequirementMatrixDecision(
          projectId,
          documentId,
          requirementSetId,
          node.node_id,
          {
            schema_version: "requirement-matrix-decision-request/v1",
            final_decision: decision,
            engineer_reason: decisionReason.trim(),
            expected_revision: node.save_revision,
          },
        );
        if (!isMatrixMutationCurrent(mutationRequest)) return false;
        const responseNode = asRecord(response).node as RequirementMatrixNode;
        onNodeUpdated(responseNode);
      }
      onSaveState(`已保存 ${savedTimeFormatter.format(new Date())}`);
      return true;
    } catch {
      if (!isMatrixMutationCurrent(mutationRequest)) return false;
      onSaveState("保存失败，请重试");
      return false;
    }
  }, [
    canEditDisposition,
    canWriteDecision,
    beginMatrixMutation,
    decision,
    decisionReason,
    disposition,
    dispositionReason,
    documentId,
    lens,
    isContainer,
    isMatrixMutationCurrent,
    node,
    onNodeUpdated,
    onImpactChanged,
    onSaveState,
    projectId,
    requirementSetId,
    saveState,
  ]);
  saveRef.current = save;

  if (!node) {
    return (
      <EmptyState
        icon="requirement"
        title="选择一项开始检查"
        description="检查器会集中显示处置、AI 建议和人工结论。"
      />
    );
  }

  const conclusionContract = matrixConclusionContract(node);
  const aiAssessmentPermitted =
    conclusionContract.conclusionSource === "AI_DEFAULT" ||
    conclusionContract.conclusionSource === "HUMAN_OVERRIDE";
  const rawAssessment = node.ai_assessment;
  const assessment =
    aiAssessmentPermitted &&
    (String(rawAssessment?.decision || "").trim() ||
      String(rawAssessment?.compliance_status || "").trim())
      ? rawAssessment
      : null;
  const aiConclusionLabel =
    conclusionContract.conclusionSource === "AI_DEFAULT"
      ? "AI 结论 · 默认采用"
      : conclusionContract.conclusionSource === "HUMAN_OVERRIDE" && assessment
        ? "AI 原结论 · 已被人工修改"
        : "AI 结论";
  const staleAssessment = node.stale_ai_assessment;
  const staleDecision = node.stale_manual_decision;
  const persistedAssessment = aiAssessmentPermitted
    ? asRecord(detail?.assessment)
    : {};
  const evidenceFindings = asArray<JsonRecord>(
    persistedAssessment.evidence_findings,
  );
  const gaps = asRecord(persistedAssessment.structured_information_gap);
  const missingItems = asArray<JsonRecord>(gaps.missing_items);
  const subJudgments = asArray<JsonRecord>(persistedAssessment.sub_judgments);
  const path = detail?.path || [];
  const displayedAnalysisStatus = analysisExecutionStatusLabel(
    analysisExecution,
    matrixAnalysisStatus(node),
  );

  return (
    <div className="matrix-inspector-content">
      <header className="matrix-inspector-heading">
        <div>
          <span>{lens === "review" ? "确认清单" : "分析检查器"}</span>
          <h2>{matrixNodeLabel(node)}</h2>
        </div>
        <StatusBadge tone={matrixAnalysisTone(displayedAnalysisStatus)}>
          {displayedAnalysisStatus}
        </StatusBadge>
      </header>

      {lens === "analysis" && analysisExecution?.status === "FAILED" && (
        <InlineNotice tone="warning" title="本次分析失败">
          {analysisExecution.error_message ||
            "当前项本次没有生成新结果；如已有历史结果，仍保留为上一版结果。"}
        </InlineNotice>
      )}

      {path.length > 1 && (
        <p className="matrix-inspector-path">
          {path.map((item) => matrixNodeLabel(item)).join(" / ")}
        </p>
      )}

      {detailError && (
        <InlineNotice title="版本详情读取失败" tone="warning">
          {detailError}
          <button className="text-button" type="button" onClick={onRetryDetail}>
            重试
          </button>
        </InlineNotice>
      )}

      {lens === "review" ? (
        <>
          {isContainer ? (
            <InlineNotice tone="info" title="内容分组无需单独设置">
              章节、条款和表格分组负责保留层级，其状态由下级条目汇总。展开后可处理具体条目，也可使用清单上方的批量处理一次应用到当前范围。
            </InlineNotice>
          ) : (
            <>
              <section className="matrix-inspector-section">
                <p className="section-label">在正式清单中的处理方式</p>
                <div className="matrix-disposition-options">
                  {dispositionOptions.map((option) => (
                    <label
                      className={cx(
                        "matrix-disposition-option",
                        disposition === option.value && "is-selected",
                      )}
                      key={option.value}
                    >
                      <input
                        type="radio"
                        name={`matrix-disposition-${node.node_id}`}
                        value={option.value}
                        checked={disposition === option.value}
                        disabled={
                          !canEditDisposition ||
                          (option.value === "ANALYZE" &&
                            !matrixAnalysisCapable(node))
                        }
                        onChange={() => {
                          setDisposition(option.value);
                          onSaveState("未保存");
                        }}
                      />
                      <span>
                        <strong>{option.label}</strong>
                        <small>{option.description}</small>
                      </span>
                    </label>
                  ))}
                </div>
              </section>
              <label className="matrix-inspector-field">
                <span>处置说明（可选）</span>
                <textarea
                  name="matrix-disposition-reason"
                  autoComplete="off"
                  rows={3}
                  value={dispositionReason}
                  disabled={!canEditDisposition}
                  placeholder="仅在需要说明排除或保留原因时填写"
                  onChange={(event) => {
                    setDispositionReason(event.target.value);
                    onSaveState("未保存");
                  }}
                />
              </label>
            </>
          )}
          <InlineNotice tone="info" title="正文保持版本原样">
            确认清单只决定“需分析 / 仅保留 / 排除”。如需修正文档结构或内容边界，请返回结构整理生成新版本。
          </InlineNotice>
          {!isContainer && !canEditDisposition && (
            <p className="matrix-form-hint">
              只有当前激活且未锁定的需求版本可以修改清单处置。
            </p>
          )}
        </>
      ) : (
        <>
          {conclusionContract.conclusionSource === "STALE_REFERENCE" && (
            <InlineNotice tone="warning" title="需重新分析">
              <p>内容或分析上下文已经变化；上版结果仅作历史追溯，不作为当前结论。</p>
              {staleAssessment && (
                <p>
                  上版 AI 建议：
                  {objectText(
                    staleAssessment.decision ||
                      staleAssessment.compliance_status,
                  )}
                </p>
              )}
              {staleDecision && (
                <p>上版人工结论：{decisionLabel(staleDecision.decision)}</p>
              )}
            </InlineNotice>
          )}
          <section className="matrix-inspector-section">
            <div className="matrix-section-title">
              <div>
                <p className="section-label">{aiConclusionLabel}</p>
                <h3>
                  {assessment
                    ? objectText(
                        assessment.decision || assessment.compliance_status,
                      )
                    : "尚无可采用的当前 AI 结论"}
                </h3>
              </div>
              {assessment?.confidence !== null && assessment?.confidence !== undefined && (
                <span>{Math.round(Number(assessment.confidence) * 100)}%</span>
              )}
            </div>
            {assessment?.reason && <p>{String(assessment.reason)}</p>}
            {assessment?.assessment_mode && (
              <small>分析方式：{assessmentModeLabel(assessment.assessment_mode)}</small>
            )}
          </section>

          {detailLoading ? (
            <LoadingBlock label="正在读取版本证据" />
          ) : (
            <>
              {evidenceFindings.length > 0 && (
                <section className="matrix-inspector-section">
                  <p className="section-label">证据判断</p>
                  <div className="matrix-evidence-list">
                    {evidenceFindings.map((finding, index) => (
                      <article key={String(finding.retrieval_evidence_id || index)}>
                        <strong>{objectText(finding.finding) || `证据 ${index + 1}`}</strong>
                        <p>{objectText(finding.rationale) || "已绑定到当前版本。"}</p>
                      </article>
                    ))}
                  </div>
                </section>
              )}
              {subJudgments.length > 0 && (
                <section className="matrix-inspector-section">
                  <p className="section-label">分项判断</p>
                  {subJudgments.map((item, index) => (
                    <p key={String(item.id || index)}>{objectText(item)}</p>
                  ))}
                </section>
              )}
              {missingItems.length > 0 && (
                <section className="matrix-inspector-section is-warning">
                  <p className="section-label">待补充信息</p>
                  {missingItems.map((item, index) => (
                    <p key={String(item.id || index)}>
                      {objectText(item.item || item.required_input || item)}
                    </p>
                  ))}
                </section>
              )}
            </>
          )}

          <section className="matrix-inspector-section is-decision">
            <p className="section-label">人工修改（可选）</p>
            <label className="matrix-inspector-field">
              <span>结论</span>
              <select
                name="matrix-manual-decision"
                autoComplete="off"
                value={decision}
                disabled={!canWriteDecision}
                onChange={(event) => {
                  setDecision(event.target.value);
                  onSaveState("未保存");
                }}
              >
                <option value="">保持 AI 结论（未修改）</option>
                {decisionOptions.map(([value, label]) => (
                  <option value={value} key={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className="matrix-inspector-field">
              <span>工程师说明</span>
              <textarea
                name="matrix-decision-reason"
                autoComplete="off"
                rows={4}
                value={decisionReason}
                disabled={!canWriteDecision}
                onChange={(event) => {
                  setDecisionReason(event.target.value);
                  onSaveState("未保存");
                }}
              />
            </label>
            {!matrixAnalysisEligible(node) && (
              <p className="matrix-form-hint">
                当前项是层级或上下文，不进入单独的符合性判定。
              </p>
            )}
            {matrixAnalysisEligible(node) && !canWriteDecision && (
              <p className="matrix-form-hint">
                人工结论只能写入当前已锁定的确认清单。
              </p>
            )}
          </section>
        </>
      )}

      <footer className="matrix-inspector-footer">
        <span aria-live="polite">
          {saveState || `当前：${dispositionLabel(node.disposition)}`}
        </span>
        <button
          className="button primary"
          type="button"
          disabled={
            saveState === "保存中…" ||
            (lens === "review"
              ? !canEditDisposition ||
                (isContainer && disposition !== "RETAIN") ||
                (disposition === "ANALYZE" && !matrixAnalysisCapable(node))
              : !canWriteDecision || !decision)
          }
          onClick={() => void save()}
        >
          <Icon
            name={saveState === "保存失败，请重试" ? "refresh" : "check"}
            size={15}
          />
          {saveState === "保存中…"
            ? "保存中…"
            : saveState === "保存失败，请重试"
              ? "重试保存"
              : "保存"}
          <kbd>⌘↵</kbd>
        </button>
      </footer>
    </div>
  );
}

export function RequirementMatrixWorkbench({
  lens,
}: {
  lens: RequirementMatrixLens;
}) {
  const {
    route,
    updateRoute,
    selectedDocument,
    workspace,
    requirementSets,
    aiStatus,
    aiSetup,
    busyAction,
    runAction,
    refreshDocument,
    refreshAi,
  } = useWorkspace();
  const sets = requirementSets?.requirement_sets || [];
  const activeSetId = String(requirementSets?.active_requirement_set_id || "");
  const activeSetExists = sets.some((set) => setId(set) === activeSetId);
  const fallbackSetId = activeSetExists
    ? activeSetId
    : sets[0]
      ? setId(sets[0])
      : "";
  const requestedSetId = route.requirementSetId;
  const requestedSetExists = sets.some((set) => setId(set) === requestedSetId);
  const selectedSetId = requestedSetExists ? requestedSetId : fallbackSetId;
  const selectedSet = sets.find((set) => setId(set) === selectedSetId) || null;
  const setStatus = String(selectedSet?.status || "").toUpperCase();
  const uiScope = `matrix:${route.projectId}:${route.documentId}:${selectedSetId}`;
  const [matrix, setMatrix] = useState<RequirementMatrixSnapshot | null>(null);
  const [matrixLoading, setMatrixLoading] = useState(false);
  const [matrixError, setMatrixError] = useState<Error | null>(null);
  const [query, setQuery] = useScopedUiState(`${uiScope}:query:${lens}`, "");
  const [filter, setFilter] = useScopedUiState<RequirementMatrixFilter>(
    `${uiScope}:filter:${lens}`,
    "all",
  );
  const availableSecondaryFilterOptions = secondaryFilterOptions;
  const isCandidateReview = Boolean(route.reviewCase);
  const [category, setCategory] = useScopedUiState(`${uiScope}:category`, "");
  const [topic, setTopic] = useScopedUiState(`${uiScope}:topic`, "");
  const facets = useMemo(() => ({ category, topic }), [category, topic]);
  const candidateMetadata = asRecord(matrix?.candidate_review);
  const topicLabels = asRecord(candidateMetadata.topics) as Record<string, string>;
  const [exportState, setExportState] = useState("");
  const [selectedId, setSelectedId] = useScopedUiState(
    `${uiScope}:selected-node`,
    "",
  );
  const currentSelectedId = useRef(selectedId);
  currentSelectedId.current = selectedId;
  const [expandedList, setExpandedList] = useScopedUiState<string[] | null>(
    `${uiScope}:expanded`,
    EMPTY_EXPANDED,
  );
  const [pdfPage, setPdfPage] = useScopedUiState(`${uiScope}:pdf-page`, 1);
  const [detailPane, setDetailPane] = useScopedUiState<DetailPane>(
    `${uiScope}:detail-pane`,
    lens === "analysis" ? "inspector" : "source",
  );
  const [scrollTop, setScrollTop] = useScopedUiState(
    `${uiScope}:tree-scroll-top`,
    0,
  );
  const [viewportHeight, setViewportHeight] = useState(560);
  const [pagePayload, setPagePayload] = useState<JsonRecord | null>(null);
  const [sourceError, setSourceError] = useState("");
  const [focusRegions, setFocusRegions] = useState<JsonRecord[]>([]);
  const [nodeDetail, setNodeDetail] = useState<RequirementMatrixNodeDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [tableError, setTableError] = useState("");
  const [tableDetails, setTableDetails] = useState<RequirementMatrixLogicalTable[]>([]);
  const [saveState, setSaveState] = useState<SaveState>("");
  const hasUnsavedChanges =
    saveState === "未保存" || saveState === "保存失败，请重试";
  const [matrixRefreshPending, setMatrixRefreshPending] = useState(false);
  const [resetSignal, setResetSignal] = useState(0);
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchScope, setBatchScope] = useState<BatchScope>("filter");
  const [batchDisposition, setBatchDisposition] =
    useState<RequirementMatrixNodeDisposition>("ANALYZE");
  const [batchReason, setBatchReason] = useState("");
  const [batchState, setBatchState] = useState("");
  const matrixMutationPending =
    saveState === "保存中…" ||
    batchState === "保存中…" ||
    batchState === "正在撤销…";
  const [workflowDialog, setWorkflowDialog] =
    useState<MatrixWorkflowDialog>(null);
  const [workflowPending, setWorkflowPending] =
    useState<MatrixWorkflowPending>(null);
  const [workflowError, setWorkflowError] = useState("");
  const [workflowConflict, setWorkflowConflict] = useState(false);
  const [analysisSelectionList, setAnalysisSelectionList] = useScopedUiState<
    string[]
  >(`${uiScope}:analysis-selection`, EMPTY_ANALYSIS_SELECTION);
  const [analysisScopeRequest, setAnalysisScopeRequest] =
    useState<RequirementAnalysisScopeRequest | null>(null);
  const [analysisScopePreview, setAnalysisScopePreview] =
    useState<RequirementAnalysisScopePreview | null>(null);
  const [analysisPreviewLoading, setAnalysisPreviewLoading] = useState(false);
  const [analysisPreviewError, setAnalysisPreviewError] = useState("");
  const [analysisRetryKind, setAnalysisRetryKind] =
    useState<AnalysisRetryKind>("");
  const [legacyAnalysisConfirmation, setLegacyAnalysisConfirmation] =
    useState(false);
  const [lockReadiness, setLockReadiness] =
    useState<RequirementSetLockReadiness | null>(null);
  const [lockReadinessLoading, setLockReadinessLoading] = useState(false);
  const [zeroAnalysisLocked, setZeroAnalysisLocked] = useState(false);
  const [impactFeedback, setImpactFeedback] = useState<{
    requirementSetId: string;
    staleResultNodeIds: string[];
  } | null>(null);
  const [batchUndo, setBatchUndo] = useState<
    Array<{
      nodeId: string;
      disposition: RequirementMatrixNodeDisposition;
      reason: string;
      expectedRevision: number;
    }>
  >([]);
  const treeScrollRef = useRef<HTMLDivElement | null>(null);
  const [setTreePaneElement, matrixPaneWidth, treePaneRef] =
    useMeasuredElementWidth<HTMLElement>();
  const treeScrollTopRef = useRef(scrollTop);
  const treeScrollUpdateFrameRef = useRef<number | null>(null);
  const [setWorkbenchElement, workbenchWidth, workbenchRef] =
    useMeasuredElementWidth<HTMLElement>();
  const treeExpandMenuRef = useRef<HTMLDetailsElement | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const pdfFrameRef = useRef<HTMLIFrameElement | null>(null);
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const saveRef = useRef<(() => Promise<boolean>) | null>(null);
  const matrixRequestRef = useRef(0);
  const matrixMutationScopeRef = useRef(uiScope);
  const matrixMutationGenerationRef = useRef<Record<MatrixMutationKind, number>>({
    single: 0,
    batch: 0,
  });
  const lockReadinessRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  const workflowErrorRef = useRef<HTMLDivElement | null>(null);
  const analysisPreviewRequestRef = useRef(0);
  const analysisSelectionTreeRef = useRef("");
  const lockConfirmButtonRef = useRef<HTMLButtonElement | null>(null);
  const focusLockConfirmAfterReadinessRef = useRef(false);
  const filterExpansionKeyRef = useRef("");
  const previousAiJobRef = useRef<{
    id: string;
    status: string;
    published: number;
  } | null>(null);
  matrixMutationScopeRef.current = uiScope;
  const beginMatrixMutation = useCallback(
    (kind: MatrixMutationKind): MatrixMutationRequest => {
      const generation = matrixMutationGenerationRef.current[kind] + 1;
      matrixMutationGenerationRef.current[kind] = generation;
      return {
        generation,
        kind,
        uiScope: matrixMutationScopeRef.current,
      };
    },
    [],
  );
  const beginSingleMatrixMutation = useCallback(
    () => beginMatrixMutation("single"),
    [beginMatrixMutation],
  );
  const isMatrixMutationCurrent = useCallback(
    (request: MatrixMutationRequest) =>
      request.uiScope === matrixMutationScopeRef.current &&
      request.generation ===
        matrixMutationGenerationRef.current[request.kind],
    [],
  );
  const compactMatrixColumns =
    lens === "analysis" &&
    matrixPaneWidth > 0 &&
    matrixPaneWidth < COMPACT_MATRIX_PANE_WIDTH;
  const compactReviewLayout =
    lens === "review" &&
    workbenchWidth > 0 &&
    workbenchWidth < COMPACT_REVIEW_WORKBENCH_WIDTH;
  if (treeScrollUpdateFrameRef.current === null) {
    treeScrollTopRef.current = scrollTop;
  }

  useEffect(() => {
    if (selectedSetId && route.requirementSetId !== selectedSetId) {
      updateRoute({ requirementSetId: selectedSetId }, { replace: true });
    }
  }, [route.requirementSetId, selectedSetId, updateRoute]);

  useEffect(() => {
    matrixMutationGenerationRef.current.single += 1;
    matrixMutationGenerationRef.current.batch += 1;
    lockReadinessRequestRef.current += 1;
    analysisPreviewRequestRef.current += 1;
    setSaveState("");
    setBatchOpen(false);
    setBatchState("");
    setBatchUndo([]);
    setBatchReason("");
    setWorkflowDialog(null);
    setWorkflowPending(null);
    setWorkflowError("");
    setWorkflowConflict(false);
    setLockReadiness(null);
    setLockReadinessLoading(false);
    setAnalysisScopeRequest(null);
    setAnalysisScopePreview(null);
    setAnalysisPreviewLoading(false);
    setAnalysisPreviewError("");
    setAnalysisRetryKind("");
    setLegacyAnalysisConfirmation(false);
    analysisSelectionTreeRef.current = "";
    focusLockConfirmAfterReadinessRef.current = false;
    setZeroAnalysisLocked(false);
  }, [uiScope]);

  useEffect(() => {
    if (!workflowError && !analysisPreviewError) return;
    window.requestAnimationFrame(() => workflowErrorRef.current?.focus());
  }, [analysisPreviewError, workflowError]);

  useEffect(
    () => () => {
      lockReadinessRequestRef.current += 1;
      matrixMutationGenerationRef.current.single += 1;
      matrixMutationGenerationRef.current.batch += 1;
    },
    [],
  );

  const loadMatrix = useCallback(async () => {
    const requestId = ++matrixRequestRef.current;
    if (!route.projectId || !route.documentId || !selectedSetId) {
      setMatrix(null);
      setMatrixError(null);
      setMatrixLoading(false);
      return;
    }
    setMatrixLoading(true);
    setMatrixError(null);
    try {
      const payload = await api.requirementMatrix(
        route.projectId,
        route.documentId,
        selectedSetId,
      );
      if (requestId !== matrixRequestRef.current) return;
      setMatrix(payload);
    } catch (cause) {
      if (requestId !== matrixRequestRef.current) return;
      setMatrix(null);
      setMatrixError(
        cause instanceof Error ? cause : new Error("需求矩阵暂时无法读取。"),
      );
    } finally {
      if (requestId === matrixRequestRef.current) setMatrixLoading(false);
    }
  }, [route.documentId, route.projectId, selectedSetId]);

  const loadLockReadiness = useCallback(async (focusConfirmation = false) => {
    const requestId = ++lockReadinessRequestRef.current;
    focusLockConfirmAfterReadinessRef.current = focusConfirmation;
    setLockReadinessLoading(true);
    setLockReadiness(null);
    if (!focusConfirmation) {
      setWorkflowError("");
      setWorkflowConflict(false);
    }
    try {
      const payload = await api.lockReadiness(
        route.projectId,
        route.documentId,
        selectedSetId,
      );
      if (requestId !== lockReadinessRequestRef.current) return;
      setLockReadiness(payload);
      setWorkflowError("");
      setWorkflowConflict(false);
    } catch (cause) {
      if (requestId !== lockReadinessRequestRef.current) return;
      focusLockConfirmAfterReadinessRef.current = false;
      const error = workflowErrorMessage(cause, "readiness");
      setWorkflowError(error.message);
      setWorkflowConflict(error.conflict);
    } finally {
      if (requestId === lockReadinessRequestRef.current) {
        setLockReadinessLoading(false);
      }
    }
    },
    [route.documentId, route.projectId, selectedSetId],
  );

  useEffect(() => {
    if (
      !focusLockConfirmAfterReadinessRef.current ||
      lockReadinessLoading ||
      !lockReadiness
    ) {
      return;
    }
    focusLockConfirmAfterReadinessRef.current = false;
    window.requestAnimationFrame(() => lockConfirmButtonRef.current?.focus());
  }, [lockReadiness, lockReadinessLoading]);

  useEffect(() => {
    void loadMatrix();
    return () => {
      matrixRequestRef.current += 1;
    };
  }, [loadMatrix]);

  useEffect(() => {
    if (matrix && expandedList === null) {
      setExpandedList(Array.from(initialMatrixExpandedIds(matrix)));
    }
  }, [expandedList, matrix, setExpandedList]);

  useEffect(() => {
    if (!matrix) return;
    const signature = `${matrix.tree_sha256}:${filter}:${query.trim().toLocaleLowerCase()}:${category}:${topic}`;
    if (filterExpansionKeyRef.current === signature) return;
    filterExpansionKeyRef.current = signature;
    const suggested = suggestedMatrixExpandedIds(matrix, query, filter, facets);
    if (!suggested.size) return;
    setExpandedList((current) =>
      Array.from(new Set([...(current || []), ...suggested])),
    );
  }, [filter, matrix, query, setExpandedList, facets, category, topic]);

  const expandedIds = useMemo(
    () => new Set(expandedList || []),
    [expandedList],
  );
  const rows = useMemo(
    () => flattenRequirementMatrix(matrix, expandedIds, query, filter, facets),
    [expandedIds, filter, matrix, query, facets],
  );
  const matchCounts = useMemo(
    () => requirementMatrixMatchCounts(matrix, query, filter, facets),
    [filter, matrix, query, facets],
  );
  const actualAnalysisCount = useMemo(
    () => (matrix?.nodes || []).filter(matrixAnalysisEligible).length,
    [matrix?.nodes],
  );
  const businessContentCount = useMemo(
    () =>
      (matrix?.nodes || []).filter(
        (node) => node.matrix_visibility !== "SYSTEM_NON_BUSINESS",
      ).length,
    [matrix?.nodes],
  );
  const businessNonAnalysisCount = useMemo(
    () =>
      (matrix?.nodes || []).filter(
        (node) =>
          node.matrix_visibility !== "SYSTEM_NON_BUSINESS" &&
          !matrixAnalysisEligible(node),
      ).length,
    [matrix?.nodes],
  );
  const staleAnalysisTargetCount = useMemo(() => {
    if (
      !matrix ||
      matrix.requirement_set_id !== selectedSetId ||
      impactFeedback?.requirementSetId !== selectedSetId
    ) {
      return 0;
    }
    const staleNodeIds = new Set(impactFeedback.staleResultNodeIds);
    return matrix.nodes.filter(
      (node) =>
        staleNodeIds.has(node.node_id) &&
        matrixAnalysisEligible(node) &&
        node.analysis_result_state === "STALE_REFERENCE",
    ).length;
  }, [impactFeedback, matrix, selectedSetId]);
  const lockAnalysisCount =
    lockReadiness?.counts.analysis_targets ?? actualAnalysisCount;
  const lockDestination =
    lockReadiness?.next_destination ??
    (lockAnalysisCount > 0 ? "ANALYSIS" : "MATRIX");
  const lockWillEnterAnalysis =
    lockDestination === "ANALYSIS" && lockAnalysisCount > 0;
  const lockClassificationCountsAvailable =
    lockReadiness?.count_metadata?.content_classification === "AVAILABLE";
  const lockCompositionCountsAvailable =
    lockReadiness?.count_metadata?.entry_composition === "AVAILABLE";
  const lockCountMetadataUnavailable = Boolean(
    lockReadiness &&
    (!lockClassificationCountsAvailable || !lockCompositionCountsAvailable),
  );
  const aiProfiles = useMemo(() => {
    const configured = normalizeAiProfiles(aiSetup?.profiles);
    return configured.length
      ? configured
      : normalizeAiProfiles(workspace?.ai_profiles);
  }, [aiSetup?.profiles, workspace?.ai_profiles]);
  const defaultAiProfileId = String(
    aiSetup?.default_profile_id ||
      aiProfiles.find((profile) => profile.default)?.id ||
      "",
  );
  const selectedAiProfile =
    aiProfiles.find((profile) => profile.id === defaultAiProfileId) ||
    aiProfiles[0] ||
    null;
  const selectedAiProfileId = selectedAiProfile?.id || "";
  const aiProfileReady = profileReady(selectedAiProfile);
  const nodeIndex = useMemo(
    () => new Map((matrix?.nodes || []).map((node) => [node.node_id, node])),
    [matrix],
  );
  const analysisSelectionIds = useMemo(
    () => new Set(analysisSelectionList),
    [analysisSelectionList],
  );
  const selectedAnalysisNodes = useMemo(
    () =>
      (matrix?.nodes || []).filter(
        (node) =>
          analysisSelectionIds.has(node.node_id) && matrixAnalysisEligible(node),
      ),
    [analysisSelectionIds, matrix?.nodes],
  );

  useEffect(() => {
    if (!matrix) return;
    const previousTree = analysisSelectionTreeRef.current;
    analysisSelectionTreeRef.current = matrix.tree_sha256;
    if (previousTree && previousTree !== matrix.tree_sha256) {
      setAnalysisSelectionList([]);
      return;
    }
    const eligibleIds = new Set(
      matrix.nodes.filter(matrixAnalysisEligible).map((node) => node.node_id),
    );
    setAnalysisSelectionList((current) => {
      const next = current.filter((nodeId) => eligibleIds.has(nodeId));
      return next.length === current.length ? current : next;
    });
  }, [matrix, setAnalysisSelectionList]);

  useEffect(() => {
    if (!rows.length) {
      if (hasUnsavedChanges) return;
      if (selectedId) setSelectedId("");
      return;
    }
    if (
      !hasUnsavedChanges &&
      !rows.some((row) => row.node.node_id === selectedId)
    ) {
      const preferred = String(candidateMetadata.initialNodeId || "");
      const initialCandidate = !selectedId && !category && !topic && !query && filter === "all" && matrix?.nodes.some(node => node.node_id === preferred);
      const firstCandidateMatch = isCandidateReview ? rows.find(row => row.node.candidate_review && matrixNodeMatchesView(row.node, query, filter, facets))?.node.node_id : "";
      setSelectedId(initialCandidate ? preferred : firstCandidateMatch || firstMatrixSelectionId(rows));
    }
  }, [hasUnsavedChanges, rows, selectedId, setSelectedId, candidateMetadata.initialNodeId, category, topic, query, filter, matrix, facets, isCandidateReview]);

  const initialCandidateRevealed = useRef(false);
  useEffect(() => {
    if (!isCandidateReview || initialCandidateRevealed.current || selectedId !== candidateMetadata.initialNodeId) return;
    const index = rows.findIndex(row => row.node.node_id === selectedId);
    if (index < 0 || !treeScrollRef.current) return;
    initialCandidateRevealed.current = true;
    const top = Math.max(0, (index - 1) * MATRIX_ROW_HEIGHT);
    treeScrollRef.current.scrollTop = top;
    setScrollTop(top);
  }, [isCandidateReview, rows, selectedId, candidateMetadata.initialNodeId, setScrollTop]);

  const selectedNode = nodeIndex.get(selectedId) || null;
  const batchScopeNodes = useMemo(() => {
    if (!matrix) return [];
    if (batchScope === "all") return matrix.nodes;
    if (batchScope === "filter") {
      return matrix.nodes.filter((node) =>
        matrixNodeMatchesView(node, query, filter, facets),
      );
    }
    if (!selectedNode) return [];
    const output: RequirementMatrixNode[] = [];
    const pending = [selectedNode.node_id];
    const visited = new Set<string>();
    while (pending.length) {
      const nodeId = pending.shift()!;
      if (visited.has(nodeId)) continue;
      visited.add(nodeId);
      const node = nodeIndex.get(nodeId);
      if (!node) continue;
      output.push(node);
      pending.unshift(...node.child_ids);
    }
    return output;
  }, [batchScope, filter, matrix, nodeIndex, query, selectedNode]);
  const batchCandidateNodes = useMemo(
    () =>
      batchScopeNodes.filter((node) => {
        if (
          node.matrix_visibility === "SYSTEM_NON_BUSINESS" ||
          node.child_ids.length > 0 ||
          node.disposition === batchDisposition
        ) {
          return false;
        }
        return batchDisposition !== "ANALYZE" || matrixAnalysisCapable(node);
      }),
    [batchDisposition, batchScopeNodes],
  );
  const batchSkippedCount = Math.max(
    0,
    batchScopeNodes.length - batchCandidateNodes.length,
  );
  const recordAtomicNodes = useMemo(() => {
    if (!selectedNode) return [];
    const recordNode =
      selectedNode.display_role === "TABLE_RECORD"
        ? selectedNode
        : selectedNode.parent_id
          ? nodeIndex.get(selectedNode.parent_id) || null
          : null;
    if (!recordNode || recordNode.display_role !== "TABLE_RECORD") return [];
    return recordNode.child_ids.flatMap((childId) => {
      const child = nodeIndex.get(childId);
      return child?.display_role === "ATOMIC_REQUIREMENT" ? [child] : [];
    });
  }, [nodeIndex, selectedNode]);
  const selectedNodeDetail =
    nodeDetail?.node.node_id === selectedNode?.node_id ? nodeDetail : null;
  useEffect(() => {
    if (selectedNode?.page_start) {
      setPdfPage(selectedNode.page_start);
    }
  }, [selectedNode?.node_id, selectedNode?.page_start, setPdfPage]);

  const maxPage = useMemo(
    () =>
      Math.max(
        1,
        Number(
          selectedDocument?.page_count || selectedDocument?.total_pages || 0,
        ),
        ...(matrix?.nodes || []).map((node) => Number(node.page_end || 0)),
      ),
    [matrix?.nodes, selectedDocument],
  );

  useEffect(() => {
    if (!route.projectId || !route.documentId || pdfPage < 1) return;
    let active = true;
    setSourceError("");
    void api
      .sourcePage(route.projectId, route.documentId, pdfPage)
      .then((payload) => {
        if (active) setPagePayload(payload);
      })
      .catch((cause) => {
        if (!active) return;
        setPagePayload(null);
        setSourceError(
          cause instanceof Error ? cause.message : "当前页无法读取。",
        );
      });
    return () => {
      active = false;
    };
  }, [pdfPage, route.documentId, route.projectId]);

  useEffect(() => {
    if (!route.projectId || !route.documentId || !selectedSetId || !selectedNode) {
      setFocusRegions([]);
      return;
    }
    // Whole-table Matrix nodes are derived read-model identities. Their logical
    // table/structure IDs are not guaranteed to exist in the frozen tree geometry
    // endpoint; exact table focus comes from each structured segment below.
    if (
      selectedNode.display_role === "TABLE" &&
      selectedNode.logical_table_ids.length
    ) {
      setFocusRegions([]);
      return;
    }
    let active = true;
    setFocusRegions([]);
    void api
      .geometryFocus(
        route.projectId,
        route.documentId,
        selectedNode.node_id,
        selectedSetId,
      )
      .then((payload) => {
        if (active) setFocusRegions(asArray<JsonRecord>(payload.regions));
      })
      .catch(() => {
        if (active) setFocusRegions([]);
      });
    return () => {
      active = false;
    };
  }, [route.documentId, route.projectId, selectedNode, selectedSetId]);

  const loadSelectedDetail = useCallback(async () => {
    const requestId = ++detailRequestRef.current;
    if (!route.projectId || !route.documentId || !selectedSetId || !selectedNode) {
      setNodeDetail(null);
      setTableDetails([]);
      setDetailError("");
      setTableError("");
      setDetailLoading(false);
      return;
    }
    setDetailLoading(true);
    setNodeDetail(null);
    setTableDetails([]);
    setDetailError("");
    setTableError("");
    const detailRequest = api.requirementMatrixNode(
      route.projectId,
      route.documentId,
      selectedSetId,
      selectedNode.node_id,
    );
    const tableRequests = selectedNode.logical_table_ids.map((logicalTableId) =>
      api.requirementMatrixLogicalTable(
        route.projectId,
        route.documentId,
        selectedSetId,
        logicalTableId,
      ),
    );
    const results = await Promise.allSettled([detailRequest, ...tableRequests]);
    if (requestId !== detailRequestRef.current) return;
    const [detailResult, ...tableResults] = results;
    setNodeDetail(
      detailResult.status === "fulfilled" ? detailResult.value : null,
    );
    setDetailError(
      detailResult.status === "rejected"
        ? detailResult.reason instanceof Error
          ? detailResult.reason.message
          : "完整父条款暂时无法读取。"
        : "",
    );
    setTableDetails(
      tableResults.flatMap((result) =>
        result.status === "fulfilled" ? [result.value.logical_table] : [],
      ),
    );
    const failedTableCount = tableResults.filter(
      (result) => result.status === "rejected",
    ).length;
    setTableError(
      failedTableCount
        ? `${failedTableCount} 个表格还原详情暂时无法读取。`
        : "",
    );
    setDetailLoading(false);
  }, [route.documentId, route.projectId, selectedNode, selectedSetId]);

  useEffect(() => {
    void loadSelectedDetail();
    return () => {
      detailRequestRef.current += 1;
    };
  }, [loadSelectedDetail]);

  const logicalTables = useMemo(() => {
    const output = new Map<string, RequirementMatrixLogicalTable>();
    (matrix?.logical_tables || []).forEach((table) => {
      const {
        projection: _projection,
        evidence_bindings: _bindings,
        ...metadata
      } = table;
      output.set(
        table.logical_table_id,
        metadata as RequirementMatrixLogicalTable,
      );
    });
    (selectedNodeDetail?.logical_tables || []).forEach((table) =>
      output.set(table.logical_table_id, table),
    );
    tableDetails.forEach((table) => output.set(table.logical_table_id, table));
    return Array.from(output.values()).filter((table) =>
      selectedNode?.logical_table_ids.includes(table.logical_table_id),
    );
  }, [matrix?.logical_tables, selectedNode, selectedNodeDetail, tableDetails]);
  const selectedRecordPresentationState = useMemo<
    "ready" | "loading" | "error"
  >(() => {
    if (
      !selectedNode?.table_record_id ||
      !["TABLE_RECORD", "ATOMIC_REQUIREMENT"].includes(
        String(selectedNode.display_role || "").toUpperCase(),
      )
    ) {
      return "ready";
    }
    if (!selectedNodeDetail || detailLoading) return "loading";
    return logicalTables.some((table) =>
      logicalTableContainsRecord(table, selectedNode.table_record_id || ""),
    )
      ? "ready"
      : "error";
  }, [detailLoading, logicalTables, selectedNode, selectedNodeDetail]);

  const pageSnapshot = useMemo(
    () => normalizeRestorationPage(pagePayload, pdfPage),
    [pagePayload, pdfPage],
  );
  const sourceSha256 =
    pageSnapshot.sourceSha256 || String(selectedDocument?.source_sha256 || "");
  const tableSegmentFocusRegions = useMemo(
    () => logicalTableSegmentFocusRegions(selectedNode, logicalTables),
    [logicalTables, selectedNode],
  );
  const tableRecordFocusRegion = useMemo(
    () => logicalTableRecordFocusRegion(selectedNode, logicalTables),
    [logicalTables, selectedNode],
  );
  const tableRecordFocusPage = Number(
    tableRecordFocusRegion?.page_number || tableRecordFocusRegion?.page || 0,
  );
  useEffect(() => {
    if (tableRecordFocusPage > 0) setPdfPage(tableRecordFocusPage);
  }, [setPdfPage, tableRecordFocusPage]);
  const selectedOverlays = useMemo(
    () =>
      (tableRecordFocusRegion
        ? [tableRecordFocusRegion]
        : [...focusRegions, ...tableSegmentFocusRegions]
      ).flatMap((region, index) => {
        const page = Number(region.page_number || region.page || 0);
        const bbox = asArray<unknown>(region.bbox).map(Number);
        if (
          page !== pdfPage ||
          bbox.length !== 4 ||
          !bbox.every(Number.isFinite) ||
          bbox[2] <= bbox[0] ||
          bbox[3] <= bbox[1]
        ) {
          return [];
        }
        const role = String(region.role || "CLAUSE_TEXT").toUpperCase();
        return [
          {
            id: String(region.id || `matrix-focus-${index + 1}`),
            type: role.includes("TABLE")
              ? "TABLE"
              : role.includes("FIGURE")
                ? "FIGURE"
                : "TEXT",
            label: index === 0 ? "当前内容" : "",
            bbox,
            sourceBounds: pageSnapshot.bounds,
            selected: true,
            focus: true,
            precision: String(region.precision || "BLOCK_APPROXIMATE"),
          },
        ];
      }),
    [
      focusRegions,
      pageSnapshot.bounds,
      pdfPage,
      tableRecordFocusRegion,
      tableSegmentFocusRegions,
    ],
  );

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
  useEffect(() => syncPdf(), [syncPdf]);
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
        const nextPage = Number(message.page || 0);
        if (nextPage > 0) setPdfPage(nextPage);
      }
    };
    window.addEventListener("message", listener);
    return () => window.removeEventListener("message", listener);
  }, [setPdfPage, syncPdf]);

  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) {
        return;
      }
      const target = event.target as HTMLElement | null;
      if (
        target?.matches(
          "input, textarea, select, [contenteditable='true'], [contenteditable='plaintext-only']",
        )
      ) {
        return;
      }
      event.preventDefault();
      searchInputRef.current?.focus();
      searchInputRef.current?.select();
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);

  useEffect(() => {
    if (!hasUnsavedChanges) return;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", beforeUnload);
    const unregister = registerNavigationGuard(() =>
      window.confirm("当前修改尚未保存，确定离开吗？"),
    );
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      unregister();
    };
  }, [hasUnsavedChanges]);

  useEffect(() => {
    const element = treeScrollRef.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setViewportHeight(entry.contentRect.height);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [matrix?.nodes.length, selectedSetId]);

  useEffect(() => {
    const element = treeScrollRef.current;
    if (!element) return;
    const frame = requestAnimationFrame(() => {
      const restoredScrollTop = treeScrollTopRef.current;
      if (Math.abs(element.scrollTop - restoredScrollTop) > 1) {
        element.scrollTop = restoredScrollTop;
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [matrix?.tree_sha256, uiScope]);

  useEffect(
    () => () => {
      if (treeScrollUpdateFrameRef.current !== null) {
        cancelAnimationFrame(treeScrollUpdateFrameRef.current);
        treeScrollUpdateFrameRef.current = null;
      }
    },
    [uiScope],
  );

  const handleTreeScroll = useCallback(
    (element: HTMLDivElement) => {
      treeScrollTopRef.current = element.scrollTop;
      if (treeScrollUpdateFrameRef.current !== null) return;
      treeScrollUpdateFrameRef.current = requestAnimationFrame(() => {
        treeScrollUpdateFrameRef.current = null;
        setScrollTop(treeScrollTopRef.current);
      });
    },
    [setScrollTop],
  );

  // Filtering can shrink the spacer before the browser emits a scroll event.
  // Clamp this render too, so a stale offset cannot hide every matching row.
  const maxTreeScrollTop = Math.max(0, rows.length * MATRIX_ROW_HEIGHT - viewportHeight);
  const visibleScrollTop = Math.min(scrollTop, maxTreeScrollTop);
  useEffect(() => {
    if (!matrix || scrollTop <= maxTreeScrollTop) return;
    treeScrollTopRef.current = maxTreeScrollTop;
    setScrollTop(maxTreeScrollTop);
    if (treeScrollRef.current) treeScrollRef.current.scrollTop = maxTreeScrollTop;
  }, [matrix, maxTreeScrollTop, scrollTop, setScrollTop]);

  const startIndex = Math.max(
    0,
    Math.floor(visibleScrollTop / MATRIX_ROW_HEIGHT) - MATRIX_OVERSCAN,
  );
  const endIndex = Math.min(
    rows.length,
    Math.ceil((visibleScrollTop + viewportHeight) / MATRIX_ROW_HEIGHT) +
      MATRIX_OVERSCAN,
  );
  const virtualRows = rows.slice(startIndex, endIndex);

  const setExpanded = useCallback(
    (nodeId: string, expanded: boolean) => {
      setExpandedList((current) => {
        const next = new Set(current || []);
        if (expanded) next.add(nodeId);
        else next.delete(nodeId);
        return Array.from(next);
      });
    },
    [setExpandedList],
  );

  const scrollToIndex = useCallback((index: number) => {
    const element = treeScrollRef.current;
    if (!element) return;
    const top = index * MATRIX_ROW_HEIGHT;
    const bottom = top + MATRIX_ROW_HEIGHT;
    if (top < element.scrollTop) element.scrollTop = top;
    else if (bottom > element.scrollTop + element.clientHeight) {
      element.scrollTop = bottom - element.clientHeight;
    }
  }, []);

  const closeTreeExpandMenu = useCallback(() => {
    if (treeExpandMenuRef.current) treeExpandMenuRef.current.open = false;
  }, []);

  const expandToDepth = useCallback(
    (depth: number) => {
      setExpandedList(
        (matrix?.nodes || [])
          .filter((node) => node.child_ids.length > 0 && node.depth < depth)
          .map((node) => node.node_id),
      );
      closeTreeExpandMenu();
    },
    [closeTreeExpandMenu, matrix?.nodes, setExpandedList],
  );

  const expandAll = useCallback(() => {
    setExpandedList(
      (matrix?.nodes || [])
        .filter((node) => node.child_ids.length > 0)
        .map((node) => node.node_id),
    );
    closeTreeExpandMenu();
  }, [closeTreeExpandMenu, matrix?.nodes, setExpandedList]);

  const collapseAll = useCallback(() => {
    setExpandedList([]);
    closeTreeExpandMenu();
  }, [closeTreeExpandMenu, setExpandedList]);

  const collapseToCurrentPath = useCallback(() => {
    setExpandedList(Array.from(matrixAncestorIds(matrix, selectedId)));
    closeTreeExpandMenu();
  }, [closeTreeExpandMenu, matrix, selectedId, setExpandedList]);

  const revealCurrentNode = useCallback(() => {
    if (!matrix || !selectedId) return;
    const nextExpanded = new Set(expandedIds);
    matrixAncestorIds(matrix, selectedId).forEach((nodeId) =>
      nextExpanded.add(nodeId),
    );
    const nextList = Array.from(nextExpanded);
    setExpandedList(nextList);
    const nextRows = flattenRequirementMatrix(
      matrix,
      nextExpanded,
      query,
      filter,
      facets,
    );
    const index = nextRows.findIndex((row) => row.node.node_id === selectedId);
    if (index >= 0) {
      requestAnimationFrame(() => {
        scrollToIndex(index);
        rowRefs.current.get(selectedId)?.focus();
      });
    }
  }, [
    expandedIds,
    filter,
    matrix,
    query,
    scrollToIndex,
    selectedId,
    setExpandedList,
  ]);

  const focusRow = useCallback(
    (index: number, force = false) => {
      const bounded = Math.max(0, Math.min(rows.length - 1, index));
      const next = rows[bounded];
      if (!next) return;
      if (
        !force &&
        next.node.node_id !== selectedId &&
        hasUnsavedChanges &&
        !window.confirm("当前修改尚未保存，确定切换到其他内容吗？")
      ) {
        requestAnimationFrame(() => rowRefs.current.get(selectedId)?.focus());
        return;
      }
      setSelectedId(next.node.node_id);
      scrollToIndex(bounded);
      requestAnimationFrame(() =>
        rowRefs.current.get(next.node.node_id)?.focus(),
      );
    },
    [hasUnsavedChanges, rows, scrollToIndex, selectedId, setSelectedId],
  );

  const saveAndAdvance = useCallback(async () => {
    const saved = await saveRef.current?.();
    if (!saved) return;
    const currentIndex = rows.findIndex(
      (row) => row.node.node_id === selectedId,
    );
    const isPendingAnalysisItem = (row: RequirementMatrixRow) =>
      isCandidateReview ? Boolean(row.node.candidate_review && row.node.candidate_review.status !== "reviewed") : matrixNeedsAnalysis(row.node);
    const after = rows
      .slice(Math.max(0, currentIndex + 1))
      .find(isPendingAnalysisItem);
    const before = rows
      .slice(0, Math.max(0, currentIndex))
      .find(isPendingAnalysisItem);
    const next = after || before;
    if (!next) return;
    const nextIndex = rows.findIndex(
      (row) => row.node.node_id === next.node.node_id,
    );
    if (nextIndex >= 0) focusRow(nextIndex, true);
  }, [focusRow, rows, selectedId, isCandidateReview]);

  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      const target = event.target;
      if (
        event.defaultPrevented ||
        !(target instanceof Node) ||
        !workbenchRef.current?.contains(target)
      ) {
        return;
      }
      if (target instanceof Element && target.closest("dialog[open]")) return;
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        void saveAndAdvance();
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setResetSignal((current) => current + 1);
        requestAnimationFrame(() => rowRefs.current.get(selectedId)?.focus());
      }
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [saveAndAdvance, selectedId]);

  const onTreeKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>, row: RequirementMatrixRow) => {
      const index = rows.findIndex(
        (candidate) => candidate.node.node_id === row.node.node_id,
      );
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        void saveAndAdvance();
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        focusRow(index + 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        focusRow(index - 1);
      } else if (event.key === "Home") {
        event.preventDefault();
        focusRow(0);
      } else if (event.key === "End") {
        event.preventDefault();
        focusRow(rows.length - 1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        if (row.hasChildren && !row.expanded) {
          setExpanded(row.node.node_id, true);
        } else if (row.hasChildren) {
          focusRow(index + 1);
        }
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        if (row.hasChildren && row.expanded) {
          setExpanded(row.node.node_id, false);
        } else if (row.node.parent_id) {
          const parentIndex = rows.findIndex(
            (candidate) => candidate.node.node_id === row.node.parent_id,
          );
          if (parentIndex >= 0) focusRow(parentIndex);
        }
      } else if (event.key === "Enter") {
        event.preventDefault();
        setDetailPane(lens === "analysis" ? "inspector" : "source");
      } else if (event.key === "Escape") {
        event.preventDefault();
        setResetSignal((current) => current + 1);
        requestAnimationFrame(() =>
          rowRefs.current.get(row.node.node_id)?.focus(),
        );
      }
    },
    [focusRow, lens, rows, saveAndAdvance, setDetailPane, setExpanded],
  );

  const handleNodeUpdated = useCallback(
    (node: RequirementMatrixNode, counts?: JsonRecord) => {
      setMatrix((current) =>
        current ? replaceMatrixNode(current, node, counts) : current,
      );
      setNodeDetail((current) =>
        current && current.node.node_id === node.node_id
          ? { ...current, node }
          : current,
      );
    },
    [],
  );
  const handleImpactChanged = useCallback(
    (payload: JsonRecord) => {
      setImpactFeedback({
        requirementSetId: selectedSetId,
        staleResultNodeIds: impactStaleResultNodeIds(payload),
      });
    },
    [selectedSetId],
  );

  const aiCapabilities = asRecord(aiStatus?.capabilities);
  const aiCapabilitySchema = String(aiCapabilities.schema_version || "");
  const atomicScopedAnalysisSupported =
    aiCapabilities.schema_version === "ai-analysis-capabilities/v1" &&
    aiCapabilities.analysis_scope_contract ===
      "requirement-analysis-scope/v1" &&
    aiCapabilities.scope_preview === true &&
    aiCapabilities.scoped_start === true &&
    aiCapabilities.atomic_batch_publication === true;
  const recoverableAnalysisSupported =
    aiCapabilities.schema_version === "ai-analysis-capabilities/v2" &&
    aiCapabilities.analysis_scope_contract ===
      "requirement-analysis-scope/v2" &&
    aiCapabilities.scope_preview === true &&
    aiCapabilities.scoped_start === true &&
    aiCapabilities.per_node_recoverable_publication === true &&
    aiCapabilities.node_execution_status === true &&
    aiCapabilities.failed_only_retry === true &&
    aiCapabilities.controlled_parallelism === true;
  const scopedAnalysisSupported =
    atomicScopedAnalysisSupported || recoverableAnalysisSupported;
  const legacyAnalysisSupported =
    aiStatus !== null && !aiCapabilitySchema;
  const reportedAiJob = asRecord(aiStatus?.job);
  const reportedAiRequirementSetId = String(
    reportedAiJob.requirement_set_id || "",
  );
  const currentAiJob =
    reportedAiRequirementSetId &&
    reportedAiRequirementSetId === selectedSetId
      ? reportedAiJob
      : {};
  const currentAiJobId = String(currentAiJob.id || currentAiJob.job_id || "");
  const currentAiStatus = String(
    currentAiJob.status || currentAiJob.job_status || "",
  ).toUpperCase();
  const aiRunning = isAiAnalysisActiveStatus(currentAiStatus);
  const recoverableJobModeValid =
    currentAiJob.publication_mode === "PER_NODE_RECOVERABLE";
  const atomicJobModeValid =
    currentAiJob.publication_mode === "ATOMIC_BATCH";
  const recoverableJobReported =
    recoverableAnalysisSupported &&
    currentAiJobId.length > 0 &&
    recoverableJobModeValid;
  const aiExecutionCounts = recoverableJobModeValid
    ? normalizeAnalysisExecutionCounts(currentAiJob.execution_counts)
    : null;
  const normalizedNodeExecutions = recoverableJobModeValid
    ? normalizeAnalysisNodeExecutions(
        currentAiJob.node_executions,
        currentAiJobId,
      )
    : null;
  const recoverableExecutionStatusValid = Boolean(
    aiExecutionCounts &&
      normalizedNodeExecutions &&
    analysisExecutionsMatchCounts(normalizedNodeExecutions, aiExecutionCounts),
  );
  const recoverableJobMalformed =
    (recoverableJobReported && !recoverableExecutionStatusValid) ||
    (recoverableAnalysisSupported &&
      currentAiJobId.length > 0 &&
      !recoverableJobModeValid &&
      !atomicJobModeValid);
  const analysisActionsSupported =
    scopedAnalysisSupported && !recoverableJobMalformed;
  const aiNodeExecutions = recoverableExecutionStatusValid
    ? normalizedNodeExecutions || []
    : [];
  const aiNodeExecutionByNodeId = useMemo(
    () =>
      new Map(
        aiNodeExecutions.map((execution) => [execution.node_id, execution]),
      ),
    [aiNodeExecutions],
  );
  const aiTotalCount = currentAiJobId
    ? (aiExecutionCounts?.total ??
      nonNegativeInteger(currentAiJob.total_requirements))
    : 0;
  const aiCompletedCount = Math.min(
    aiExecutionCounts?.settled ??
      nonNegativeInteger(
        currentAiJob.computed_requirements ?? currentAiJob.completed_requirements,
      ),
    aiTotalCount,
  );
  const aiPublishedCount = Math.min(
    recoverableExecutionStatusValid
      ? aiNodeExecutions.filter((execution) => execution.published).length
      : (optionalNonNegativeInteger(currentAiJob.published_requirements) ??
          (currentAiStatus === "COMPLETED" ? aiTotalCount : 0)),
    aiTotalCount,
  );
  const aiCurrentRequirement = String(
    currentAiJob.current_requirement_code ||
      currentAiJob.current_requirement_id ||
      "",
  ).trim();
  const aiCurrentStage = String(currentAiJob.current_stage || currentAiStatus)
    .trim()
    .toLowerCase();
  const aiActiveCount = Math.min(
    aiExecutionCounts?.running ??
      optionalNonNegativeInteger(currentAiJob.running_requirements) ??
      (aiRunning && aiCurrentRequirement && aiCompletedCount < aiTotalCount ? 1 : 0),
    aiTotalCount,
  );
  const aiPublishingCount = Math.min(
    aiExecutionCounts?.publishing ?? 0,
    aiTotalCount,
  );
  const aiWaitingCount = Math.min(
    aiExecutionCounts?.waiting ??
      optionalNonNegativeInteger(currentAiJob.waiting_requirements) ??
      Math.max(aiTotalCount - aiCompletedCount - aiActiveCount, 0),
    aiTotalCount,
  );
  const aiSucceededCount = aiExecutionCounts?.succeeded ?? null;
  const aiFailedCount =
    aiExecutionCounts?.failed ??
    optionalNonNegativeInteger(currentAiJob.failed_requirements);
  const aiCancelledCount = aiExecutionCounts?.cancelled ?? null;
  const aiRetryableFailedCount = aiExecutionCounts?.retryable_failed ?? 0;
  const aiRemainingLabel = aiRunning ? "等待" : "未完成";
  const aiCanCancel =
    aiRunning &&
    (recoverableJobModeValid || aiCurrentStage !== "publishing") &&
    currentAiJob.can_cancel !== false;
  const aiCancelRequested =
    currentAiStatus === "CANCEL_REQUESTED" || aiCurrentStage === "cancelling";
  const aiPublishing = aiCurrentStage === "publishing";
  const aiScope = String(currentAiJob.scope || "ALL").toUpperCase() as
    RequirementAnalysisScope | "";
  const aiLastMessage = String(currentAiJob.last_message || "").trim();
  const canRetryFailedAnalysis = Boolean(
    recoverableAnalysisSupported &&
      recoverableExecutionStatusValid &&
      !aiRunning &&
      aiRetryableFailedCount > 0,
  );
  const analysisRequestCapabilityAvailable = analysisScopeRequest
    ? analysisScopeRequest.schema_version ===
      "requirement-analysis-scope-request/v2"
      ? recoverableAnalysisSupported && !recoverableJobMalformed
      : atomicScopedAnalysisSupported
    : false;
  useEffect(() => {
    const previous = previousAiJobRef.current;
    const current = {
      id: currentAiJobId,
      status: currentAiStatus,
      published: aiPublishedCount,
    };
    const sameJob =
      !previous?.id || !current.id || previous.id === current.id;
    const reachedTerminal = Boolean(
      previous &&
        sameJob &&
        isAiAnalysisActiveStatus(previous.status) &&
        current.status &&
        !isAiAnalysisActiveStatus(current.status),
    );
    const publishedMore = Boolean(
      previous &&
        sameJob &&
        recoverableJobModeValid &&
        current.published > previous.published,
    );
    if (reachedTerminal || publishedMore) {
      if (hasUnsavedChanges) setMatrixRefreshPending(true);
      else void loadMatrix();
    }
    previousAiJobRef.current = current;
  }, [
    aiPublishedCount,
    currentAiJobId,
    currentAiStatus,
    hasUnsavedChanges,
    loadMatrix,
    recoverableJobModeValid,
  ]);

  useEffect(() => {
    if (!matrixRefreshPending || hasUnsavedChanges) return;
    setMatrixRefreshPending(false);
    void loadMatrix();
  }, [hasUnsavedChanges, loadMatrix, matrixRefreshPending]);
  const canEditDisposition = Boolean(
    selectedNode &&
      selectedNode.child_ids.length === 0 &&
      selectedNode.matrix_visibility !== "SYSTEM_NON_BUSINESS" &&
      selectedSetId === activeSetId &&
      setStatus !== "LOCKED",
  );
  const canWriteDecision = Boolean(
    selectedNode &&
      matrixAnalysisEligible(selectedNode) &&
      selectedSetId === activeSetId &&
      setStatus === "LOCKED",
  );
  const canAnalyzeCurrent = Boolean(
    lens === "analysis" &&
      selectedNode &&
      matrixAnalysisEligible(selectedNode) &&
      selectedSetId === activeSetId &&
      setStatus === "LOCKED",
  );
  const canBatchDisposition = Boolean(
    lens === "review" &&
      matrix &&
      selectedSetId === activeSetId &&
      setStatus !== "LOCKED",
  );
  const resetWorkflowDialog = useCallback(() => {
    lockReadinessRequestRef.current += 1;
    analysisPreviewRequestRef.current += 1;
    setWorkflowDialog(null);
    setWorkflowError("");
    setWorkflowConflict(false);
    setAnalysisScopeRequest(null);
    setAnalysisScopePreview(null);
    setAnalysisPreviewLoading(false);
    setAnalysisPreviewError("");
    setAnalysisRetryKind("");
    setLegacyAnalysisConfirmation(false);
  }, []);
  const closeWorkflowDialog = useCallback(() => {
    if (workflowPending) return;
    resetWorkflowDialog();
  }, [resetWorkflowDialog, workflowPending]);
  useEffect(() => {
    if (workflowDialog !== "analysis" || workflowPending) return;
    const capabilityLost = legacyAnalysisConfirmation
      ? !legacyAnalysisSupported
      : Boolean(analysisScopeRequest) && !analysisRequestCapabilityAvailable;
    if (capabilityLost) resetWorkflowDialog();
  }, [
    analysisScopeRequest,
    legacyAnalysisConfirmation,
    legacyAnalysisSupported,
    resetWorkflowDialog,
    analysisRequestCapabilityAvailable,
    workflowDialog,
    workflowPending,
  ]);
  const openLockConfirmation = useCallback(() => {
    setWorkflowDialog("lock");
    setWorkflowError("");
    setWorkflowConflict(false);
    void loadLockReadiness();
  }, [loadLockReadiness]);
  const openUnlockConfirmation = useCallback(() => {
    setWorkflowDialog("unlock");
    setWorkflowError("");
    setWorkflowConflict(false);
  }, []);
  const recoverUnlockConflict = useCallback(async () => {
    if (workflowPending) return;
    closeWorkflowDialog();
    await refreshDocument();
  }, [closeWorkflowDialog, refreshDocument, workflowPending]);
  const lockConfirmationList = useCallback(async () => {
    if (
      workflowPending ||
      !lockReadiness ||
      !lockReadiness.can_lock ||
      lockReadiness.hard_blockers.length > 0 ||
      !lockReadiness.readiness_sha256
    ) {
      return;
    }
    setWorkflowPending("lock");
    setWorkflowError("");
    setWorkflowConflict(false);
    let actionError: unknown = null;
    let locked: Awaited<ReturnType<typeof api.lockExtraction>> | null = null;
    try {
      locked = await runAction(
        "锁定确认清单",
        async () => {
          try {
            return await api.lockExtraction(
              route.projectId,
              route.documentId,
              selectedSetId,
              lockReadiness.readiness_sha256,
            );
          } catch (cause) {
            actionError = cause;
            throw cause;
          }
        },
        { refresh: "document", scope: "requirementSet" },
      );
    } catch (cause) {
      actionError = cause;
    }
    if (!locked) {
      const error = workflowErrorMessage(actionError, "lock");
      setWorkflowPending(null);
      setWorkflowError(error.message);
      setWorkflowConflict(error.conflict);
      if (error.conflict) setLockReadiness(null);
      return;
    }
    const lockedPayload = asRecord(locked);
    const lockedReadiness = asRecord(asRecord(lockedPayload.readiness).lock);
    const lockedSummary = asRecord(lockedPayload.summary);
    const lockedCounts = asRecord(lockedPayload.counts);
    const lockedAnalysisCount = optionalNonNegativeInteger(
      lockedCounts.analysis_targets,
      lockedPayload.locked_requirement_count,
      lockedReadiness.analysis_target_count,
      lockedSummary.matrix_analysis_target_count,
      lockReadiness.counts.analysis_targets,
    ) ?? 0;
    const nextDestination = String(
      lockedPayload.next_destination || lockReadiness.next_destination,
    ).toUpperCase();
    setWorkflowPending(null);
    setWorkflowDialog(null);
    setBatchOpen(false);
    setBatchState("");
    setBatchUndo([]);
    if (
      lockedAnalysisCount !== lockReadiness.counts.analysis_targets
    ) {
      await loadMatrix();
    }
    if (lockedAnalysisCount === 0 || nextDestination === "MATRIX") {
      setZeroAnalysisLocked(true);
      return;
    }
    setZeroAnalysisLocked(false);
    updateRoute({ task: "analysis", requirementSetId: selectedSetId });
  }, [
    loadMatrix,
    lockReadiness,
    route.documentId,
    route.projectId,
    runAction,
    selectedSetId,
    updateRoute,
    workflowPending,
  ]);
  const toggleAnalysisSelection = useCallback(
    (nodeId: string) => {
      if (aiRunning) return;
      setAnalysisSelectionList((current) =>
        current.includes(nodeId)
          ? current.filter((item) => item !== nodeId)
          : [...current, nodeId],
      );
    },
    [aiRunning, setAnalysisSelectionList],
  );
  const openAnalysisConfirmation = useCallback(
    async (scope: RequirementAnalysisScope, sourceJobId = "") => {
      if (
        !matrix ||
        aiRunning ||
        !aiProfileReady ||
        !analysisActionsSupported ||
        (scope === "FAILED_ONLY" &&
          (!recoverableAnalysisSupported ||
            !canRetryFailedAnalysis ||
            sourceJobId !== currentAiJobId))
      ) {
        return;
      }
      const nodeIds =
        scope === "CURRENT"
          ? selectedNode && matrixAnalysisEligible(selectedNode)
            ? [selectedNode.node_id]
            : []
          : scope === "SELECTED"
            ? selectedAnalysisNodes.map((node) => node.node_id)
            : [];
      if (!["ALL", "FAILED_ONLY"].includes(scope) && nodeIds.length === 0) return;
      const requestIdentity = {
        command_id: analysisScopeCommandId(),
        requirement_set_id: selectedSetId,
        expected_tree_sha256: matrix.tree_sha256,
      };
      const request: RequirementAnalysisScopeRequest =
        scope === "FAILED_ONLY"
          ? {
              ...requestIdentity,
              schema_version: "requirement-analysis-scope-request/v2",
              scope,
              node_ids: [],
              source_job_id: sourceJobId,
            }
          : recoverableAnalysisSupported
            ? {
                ...requestIdentity,
                schema_version: "requirement-analysis-scope-request/v2",
                scope,
                node_ids: nodeIds,
              }
            : {
                ...requestIdentity,
                schema_version: "requirement-analysis-scope-request/v1",
                scope,
                node_ids: nodeIds,
              };
      const requestId = ++analysisPreviewRequestRef.current;
      setAnalysisScopeRequest(request);
      setAnalysisScopePreview(null);
      setAnalysisPreviewError("");
      setAnalysisRetryKind("");
      setAnalysisPreviewLoading(true);
      setLegacyAnalysisConfirmation(false);
      setWorkflowDialog("analysis");
      try {
        const preview = await api.previewAiScope(
          route.projectId,
          route.documentId,
          request,
        );
        if (requestId !== analysisPreviewRequestRef.current) return;
        if (!analysisScopePreviewMatchesRequest(preview, request)) {
          throw new Error("analysis_scope_preview_identity_mismatch");
        }
        setAnalysisScopePreview(preview);
      } catch (cause) {
        if (requestId !== analysisPreviewRequestRef.current) return;
        const conflictCode = apiErrorCode(cause);
        const isConflict = cause instanceof ApiError && cause.status === 409;
        if (
          conflictCode === "analysis_requirement_set_not_found" ||
          conflictCode === "requirement_set_selection_changed" ||
          conflictCode === "analysis_requirement_set_not_current" ||
          conflictCode === "analysis_requirement_set_not_locked"
        ) {
          resetWorkflowDialog();
          await refreshDocument();
          return;
        }
        if (
          conflictCode === "analysis_tree_changed" ||
          conflictCode === "analysis_scope_node_not_in_requirement_set"
        ) {
          resetWorkflowDialog();
          await loadMatrix();
          return;
        }
        if (isConflict) {
          setAnalysisRetryKind("");
          setAnalysisScopePreview(null);
          setAnalysisPreviewError(
            conflictCode === "analysis_tree_identity_mismatch" ||
              conflictCode === "matrix_requires_reextraction"
              ? "当前冻结需求矩阵无法验证。请返回需求提取重新生成并锁定清单。"
              : "服务端拒绝了当前分析范围。本次不会启动任务，请关闭后刷新再试。",
          );
          return;
        }
        if (
          cause instanceof Error &&
          cause.message === "analysis_scope_preview_identity_mismatch"
        ) {
          setAnalysisRetryKind("");
          setAnalysisScopePreview(null);
          setAnalysisPreviewError(
            "服务端返回的范围身份与当前清单不一致。本次不会启动任务，请关闭并同步清单。",
          );
          return;
        }
        if (
          cause instanceof ApiError &&
          cause.status >= 400 &&
          cause.status < 500
        ) {
          setAnalysisRetryKind("");
          setAnalysisScopePreview(null);
          setAnalysisPreviewError(
            cause.message || "当前范围不能启动分析，请关闭后检查清单。",
          );
          return;
        }
        setAnalysisRetryKind("preview");
        setAnalysisPreviewError("暂时无法核对分析范围。请检查连接后重新读取。");
      } finally {
        if (requestId === analysisPreviewRequestRef.current) {
          setAnalysisPreviewLoading(false);
        }
      }
    },
    [
      aiProfileReady,
      aiRunning,
      analysisActionsSupported,
      canRetryFailedAnalysis,
      currentAiJobId,
      matrix,
      loadMatrix,
      refreshDocument,
      resetWorkflowDialog,
      route.documentId,
      route.projectId,
      recoverableAnalysisSupported,
      selectedAnalysisNodes,
      selectedNode,
      selectedSetId,
    ],
  );
  const openLegacyAnalysisConfirmation = useCallback(() => {
    if (aiRunning || !aiProfileReady || !legacyAnalysisSupported) return;
    analysisPreviewRequestRef.current += 1;
    setAnalysisScopeRequest(null);
    setAnalysisScopePreview(null);
    setAnalysisPreviewLoading(false);
    setAnalysisPreviewError("");
    setAnalysisRetryKind("");
    setLegacyAnalysisConfirmation(true);
    setWorkflowDialog("analysis");
  }, [aiProfileReady, aiRunning, legacyAnalysisSupported]);
  const startAnalysis = useCallback(async () => {
    if (
      workflowPending ||
      !analysisScopeRequest ||
      !analysisScopePreview ||
      !analysisRequestCapabilityAvailable
    ) {
      return;
    }
    setWorkflowPending("analysis");
    let startError: unknown = null;
    const started = await runAction(
      "启动需求分析",
      async () => {
        try {
          return await api.startAi(
            route.projectId,
            route.documentId,
            {
              ...analysisScopeRequest,
              expected_scope_sha256: analysisScopePreview.scope_sha256,
            ...(selectedAiProfileId ? { profile_id: selectedAiProfileId } : {}),
          });
        } catch (cause) {
          startError = cause;
          throw cause;
        }
      },
      {
        refresh: "none",
        scope: "requirementSet",
        successNotice: `已创建${analysisScopeLabel(analysisScopeRequest.scope)} ${analysisScopePreview.target_count} 项分析任务`,
        failureNotice: "分析任务未创建",
      },
    );
    setWorkflowPending(null);
    if (!started) {
      const conflictCode = apiErrorCode(startError);
      const isConflict =
        startError instanceof ApiError && startError.status === 409;
      if (
        conflictCode === "analysis_job_already_running" ||
        conflictCode === "analysis_command_id_conflict"
      ) {
        resetWorkflowDialog();
        await refreshAi();
        return;
      }
      if (
        conflictCode === "analysis_requirement_set_not_found" ||
        conflictCode === "requirement_set_selection_changed" ||
        conflictCode === "analysis_requirement_set_not_current" ||
        conflictCode === "analysis_requirement_set_not_locked" ||
        conflictCode === "analysis_tree_identity_mismatch"
      ) {
        resetWorkflowDialog();
        await refreshDocument();
        return;
      }
      const requiresMatrixReselection =
        conflictCode === "analysis_scope_changed" ||
        conflictCode === "analysis_scope_empty" ||
        conflictCode === "matrix_has_no_analysis_targets" ||
        conflictCode === "analysis_tree_changed" ||
        conflictCode === "analysis_scope_node_not_in_requirement_set";
      if (requiresMatrixReselection) {
        resetWorkflowDialog();
        await loadMatrix();
        return;
      }
      const canRepreview = conflictCode === "analysis_knowledge_base_changed";
      if (isConflict && !canRepreview) {
        resetWorkflowDialog();
        await Promise.all([refreshDocument(), refreshAi()]);
        return;
      }
      if (
        startError instanceof ApiError &&
        startError.status >= 400 &&
        startError.status < 500
      ) {
        setAnalysisRetryKind("");
        setAnalysisPreviewError(
          startError.message || "当前范围不能启动分析，请关闭后检查清单。",
        );
        return;
      }
      if (canRepreview) {
        setAnalysisScopePreview(null);
      }
      setAnalysisRetryKind(canRepreview ? "preview" : "start");
      setAnalysisPreviewError(
        canRepreview
          ? "清单或已确认范围已经变化。本次任务没有创建，请重新核对范围。"
          : "没有收到明确的启动结果。将使用同一操作 ID 重试，以免重复创建任务。",
      );
      return;
    }
    setWorkflowDialog(null);
    setAnalysisScopeRequest(null);
    setAnalysisScopePreview(null);
    setAnalysisPreviewError("");
    setAnalysisRetryKind("");
    setLegacyAnalysisConfirmation(false);
    await refreshAi();
  }, [
    analysisScopePreview,
    analysisScopeRequest,
    loadMatrix,
    refreshAi,
    refreshDocument,
    resetWorkflowDialog,
    route.documentId,
    route.projectId,
    runAction,
    selectedAiProfileId,
    analysisRequestCapabilityAvailable,
    workflowPending,
  ]);
  const startLegacyAnalysis = useCallback(async () => {
    if (
      workflowPending ||
      !legacyAnalysisConfirmation ||
      !legacyAnalysisSupported
    ) {
      return;
    }
    setWorkflowPending("analysis");
    let startError: unknown = null;
    const started = await runAction(
      "启动全部需求分析",
      async () => {
        try {
          return await api.startAiLegacy(
            route.projectId,
            route.documentId,
            selectedSetId,
            selectedAiProfileId,
          );
        } catch (cause) {
          startError = cause;
          throw cause;
        }
      },
      {
        refresh: "none",
        scope: "requirementSet",
        successNotice: `已创建全部 ${actualAnalysisCount} 项分析任务`,
        failureNotice: "分析任务未创建",
      },
    );
    setWorkflowPending(null);
    if (!started) {
      setWorkflowError(
        startError instanceof ApiError && startError.status === 409
          ? "当前清单或分析任务状态已经变化，本次任务没有创建。请关闭后刷新再试。"
          : "分析任务没有创建。请检查连接后重试。",
      );
      return;
    }
    setWorkflowDialog(null);
    setWorkflowError("");
    setLegacyAnalysisConfirmation(false);
    await refreshAi();
  }, [
    actualAnalysisCount,
    legacyAnalysisConfirmation,
    legacyAnalysisSupported,
    refreshAi,
    route.documentId,
    route.projectId,
    runAction,
    selectedAiProfileId,
    selectedSetId,
    workflowPending,
  ]);
  const stopAnalysis = useCallback(async () => {
    if (workflowPending) return;
    setWorkflowPending("stop");
    setWorkflowError("");
    let stopError: unknown = null;
    const stopped = await runAction(
      "提交停止分析请求",
      async () => {
        try {
          return await api.stopAi(
            route.projectId,
            route.documentId,
            currentAiJobId,
          );
        } catch (cause) {
          stopError = cause;
          throw cause;
        }
      },
      {
        refresh: "none",
        scope: "requirementSet",
        successNotice: "已提交停止请求，正在等待当前步骤结束",
        failureNotice: "停止请求未提交",
      },
    );
    setWorkflowPending(null);
    if (!stopped) {
      setWorkflowError(
        stopError instanceof ApiError && stopError.status === 409
          ? "任务状态已经变化，停止请求未提交。请关闭后查看最新进度。"
          : "停止请求未提交。请检查连接后重试。",
      );
      return;
    }
    setWorkflowDialog(null);
    await refreshAi();
  }, [
    currentAiJobId,
    refreshAi,
    route.documentId,
    route.projectId,
    runAction,
    workflowPending,
  ]);
  const deriveEditableConfirmation = useCallback(async () => {
    if (workflowPending) return;
    setWorkflowPending("unlock");
    setWorkflowError("");
    setWorkflowConflict(false);
    let actionError: unknown = null;
    let derived: {
      requirementSetId: string;
      staleResultNodeIds: string[];
    } | null = null;
    try {
      derived = await runAction(
        "解锁并修改",
        async () => {
          try {
            const payload = await api.deriveRequirementSet(
              route.projectId,
              route.documentId,
              selectedSetId,
            );
            const requirementSetId = derivedRequirementSetId(payload);
            if (!requirementSetId) {
              throw new Error("editable_confirmation_missing");
            }
            return {
              requirementSetId,
              staleResultNodeIds: impactStaleResultNodeIds(payload),
            };
          } catch (cause) {
            actionError = cause;
            throw cause;
          }
        },
        { refresh: "document", scope: "requirementSet" },
      );
    } catch (cause) {
      actionError = cause;
    }
    if (!derived) {
      const error = workflowErrorMessage(actionError, "unlock");
      setWorkflowPending(null);
      setWorkflowError(error.message);
      setWorkflowConflict(error.conflict);
      return;
    }
    setWorkflowPending(null);
    setWorkflowDialog(null);
    setImpactFeedback({
      requirementSetId: derived.requirementSetId,
      staleResultNodeIds: derived.staleResultNodeIds,
    });
    updateRoute({
      task: "extraction",
      mode: "review",
      checkpoint: "review",
      requirementSetId: derived.requirementSetId,
    });
  }, [
    route.documentId,
    route.projectId,
    runAction,
    selectedSetId,
    updateRoute,
    workflowPending,
  ]);
  const applyBatchDisposition = useCallback(async () => {
    if (
      !matrix ||
      !canBatchDisposition ||
      !batchCandidateNodes.length ||
      batchState === "保存中…" ||
      batchState === "正在撤销…"
    ) {
      return;
    }
    const previous = batchCandidateNodes.map((node) => ({
      nodeId: node.node_id,
      disposition: node.disposition,
      reason: String(node.disposition_reason || ""),
    }));
    const mutationRequest = beginMatrixMutation("batch");
    setBatchState("保存中…");
    try {
      const response = await api.updateRequirementMatrixDispositions(
        route.projectId,
        route.documentId,
        selectedSetId,
        {
          schema_version: "requirement-matrix-disposition-batch-patch/v1",
          expected_tree_sha256: matrix.tree_sha256,
          items: batchCandidateNodes.map((node) => ({
            node_id: node.node_id,
            disposition: batchDisposition,
            reason: batchReason.trim(),
            expected_revision: node.save_revision,
          })),
        },
      );
      if (!isMatrixMutationCurrent(mutationRequest)) return;
      const responseRecord = asRecord(response);
      handleImpactChanged(responseRecord);
      const updatedNodes = asArray<RequirementMatrixNode>(responseRecord.nodes);
      const updatedById = new Map(
        updatedNodes.map((node) => [node.node_id, node]),
      );
      setMatrix((current) =>
        current
          ? replaceMatrixNodes(
              current,
              updatedNodes,
              asRecord(responseRecord.counts),
            )
          : current,
      );
      setBatchUndo(
        previous.flatMap((item) => {
          const updated = updatedById.get(item.nodeId);
          return updated
            ? [
                {
                  ...item,
                  expectedRevision: updated.save_revision,
                },
              ]
            : [];
        }),
      );
      setBatchState(`已批量处理 ${updatedNodes.length} 项`);
      setBatchOpen(false);
      setBatchReason("");
    } catch (cause) {
      if (!isMatrixMutationCurrent(mutationRequest)) return;
      setBatchState(
        cause instanceof Error ? cause.message : "批量处理失败，请重试。",
      );
    }
  }, [
    batchCandidateNodes,
    batchDisposition,
    batchReason,
    batchState,
    beginMatrixMutation,
    canBatchDisposition,
    handleImpactChanged,
    matrix,
    isMatrixMutationCurrent,
    route.documentId,
    route.projectId,
    selectedSetId,
  ]);
  const undoBatchDisposition = useCallback(async () => {
    if (
      !matrix ||
      !canBatchDisposition ||
      !batchUndo.length ||
      batchState === "保存中…" ||
      batchState === "正在撤销…"
    ) {
      return;
    }
    const mutationRequest = beginMatrixMutation("batch");
    setBatchState("正在撤销…");
    try {
      const response = await api.updateRequirementMatrixDispositions(
        route.projectId,
        route.documentId,
        selectedSetId,
        {
          schema_version: "requirement-matrix-disposition-batch-patch/v1",
          expected_tree_sha256: matrix.tree_sha256,
          items: batchUndo.map((item) => ({
            node_id: item.nodeId,
            disposition: item.disposition,
            reason: item.reason,
            expected_revision: item.expectedRevision,
          })),
        },
      );
      if (!isMatrixMutationCurrent(mutationRequest)) return;
      const responseRecord = asRecord(response);
      handleImpactChanged(responseRecord);
      const updatedNodes = asArray<RequirementMatrixNode>(responseRecord.nodes);
      setMatrix((current) =>
        current
          ? replaceMatrixNodes(
              current,
              updatedNodes,
              asRecord(responseRecord.counts),
            )
          : current,
      );
      setBatchUndo([]);
      setBatchState(`已撤销 ${updatedNodes.length} 项批量处理`);
    } catch (cause) {
      if (!isMatrixMutationCurrent(mutationRequest)) return;
      setBatchState(
        cause instanceof Error ? cause.message : "撤销失败，请刷新后重试。",
      );
    }
  }, [
    batchUndo,
    batchState,
    beginMatrixMutation,
    canBatchDisposition,
    handleImpactChanged,
    matrix,
    isMatrixMutationCurrent,
    route.documentId,
    route.projectId,
    selectedSetId,
  ]);
  const pdfPath = route.projectId && route.documentId
    ? `/api/projects/${encodeURIComponent(route.projectId)}/sor-documents/${encodeURIComponent(route.documentId)}/file`
    : "";
  const pdfViewerUrl = pdfPath
    ? `/pdf-viewer?embedded=1&keyboard=parent&fit=page&pdf=${encodeURIComponent(pdfPath)}&page=${pdfPage}`
    : "";

  if (!route.projectId || !route.documentId) {
    return (
      <EmptyState
        icon="projects"
        title="先选择项目和来源文件"
        description="统一需求矩阵严格绑定项目、文档和需求版本。"
      />
    );
  }
  if (requirementSets === null || matrixLoading) {
    return <LoadingBlock label="正在读取统一需求矩阵" />;
  }
  if (!selectedSetId) {
    return (
      <EmptyState
        icon="matrix"
        title="尚无确认清单"
        description="请先从解析核对开始，完成结构整理并生成确认清单。"
        action={
          <button
            className="button primary"
            type="button"
            onClick={() =>
              updateRoute({
                task: "extraction",
                mode: "organize",
                checkpoint: "diagnose",
                requirementSetId: "",
              })
            }
          >
            前往需求提取
          </button>
        }
      />
    );
  }
  if (matrixError) return <ErrorBlock error={matrixError} onRetry={loadMatrix} />;
  if (matrix?.state === "REQUIRES_REEXTRACTION") {
    return (
      <EmptyState
        icon="refresh"
        title="该历史版本需要重新提取"
        description="当前版本缺少可证明的冻结结构或证据，不会用后来变化的整理结果猜测补齐。"
        action={
          <button
            className="button primary"
            type="button"
            onClick={() =>
              updateRoute({
                task: "extraction",
                mode: "organize",
                checkpoint: "diagnose",
                requirementSetId: "",
              })
            }
          >
            前往需求提取
          </button>
        }
      />
    );
  }
  if (!matrix || !matrix.nodes.length) {
    return (
      <EmptyState
        icon="tree"
        title="需求矩阵尚未生成"
        description="完成结构整理并生成确认清单后，层级内容会在这里出现。"
        action={
          <button
            className="button primary"
            type="button"
            onClick={() =>
              updateRoute({
                task: "extraction",
                mode: "organize",
                checkpoint: "diagnose",
                requirementSetId: "",
              })
            }
          >
            前往需求提取
          </button>
        }
      />
    );
  }

  const sourceSurface = (
    <>
      {(detailError || tableError) && (
        <div className="matrix-detail-error" role="alert">
          <InlineNotice title="部分版本内容读取失败" tone="warning">
            {[detailError, tableError].filter(Boolean).join(" ")}
            <button
              className="text-button"
              type="button"
              onClick={() => void loadSelectedDetail()}
            >
              重试
            </button>
          </InlineNotice>
        </div>
      )}
      {selectedRecordPresentationState === "loading" ? (
        <LoadingBlock label="正在还原当前表格条目…" />
      ) : selectedRecordPresentationState === "error" ? (
        <EmptyState
          icon="matrix"
          title="当前表格条目尚未还原"
          description="没有取得可验证的当前行和表头，因此不会用整张原表临时替代。"
          action={
            <button
              className="button secondary"
              type="button"
              onClick={() => void loadSelectedDetail()}
            >
              <Icon name="refresh" size={14} />
              重试
            </button>
          }
        />
      ) : (
        <MatrixSourceContent
          node={selectedNodeDetail?.node || selectedNode}
          contextPath={selectedNodeDetail?.path || []}
          parentClause={selectedNodeDetail?.parent_clause || null}
          logicalTables={logicalTables}
          recordAtomicNodes={recordAtomicNodes}
          pageSnapshot={pageSnapshot}
          projectId={route.projectId}
          documentId={route.documentId}
          sourceSha256={sourceSha256}
          onPageChange={(page) => {
            setPdfPage(page);
            if (lens === "analysis" || compactReviewLayout) {
              setDetailPane("pdf");
            }
          }}
        />
      )}
    </>
  );

  const pdfSurface = (
    <div className="matrix-pdf-view">
      <header>
        <div>
          <span>可信原文</span>
          <strong>原 PDF · 第 {pdfPage} 页</strong>
        </div>
        <div className="matrix-pdf-controls">
          <button
            type="button"
            aria-label="上一页"
            disabled={pdfPage <= 1}
            onClick={() => setPdfPage(Math.max(1, pdfPage - 1))}
          >
            <Icon name="chevron" size={14} className="is-back" />
          </button>
          <label>
            <span className="sr-only">PDF 页码</span>
            <input
              type="number"
              name="matrix-pdf-page"
              autoComplete="off"
              inputMode="numeric"
              min={1}
              max={maxPage}
              value={pdfPage}
              onChange={(event) =>
                setPdfPage(
                  Math.max(
                    1,
                    Math.min(maxPage, Number(event.target.value) || 1),
                  ),
                )
              }
            />
          </label>
          <span>/ {maxPage}</span>
          <button
            type="button"
            aria-label="下一页"
            disabled={pdfPage >= maxPage}
            onClick={() => setPdfPage(Math.min(maxPage, pdfPage + 1))}
          >
            <Icon name="chevron" size={14} />
          </button>
          <a
            href={`/pdf-viewer?pdf=${encodeURIComponent(pdfPath)}&page=${pdfPage}`}
            target="_blank"
            rel="noreferrer"
            aria-label="在新窗口打开 PDF"
          >
            <Icon name="external" size={14} />
          </a>
        </div>
      </header>
      {sourceError && <p className="matrix-source-warning">{sourceError}</p>}
      <iframe
        ref={pdfFrameRef}
        src={pdfViewerUrl}
        title="带当前内容框选的原 PDF"
      />
    </div>
  );

  const inspectorSurface = isCandidateReview ? (
    <CandidateReviewInspector key={`${uiScope}:${selectedNode?.node_id || "none"}`} node={selectedNodeDetail?.node || selectedNode}
      topics={topicLabels} saveRef={saveRef} resetSignal={resetSignal} onSaveState={setSaveState}
      beginMutation={beginSingleMatrixMutation} isMutationCurrent={isMatrixMutationCurrent}
      isTargetCurrent={target => currentSelectedId.current === target}
      onSaved={loadMatrix} />
  ) : (
    <MatrixInspector
      key={`${uiScope}:${selectedNode?.node_id || "none"}`}
      lens={lens}
      projectId={route.projectId}
      documentId={route.documentId}
      requirementSetId={selectedSetId}
      node={selectedNode}
      detail={selectedNodeDetail}
      detailLoading={detailLoading}
      detailError={[detailError, tableError].filter(Boolean).join(" ")}
      canEditDisposition={canEditDisposition}
      canWriteDecision={canWriteDecision}
      resetSignal={resetSignal}
      saveRef={saveRef}
      saveState={saveState}
      onSaveState={setSaveState}
      beginMatrixMutation={beginSingleMatrixMutation}
      isMatrixMutationCurrent={isMatrixMutationCurrent}
      onNodeUpdated={handleNodeUpdated}
      onImpactChanged={handleImpactChanged}
      onRetryDetail={() => void loadSelectedDetail()}
      analysisExecution={
        selectedNode
          ? aiNodeExecutionByNodeId.get(selectedNode.node_id) || null
          : null
      }
    />
  );

  return (
    <section
      ref={setWorkbenchElement}
      className="requirement-matrix-workbench"
      data-lens={lens}
      data-active-detail={detailPane}
      aria-label={lens === "review" ? "需求确认清单" : "需求分析矩阵"}
    >
      <header className="matrix-workbench-toolbar">
        {lens === "review" ? (
          <div className="matrix-review-summary" aria-label="确认清单统计">
            {isCandidateReview ? <><strong>{String(candidateMetadata.sourceCount || 0)} 条原文</strong><span>{String(candidateMetadata.derivedCount || 0)} 条派生</span><span>来源与候选分别保留</span></> : <><strong>{businessContentCount} 项清单内容</strong><span>{actualAnalysisCount} 项需分析</span><span>{businessNonAnalysisCount} 项不分析</span></>}
          </div>
        ) : (
          <div className="matrix-workbench-title">
            <span>需求分析</span>
            <h1>需求合规矩阵</h1>
            <p>
              {businessContentCount} 项可见内容 · {actualAnalysisCount}{" "}
              项分析范围
            </p>
          </div>
        )}
        <div className="matrix-workbench-actions">
          <label className="matrix-version-select">
            <span>需求版本</span>
            <select
              name="matrix-requirement-set"
              autoComplete="off"
              disabled={Boolean(
                workflowPending || busyAction || matrixMutationPending,
              )}
              value={selectedSetId}
              onChange={(event) => {
                // A node identity is only valid inside its frozen Requirement Set.
                // Clear the previous projection before changing the URL so stale
                // detail/table/geometry requests cannot leak into the next version.
                matrixRequestRef.current += 1;
                detailRequestRef.current += 1;
                setMatrix(null);
                setNodeDetail(null);
                setTableDetails([]);
                setFocusRegions([]);
                setDetailError("");
                setTableError("");
                updateRoute({ requirementSetId: event.target.value });
              }}
            >
              {sets.map((set) => (
                <option value={setId(set)} key={setId(set)}>
                  {requirementSetLabel(set)}
                  {setId(set) === activeSetId ? " · 当前" : ""}
                </option>
              ))}
            </select>
          </label>
          <StatusBadge tone={setStatus === "LOCKED" ? "success" : "warning"}>
            {setStatus === "LOCKED" ? "已锁定" : "确认中"}
          </StatusBadge>
          {!isCandidateReview && canBatchDisposition && (
            <button
              className="button secondary"
              type="button"
              aria-expanded={batchOpen}
              onClick={() => setBatchOpen((current) => !current)}
            >
              <Icon name="check" size={15} />
              批量处理
            </button>
          )}
          {!isCandidateReview && lens === "review" &&
            setStatus !== "LOCKED" &&
            selectedSetId === activeSetId &&
            !zeroAnalysisLocked && (
            <button
              className="button primary"
              type="button"
              disabled={Boolean(busyAction) || hasUnsavedChanges}
              onClick={openLockConfirmation}
            >
              <Icon name="lock" size={15} />
              {actualAnalysisCount > 0
                ? `锁定 ${actualAnalysisCount} 条需求并进入分析`
                : "锁定清单并查看矩阵"}
            </button>
          )}
          {lens === "review" &&
            setStatus === "LOCKED" &&
            selectedSetId === activeSetId &&
            !zeroAnalysisLocked &&
            actualAnalysisCount > 0 && (
              <button
                className="button secondary"
                type="button"
                disabled={Boolean(busyAction)}
                title="保留当前已锁定清单，并创建一份可编辑清单"
                onClick={openUnlockConfirmation}
              >
                <Icon name="refresh" size={15} />
                解锁并修改
              </button>
            )}
          {lens === "analysis" &&
            setStatus === "LOCKED" &&
            selectedSetId === activeSetId && (
              aiRunning ? (
                <button
                  className="button secondary"
                  type="button"
                  disabled={Boolean(busyAction) || !aiCanCancel}
                  title={
                    aiCancelRequested
                      ? "停止请求已提交，正在等待当前步骤结束"
                      : aiPublishing && !recoverableJobModeValid
                        ? "本次分析正在原子发布，当前不能停止"
                        : aiCanCancel
                      ? "停止本次分析"
                      : "当前任务状态不能停止"
                  }
                  onClick={() => {
                    setWorkflowError("");
                    setWorkflowDialog("stop");
                  }}
                >
                  <Icon name="stop" size={15} />
                  {aiCancelRequested
                    ? "正在停止"
                    : aiPublishing && !recoverableJobModeValid
                      ? "正在发布"
                      : aiCanCancel
                        ? "停止分析"
                        : "暂不可停止"}
                </button>
              ) : actualAnalysisCount > 0 ? (
                analysisActionsSupported ? (
                  <div className="matrix-analysis-actions" role="group" aria-label="启动需求分析">
                    <button
                      className="button secondary"
                      type="button"
                      disabled={Boolean(busyAction) || !aiProfileReady || !canAnalyzeCurrent}
                      onClick={() => void openAnalysisConfirmation("CURRENT")}
                    >
                      分析当前项
                    </button>
                    <button
                      className="button secondary"
                      type="button"
                      disabled={
                        Boolean(busyAction) ||
                        !aiProfileReady ||
                        selectedAnalysisNodes.length === 0
                      }
                      onClick={() => void openAnalysisConfirmation("SELECTED")}
                    >
                      分析所选 {selectedAnalysisNodes.length} 项
                    </button>
                    <button
                      className="button primary"
                      type="button"
                      disabled={Boolean(busyAction) || !aiProfileReady}
                      onClick={() => void openAnalysisConfirmation("ALL")}
                    >
                      <Icon name="spark" size={15} />
                      分析全部 {actualAnalysisCount} 项
                    </button>
                  </div>
                ) : recoverableJobMalformed ? (
                  <button
                    className="button primary"
                    type="button"
                    disabled
                    title="服务端返回的逐项分析状态不完整；为避免重复任务，暂不允许启动"
                  >
                    <Icon name="refresh" size={15} />
                    分析状态需要刷新
                  </button>
                ) : legacyAnalysisSupported ? (
                  <button
                    className="button primary"
                    type="button"
                    disabled={Boolean(busyAction) || !aiProfileReady}
                    title="当前服务端未回显精确范围能力，将沿用安全的全部分析确认流程"
                    onClick={openLegacyAnalysisConfirmation}
                  >
                    <Icon name="spark" size={15} />
                    分析全部 {actualAnalysisCount} 项
                  </button>
                ) : (
                  <button
                    className="button primary"
                    type="button"
                    disabled
                    title="服务端分析能力尚未确认，暂不允许启动任务"
                  >
                    <Icon name="spark" size={15} />
                    分析能力尚未确认
                  </button>
                )
              ) : null
          )}
          {selectedSetId !== activeSetId && (
            <span className="matrix-readonly-note">历史清单只读</span>
          )}
        </div>
      </header>

      {isCandidateReview && <div className="matrix-workflow-notice"><p>{String(candidateMetadata.scope || "")}</p></div>}
      {(isCandidateReview || matrix?.nodes.some(node => node.classification_paths?.length)) && <div className="matrix-facet-toolbar">
        <label>需求类别<select aria-label="筛选需求类别" value={category} onChange={event => setCategory(event.target.value)}>{CLASSIFICATION_OPTIONS.map(([value,label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        <label>主题标签<select aria-label="筛选主题标签" value={topic} onChange={event => setTopic(event.target.value)}><option value="">全部主题</option>{Object.entries(topicLabels).map(([value,label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        {(category || topic) && <button type="button" className="text-button" onClick={() => { setCategory(""); setTopic(""); }}>清除分类筛选</button>}
        {isCandidateReview && <button type="button" className="button secondary" disabled={exportState === "正在导出…"} onClick={async () => {
          setExportState("正在导出…");
          try {
            const state = await request<JsonRecord>("/api/review-state");
            const completeSource = candidateMetadata.exportScope === "COMPLETE_SOURCE";
            const ids = completeSource
              ? (matrix?.nodes || []).filter(node => Boolean(node.candidate_review)).map(node => node.node_id)
              : matchingCandidateExportIds(matrix, query, filter, facets);
            const response = await fetch("/api/candidate-review/export", { method: "POST", headers: { "Content-Type": "application/json", "X-Review-Token": String(state.writeToken) }, body: JSON.stringify({ sourceRevision: matrix?.tree_sha256, expectedRevision: state.revision, nodeIds: ids }) });
            if (!response.ok) throw new Error("导出未完成，请刷新后重试。");
            const url = URL.createObjectURL(await response.blob()); const link = document.createElement("a"); link.href = url; link.download = "ReqPilotX-原文核对清单.xlsx"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); setExportState(completeSource ? "已导出完整原文矩阵" : "已导出当前命中条目");
          } catch (cause) { setExportState(cause instanceof Error ? cause.message : String(cause)); }
        }}>{candidateMetadata.exportScope === "COMPLETE_SOURCE" ? "导出完整原文矩阵" : "导出筛选结果"}</button>}
        <span role="status" aria-live="polite">{exportState}</span>
      </div>}
      {lens === "review" &&
        impactFeedback?.requirementSetId === selectedSetId && (
          <div className="matrix-workflow-notice" role="status" aria-live="polite">
            <InlineNotice
              tone={staleAnalysisTargetCount > 0 ? "warning" : "success"}
              title={
                staleAnalysisTargetCount > 0
                  ? `本次修改后有 ${staleAnalysisTargetCount} 条分析结果需要重跑`
                  : "本次修改后没有分析结果需要重跑"
              }
            >
              {staleAnalysisTargetCount > 0
                ? "这些结果已标记为历史参考；未受影响的分析与人工结论继续保留。"
                : "当前没有仍处于分析范围且需要重跑的结果；未受影响结果继续有效。"}
            </InlineNotice>
          </div>
        )}

      {lens === "review" &&
        selectedSetId === activeSetId &&
        (zeroAnalysisLocked ||
          (actualAnalysisCount === 0 && setStatus === "LOCKED")) && (
        <div className="matrix-workflow-notice" role="status" aria-live="polite">
          <InlineNotice tone="success" title="清单已锁定，没有待分析项">
            <p>全部内容继续保留在当前矩阵中；本次没有启动 AI，也没有进入空分析页面。</p>
            <div className="matrix-workflow-notice-actions">
              <button
                className="button secondary"
                type="button"
                onClick={openUnlockConfirmation}
              >
                解锁并修改
              </button>
              <button
                className="button secondary"
                type="button"
                onClick={() =>
                    updateRoute({
                      task: "export",
                      requirementSetId: selectedSetId,
                    })
                }
              >
                前往矩阵导出
              </button>
            </div>
          </InlineNotice>
        </div>
      )}

      {lens === "analysis" &&
        selectedSetId === activeSetId &&
        setStatus === "LOCKED" &&
        actualAnalysisCount === 0 && (
          <div className="matrix-workflow-notice" role="status" aria-live="polite">
            <InlineNotice tone="info" title="当前清单没有需要分析的内容">
              <p>全部业务内容仍保留在矩阵中；这里不会创建空分析任务。</p>
              <div className="matrix-workflow-notice-actions">
                <button
                  className="button secondary"
                  type="button"
                  onClick={() =>
                    updateRoute({
                      task: "export",
                      requirementSetId: selectedSetId,
                    })
                  }
                >
                  前往矩阵导出
                </button>
                <button
                  className="button ghost"
                  type="button"
                  onClick={() =>
                    updateRoute({
                      task: "extraction",
                      requirementSetId: selectedSetId,
                    })
                  }
                >
                  返回确认清单
                </button>
              </div>
            </InlineNotice>
          </div>
        )}

      {lens === "analysis" && currentAiJobId && (
        <section className="matrix-ai-progress" aria-label="需求分析进度">
          {recoverableJobMalformed ? (
            <InlineNotice tone="warning" title="逐项分析状态暂时不完整">
              为避免重复启动或误重试，当前操作已暂停。请刷新后查看最新状态。
            </InlineNotice>
          ) : (
            <>
              <span className="sr-only" role="status" aria-live="polite">
                {aiStageLabel(aiCurrentStage)}，
                {recoverableExecutionStatusValid
                  ? `已处理 ${aiCompletedCount}/${aiTotalCount}，成功 ${aiSucceededCount ?? 0}，失败 ${aiFailedCount ?? 0}，运行 ${aiActiveCount}，等待 ${aiWaitingCount}，已发布 ${aiPublishedCount}`
                  : `本次已计算 ${aiCompletedCount}/${aiTotalCount}，本次已发布 ${aiPublishedCount}/${aiTotalCount}`}
              </span>
              <div className="matrix-ai-progress-heading">
                <div>
                  <strong>{aiStageLabel(aiCurrentStage)}</strong>
                  <span>
                    {analysisScopeLabel(aiScope || "ALL")} ·{" "}
                    {recoverableExecutionStatusValid
                      ? `已处理 ${aiCompletedCount}/${aiTotalCount}`
                      : `本次已计算 ${aiCompletedCount}/${aiTotalCount}`}
                  </span>
                </div>
                <div className="matrix-ai-progress-actions">
                  <span className="matrix-ai-publish-mode">
                    {recoverableExecutionStatusValid
                      ? `逐项发布 · 已发布 ${aiPublishedCount}/${aiTotalCount}`
                      : `本次已发布 ${aiPublishedCount}/${aiTotalCount}`}
                  </span>
                  {canRetryFailedAnalysis && (
                    <button
                      className="button secondary compact"
                      type="button"
                      disabled={Boolean(busyAction) || !aiProfileReady}
                      onClick={() =>
                        void openAnalysisConfirmation(
                          "FAILED_ONLY",
                          currentAiJobId,
                        )
                      }
                    >
                      <Icon name="refresh" size={14} />
                      重试失败 {aiRetryableFailedCount} 项
                    </button>
                  )}
                </div>
              </div>
              <progress
                max={Math.max(aiTotalCount, 1)}
                value={aiCompletedCount}
                aria-label={
                  recoverableExecutionStatusValid
                    ? `已处理 ${aiCompletedCount} 项，共 ${aiTotalCount} 项`
                    : `本次已计算 ${aiCompletedCount} 项，共 ${aiTotalCount} 项`
                }
                aria-valuetext={
                  recoverableExecutionStatusValid
                    ? `已处理 ${aiCompletedCount}/${aiTotalCount}，成功 ${aiSucceededCount ?? 0}，失败 ${aiFailedCount ?? 0}，已发布 ${aiPublishedCount}/${aiTotalCount}`
                    : `本次已计算 ${aiCompletedCount}/${aiTotalCount}，本次已发布 ${aiPublishedCount}/${aiTotalCount}`
                }
              />
              <div className="matrix-ai-progress-counts">
                {recoverableExecutionStatusValid ? (
                  <>
                    <span>已处理 {aiCompletedCount}</span>
                    <span>成功 {aiSucceededCount ?? 0}</span>
                    <span>失败 {aiFailedCount ?? 0}</span>
                    <span>运行中 {aiActiveCount}</span>
                    {aiPublishingCount > 0 && (
                      <span>发布中 {aiPublishingCount}</span>
                    )}
                    <span>
                      {aiRemainingLabel} {aiWaitingCount}
                    </span>
                    <span>已发布 {aiPublishedCount}</span>
                    {aiCancelledCount !== null && aiCancelledCount > 0 && (
                      <span>未完成 {aiCancelledCount}</span>
                    )}
                  </>
                ) : (
                  <>
                    <span>{aiRemainingLabel} {aiWaitingCount}</span>
                    <span>运行中 {aiActiveCount}</span>
                    <span>本次已计算 {aiCompletedCount}</span>
                    <span>本次已发布 {aiPublishedCount}</span>
                    {aiFailedCount !== null && (
                      <span>失败 {aiFailedCount}</span>
                    )}
                    {currentAiStatus === "FAILED" && <span>任务失败</span>}
                  </>
                )}
              </div>
              {(aiCurrentRequirement || aiLastMessage) && (
                <p>
                  {aiCurrentRequirement && (
                    <strong>当前 {aiCurrentRequirement}</strong>
                  )}
                  {aiLastMessage && <span>{aiLastMessage}</span>}
                </p>
              )}
            </>
          )}
        </section>
      )}

      <Dialog
        open={workflowDialog === "lock"}
        title="锁定确认清单"
        className="confirm-action-dialog matrix-workflow-dialog"
        busy={workflowPending === "lock"}
        onClose={closeWorkflowDialog}
      >
        <header>
          <div>
            <p className="section-label">锁定确认清单</p>
            <h2>
              {lockReadinessLoading
                ? "正在检查锁定范围"
                : lockWillEnterAnalysis
                ? `锁定 ${lockAnalysisCount} 条需求并进入分析`
                : "锁定清单并查看矩阵"}
            </h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭锁定确认"
            disabled={workflowPending === "lock"}
            onClick={closeWorkflowDialog}
          >
            <Icon name="close" size={16} />
          </button>
        </header>
        <div className="matrix-workflow-dialog-body">
          {lockReadinessLoading && (
            <LoadingBlock label="正在读取清单数量和风险…" />
          )}
          {workflowDialog === "lock" && workflowError && (
            <div
              ref={workflowErrorRef}
              className="matrix-workflow-error"
              role="alert"
              tabIndex={-1}
            >
              <div>
                <strong>{workflowConflict ? "锁定范围需要更新" : "暂时无法锁定"}</strong>
                <p>{workflowError}</p>
              </div>
              <button
                className="button secondary"
                type="button"
                disabled={workflowPending === "lock" || lockReadinessLoading}
                onClick={() => void loadLockReadiness(true)}
              >
                <Icon name="refresh" size={15} />
                重新读取摘要
              </button>
            </div>
          )}
          {lockReadiness && (
            <>
              <dl className="matrix-action-receipt matrix-lock-receipt">
                <div>
                  <dt>清单内容</dt>
                  <dd>{lockReadiness.counts.visible_content}</dd>
                </div>
                <div>
                  <dt>需分析</dt>
                  <dd>{lockReadiness.counts.analysis_targets}</dd>
                </div>
                <div>
                  <dt>仅保留</dt>
                  <dd>{lockReadiness.counts.retained_visible}</dd>
                </div>
                <div>
                  <dt>排除分析</dt>
                  <dd>{lockReadiness.counts.excluded_visible}</dd>
                </div>
              </dl>
              <section className="matrix-lock-readiness-details" aria-label="锁定清单明细">
                <div>
                  <strong>内容分类</strong>
                  {lockClassificationCountsAvailable ? (
                    <>
                      <span>
                        技术内容 {lockReadiness.counts.technical_source_entries}
                      </span>
                      <span>
                        非技术内容{" "}
                        {lockReadiness.counts.non_technical_source_entries}
                      </span>
                      <span>
                        信息内容{" "}
                        {lockReadiness.counts.information_source_entries}
                      </span>
                    </>
                  ) : (
                    <span>未记录</span>
                  )}
                </div>
                <div>
                  <strong>条目构成</strong>
                  {lockCompositionCountsAvailable ? (
                    <>
                      <span>
                        原文条目 {lockReadiness.counts.source_entries}
                      </span>
                      <span>
                        拆分后需求 {lockReadiness.counts.atomic_requirements}
                      </span>
                      <span>
                        不单独分析 {lockReadiness.counts.skipped_visible}
                      </span>
                    </>
                  ) : (
                    <span>未记录</span>
                  )}
                </div>
              </section>
              {lockCountMetadataUnavailable && (
                <InlineNotice tone="info" title="明细来源未证明">
                  当前版本没有提供可验证的分类与条目构成来源；系统不会把兼容数值冒充真实统计，也不会据此改变锁定资格。
                </InlineNotice>
              )}
              {lockReadiness.hard_blockers.length > 0 && (
                <InlineNotice tone="danger" title="需要先处理以下问题">
                  <ul className="matrix-readiness-messages">
                    {lockReadiness.hard_blockers.map((item, index) => (
                      <li key={`blocker-${index}`}>{lockReadinessItemMessage(item)}</li>
                    ))}
                  </ul>
                </InlineNotice>
              )}
              {!lockReadiness.can_lock &&
                lockReadiness.hard_blockers.length === 0 && (
                  <InlineNotice tone="danger" title="当前清单暂时不能锁定">
                    请重新读取摘要；如果问题持续，请返回清单检查内容。
                  </InlineNotice>
                )}
              {lockReadiness.warnings.length > 0 && (
                <InlineNotice tone="warning" title="可带风险继续">
                  <ul className="matrix-readiness-messages">
                    {lockReadiness.warnings.map((item, index) => (
                      <li key={`warning-${index}`}>{lockReadinessItemMessage(item)}</li>
                    ))}
                  </ul>
                </InlineNotice>
              )}
              <InlineNotice
                tone={lockWillEnterAnalysis ? "info" : "warning"}
                title={
                  lockWillEnterAnalysis
                    ? "锁定不会自动运行分析"
                    : "当前没有需要分析的内容"
                }
              >
                {lockWillEnterAnalysis
                  ? "锁定后进入分析页面，仍需再次确认才会启动；进入页面本身不会运行 AI。"
                  : "锁定后停留在当前矩阵，不调用 AI；你可以直接导出，或解锁并修改处理方式。"}
              </InlineNotice>
            </>
          )}
        </div>
        <footer className="confirm-action-footer">
          <button
            className="button ghost"
            type="button"
            disabled={workflowPending === "lock"}
            onClick={closeWorkflowDialog}
          >
            返回检查
          </button>
          <button
            ref={lockConfirmButtonRef}
            className="button primary"
            type="button"
            disabled={
              Boolean(busyAction) ||
              hasUnsavedChanges ||
              lockReadinessLoading ||
              workflowPending === "lock" ||
              !lockReadiness?.can_lock ||
              Boolean(lockReadiness?.hard_blockers.length)
            }
            onClick={() => void lockConfirmationList()}
          >
            <Icon name="lock" size={15} />
            {workflowPending === "lock"
              ? "正在锁定…"
              : lockWillEnterAnalysis
              ? `锁定并进入分析 · ${lockAnalysisCount} 项`
              : "锁定清单"}
          </button>
        </footer>
      </Dialog>

      <Dialog
        open={workflowDialog === "unlock"}
        title="解锁并修改"
        className="confirm-action-dialog matrix-workflow-dialog"
        busy={workflowPending === "unlock"}
        onClose={closeWorkflowDialog}
      >
        <header>
          <div>
            <p className="section-label">确认清单</p>
            <h2>解锁并修改</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭解锁确认"
            disabled={workflowPending === "unlock"}
            onClick={closeWorkflowDialog}
          >
            <Icon name="close" size={16} />
          </button>
        </header>
        <div className="matrix-workflow-dialog-body">
          <InlineNotice tone="info" title="当前已锁定清单会继续保留">
            系统会创建一份可编辑清单并切换过去，不会改写当前已锁定清单。
          </InlineNotice>
          <p className="matrix-workflow-explanation">
            未修改内容的分析结果会继续使用；修改后，系统只提示实际需要重新分析的内容。
          </p>
          {workflowDialog === "unlock" && workflowError && (
            <div
              ref={workflowErrorRef}
              className="matrix-workflow-error"
              role="alert"
              tabIndex={-1}
            >
              <div>
                <strong>{workflowConflict ? "清单需要重新检查" : "暂时无法解锁"}</strong>
                <p>{workflowError}</p>
              </div>
            </div>
          )}
        </div>
        <footer className="confirm-action-footer">
          <button
            className="button ghost"
            type="button"
            disabled={workflowPending === "unlock"}
            onClick={() => {
              if (workflowConflict) void recoverUnlockConflict();
              else closeWorkflowDialog();
            }}
          >
            {workflowConflict ? "关闭并重新检查" : "保持锁定"}
          </button>
          <button
            className="button primary"
            type="button"
            disabled={
              Boolean(busyAction) ||
              workflowPending === "unlock" ||
              workflowConflict
            }
            onClick={() => void deriveEditableConfirmation()}
          >
            <Icon name="refresh" size={15} />
            {workflowPending === "unlock" ? "正在解锁…" : "解锁并继续修改"}
          </button>
        </footer>
      </Dialog>

      <Dialog
        open={
          workflowDialog === "analysis" &&
          (legacyAnalysisConfirmation
            ? legacyAnalysisSupported
            : analysisRequestCapabilityAvailable)
        }
        title={legacyAnalysisConfirmation ? "分析全部需求" : "确认分析范围"}
        className="confirm-action-dialog matrix-workflow-dialog"
        busy={
          analysisPreviewLoading || workflowPending === "analysis"
        }
        onClose={closeWorkflowDialog}
      >
        <header>
          <div>
            <p className="section-label">需求分析范围</p>
            <h2>
              {analysisPreviewLoading
                ? "正在核对实际目标"
                : legacyAnalysisConfirmation
                  ? `分析全部 ${actualAnalysisCount} 项`
                : `${analysisScopeLabel(analysisScopeRequest?.scope || "ALL")} · ${analysisScopePreview?.target_count ?? 0} 项`}
            </h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭分析确认"
            disabled={workflowPending === "analysis"}
            onClick={closeWorkflowDialog}
          >
            <Icon name="close" size={16} />
          </button>
        </header>
        <div className="matrix-workflow-dialog-body">
          {analysisPreviewLoading && (
            <LoadingBlock label="正在从锁定清单核对范围…" />
          )}
          {analysisPreviewError && (
            <div
              ref={workflowErrorRef}
              className="matrix-workflow-error"
              role="alert"
              tabIndex={-1}
            >
              <div>
                <strong>分析范围需要重新核对</strong>
                <p>{analysisPreviewError}</p>
              </div>
            </div>
          )}
          {legacyAnalysisConfirmation && (
            <>
              <dl className="matrix-action-receipt">
                <div>
                  <dt>实际目标</dt>
                  <dd>{actualAnalysisCount}</dd>
                </div>
                <div>
                  <dt>范围</dt>
                  <dd>全部需分析项</dd>
                </div>
                <div>
                  <dt>分析方案</dt>
                  <dd>{selectedAiProfile?.label || selectedAiProfileId || "默认方案"}</dd>
                </div>
              </dl>
              {workflowError && (
                <div
                  ref={workflowErrorRef}
                  className="matrix-workflow-error"
                  role="alert"
                  tabIndex={-1}
                >
                  <div>
                    <strong>分析任务没有创建</strong>
                    <p>{workflowError}</p>
                  </div>
                </div>
              )}
              <InlineNotice tone="info" title="打开窗口不会启动分析">
                当前服务端未回显精确范围能力，因此只提供安全的“全部分析”兼容流程；点击下方确认后才会创建任务。
              </InlineNotice>
              <InlineNotice tone="warning" title="更新服务端后可使用精确范围">
                服务端回显范围预览能力后，这里会显示“当前项、所选项、全部”三个动作和实际跳过原因。
              </InlineNotice>
            </>
          )}
          {analysisScopePreview && (
            <>
              <dl className="matrix-action-receipt">
                <div>
                  <dt>实际目标</dt>
                  <dd>{analysisScopePreview.target_count}</dd>
                </div>
                <div>
                  <dt>跳过内容</dt>
                  <dd>{analysisScopePreview.skipped_count}</dd>
                </div>
                {analysisScopePreview.schema_version ===
                  "requirement-analysis-scope-preview/v2" &&
                  analysisScopePreview.scope === "FAILED_ONLY" && (
                    <div>
                      <dt>原失败项</dt>
                      <dd>{analysisScopePreview.source_failed_count}</dd>
                    </div>
                  )}
                {analysisScopePreview.schema_version ===
                  "requirement-analysis-scope-preview/v2" &&
                  analysisScopePreview.already_current_count > 0 && (
                    <div>
                      <dt>已是最新结果</dt>
                      <dd>{analysisScopePreview.already_current_count}</dd>
                    </div>
                  )}
                {analysisScopePreview.schema_version ===
                  "requirement-analysis-scope-preview/v2" &&
                  analysisScopePreview.scope === "FAILED_ONLY" &&
                  analysisScopePreview.retry_skipped_count > 0 && (
                    <div>
                      <dt>已不再适用</dt>
                      <dd>{analysisScopePreview.retry_skipped_count}</dd>
                    </div>
                  )}
                <div>
                  <dt>分析方案</dt>
                  <dd>{selectedAiProfile?.label || selectedAiProfileId || "默认方案"}</dd>
                </div>
              </dl>
              {analysisScopePreview.target_examples.length > 0 && (
                <section className="matrix-analysis-preview-list" aria-label="分析目标示例">
                  <strong>目标示例</strong>
                  <ul>
                    {analysisScopePreview.target_examples.slice(0, 5).map((item) => (
                      <li key={item.node_id}>
                          {item.requirement_code && (
                            <span>{item.requirement_code}</span>
                          )}
                        {item.label}
                      </li>
                    ))}
                  </ul>
                </section>
              )}
              {Object.keys(analysisScopePreview.skipped_reason_counts).length > 0 && (
                <section className="matrix-analysis-preview-list" aria-label="跳过原因">
                  <strong>跳过原因</strong>
                  <ul>
                    {Object.entries(
                      analysisScopePreview.skipped_reason_counts,
                    ).map(([reason, count]) => (
                        <li key={reason}>
                          {analysisSkipReasonLabel(reason)} · {count} 项
                        </li>
                    ))}
                  </ul>
                </section>
              )}
              {analysisScopePreview.target_count === 0 && (
                <InlineNotice tone="warning" title="当前范围没有可分析需求">
                  {analysisScopePreview.scope === "FAILED_ONLY"
                    ? "这些失败项已不再需要重试，或已由其他任务生成最新结果。"
                    : "请选择标记为“需分析”的需求叶节点，或改用“分析全部”。"}
                </InlineNotice>
              )}
              <InlineNotice tone="info" title="打开窗口不会启动分析">
                只有点击下方确认按钮才会创建任务。
              </InlineNotice>
              {analysisScopePreview.schema_version ===
              "requirement-analysis-scope-preview/v2" ? (
                <InlineNotice
                  tone="info"
                  title={
                    analysisScopePreview.scope === "FAILED_ONLY"
                      ? "只重试仍失败的项目"
                      : "成功结果会逐项保留"
                  }
                >
                  {analysisScopePreview.scope === "FAILED_ONLY"
                    ? "已成功并发布的项目不会重复分析；本次只处理预览中的失败项。"
                    : "每项成功后会独立发布；其他项目失败或停止，不会撤回已经发布的成功结果。"}
                </InlineNotice>
              ) : (
                <InlineNotice tone="warning" title="本阶段按整批原子发布">
                  只有本次范围全部完成后才统一发布；停止或失败时，本次已计算项仍记为已发布 0 项。
                </InlineNotice>
              )}
            </>
          )}
        </div>
        <footer className="confirm-action-footer">
          <button
            className="button ghost"
            type="button"
            disabled={workflowPending === "analysis"}
            onClick={closeWorkflowDialog}
          >
            取消
          </button>
          <button
            className="button primary"
            type="button"
            disabled={
              Boolean(busyAction) ||
              analysisPreviewLoading ||
              workflowPending === "analysis" ||
              !aiProfileReady ||
              (!legacyAnalysisConfirmation &&
                Boolean(analysisPreviewError) &&
                !analysisRetryKind) ||
              (!legacyAnalysisConfirmation &&
                !analysisPreviewError &&
                !analysisScopePreview?.target_count)
            }
            onClick={() =>
              legacyAnalysisConfirmation
                ? void startLegacyAnalysis()
                : analysisRetryKind === "preview" && analysisScopeRequest
                  ? void openAnalysisConfirmation(
                      analysisScopeRequest.scope,
                      analysisScopeRequest.schema_version ===
                          "requirement-analysis-scope-request/v2" &&
                        analysisScopeRequest.scope === "FAILED_ONLY"
                        ? analysisScopeRequest.source_job_id
                        : "",
                    )
                  : void startAnalysis()
            }
          >
            <Icon
              name={analysisRetryKind === "preview" ? "refresh" : "spark"}
              size={15}
            />
            {workflowPending === "analysis"
              ? "正在启动…"
              : legacyAnalysisConfirmation
                ? `开始分析全部 ${actualAnalysisCount} 项`
                : analysisRetryKind === "preview"
                  ? "重新核对范围"
                  : analysisRetryKind === "start"
                    ? `重试启动 ${analysisScopePreview?.target_count ?? 0} 项`
                    : analysisScopeRequest?.scope === "FAILED_ONLY"
                      ? `开始重试 ${analysisScopePreview?.target_count ?? 0} 项`
                      : `开始分析 ${analysisScopePreview?.target_count ?? 0} 项`}
          </button>
        </footer>
      </Dialog>

      <Dialog
        open={workflowDialog === "stop"}
        title="停止需求分析"
        className="confirm-action-dialog matrix-workflow-dialog"
        busy={workflowPending === "stop"}
        onClose={closeWorkflowDialog}
      >
        <header>
          <div>
            <p className="section-label">需求分析运行中</p>
            <h2>
              {recoverableExecutionStatusValid
                ? "停止本次分析？"
                : "停止本次整批分析？"}
            </h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭停止分析确认"
            disabled={workflowPending === "stop"}
            onClick={closeWorkflowDialog}
          >
            <Icon name="close" size={16} />
          </button>
        </header>
        <div className="matrix-workflow-dialog-body">
          <dl className="matrix-action-receipt">
            <div>
              <dt>
                {recoverableExecutionStatusValid ? "已处理" : "本次已计算"}
              </dt>
              <dd>{aiCompletedCount}</dd>
            </div>
            <div>
              <dt>本次已发布</dt>
              <dd>{aiPublishedCount}</dd>
            </div>
            <div>
              <dt>当前运行</dt>
              <dd>{aiActiveCount}</dd>
            </div>
            <div>
              <dt>尚未开始</dt>
              <dd>{aiWaitingCount}</dd>
            </div>
          </dl>
          {recoverableExecutionStatusValid ? (
            <InlineNotice tone="warning" title="已发布成功项会保留">
              停止后，等待中的项目不会再启动；可取消的运行项会停止。已经成功并发布的结果不会回退，失败项仍可稍后单独重试。
            </InlineNotice>
          ) : (
            <InlineNotice tone="warning" title="部分计算结果不会发布">
              当前仍按整批原子发布。停止后会保留任务回执，但本次已经计算的部分不会写成已发布分析结论。
            </InlineNotice>
          )}
          {workflowDialog === "stop" && workflowError && (
            <div
              ref={workflowErrorRef}
              className="matrix-workflow-error"
              role="alert"
              tabIndex={-1}
            >
              <div>
                <strong>停止请求没有提交</strong>
                <p>{workflowError}</p>
              </div>
            </div>
          )}
        </div>
        <footer className="confirm-action-footer">
          <button
            className="button ghost"
            type="button"
            disabled={workflowPending === "stop"}
            onClick={closeWorkflowDialog}
          >
            继续运行
          </button>
          <button
            className="button danger"
            type="button"
            disabled={Boolean(busyAction) || workflowPending === "stop"}
            onClick={() => void stopAnalysis()}
          >
            <Icon name="stop" size={15} />
            {workflowPending === "stop" ? "正在提交…" : "确认停止分析"}
          </button>
        </footer>
      </Dialog>

      {lens === "review" && batchOpen && (
        <section className="matrix-batch-panel" aria-label="批量处理正式清单">
          <div className="matrix-batch-heading">
            <div>
              <strong>批量处理例外</strong>
              <span>只处理叶节点；章节和表格容器继续保留完整层级。</span>
            </div>
            <button
              className="icon-button"
              type="button"
              aria-label="关闭批量处理"
              onClick={() => setBatchOpen(false)}
            >
              <Icon name="close" size={16} />
            </button>
          </div>
          <div className="matrix-batch-controls">
            <label>
              <span>作用范围</span>
              <select
                name="matrix-batch-scope"
                value={batchScope}
                onChange={(event) =>
                  setBatchScope(event.target.value as BatchScope)
                }
              >
                <option value="branch">当前分支</option>
                <option value="filter">当前筛选结果</option>
                <option value="all">全部清单</option>
              </select>
            </label>
            <label>
              <span>统一处理为</span>
              <select
                name="matrix-batch-disposition"
                value={batchDisposition}
                onChange={(event) =>
                  setBatchDisposition(
                    event.target.value as RequirementMatrixNodeDisposition,
                  )
                }
              >
                {dispositionOptions.map((option) => (
                  <option value={option.value} key={option.value}>
                    {isCandidateReview ? ({ analysis: "仅需求", pending: "待核对", attention: "需关注" } as Record<string, string>)[option.value] || option.label : option.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="matrix-batch-reason">
              <span>统一说明（可选）</span>
              <input
                type="text"
                name="matrix-batch-reason"
                autoComplete="off"
                value={batchReason}
                placeholder="仅在需要解释本次批量决定时填写"
                onChange={(event) => setBatchReason(event.target.value)}
              />
            </label>
          </div>
          <div className="matrix-batch-preview" role="status" aria-live="polite">
            <div>
              <strong>{batchCandidateNodes.length}</strong>
              <span>项将改为“{dispositionLabel(batchDisposition)}”</span>
            </div>
            <p>
              {batchSkippedCount}{" "}
              项保持不变或因容器/分析资格不适用而跳过。提交前不会修改清单。
            </p>
            <button
              className="button primary"
              type="button"
              disabled={!batchCandidateNodes.length || batchState === "保存中…"}
              onClick={() => void applyBatchDisposition()}
            >
              {batchState === "保存中…" ? "正在应用…" : "确认批量应用"}
            </button>
          </div>
        </section>
      )}

      {lens === "review" && batchState && !batchOpen && (
        <div className="matrix-batch-result" role="status" aria-live="polite">
          <span>{batchState}</span>
          {canBatchDisposition && batchUndo.length > 0 && (
            <button
              className="text-button"
              type="button"
              onClick={() => void undoBatchDisposition()}
            >
              撤销本次批量处理
            </button>
          )}
        </div>
      )}

      {lens === "analysis" && (
      <div className="matrix-context-toolbar">
        <label className="matrix-search-field">
          <Icon name="search" size={16} />
          <input
            ref={searchInputRef}
            type="search"
            name={`matrix-search-${lens}`}
            aria-label="搜索需求矩阵"
            autoComplete="off"
            value={query}
            placeholder="搜索编号、正文、状态或结论（/）"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <div className="matrix-filter-tabs" role="group" aria-label="矩阵主筛选">
          {primaryFilterOptions.map((option) => (
            <button
              type="button"
              key={option.value}
              className={filter === option.value ? "is-active" : ""}
              aria-pressed={filter === option.value}
              onClick={() => setFilter(option.value)}
            >
              {isCandidateReview ? ({ analysis: "仅需求", pending: "待核对", attention: "需关注" } as Record<string, string>)[option.value] || option.label : option.label}
            </button>
          ))}
        </div>
        <details className="matrix-more-filters">
          <summary>
            {filter === "tables"
              ? "更多筛选 · 表格"
              : filter === "excluded"
                ? "更多筛选 · 已排除"
                : "更多筛选"}
          </summary>
          <div role="group" aria-label="更多矩阵筛选">
            {availableSecondaryFilterOptions.map((option) => (
              <button
                type="button"
                key={option.value}
                className={filter === option.value ? "is-active" : ""}
                aria-pressed={filter === option.value}
                onClick={(event) => {
                  setFilter(option.value);
                  event.currentTarget.closest("details")?.removeAttribute("open");
                }}
              >
                {isCandidateReview ? ({ analysis: "仅需求", pending: "待核对", attention: "需关注" } as Record<string, string>)[option.value] || option.label : option.label}
              </button>
            ))}
          </div>
        </details>
        {scopedAnalysisSupported && (
          <div className="matrix-analysis-selection" role="status" aria-live="polite">
            <span>已选 {selectedAnalysisNodes.length} 项</span>
            {selectedAnalysisNodes.length > 0 && !aiRunning && (
              <button
                className="text-button"
                type="button"
                onClick={() => setAnalysisSelectionList([])}
              >
                清除
              </button>
            )}
          </div>
        )}
        {scopedAnalysisSupported &&
          !aiRunning &&
          selectedNode &&
          !matrixAnalysisEligible(selectedNode) && (
          <span className="matrix-analysis-current-note" role="status">
            当前内容不单独分析，请选择需求叶节点
          </span>
        )}
        {lens === "analysis" &&
          setStatus === "LOCKED" &&
          selectedSetId === activeSetId &&
          !aiRunning &&
          !aiProfileReady && (
            <span className="matrix-ai-readiness-note" role="status">
              智能分析未配置，可继续人工处理与导出；需要 AI 建议时，请在右上角“管理员设置”完成连接。
            </span>
          )}
        {(query.trim() || filter !== "all" || category || topic) && (
            <span
              className="matrix-filter-result"
              role="status"
              aria-live="polite"
            >
              {matchCounts.matched} 项命中 · 保留{" "}
              {matchCounts.retainedAncestors} 个父级路径
          </span>
        )}
        {matrixRefreshPending && (
          <span className="matrix-refresh-notice" role="status">
            AI 分析已完成；保存或撤销当前修改后刷新结果
          </span>
        )}
        <div className="matrix-detail-switch" role="group" aria-label="详情视图">
          <button
            type="button"
            className={detailPane === "source" ? "is-active" : ""}
            onClick={() => setDetailPane("source")}
          >
            完整内容
          </button>
          <button
            type="button"
            className={detailPane === "pdf" ? "is-active" : ""}
            onClick={() => setDetailPane("pdf")}
          >
            原 PDF
          </button>
          <button
            type="button"
            className={detailPane === "inspector" ? "is-active" : ""}
            onClick={() => setDetailPane("inspector")}
          >
            分析
          </button>
        </div>
      </div>
      )}

      {lens === "review" ? (
        <div
          className="matrix-workbench-grid is-review-extraction"
          data-review-layout={compactReviewLayout ? "compact" : "wide"}
        >
          <section
            ref={setTreePaneElement}
            className="matrix-tree-pane matrix-review-tree-pane"
            aria-label="确认需求结构树"
          >
            <div className="tree-tools matrix-review-tree-tools">
              <label className="search-field">
                <Icon name="search" size={16} />
                <input
                  ref={searchInputRef}
                  type="search"
                  name="matrix-search-review"
                  aria-label="搜索确认需求"
                  autoComplete="off"
                  value={query}
                  placeholder="搜索编号、章节、条款或表格"
                  onChange={(event) => setQuery(event.target.value)}
                />
              </label>
              <div className="tree-tool-row">
                <div className="tree-filter-chips" role="group" aria-label="确认需求筛选">
                  {primaryFilterOptions.map((option) => (
                    <button
                      type="button"
                      key={option.value}
                      className={filter === option.value ? "is-active" : ""}
                      aria-pressed={filter === option.value}
                      onClick={() => setFilter(option.value)}
                    >
                      {isCandidateReview ? ({ analysis: "仅需求", pending: "待核对", attention: "需关注" } as Record<string, string>)[option.value] || option.label : option.label}
                    </button>
                  ))}
                  <details className="matrix-review-more-filters">
                    <summary>更多</summary>
                    <div role="group" aria-label="更多确认需求筛选">
                      {availableSecondaryFilterOptions.map((option) => (
                        <button
                          type="button"
                          key={option.value}
                          className={filter === option.value ? "is-active" : ""}
                          aria-pressed={filter === option.value}
                          onClick={(event) => {
                            setFilter(option.value);
                            event.currentTarget
                              .closest("details")
                              ?.removeAttribute("open");
                          }}
                        >
                          {isCandidateReview ? ({ analysis: "仅需求", pending: "待核对", attention: "需关注" } as Record<string, string>)[option.value] || option.label : option.label}
                        </button>
                      ))}
                    </div>
                  </details>
                </div>
                <div className="tree-view-actions">
                  <details ref={treeExpandMenuRef} className="tree-expand-menu">
                    <summary title="选择确认树展开范围">
                      展开
                      <Icon name="chevron-down" size={14} />
                    </summary>
                    <div className="tree-expand-menu-popover">
                      <button type="button" onClick={() => expandToDepth(2)}>
                        展开 2 层
                      </button>
                      <button type="button" onClick={() => expandToDepth(3)}>
                        展开 3 层
                      </button>
                      <button type="button" onClick={expandAll}>
                        {query.trim() || filter !== "all" || category || topic
                          ? "展开全部筛选结果"
                          : "全部展开"}
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
                    title="将确认树收起到根节点"
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
              <span>{isCandidateReview
                ? (category || topic || query.trim() || filter !== "all"
                  ? `命中 ${matchCounts.matched} · 上级背景 ${matchCounts.retainedAncestors}`
                  : `${rows.length} 项层级/条目`)
                : `${rows.length} 项`}</span>
              <span>↑↓ 选择 · ←→ 层级</span>
            </div>
            <div
              className="matrix-treegrid-scroll matrix-review-tree-scroll"
              role="tree"
              aria-label="确认需求层级树"
              aria-setsize={rows.length}
              ref={treeScrollRef}
              onScroll={(event) => handleTreeScroll(event.currentTarget)}
            >
              {rows.length ? (
                <div
                  className="matrix-treegrid-spacer matrix-review-tree-spacer"
                  style={{ height: `${rows.length * MATRIX_ROW_HEIGHT}px` }}
                >
                  {virtualRows.map((row, offset) => {
                    const absoluteIndex = startIndex + offset;
                    const selected = row.node.node_id === selectedId;
                    const sourceNumber = matrixSourceNumber(row.node);
                    return (
                      <div
                        className={cx(
                          "matrix-review-tree-row",
                          selected && "is-selected",
                          row.node.matrix_visibility === "HUMAN_EXCLUDED" &&
                            "is-excluded",
                        )}
                        key={row.node.node_id}
                        role="treeitem"
                        data-review-target={isCandidateReview ? row.node.node_id : undefined}
                        aria-label={isCandidateReview ? `${String(row.node.candidate_review?.sourceId || "")} ${row.node.requirement_code} ${sourceNumber}，${matrixNodeLabel(row.node)}，${classificationLabels(row.node).join("、") || matrixHierarchyLabel(row.node)}` : matrixRowAccessibleName(row)}
                        aria-level={row.depth + 1}
                        aria-posinset={absoluteIndex + 1}
                        aria-setsize={rows.length}
                        aria-expanded={row.hasChildren ? row.expanded : undefined}
                        aria-selected={selected}
                        tabIndex={selected ? 0 : -1}
                        ref={(element) => {
                          if (element) rowRefs.current.set(row.node.node_id, element);
                          else rowRefs.current.delete(row.node.node_id);
                        }}
                        style={{
                          height: `${MATRIX_ROW_HEIGHT}px`,
                          transform: `translateY(${absoluteIndex * MATRIX_ROW_HEIGHT}px)`,
                          paddingInlineStart: `${10 + Math.min(row.depth, 8) * 15}px`,
                        }}
                        onClick={() => focusRow(absoluteIndex)}
                        onDoubleClick={() => setDetailPane("source")}
                        onKeyDown={(event) => onTreeKeyDown(event, row)}
                      >
                        {row.hasChildren ? (
                          <button
                            className="matrix-review-tree-expander"
                            type="button"
                            tabIndex={-1}
                            aria-label={row.expanded ? "收起" : "展开"}
                            onClick={(event) => {
                              event.stopPropagation();
                              setExpanded(row.node.node_id, !row.expanded);
                            }}
                          >
                            <Icon
                              name={row.expanded ? "chevron-down" : "chevron"}
                              size={15}
                            />
                          </button>
                        ) : (
                          <span className="matrix-review-tree-leaf" aria-hidden="true" />
                        )}
                        <span className="matrix-review-tree-copy">
                          <strong>
                            {sourceNumber && (
                              <span
                                className="matrix-review-tree-number"
                                title={sourceNumber}
                              >
                                {sourceNumber}
                              </span>
                            )}
                            <span>{matrixNodeLabel(row.node)}</span>
                          </strong>
                          <small>
                            {isCandidateReview
                              ? [String(row.node.candidate_review?.sourceId || ""), row.node.requirement_code,
                                 row.node.source_title_role === "AGENT_ASSIGNED" ? "Agent 拟名/编号" : "",
                                 classificationLabels(row.node).join("；") || String(row.node.candidate_review?.contentRole || matrixHierarchyLabel(row.node)),
                                 (row.node.topic_tags || []).map(tag => topicLabels[tag] || tag).join("、")].filter(Boolean).join(" · ")
                              : <>{matrixHierarchyLabel(row.node)} · {row.analysisStatus}</>}
                          </small>
                        </span>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <EmptyState
                  icon="search"
                  title="没有符合条件的内容"
                  description="调整搜索词或筛选条件，父级路径会随匹配项一起保留。"
                />
              )}
            </div>
          </section>

          <div className="matrix-review-detail-bar">
            <span>当前内容</span>
            <div className="matrix-detail-switch" role="group" aria-label="确认详情视图">
              <button
                type="button"
                className={detailPane === "source" ? "is-active" : ""}
                onClick={() => setDetailPane("source")}
              >
                完整内容
              </button>
              {compactReviewLayout && (
                <button
                  type="button"
                  className={detailPane === "pdf" ? "is-active" : ""}
                  onClick={() => setDetailPane("pdf")}
                >
                  原 PDF
                </button>
              )}
              <button
                type="button"
                className={detailPane === "inspector" ? "is-active" : ""}
                onClick={() => setDetailPane("inspector")}
              >
                {isCandidateReview ? "核对反馈" : "确认处置"}
              </button>
            </div>
          </div>

          <section
            className="matrix-review-center-pane"
            aria-label="确认需求完整内容"
          >
            <div className="matrix-review-center-scroll">
              {detailPane === "inspector" ? inspectorSurface : sourceSurface}
            </div>
          </section>

          <aside
            className="matrix-review-pdf-pane"
            aria-label="确认需求原 PDF"
          >
            {pdfSurface}
          </aside>
        </div>
      ) : (
        <div className="matrix-workbench-grid">
        <section
          ref={setTreePaneElement}
          className="matrix-tree-pane"
          aria-label="需求层级矩阵"
        >
          <div
              className={cx(
                "matrix-treegrid",
                compactMatrixColumns && "is-compact",
              )}
            role="treegrid"
            aria-label="需求层级与分析状态"
            aria-colcount={compactMatrixColumns ? 3 : 6}
            aria-rowcount={rows.length + 1}
          >
            <div className="matrix-treegrid-head" role="row">
              <span role="columnheader">编号</span>
              <span role="columnheader">内容</span>
              <span role="columnheader">层级</span>
              {!compactMatrixColumns && (
                <>
                  <span role="columnheader">分析状态</span>
                  <span role="columnheader">AI 结论</span>
                  <span role="columnheader">人工修改</span>
                </>
              )}
            </div>
            <div
              className="matrix-treegrid-scroll"
              role="rowgroup"
              ref={treeScrollRef}
              onScroll={(event) => handleTreeScroll(event.currentTarget)}
            >
              {rows.length ? (
                <div
                  className="matrix-treegrid-spacer"
                  style={{ height: `${rows.length * MATRIX_ROW_HEIGHT}px` }}
                >
                  {virtualRows.map((row, offset) => {
                    const absoluteIndex = startIndex + offset;
                    const selected = row.node.node_id === selectedId;
                    const displayedAnalysisStatus = analysisExecutionStatusLabel(
                      aiNodeExecutionByNodeId.get(row.node.node_id) || null,
                      row.analysisStatus,
                    );
                    return (
                      <div
                        className={cx(
                          "matrix-treegrid-row",
                          selected && "is-selected",
                          row.node.matrix_visibility === "HUMAN_EXCLUDED" &&
                            "is-excluded",
                        )}
                        key={row.node.node_id}
                        role="row"
                        aria-label={matrixRowAccessibleName({
                          ...row,
                          analysisStatus: displayedAnalysisStatus,
                        })}
                        aria-level={row.depth + 1}
                        aria-expanded={row.hasChildren ? row.expanded : undefined}
                        aria-selected={selected}
                        aria-rowindex={absoluteIndex + 2}
                        tabIndex={selected ? 0 : -1}
                        ref={(element) => {
                          if (element) rowRefs.current.set(row.node.node_id, element);
                          else rowRefs.current.delete(row.node.node_id);
                        }}
                        style={{
                          height: `${MATRIX_ROW_HEIGHT}px`,
                          transform: `translateY(${absoluteIndex * MATRIX_ROW_HEIGHT}px)`,
                        }}
                        onClick={() => focusRow(absoluteIndex)}
                        onDoubleClick={() => setDetailPane("source")}
                        onKeyDown={(event) => onTreeKeyDown(event, row)}
                      >
                        <span
                          className="matrix-treegrid-number"
                          role="gridcell"
                          title={matrixSourceNumber(row.node) || undefined}
                        >
                          {matrixSourceNumber(row.node) || "—"}
                        </span>
                        <span className="matrix-treegrid-primary" role="gridcell">
                          <span
                            className="matrix-tree-indent"
                            style={{ width: `${row.depth * 16}px` }}
                            aria-hidden="true"
                          />
                          {row.hasChildren ? (
                            <button
                              type="button"
                              tabIndex={-1}
                              aria-label={row.expanded ? "收起" : "展开"}
                              onClick={(event) => {
                                event.stopPropagation();
                                setExpanded(row.node.node_id, !row.expanded);
                              }}
                            >
                              <Icon
                                name={row.expanded ? "chevron-down" : "chevron"}
                                size={14}
                              />
                            </button>
                          ) : (
                            <span className="matrix-tree-leaf" aria-hidden="true" />
                          )}
                          {analysisActionsSupported && matrixAnalysisEligible(row.node) ? (
                            <label
                              className="matrix-analysis-checkbox-hit"
                              onClick={(event) => event.stopPropagation()}
                            >
                              <input
                                className="matrix-analysis-checkbox"
                                type="checkbox"
                                aria-label={`选择分析 ${row.node.requirement_code || matrixNodeLabel(row.node)}`}
                                  checked={analysisSelectionIds.has(
                                    row.node.node_id,
                                  )}
                                disabled={aiRunning}
                                onChange={() => toggleAnalysisSelection(row.node.node_id)}
                              />
                            </label>
                          ) : (
                            <span
                              className="matrix-analysis-checkbox-spacer"
                              aria-hidden="true"
                            />
                          )}
                          <span>
                            <strong>{truncate(matrixNodeLabel(row.node), 92)}</strong>
                            {matrixDisplayPreview(row.node) && (
                                <small>
                                  {truncate(
                                    matrixDisplayPreview(row.node),
                                    110,
                                  )}
                                </small>
                            )}
                          </span>
                        </span>
                        <span role="gridcell">
                          <em>{matrixHierarchyLabel(row.node)}</em>
                        </span>
                        {!compactMatrixColumns && (
                          <>
                            <span role="gridcell">
                                <StatusBadge
                                  tone={matrixAnalysisTone(
                                    displayedAnalysisStatus,
                                  )}
                                >
                                {displayedAnalysisStatus}
                              </StatusBadge>
                            </span>
                            <span role="gridcell">{row.aiSuggestion}</span>
                            <span role="gridcell" className="matrix-human-cell">
                              {row.humanDecision}
                            </span>
                          </>
                        )}
                      </div>
                    );
                  })}
                </div>
              ) : (
                <EmptyState
                  icon="search"
                  title="没有符合条件的内容"
                  description="调整搜索词或筛选条件，层级祖先会随匹配项一起保留。"
                />
              )}
            </div>
          </div>
        </section>

        <section
          className={cx(
            "matrix-evidence-pane",
            detailPane === "pdf" ? "is-pdf" : "is-source",
          )}
          aria-label={detailPane === "pdf" ? "原 PDF 证据" : "完整来源内容"}
        >
          {detailPane === "pdf" ? pdfSurface : sourceSurface}
        </section>

        <aside className="matrix-inspector" aria-label="集中式检查器">
          {inspectorSurface}
        </aside>
      </div>
      )}
      <p className="matrix-keyboard-hint">
        / 搜索 · ↑↓ 选择 · ←→ 展开与返回 · Home/End 首尾 · Enter 打开 · ⌘/Ctrl + Enter 保存并到下一项 · Esc 撤销未保存输入
      </p>
    </section>
  );
}
