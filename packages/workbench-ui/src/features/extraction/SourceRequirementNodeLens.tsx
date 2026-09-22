import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api, request, asRecord } from "../../shared/api/client";
import type {
  NativeStructureTablePreviewV1,
  SourceRequirementBaselineOutlineV1,
  SourceRequirementRecordDetailV1,
  SourceItemImageBindingV1,
} from "../../shared/api/contracts";
import { Icon } from "../../shared/ui/Icon";
import { StructuredTable } from "../restoration/StructuredTable";
import { NativeOfficeSourceView, NativeOfficePreviewTable, nativeOfficeObjectUrl, officeLocation, officeLocationLabel } from "./NativeOfficeSourceView";
import { tableDisplayTitle, tableDisplayAuthority } from "../restoration/tableDisplay";
import { sourceCropUrl } from "../restoration/restorationModel";
import { originalNumber } from "./sourcePresentation";
import { SourceDispositionNote, SourceItemReasoningDetail, SourceItemReasoningStatus, type SourceItemReasoningController } from "./SourceItemReasoning";
import { clearSourceFeedbackDraft, sourceFeedbackDraftKey, useSourceFeedbackDraft } from "./sourceItemDrafts";
import { FOCUS_LABELS, type SourceItemReviewRecord, type SourceItemReviewContext } from "./sourceItemReviewContext";
import { useDocumentAgent } from "../workspace/DocumentAgentContext";
import { useWorkbenchHost } from "../workspace/WorkbenchHostContext";
import type {
  StructuredTableModel,
  StructuredTableRecordFocus,
} from "../restoration/restorationModel";

export interface SourceRequirementEvidenceOverlay {
  id: string;
  page: number;
  type: "TABLE" | "TEXT" | "IMAGE";
  label: string;
  bbox: [number, number, number, number];
  sourceBounds: [number, number, number, number];
}

import type { SourceWorkbenchReader } from "./SourceWorkbenchReader";

interface SourceItemLensProps {
  sourceReader?: SourceWorkbenchReader;
  onSelectContext?: (sourceId: string) => void;
  projectId: string;
  documentId: string;
  sourceSha256: string;
  selectedSourceItemId: string;
  outline: SourceRequirementBaselineOutlineV1;
  reasoning?: SourceItemReasoningController;
  review?: SourceItemReviewRecord;
  onNextReviewItem?: () => void;
  atReviewEnd?: boolean;
  nextReviewLabel?: string;
  capabilities?: SourceItemReviewContext["capabilities"];
  analysisMode?: boolean;
  onChangePurpose?: (purpose: "review" | "analysis") => void;
  analysisUnitIds?: string[];
  tableView?: boolean; selectedDerivedUnitId?: string; onAnalysisUnitChange?: (unitId: string) => void;
  sourceHeading?: string;
  tablePreviewsById: Record<string, NativeStructureTablePreviewV1>;
  tableModelsById: Record<string, StructuredTableModel[]>;
  activePage: number;
  onPageChange: (page: number) => void;
  onShowSource: () => void;
  onLocateNative?: (ids: string[]) => void;
  onEvidenceChange: (overlays: SourceRequirementEvidenceOverlay[]) => void;
  onFeedbackSaved: () => void;
}

function requirementKindLabel(value: string): string {
  return (
    {
      FUNCTIONAL: "功能",
      QUALITY: "质量",
      CONSTRAINT: "约束",
      PROJECT: "项目",
      PROCESS: "过程",
    }[value] || value
  );
}

function nonRequirementLabel(value: string | null): string {
  if (!value) return "";
  return (
    {
      HEADING: "标题",
      METADATA: "文控信息",
      REVISION_HISTORY: "修订记录",
      CONTEXT: "背景说明",
      DEFINITION: "定义",
      RATIONALE: "理由",
      EXAMPLE: "示例",
      NOTE: "注释",
      REFERENCE: "外部引用",
    }[value] || value
  );
}

function feedbackCommandId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `source-requirement-feedback:${crypto.randomUUID()}`;
  }
  return `source-requirement-feedback:${Date.now()}:${Math.random()
    .toString(16)
    .slice(2)}`;
}

function errorMessage(value: unknown): string {
  if (value instanceof ApiError) return value.message;
  return "当前原文条目暂时无法读取。";
}

function SourceItemImage({ binding, projectId, documentId, sourceId, onLocate, onLocateNative }: {
  binding: SourceItemImageBindingV1; projectId: string; documentId: string; sourceId: string;
  onLocate: (page: number) => void; onLocateNative?: (ids: string[]) => void;
}) {
  const host = useWorkbenchHost();
  const [failedUrl, setFailedUrl] = useState("");
  const native = officeLocation(binding.native_locator);
  const handle = typeof binding.source_handle === "string" ? binding.source_handle : binding.anchor_id;
  const contentSha = typeof binding.content_sha256 === "string" ? binding.content_sha256 : "";
  const url = native ? nativeOfficeObjectUrl(projectId, documentId, binding.source_sha256, handle, contentSha)
    : sourceCropUrl({ projectId, documentId, sourceSha256: binding.source_sha256, page: binding.page, bbox: binding.bbox });
  const failed = failedUrl === url;
  if (native && !contentSha) return <NativeOfficeSourceView projectId={projectId} documentId={documentId}
    sourceSha256={binding.source_sha256} imageOnlyId={handle} />;
  return <figure className="matrix-source-figure">
    {failed ? <p role="status">{native ? "原图暂时无法加载，请查看或下载原件核查。" : "原图暂时无法加载，可定位到原 PDF 核查。"}</p>
      : <img src={host.resourceUrl(url)} alt={native ? `原文图像 · ${sourceId} · ${officeLocationLabel(native)}` : `原文图像 · ${sourceId} · 第 ${binding.page} 页`}
          loading="lazy" decoding="async" onError={() => setFailedUrl(url)} />}
    <figcaption>原文图像 · {native ? officeLocationLabel(native) : `P${binding.page}`} <button type="button"
      onClick={() => native ? onLocateNative?.([handle]) : onLocate(binding.page)}>{native ? "定位到原件" : "定位到原页"}</button></figcaption>
  </figure>;
}

export function SourceItemLens({
  sourceReader,
  onSelectContext,
  projectId,
  documentId,
  sourceSha256,
  selectedSourceItemId,
  outline,
  reasoning,
  review,
  onNextReviewItem,
  atReviewEnd = false,
  nextReviewLabel = "下一项",
  capabilities,
  analysisMode = false,
  onChangePurpose,
  analysisUnitIds,
  tableView = false, selectedDerivedUnitId, onAnalysisUnitChange,
  sourceHeading,
  tablePreviewsById,
  tableModelsById,
  activePage,
  onPageChange,
  onShowSource,
  onLocateNative,
  onEvidenceChange,
  onFeedbackSaved,
}: SourceItemLensProps) {
  const host = useWorkbenchHost();
  const documentAgent = useDocumentAgent();
  const [loadedDetail, setDetail] = useState<SourceRequirementRecordDetailV1 | null>(null);
  const detailCache = useRef(new Map<string, SourceRequirementRecordDetailV1>());
  const detailKey = JSON.stringify([projectId, documentId, sourceSha256, outline.document_map.artifact_sha256,
    outline.baseline.artifact_sha256, sourceReader?.revision_id, selectedSourceItemId]);
  const detail = loadedDetail?.record.source_record_id === selectedSourceItemId
    && loadedDetail.baseline_artifact_sha256 === outline.baseline.artifact_sha256
    && loadedDetail.source_sha256 === sourceSha256 ? loadedDetail : null;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const draftKey = sourceFeedbackDraftKey(projectId, documentId, selectedSourceItemId, sourceReader?.revision_id || outline.baseline.artifact_sha256);
  const draft = useSourceFeedbackDraft(draftKey, sourceReader?.revision_number || outline.baseline.revision);
  const feedback = draft.text;
  const sharedFeedback = Boolean(sourceReader && documentAgent?.producer === "EXTERNAL_PLUGIN");
  const hasLegacyDraft = Boolean(feedback || draft.otherDrafts.length);
  const setFeedback = draft.update;
  const activeDraftKey = useRef(draftKey);
  activeDraftKey.current = draftKey;
  const [feedbackState, setFeedbackState] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [applyingRevision, setApplyingRevision] = useState(false);
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [explanationOpen, setExplanationOpen] = useState(false);
  const [focusReasonOpen, setFocusReasonOpen] = useState(false);
  const [originalOpen, setOriginalOpen] = useState(false);
  const [analysisSelection, setAnalysisSelection] = useState<{ sourceId: string; unitId: string } | null>(null);
  const analysisChildren = reasoning?.state?.decomposition?.records[selectedSourceItemId] || [];
  const analysisUnits = analysisChildren.length ? analysisChildren.map(child => child.singular_requirement_id || child.node_id || "") : [selectedSourceItemId];
  const queueUnitIds = analysisUnitIds ? analysisUnits.filter(unit => analysisUnitIds.includes(unit)) : analysisUnits;
  const selectedChild = analysisChildren.find(child => (child.singular_requirement_id || child.node_id) === selectedDerivedUnitId);
  const parentOverview = tableView && analysisChildren.length > 0 && !selectedChild;
  const responseMode = analysisMode && !parentOverview;
  const selection = selectedChild ? selectedDerivedUnitId : analysisSelection?.sourceId === selectedSourceItemId ? analysisSelection.unitId : undefined;
  const selectedAnalysisUnit = parentOverview ? undefined : selection && queueUnitIds.includes(selection) ? selection : queueUnitIds[0];
  const nextAnalysisUnit = parentOverview ? queueUnitIds[0] : responseMode && selectedAnalysisUnit ? queueUnitIds[queueUnitIds.indexOf(selectedAnalysisUnit) + 1] : undefined;
  const selectAnalysisUnit = (unitId: string) => { setAnalysisSelection({ sourceId: selectedSourceItemId, unitId }); onAnalysisUnitChange?.(unitId); };
  const nextItem = () => { if (nextAnalysisUnit) selectAnalysisUnit(nextAnalysisUnit); else onNextReviewItem?.(); };
  const feedbackInputRef = useRef<HTMLTextAreaElement | null>(null);
  const reviewHeaderRef = useRef<HTMLElement | null>(null);
  const originalRef = useRef<HTMLElement | null>(null);
  const derivedRef = useRef<HTMLDivElement | null>(null);
  const imageReadingRef = useRef<HTMLElement | null>(null);
  const changesRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    setFeedbackState("");
    setFeedbackOpen(false);
    setExplanationOpen(false);
    setFocusReasonOpen(false);
  }, [projectId, documentId, selectedSourceItemId, outline.baseline.artifact_sha256]);

  useEffect(() => {
    if (!selectedSourceItemId) {
      setDetail(null);
      onEvidenceChange([]);
      return;
    }
    const controller = new AbortController();
    const cached = detailCache.current.get(detailKey);
    setLoading(!cached);
    setDetail(cached || null);
    if (!cached) onEvidenceChange([]);
    setError("");
    void (sourceReader ? sourceReader.loadRecord(selectedSourceItemId, controller.signal)
      : api.sourceRequirementRecord(projectId, documentId, selectedSourceItemId, controller.signal))
      .then((value) => {
        if (controller.signal.aborted) return;
        if (value.source_sha256 !== sourceSha256 || value.baseline_artifact_sha256 !== outline.baseline.artifact_sha256
            || value.document_map_artifact_sha256 !== outline.document_map.artifact_sha256
            || value.record.source_record_id !== selectedSourceItemId) {
          throw new Error("原文版本已更新，请同步清单后重新查看；当前未混用旧内容。");
        }
        detailCache.current.delete(detailKey);
        detailCache.current.set(detailKey, value);
        if (detailCache.current.size > 32) {
          detailCache.current.delete(detailCache.current.keys().next().value!);
        }
        setDetail(value);
      })
      .catch((caught) => {
        if (!controller.signal.aborted) {
          detailCache.current.delete(detailKey);
          setDetail(null);
          setError(caught instanceof Error ? caught.message : errorMessage(caught));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [detailKey, documentId, onEvidenceChange, outline.baseline.artifact_sha256, outline.document_map.artifact_sha256, projectId, selectedSourceItemId, sourceSha256]);

  const selected =
    outline.records.find(
      (record) => record.source_record_id === selectedSourceItemId,
    ) || null;
  const tablePreview = selected
    ? tablePreviewsById[selected.logical_table_scope_id]
    : undefined;
  const tableModels = selected
    ? tableModelsById[selected.logical_table_scope_id] || []
    : [];
  const recordFocus = useMemo<StructuredTableRecordFocus | null>(() => {
    if (!detail || !tablePreview || !tableModels.length) return null;
    const record = detail.record;
    const rowBindings = record.table_fragments.flatMap((binding) => {
      const fragment = tablePreview.fragments.find(
        (item) => item.fragment_id === binding.fragment_id,
      );
      if (!fragment) return [];
      return Array.from(
        { length: binding.row_end - binding.row_start },
        (_, offset) => {
          const index = binding.row_start + offset;
          return {
            tableId: binding.fragment_id,
            index,
            cellIds: fragment.cells
              .filter(
                (cell) =>
                  cell.row <= index && cell.row + cell.row_span > index,
              )
              .map((cell) => cell.cell_id),
          };
        },
      );
    });
    const headerCellIdsByTable = Object.fromEntries(
      tablePreview.fragments.map((fragment) => [
        fragment.fragment_id,
        fragment.cells
          .map((cell) => cell.cell_id)
          .filter((cellId) => record.shared_header_cell_ids.includes(cellId)),
      ]),
    );
    const sharedCellIdsByTable = Object.fromEntries(
      tablePreview.fragments.map((fragment) => [
        fragment.fragment_id,
        fragment.cells
          .map((cell) => cell.cell_id)
          .filter((cellId) => record.shared_table_cell_ids.includes(cellId)),
      ]),
    );
    return {
      recordId: record.source_record_id,
      recordLabel: record.label,
      mode: "ROW",
      logicalTableId: record.logical_table_scope_id,
      rowBindings,
      columnBindings: [],
      headerCellIdsByTable,
      headerTexts: [],
      sharedCellIdsByTable,
      // Full source context stays in the collapsed evidence disclosure above. Keeping
      // it out of the table focus prevents a long text dump from obscuring the
      // native headers and current business rows.
      sharedValues: [],
      notes: [],
      imageCount: record.related_images?.length || 0,
      classificationLabel:
        record.requirementness === "REQUIREMENT"
          ? `需求 · ${record.requirement_kinds
              .map(requirementKindLabel)
              .join(" / ")}`
          : `非需求 · ${nonRequirementLabel(record.non_requirement_role)}`,
    };
  }, [detail, tableModels, tablePreview]);

  useEffect(() => {
    if (!detail) {
      onEvidenceChange([]);
      return;
    }
    const tableOverlays = detail.record.table_fragments
      .filter((binding) => binding.page === activePage)
      .map((binding, index) => ({
        id: `${detail.record.source_record_id}:${binding.fragment_id}:${index}`,
        page: binding.page,
        type: "TABLE" as const,
        label: `${detail.record.source_display_id} · ${detail.record.display_label}`,
        bbox: binding.bbox,
        sourceBounds: binding.source_bounds,
      }));
    const textOverlays = detail.record.text_bindings
      .filter((binding) => binding.page === activePage)
      .map((binding, index) => ({
        id: `${detail.record.source_record_id}:${binding.content_id}:${index}`,
        page: binding.page,
        type: "TEXT" as const,
        label: `${detail.record.source_display_id} · ${detail.record.display_label}`,
        bbox: binding.bbox,
        sourceBounds: binding.source_bounds,
      }));
    const sharedTextOverlays = detail.record.shared_source_content
      .filter((content) => content.page === activePage)
      .map((content, index) => ({
        id: `${detail.record.source_record_id}:shared:${content.content_id}:${index}`,
        page: content.page,
        type: "TEXT" as const,
        label: `${detail.record.source_display_id} · 共同适用原文`,
        bbox: content.bbox,
        sourceBounds: content.source_bounds,
      }));
    const imageOverlays = [...(detail.record.image_bindings || []), ...(detail.record.related_images || [])]
      .filter((binding, index, bindings) => binding.page === activePage
        && bindings.findIndex(candidate => candidate.anchor_id === binding.anchor_id) === index).map(binding => ({
        id: `${detail.record.source_record_id}:${binding.anchor_id}`, page: binding.page,
        type: "IMAGE" as const, label: `${detail.record.source_display_id} · 原文图像`,
        bbox: binding.bbox, sourceBounds: binding.source_bounds,
      }));
    const overlays = [...tableOverlays, ...textOverlays, ...sharedTextOverlays, ...imageOverlays];
    onEvidenceChange(overlays);
  }, [activePage, detail, onEvidenceChange]);

  useEffect(
    () => () => onEvidenceChange([]),
    [onEvidenceChange],
  );

  const submitFeedback = async () => {
    if (!selected || !feedback.trim() || submitting) return;
    const submittedKey = draftKey;
    const submittedText = feedback;
    setSubmitting(true);
    setFeedbackState("");
    try {
      if (sourceReader) {
        await sourceReader.saveFeedback(feedback.trim(), selected.node_id, [selected.source_record_id]);
        clearSourceFeedbackDraft(submittedKey, submittedText);
        const updated = await sourceReader.loadRecord(selected.source_record_id);
        if (activeDraftKey.current === submittedKey) {
          detailCache.current.set(detailKey, updated); setDetail(updated);
          setFeedbackState("修改意见已保存，等待当前插件任务取回并修订。");
        }
        onFeedbackSaved(); return;
      }
      const result = await api.submitSourceRequirementFeedback(projectId, documentId, {
        schema_version: "source-requirement-feedback-request/v1",
        command_id: feedbackCommandId(),
        expected_source_sha256: sourceSha256,
        expected_document_map_artifact_sha256:
          outline.document_map.artifact_sha256,
        expected_baseline_artifact_id: outline.baseline.artifact_id,
        source_record_id: selected.source_record_id,
        message: feedback.trim(),
      });
      const savedFeedback = result.feedback as SourceRequirementRecordDetailV1["record"]["feedbacks"][number];
      setDetail((current) =>
        current && current.record.source_record_id === selected.source_record_id
          && current.baseline_artifact_sha256 === outline.baseline.artifact_sha256
          ? {
              ...current,
              record: {
                ...current.record,
                feedbacks: [
                  ...current.record.feedbacks.filter(
                    (item) => item.feedback_id !== savedFeedback.feedback_id,
                  ),
                  savedFeedback,
                ],
              },
            }
          : current,
      );
      clearSourceFeedbackDraft(submittedKey, submittedText);
      onFeedbackSaved();
      if (!selected.logical_table_scope_id) {
        setApplyingRevision(true);
        const job = await api.startSourceItemScopeRevisionRun(projectId, documentId, {
          schema_version: "source-item-scope-revision-start-request/v1",
          command_id: feedbackCommandId().replace(
            "source-requirement-feedback:",
            "source-item-scope-revision:",
          ),
          expected_source_sha256: sourceSha256,
          expected_document_map_artifact_sha256:
            outline.document_map.artifact_sha256,
          expected_baseline_artifact_id: outline.baseline.artifact_id,
          expected_baseline_artifact_sha256: outline.baseline.artifact_sha256,
          expected_baseline_revision: outline.baseline.revision,
          affected_scope_ids: [`text:${selected.node_id}`],
          comparison_scope_ids: [],
          feedback_ids: [savedFeedback.feedback_id],
        });
        if (activeDraftKey.current === submittedKey) setFeedbackState(
          job.progress.message || "指引已保存，Agent 局部修订任务已排队。",
        );
      } else {
        if (activeDraftKey.current === submittedKey) setFeedbackState(result.delivery_message);
      }
      onFeedbackSaved();
    } catch (caught) {
      if (activeDraftKey.current === submittedKey) setFeedbackState(errorMessage(caught));
    } finally {
      setSubmitting(false);
      setApplyingRevision(false);
    }
  };

  const retryPendingRevision = async () => {
    if (!selected || applyingRevision || !detail) {
      return;
    }
    const feedbackIds = detail.record.feedbacks
      .filter((item) => item.status !== "CONSUMED")
      .map((item) => item.feedback_id)
      .sort();
    setApplyingRevision(true);
    setFeedbackState("");
    const startedKey = draftKey;
    try {
      if (selected.logical_table_scope_id) {
        const capability = capabilities?.table_classification_review;
        if (!capability?.available || !capability.endpoint) throw new Error(capability?.unavailable_reason || "表格分类复核暂不可用；反馈可以先保存。");
        const pendingMessages = detail.record.feedbacks.filter(item => feedbackIds.includes(item.feedback_id)).map(item => item.message);
        const job = await request<{ progress?: { message?: string } }>(capability.endpoint, { method: "POST", body: JSON.stringify({
          command_id: feedbackCommandId(), expected_source_sha256: sourceSha256,
          expected_baseline_sha256: outline.baseline.artifact_sha256, source_item_ids: [selected.source_record_id],
          feedback_ids: feedbackIds, feedback: pendingMessages.join("\n") || "请结合完整表格上下文复核本条需求判定与分类，保持原文行组和边界。",
        }) });
        if (activeDraftKey.current === startedKey) setFeedbackState(job.progress?.message || "已交给Agent复核分类，原文边界保持不变。");
        onFeedbackSaved();
        return;
      }
      const job = await api.startSourceItemScopeRevisionRun(projectId, documentId, {
        schema_version: "source-item-scope-revision-start-request/v1",
        command_id: feedbackCommandId().replace(
          "source-requirement-feedback:",
          "source-item-scope-revision:",
        ),
        expected_source_sha256: sourceSha256,
        expected_document_map_artifact_sha256:
          outline.document_map.artifact_sha256,
        expected_baseline_artifact_id: outline.baseline.artifact_id,
        expected_baseline_artifact_sha256: outline.baseline.artifact_sha256,
        expected_baseline_revision: outline.baseline.revision,
        affected_scope_ids: [`text:${selected.node_id}`],
        comparison_scope_ids: [],
        feedback_ids: feedbackIds,
      });
      if (activeDraftKey.current === startedKey) setFeedbackState(
        job.progress.message || "Agent 局部修订任务已排队。",
      );
      onFeedbackSaved();
    } catch (caught) {
      if (activeDraftKey.current === startedKey) setFeedbackState(errorMessage(caught));
    } finally {
      setApplyingRevision(false);
    }
  };

  const undoLatestRevision = async () => {
    const revision = outline.scope_revision;
    if (!revision?.can_undo || applyingRevision) return;
    setApplyingRevision(true);
    setFeedbackState("");
    try {
      const job = await api.startSourceItemScopeUndoRun(projectId, documentId, {
        schema_version: "source-item-scope-undo-start-request/v1",
        command_id: feedbackCommandId().replace(
          "source-requirement-feedback:",
          "source-item-scope-undo:",
        ),
        expected_source_sha256: sourceSha256,
        expected_document_map_artifact_sha256:
          outline.document_map.artifact_sha256,
        expected_baseline_artifact_id: outline.baseline.artifact_id,
        expected_baseline_artifact_sha256: outline.baseline.artifact_sha256,
        expected_baseline_revision: outline.baseline.revision,
        expected_scope_revision_receipt_sha256: revision.receipt_sha256,
      });
      setFeedbackState(job.progress.message || "撤销任务已排队。当前清单在发布前保持不变。");
      onFeedbackSaved();
    } catch (caught) {
      setFeedbackState(errorMessage(caught));
    } finally {
      setApplyingRevision(false);
    }
  };

  if (!selected) return null;

  const evidencePages = Array.from(
    new Set([
      ...selected.pages,
      ...(detail?.record.shared_source_content.map((content) => content.page) || []),
    ].filter((page): page is number => typeof page === "number" && page > 0)),
  ).sort((left, right) => left - right);

  const children = reasoning?.state?.decomposition?.records[selected.source_record_id] || [];
  const jump = (element: HTMLElement | null) => {
    if (!element) return;
    element.style.scrollMarginTop = `${(reviewHeaderRef.current?.offsetHeight || 130) + 32}px`;
    element.scrollIntoView?.({ block: "start", behavior: "smooth" });
  };
  const openFeedback = () => {
    if (host.readOnly) { if (documentAgent?.collaborationAvailable) documentAgent.show("", "selected"); return; }
    if (sharedFeedback && !hasLegacyDraft) { documentAgent!.show("", "selected"); return; }
    setFeedbackOpen(true);
    requestAnimationFrame(() => feedbackInputRef.current?.focus({ preventScroll: true }));
  };
  const history = detail?.record.feedbacks || [];

  const originalBlock = detail ? (
<section ref={originalRef} className="source-review-original" aria-label="本条原文与证据">
            {detail.record.text_bindings.length > 0 && <section className="source-item-verbatim" aria-label="当前原文条目的逐字内容">
              <header><strong>客户原文</strong></header>
              {detail.record.text_bindings.map(binding => <article key={`${binding.content_id}:${binding.start_offset}`}>
                <div><p>{binding.reading_projection?.display_text ?? binding.text}</p>
                  {binding.reading_projection?.remaining_partial_overlaps?.length ? <small>仍有部分文字交叠，请结合原页核查。</small> : null}
                  {binding.reading_projection && <details><summary>已合并同位置叠印 · 查看原始捕获</summary><p>{binding.text}</p></details>}
                </div>
                <button type="button" onClick={() => { if (officeLocation(binding.native_locator)) { onLocateNative?.([binding.content_id]); } else { onPageChange(binding.page); } onShowSource(); }}>
                  {officeLocation(binding.native_locator) ? officeLocationLabel(binding.native_locator) : `P${binding.page}`}</button>
              </article>)}
            </section>}
            {tablePreview?.fragments.some(fragment => officeLocation(fragment.native_locator)) ? <NativeOfficePreviewTable preview={tablePreview}
              focusedCells={detail.record.table_fragments.flatMap(fragment => fragment.cell_ids)} />
              : tablePreview && tableModels.length && recordFocus ? <section className="source-requirement-table" aria-label="当前原文条目的表格内容">
              <StructuredTable table={tableModels[0]} tables={tableModels}
                displayLabel={tableDisplayAuthority(tablePreview) ? tableDisplayTitle(tablePreview) : selected.display_label}
                displayLabelAuthority={tableDisplayAuthority(tablePreview)}
                fitContainer compactRecord recordFocus={recordFocus} projectId={projectId} documentId={documentId} sourceSha256={sourceSha256}
                onPageChange={page => { onPageChange(page); onShowSource(); }} />
            </section> : selected.logical_table_scope_id ? <div className="source-requirement-state" role="status">正在还原当前原文条目的结构化表格内容…</div> : null}
            {detail.record.image_bindings?.map(binding => <SourceItemImage key={binding.anchor_id} binding={binding}
              projectId={projectId} documentId={documentId} sourceId={selected.source_display_id} onLocateNative={onLocateNative}
              onLocate={page => { onPageChange(page); onShowSource(); }} />)}
          </section>
  ) : null;
  const analysisBlock = !parentOverview && reasoning && selected.requirementness === "REQUIREMENT" && <div ref={derivedRef} className="source-review-derived">
            <SourceItemReasoningDetail workflow={reasoning} readOnly={Boolean(sourceReader)} sourceItemId={selected.source_record_id}
              requirementId={selected.requirement_display_id || selected.source_display_id} reviewMode={!responseMode}
              purpose={analysisMode ? "analysis" : "review"}
              showUnitPicker={!tableView} showUnitText={!selectedChild}
              matchingUnitIds={analysisUnitIds} selectedUnitId={selectedAnalysisUnit} onSelectUnit={selectAnalysisUnit}
              onPageChange={page => { onPageChange(page); onShowSource(); }} />
          </div>;
  return (
    <section className="source-requirement-lens" aria-label="原文需求清单">
      {loading ? <div className="source-requirement-state" role="status">正在读取当前原文条目与表格证据…</div>
        : error ? <div className="source-requirement-state is-error" role="alert">{error}</div>
        : selected && detail ? <article className={`source-requirement-detail${responseMode ? " is-analysis-mode" : ""}`}>
          <header ref={reviewHeaderRef} className="source-review-item-header">
            <div className="source-review-identity">
              <span>{selectedChild ? `${selectedChild.requirement_display_id} · 派生自 ${selected.requirement_display_id || selected.source_display_id} · 来源 ${selected.source_display_id}` : `${selected.requirement_display_id || selected.source_display_id}${selected.requirement_display_id ? ` · 来源 ${selected.source_display_id}` : ""}`}</span>
              <h4>{selectedChild?.text || sourceHeading || selected.display_label}</h4>
              <span className="source-original-number">{selectedChild ? "来源原编号" : "客户原编号"}：{originalNumber(selectedChild || selected)}</span>
              {!sourceHeading && detail.record.image_bindings?.length ? <small>图像条目 · 辅助标题</small> : null}
              <div className="source-review-facets">
                <span>{selectedChild ? "派生需求" : selected.requirementness === "REQUIREMENT" ? "需求" : "非需求"}</span>
                {selected.requirementness === "REQUIREMENT"
                  ? (selectedChild?.requirement_kinds || selected.requirement_kinds).map(kind => <span key={kind}>{requirementKindLabel(kind)}</span>)
                  : <span>{nonRequirementLabel(selected.non_requirement_role)}</span>}
                {review?.focus_areas.length ? <button type="button" className="source-review-priority" title={review.focus_reason}
                  aria-expanded={focusReasonOpen} onClick={() => setFocusReasonOpen(value => !value)}>
                  重点分析 · {review.focus_areas.map(area => FOCUS_LABELS[area]).join(" / ")}</button> : null}
                {review?.needs_attention ? <span className="source-review-attention">有待核对事项</span> : null}
              </div>
            </div>
            <nav className="source-review-shortcuts" aria-label="本条审阅操作">
              <button type="button" onClick={() => { setOriginalOpen(true); requestAnimationFrame(() => jump(originalRef.current)); }}>原文</button>
              {children.length > 0 && !parentOverview && !selectedChild && <button type="button" onClick={() => jump(derivedRef.current)}>派生 {children.length}</button>}
              {detail.record.visual_interpretation?.text && <button type="button" onClick={() => jump(imageReadingRef.current)}>图与释读</button>}
              {review?.changed_from_previous === true && <button type="button" onClick={() => jump(changesRef.current)}>本次变化</button>}
              {host.readOnly ? <button type="button" className="is-primary" disabled={!documentAgent?.collaborationAvailable} onClick={openFeedback}>在会话中讨论此条</button> : <button type="button" className="is-primary" onClick={() => analysisMode && onChangePurpose ? onChangePurpose("review") : openFeedback()}>{analysisMode && onChangePurpose ? selectedChild ? "修改父条款" : "修改原文整理" : feedback ? "继续写反馈" : selectedChild ? "修改父条款" : "提出修改"}</button>}
              {!analysisMode && selected.requirementness === "REQUIREMENT" && onChangePurpose && <button type="button" onClick={() => onChangePurpose("analysis")}>评审此项响应</button>}
              {onNextReviewItem && <button type="button" disabled={atReviewEnd && !nextAnalysisUnit} title={atReviewEnd && !nextAnalysisUnit ? "已到当前筛选的最后一项" : undefined} onClick={nextItem}>{nextAnalysisUnit ? "下个分析单元" : nextReviewLabel}</button>}
            </nav>
          </header>

          {review?.focus_areas.length && focusReasonOpen ? <p className="source-review-focus-reason">{review.focus_reason}</p> : null}
          {review?.attention_reasons.length ? <section className="source-review-issues" aria-label="本条待核对事项">
            <strong>本条待核对</strong><ul>{review.attention_reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>
          </section> : null}

          {parentOverview && <section className="source-parent-overview" aria-label="父条款与派生项"><p>完整父条款保留如下。{analysisChildren.length} 个派生项在表格中独立成行；选择派生行{analysisMode ? "查看对应证据与响应" : "核对内容、分类和派生依据"}。</p><SourceDispositionNote state={reasoning?.state || null} sourceItemId={selectedSourceItemId}/><div className="source-item-delivery-actions">
            {!analysisMode && <button type="button" className="button secondary compact" disabled={Boolean(sourceReader) || reasoning?.busy} onClick={() => void reasoning?.run("decomposition", selectedSourceItemId)}>检查本条拆分</button>}
            {analysisMode && <button type="button" className="button secondary compact" disabled={Boolean(sourceReader) || reasoning?.busy} onClick={() => void reasoning?.run("analysis", selectedSourceItemId)}>分析本条派生项</button>}
          </div></section>}
          {responseMode && analysisBlock}
          {responseMode ? <details className="analysis-original" open={originalOpen} onToggle={event => setOriginalOpen(event.currentTarget.open)}><summary>客户原文与图表 · 展开核对完整内容</summary>{originalBlock}</details> : originalBlock}

          {Boolean(detail.record.context_source_items?.length) && <details className="source-requirement-shared-context">
            <summary>关联原文与条件 · {detail.record.context_source_items!.length} 项</summary>
            {detail.record.context_source_items!.map(context => <article key={context.source_item_id}>
              <div><strong>{context.source_display_id} · {context.label}</strong>
                {context.text_bindings.map(binding => <p key={`${binding.content_id}:${binding.start_offset}`}>{binding.text}</p>)}
              </div>
              {onSelectContext && <button type="button" onClick={() => onSelectContext(context.source_item_id)}>查看完整关联条目</button>}
            </article>)}
          </details>}
          {detail.record.shared_source_content.length > 0 && <details className="source-requirement-shared-context">
            <summary>共用条件与上下文 · {detail.record.shared_source_content.length} 段</summary>
            <p className="muted">以下来源仍随本条参与分析，展开查看完整内容。</p>
            {detail.record.shared_source_content.map(content => <article key={content.content_id}>
              <div><p>{content.text}</p></div>
              <button type="button" onClick={() => { if (officeLocation(content.native_locator)) { onLocateNative?.(content.source_handles); } else { onPageChange(content.page); } onShowSource(); }}>
                {officeLocation(content.native_locator) ? officeLocationLabel(content.native_locator) : `P${content.page}`}</button>
            </article>)}
          </details>}

          {detail.record.visual_interpretation?.text ? <section ref={imageReadingRef} className="source-review-image-reading" aria-label="Agent 图片释读">
            <header><strong>Agent 图片释读</strong><span>待核对 · 以原图为准</span></header>
            {detail.record.related_images?.length ? <details className="source-review-original-images"><summary>放大本条原图（{detail.record.related_images.length}）</summary>
              {detail.record.related_images.map(binding => <SourceItemImage key={binding.anchor_id} binding={binding}
                projectId={projectId} documentId={documentId} sourceId={selected.source_display_id} onLocateNative={onLocateNative}
                onLocate={page => { onPageChange(page); onShowSource(); }} />)}
            </details> : null}
            <p>{detail.record.visual_interpretation.text}</p>
          </section> : detail.record.related_images?.length ? <details className="source-requirement-shared-context">
            <summary>本条来源图像（{detail.record.related_images.length}）</summary>
            {detail.record.related_images.map(binding => <SourceItemImage key={binding.anchor_id} binding={binding}
              projectId={projectId} documentId={documentId} sourceId={selected.source_display_id} onLocateNative={onLocateNative}
              onLocate={page => { onPageChange(page); onShowSource(); }} />)}
          </details> : null}

          {!responseMode && analysisBlock}

          <details className="source-requirement-agent-explanation" open={explanationOpen}
            onToggle={event => setExplanationOpen(event.currentTarget.open)}>
            <summary>查看分类与边界依据</summary>
            <p>{selected.decision_summary}</p><small>Agent建议，原文保持不变。</small>
          </details>
          {review?.changed_from_previous === true && <section ref={changesRef} className="source-review-changes" aria-label="本条修订变化">
            <header><strong>本次变化</strong><span>修订前 → 当前</span></header><p>{review.change_summary}</p>
            {review.previous_judgment && <details><summary>比较前后判断依据</summary>
              <div className="source-review-comparison"><strong>修订前</strong>
                <p>{review.previous_judgment.requirementness === "REQUIREMENT" ? "需求" : "非需求"} · {review.previous_judgment.requirement_kinds.map(requirementKindLabel).join(" / ") || nonRequirementLabel(review.previous_judgment.non_requirement_role)}</p>
                <p>{review.previous_judgment.decision_summary}</p><strong>当前</strong><p>{selected.decision_summary}</p>
              </div>
            </details>}
          </section>}
          {reasoning && <SourceItemReasoningStatus workflow={reasoning} showSummary={false} />}

          {(feedbackOpen || feedback || history.length > 0 || feedbackState) && <section className={`source-feedback-dock${feedbackOpen ? " is-open" : ""}`} aria-label="本条反馈">
            <div className="source-feedback-dock-bar">
              <button type="button" className="button primary compact" onClick={() => feedbackOpen ? setFeedbackOpen(false) : openFeedback()}>
                {feedbackOpen ? "收起反馈" : "打开反馈"}</button>
              <span role="status">{feedback ? draft.durable ? "草稿已在本机保存 · 未发送" : "草稿仅在当前窗口暂存" : feedbackState || `${history.length} 条已保存反馈`}</span>
              {history.length > 0 && <button type="button" onClick={() => setFeedbackOpen(value => !value)}>查看反馈 {history.length}</button>}
            </div>
            {feedbackOpen && <div className="source-feedback-dock-editor">
              {!host.readOnly && (!sharedFeedback || hasLegacyDraft) && <>
              {sharedFeedback && <p className="muted">已有条款草稿保留在这里；新的修改意见可从 Agent 协作统一处理。</p>}
              <label className="source-requirement-feedback"><span>对 {selected.source_display_id} 提出修改 <small>绑定当前版本；切换条目不会丢失草稿</small></span>
                <textarea ref={feedbackInputRef} rows={3} maxLength={4000} value={feedback}
                  onChange={event => setFeedback(event.target.value)} placeholder="说明哪里需要修改、为什么；也可以指出原图或派生项的问题。" />
              </label>
              <div className="source-requirement-feedback-actions">
                <span>{feedbackState}</span>
                <button type="button" className="button secondary compact"
                  disabled={Boolean(sourceReader) || applyingRevision || submitting || (Boolean(selected.logical_table_scope_id) && !capabilities?.table_classification_review.available)}
                  title={selected.logical_table_scope_id ? capabilities?.table_classification_review.unavailable_reason || "仅复核分类，不修改表格行组或拆合边界" : undefined}
                  onClick={() => void retryPendingRevision()}>
                  {applyingRevision ? "Agent 正在接收…" : selected.logical_table_scope_id ? "交给 Agent 复核分类" : history.some(item => item.status !== "CONSUMED") ? "处理已保存反馈" : "让 Agent 复核本条"}</button>
                {!sourceReader && outline.scope_revision?.can_undo && outline.scope_revision.affected_scope_ids.includes(selected.logical_table_scope_id || `text:${selected.node_id}`) &&
                  <button type="button" className="button secondary compact" disabled={applyingRevision || submitting} onClick={() => void undoLatestRevision()}>撤销上次局部修订</button>}
                <button type="button" className="button primary compact" disabled={!feedback.trim() || submitting || applyingRevision || Boolean(sourceReader?.feedbackCapability && !sourceReader.feedbackCapability.enabled)} onClick={() => void submitFeedback()}>{submitting || applyingRevision ? "正在提交…" : sourceReader || selected.logical_table_scope_id ? "保存反馈" : "保存并交给 Agent"}</button>
              </div>
              {sourceReader?.feedbackCapability && !sourceReader.feedbackCapability.enabled && <p className="muted">{sourceReader.feedbackCapability.reason || "当前版本暂不可提交意见。"}</p>}
              {!sourceReader && selected.logical_table_scope_id && <p className="muted">表格反馈先保存，再交给Agent复核分类。行组、拆合或来源边界问题保留待处理，当前不自动改写边界。</p>}
              </>}
              {history.length > 0 && <details className="source-feedback-history"><summary>反馈处理记录（{history.length}）</summary>
                {[...history].reverse().map(item => <article key={item.feedback_id}>
                  <strong>{item.resolution ? "插件已提交修订 · 待核对" : ({ PENDING_AGENT: "待 Agent 处理", ACKNOWLEDGED: "Agent 已读取", CONSUMED: "已用于处理" })[item.status]}</strong>
                  <time>{new Date(item.created_at_utc).toLocaleString()}</time><p>{item.message}</p>
                  {Boolean(asRecord(item.resolution).reason) && <p>{String(asRecord(item.resolution).reason)}</p>}
                </article>)}
              </details>}
              {draft.otherDrafts.length > 0 && <details><summary>其他版本的未提交草稿（{draft.otherDrafts.length}）</summary>
                {draft.otherDrafts.map(other => <article key={other.key}><strong>工作版 v{other.revision}</strong><p>{other.text}</p>
                  <button type="button" onClick={() => setFeedback([feedback, `[来自v${other.revision}的草稿]\n${other.text}`].filter(Boolean).join("\n\n"))}>追加到当前草稿</button>
                </article>)}
              </details>}
            </div>}
          </section>}
        </article> : null}
    </section>
  );
}
