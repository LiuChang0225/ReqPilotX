import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Icon } from "../../shared/ui/Icon";
import { overlayStyleFor } from "../pdf/overlayGeometry";
import type { PageSnapshot } from "../restoration/restorationModel";
import {
  BOUNDARY_CALIBRATION_PAGE_SCHEMA_VERSION,
  boundaryAnchorAtOffset,
  boundarySelection,
  buildCanonicalBoundaryOperation,
  initialBoundaryOffset,
  nearestBoundaryAnchor,
  normalizeBoundaryCalibrationPlan,
  stepBoundaryAnchor,
  type BoundaryCalibrationBoundary,
  type BoundaryCalibrationOperation,
  type BoundaryCalibrationPlan,
  type BoundaryCalibrationSegment,
  type BoundaryCalibrationSelection,
} from "./boundaryCalibrationModel";

type BoundaryAction = (
  operation: BoundaryCalibrationOperation,
  selection: BoundaryCalibrationSelection,
) => unknown | Promise<unknown>;

export interface BoundaryCalibrationDraftVisual {
  boundaryId: string;
  offset: number;
  bbox: number[];
  label: string;
}

export interface BoundaryCalibrationWorkbenchProps {
  plan: unknown;
  pageSnapshot: PageSnapshot | null;
  busy: boolean;
  onPreview: BoundaryAction;
  onApply: BoundaryAction;
  onCancel: () => void;
  onDraftChange?: (draft: BoundaryCalibrationDraftVisual | null) => void;
  onSegmentSelect?: (nodeId: string) => void;
}

function isNormalizedPlan(value: unknown): value is BoundaryCalibrationPlan {
  return Boolean(
    value &&
      typeof value === "object" &&
      (value as Partial<BoundaryCalibrationPlan>).schemaVersion ===
        BOUNDARY_CALIBRATION_PAGE_SCHEMA_VERSION,
  );
}

function kindLabel(kind: string): string {
  const labels: Record<string, string> = {
    DOCUMENT: "文档",
    SECTION: "Section",
    CLAUSE: "Clause",
    CONTENT: "Item",
    CONTINUATION: "续接",
  };
  return labels[kind] || kind || "Item";
}

function lockedReasonLabel(reason: string): string {
  const labels: Record<string, string> = {
    UNSUPPORTED_STRUCTURE_KIND: "该结构类型不支持拖动边界。",
    MULTI_SOURCE_SPAN: "该条目跨越多个来源区间，首版保持只读。",
    NON_NATIVE_TEXT_SOURCE: "该条目不是可验证的原生 PDF 文字来源。",
    CROSS_PAGE_OR_NON_PDF_SOURCE: "跨页或非 PDF 来源不能在当前页拖动。",
    NON_PARENT_RELATION_BOUND: "该节点还绑定其他结构关系，不能静默重分段。",
    SOURCE_SIDECAR_BOUND: "该节点绑定表格、双语或异常旁路证据。",
    SOURCE_CAPTURE_NOT_COMPLETE:
      "原文捕获不是 COMPLETE，当前页面只允许核对。",
    NO_EXACT_LINE_GEOMETRY: "缺少精确原文行几何，系统不会猜测位置。",
    NO_ALTERNATE_LINE_ANCHOR: "当前边界没有其他可验证行锚点。",
    CROSS_PARENT_BOUNDARY:
      "两侧条目不属于同一父级，请在树形整理中调整层级。",
    CROSS_SOURCE_BOUNDARY:
      "两侧属于不同原文块；首版只显示该边界，不跨来源移动文字。",
    NON_CONTIGUOUS_SOURCE_BOUNDARY:
      "两侧来源区间不连续，不能用单条横线安全重分段。",
  };
  return labels[reason] || reason;
}

function segmentTrail(segment: BoundaryCalibrationSegment | null): string {
  if (!segment) return "未绑定条目";
  const trail = segment.path.length ? segment.path : [segment.label];
  return trail.join(" › ");
}

function boundarySegment(
  plan: BoundaryCalibrationPlan,
  boundary: BoundaryCalibrationBoundary,
): BoundaryCalibrationSegment | null {
  const preferredId =
    boundary.mode === "SPLIT"
      ? boundary.targetNodeId
      : boundary.afterNodeId || boundary.beforeNodeId;
  return plan.segments.find((segment) => segment.nodeId === preferredId) || null;
}

function boundaryLabel(
  plan: BoundaryCalibrationPlan,
  boundary: BoundaryCalibrationBoundary,
): string {
  const segment = boundarySegment(plan, boundary);
  return `${boundary.mode === "SPLIT" ? "拆分" : "调整"} · ${segmentTrail(segment)}`;
}

function initialOffsets(
  plan: BoundaryCalibrationPlan | null,
): Record<string, number> {
  return Object.fromEntries(
    (plan?.boundaries || []).map((boundary) => [
      boundary.boundaryId,
      initialBoundaryOffset(boundary),
    ]),
  );
}

function initialBoundaryId(plan: BoundaryCalibrationPlan | null): string {
  return (
    plan?.boundaries.find((boundary) => boundary.editable)?.boundaryId ||
    plan?.boundaries[0]?.boundaryId ||
    ""
  );
}

function operationSignature(
  operation: BoundaryCalibrationOperation | null,
): string {
  return operation ? JSON.stringify(operation) : "";
}

function previewImpactSummary(value: unknown): string {
  const preview =
    value && typeof value === "object"
      ? (value as Record<string, unknown>)
      : {};
  const impact =
    preview.impact && typeof preview.impact === "object"
      ? (preview.impact as Record<string, unknown>)
      : {};
  const affected = Array.isArray(impact.affected_structure_node_ids)
    ? impact.affected_structure_node_ids.length
    : 0;
  const sections = Array.isArray(impact.affected_section_node_ids)
    ? impact.affected_section_node_ids.length
    : 0;
  return affected
    ? `将使 ${affected} 个结构节点失效并重建${sections ? `，涉及 ${sections} 个 Section` : ""}。`
    : "服务端已验证完整覆盖；提交后会重建受影响的语义与需求投影。";
}

export function BoundaryCalibrationWorkbench({
  plan,
  pageSnapshot,
  busy,
  onPreview,
  onApply,
  onCancel,
  onDraftChange,
  onSegmentSelect,
}: BoundaryCalibrationWorkbenchProps) {
  const normalizedPlan = useMemo(
    () =>
      isNormalizedPlan(plan) ? plan : normalizeBoundaryCalibrationPlan(plan),
    [plan],
  );
  const [selectedBoundaryId, setSelectedBoundaryId] = useState("");
  const [draftOffsets, setDraftOffsets] = useState<Record<string, number>>({});
  const [previewedSignature, setPreviewedSignature] = useState("");
  const [previewResult, setPreviewResult] = useState<unknown>(null);
  const [localAction, setLocalAction] = useState<"preview" | "apply" | "">("");
  const [actionError, setActionError] = useState("");
  const pageRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setSelectedBoundaryId(initialBoundaryId(normalizedPlan));
    setDraftOffsets(initialOffsets(normalizedPlan));
    setPreviewedSignature("");
    setPreviewResult(null);
    setActionError("");
  }, [normalizedPlan]);

  const selectedBoundary =
    normalizedPlan?.boundaries.find(
      (boundary) => boundary.boundaryId === selectedBoundaryId,
    ) || null;
  const selectedOffset = selectedBoundary
    ? (draftOffsets[selectedBoundary.boundaryId] ??
      initialBoundaryOffset(selectedBoundary))
    : 0;
  const selectedOperation =
    normalizedPlan && selectedBoundary
      ? buildCanonicalBoundaryOperation(
          normalizedPlan,
          selectedBoundary.boundaryId,
          selectedOffset,
        )
      : null;
  const selectedSignature = operationSignature(selectedOperation);
  const previewIsCurrent =
    Boolean(selectedSignature) && previewedSignature === selectedSignature;
  const locallyBusy = Boolean(localAction);
  const controlsBusy = busy || locallyBusy;
  const pageBounds = normalizedPlan
    ? [0, 0, normalizedPlan.page.width, normalizedPlan.page.height]
    : [0, 0, 1, 1];

  useEffect(() => {
    if (
      !normalizedPlan ||
      !selectedBoundary ||
      !selectedBoundary.editable ||
      (selectedBoundary.mode === "RESEGMENT" &&
        selectedOffset === selectedBoundary.currentOffset)
    ) {
      onDraftChange?.(null);
      return;
    }
    const selectedAnchor = boundaryAnchorAtOffset(
      selectedBoundary,
      selectedOffset,
    );
    if (!selectedAnchor) {
      onDraftChange?.(null);
      return;
    }
    onDraftChange?.({
      boundaryId: selectedBoundary.boundaryId,
      offset: selectedOffset,
      bbox: selectedAnchor.bbox,
      label: boundaryLabel(normalizedPlan, selectedBoundary),
    });
  }, [
    normalizedPlan,
    onDraftChange,
    selectedBoundary,
    selectedOffset,
  ]);

  const setBoundaryOffset = (
    boundary: BoundaryCalibrationBoundary,
    offset: number,
  ) => {
    if (!boundary.editable || !boundaryAnchorAtOffset(boundary, offset)) return;
    setSelectedBoundaryId(boundary.boundaryId);
    setDraftOffsets((current) => ({
      ...current,
      [boundary.boundaryId]: offset,
    }));
    setPreviewedSignature("");
    setPreviewResult(null);
    setActionError("");
  };

  const resetDraft = () => {
    setDraftOffsets(initialOffsets(normalizedPlan));
    setPreviewedSignature("");
    setPreviewResult(null);
    setActionError("");
  };

  const cancel = () => {
    resetDraft();
    onCancel();
  };

  const runPreview = async (
    boundaryOverride: BoundaryCalibrationBoundary | null = selectedBoundary,
    offsetOverride: number = selectedOffset,
  ) => {
    const operation =
      normalizedPlan && boundaryOverride
        ? buildCanonicalBoundaryOperation(
            normalizedPlan,
            boundaryOverride.boundaryId,
            offsetOverride,
          )
        : null;
    if (!normalizedPlan || !boundaryOverride || !operation || controlsBusy) {
      setActionError("请把边界吸附到可验证的原文行后再预览。");
      return;
    }
    setLocalAction("preview");
    setPreviewResult(null);
    setActionError("");
    try {
      const result = await onPreview(
        operation,
        boundarySelection(
          normalizedPlan,
          boundaryOverride.boundaryId,
          offsetOverride,
        ),
      );
      setPreviewedSignature(operationSignature(operation));
      setPreviewResult(result);
    } catch (error) {
      setActionError(
        error instanceof Error ? error.message : "边界预览失败，请重试。",
      );
    } finally {
      setLocalAction("");
    }
  };

  const runApply = async () => {
    if (
      !normalizedPlan ||
      !selectedBoundary ||
      !selectedOperation ||
      !previewIsCurrent
    ) {
      setActionError("边界已变化，请先重新预览。");
      return;
    }
    setLocalAction("apply");
    setActionError("");
    try {
      await onApply(
        selectedOperation,
        boundarySelection(
          normalizedPlan,
          selectedBoundary.boundaryId,
          selectedOffset,
        ),
      );
    } catch (error) {
      setActionError(
        error instanceof Error ? error.message : "应用边界修订失败，请重试。",
      );
    } finally {
      setLocalAction("");
    }
  };

  const moveFromPointer = (
    event: ReactPointerEvent<HTMLButtonElement>,
    boundary: BoundaryCalibrationBoundary,
  ) => {
    if (!boundary.editable || controlsBusy || !normalizedPlan) return;
    const rect = pageRef.current?.getBoundingClientRect();
    if (!rect?.height) return;
    const pageY =
      ((event.clientY - rect.top) / rect.height) * normalizedPlan.page.height;
    const nextAnchor = nearestBoundaryAnchor(boundary, pageY);
    if (nextAnchor) setBoundaryOffset(boundary, nextAnchor.offset);
  };

  const handleBoundaryKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    boundary: BoundaryCalibrationBoundary,
  ) => {
    if (event.key === "Enter") {
      event.preventDefault();
      setSelectedBoundaryId(boundary.boundaryId);
      const offset =
        draftOffsets[boundary.boundaryId] ?? initialBoundaryOffset(boundary);
      void runPreview(boundary, offset);
      return;
    }
    if (
      !boundary.editable ||
      (event.key !== "ArrowUp" && event.key !== "ArrowDown")
    ) {
      return;
    }
    event.preventDefault();
    const current =
      draftOffsets[boundary.boundaryId] ?? initialBoundaryOffset(boundary);
    const next = stepBoundaryAnchor(
      boundary,
      current,
      event.key === "ArrowUp" ? -1 : 1,
    );
    if (next) setBoundaryOffset(boundary, next.offset);
  };

  if (!normalizedPlan) {
    return (
      <section
        className="structure-workbench boundary-calibration-workbench"
        aria-label="版面边界校准"
      >
        <div className="inline-notice is-warning">
          <span className="notice-pip" />
          <div>
            <strong>当前页面还不能校准</strong>
            <p>缺少与当前结构版本绑定的页面几何计划。</p>
          </div>
        </div>
      </section>
    );
  }

  const editableCount = normalizedPlan.boundaries.filter(
    (boundary) => boundary.editable,
  ).length;
  const selectedSegment = selectedBoundary
    ? boundarySegment(normalizedPlan, selectedBoundary)
    : null;

  return (
    <section
      className="structure-workbench boundary-calibration-workbench"
      aria-label="版面边界校准"
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        event.preventDefault();
        cancel();
      }}
    >
      <header className="structure-command-bar boundary-calibration-command-bar">
        <div>
          <p className="section-label">SOURCE-BOUND CALIBRATION</p>
          <h2>版面边界校准</h2>
          <span>拖动只会吸附到已验证的原文行，不会改写 PDF。</span>
        </div>
        <div className="boundary-calibration-status">
          <span className="status-badge is-info">
            第 {normalizedPlan.page.number} 页
          </span>
          <span className="status-badge is-success">
            {editableCount} 个可调边界
          </span>
          <span className="status-badge">
            结构版本 {normalizedPlan.binding.revisionNumber}
          </span>
        </div>
      </header>

      <div className="boundary-calibration-layout">
        <nav
          className="boundary-calibration-index"
          aria-label="当前页结构边界"
        >
          <header>
            <strong>边界轨道</strong>
            <small>{normalizedPlan.boundaries.length} 条</small>
          </header>
          <div className="boundary-calibration-index-list">
            {normalizedPlan.boundaries.map((boundary, index) => {
              const segment = boundarySegment(normalizedPlan, boundary);
              const active = boundary.boundaryId === selectedBoundaryId;
              return (
                <button
                  key={boundary.boundaryId}
                  className={`clause-row boundary-calibration-index-row${active ? " is-active" : ""}${boundary.editable ? "" : " is-excluded"}`}
                  type="button"
                  aria-pressed={active}
                  onClick={() => setSelectedBoundaryId(boundary.boundaryId)}
                >
                  <span className="clause-index">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  <span className="clause-row-copy">
                    <strong>{boundaryLabel(normalizedPlan, boundary)}</strong>
                    <small>{segment?.label || boundary.sourceId}</small>
                  </span>
                  <span className="clause-state-column">
                    <span className="clause-state-badge">
                      {boundary.editable ? "可校准" : "已锁定"}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </nav>

        <main className="boundary-calibration-canvas-pane">
          <div className="boundary-calibration-canvas-toolbar">
            <div>
              <strong>提取版面画布</strong>
              <small>
                {pageSnapshot?.blocks.filter((block) => block.locatable).length ||
                  0}{" "}
                个已定位内容块
              </small>
            </div>
            <span className="status-badge is-info">原页坐标</span>
          </div>

          <div className="boundary-calibration-page-scroll">
            <div
              ref={pageRef}
              className="boundary-calibration-page"
              aria-label={`第 ${normalizedPlan.page.number} 页提取版面`}
              style={{
                aspectRatio: `${normalizedPlan.page.width} / ${normalizedPlan.page.height}`,
                position: "relative",
                width: "100%",
              }}
            >
              {(pageSnapshot?.blocks || [])
                .filter(
                  (block) =>
                    block.locatable &&
                    block.bbox.length === 4 &&
                    block.pageNumber === normalizedPlan.page.number,
                )
                .map((block) => (
                  <article
                    key={block.id}
                    className="boundary-calibration-source-block"
                    data-content-type={block.type}
                    title={block.text || block.label}
                    style={{
                      ...overlayStyleFor({
                        bbox: block.bbox,
                        sourceBounds: pageBounds,
                      }),
                      overflow: "hidden",
                      position: "absolute",
                    }}
                  >
                    <span>{block.text || block.label}</span>
                  </article>
                ))}

              {normalizedPlan.segments.map((segment) => (
                <button
                  key={segment.nodeId}
                  className="boundary-calibration-segment"
                  type="button"
                  data-kind={segment.kind}
                  data-editable={segment.editable}
                  data-node-id={segment.nodeId}
                  aria-label={`${kindLabel(segment.kind)}：${segmentTrail(segment)}`}
                  title="选择该条目并建立可验证的内部拆分边界"
                  style={{
                    ...overlayStyleFor({
                      bbox: segment.bbox,
                      sourceBounds: pageBounds,
                    }),
                    position: "absolute",
                  }}
                  onClick={() => onSegmentSelect?.(segment.nodeId)}
                >
                  <span className="status-badge">
                    {kindLabel(segment.kind)}
                  </span>
                  <strong>{segmentTrail(segment)}</strong>
                </button>
              ))}

              {normalizedPlan.boundaries.map((boundary) => {
                const offset =
                  draftOffsets[boundary.boundaryId] ??
                  initialBoundaryOffset(boundary);
                const selectedAnchor = boundaryAnchorAtOffset(boundary, offset);
                if (!selectedAnchor) return null;
                const pageTop = Math.max(
                  0,
                  Math.min(
                    100,
                    (selectedAnchor.bbox[1] / normalizedPlan.page.height) * 100,
                  ),
                );
                const active = boundary.boundaryId === selectedBoundaryId;
                return (
                  <button
                    key={boundary.boundaryId}
                    className={`boundary-calibration-rail${active ? " is-active" : ""}${boundary.editable ? "" : " is-locked"}`}
                    type="button"
                    aria-label={`${boundaryLabel(normalizedPlan, boundary)}，字符偏移 ${offset}${boundary.editable ? "，可用上下方向键逐行调整" : "，已锁定"}`}
                    aria-pressed={active}
                    aria-disabled={!boundary.editable || controlsBusy}
                    style={{
                      left: 0,
                      position: "absolute",
                      right: 0,
                      top: `${Number(pageTop.toFixed(6))}%`,
                    }}
                    onClick={() => setSelectedBoundaryId(boundary.boundaryId)}
                    onKeyDown={(event) =>
                      handleBoundaryKeyDown(event, boundary)
                    }
                    onPointerDown={(event) => {
                      setSelectedBoundaryId(boundary.boundaryId);
                      if (!boundary.editable || controlsBusy) return;
                      event.currentTarget.setPointerCapture(event.pointerId);
                      moveFromPointer(event, boundary);
                    }}
                    onPointerMove={(event) => {
                      if (
                        event.currentTarget.hasPointerCapture(event.pointerId)
                      ) {
                        moveFromPointer(event, boundary);
                      }
                    }}
                    onPointerUp={(event) => {
                      if (
                        event.currentTarget.hasPointerCapture(event.pointerId)
                      ) {
                        event.currentTarget.releasePointerCapture(event.pointerId);
                      }
                    }}
                  >
                    <span
                      className="boundary-calibration-rule"
                      aria-hidden="true"
                    />
                    <span className="boundary-calibration-handle">
                      <Icon name={boundary.editable ? "split" : "stop"} size={14} />
                    </span>
                    <span className="boundary-calibration-rail-label">
                      {boundaryLabel(normalizedPlan, boundary)}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        </main>

        <aside className="boundary-calibration-inspector">
          <header>
            <p className="section-label">BOUNDARY INSPECTOR</p>
            <h3>{selectedBoundary ? "校准选中边界" : "选择一个边界"}</h3>
          </header>

          {selectedBoundary ? (
            <>
              <dl>
                <div>
                  <dt>归属</dt>
                  <dd>{segmentTrail(selectedSegment)}</dd>
                </div>
                <div>
                  <dt>类型</dt>
                  <dd>
                    {selectedSegment
                      ? kindLabel(selectedSegment.kind)
                      : selectedBoundary.mode}
                  </dd>
                </div>
                <div>
                  <dt>字符偏移</dt>
                  <dd>
                    {selectedBoundary.currentOffset} → {selectedOffset}
                  </dd>
                </div>
                <div>
                  <dt>安全锚点</dt>
                  <dd>{selectedBoundary.anchors.length} 个原文行位置</dd>
                </div>
              </dl>

              {selectedBoundary.editable ? (
                <div className="inline-notice is-info">
                  <span className="notice-pip" />
                  <div>
                    <strong>按原文行吸附</strong>
                    <p>
                      拖动横线，或用 ↑ / ↓ 逐行移动。Enter
                      预览，Escape 取消。
                    </p>
                  </div>
                </div>
              ) : (
                <div className="inline-notice is-warning">
                  <span className="notice-pip" />
                  <div>
                    <strong>当前边界已锁定</strong>
                    <p>
                      {lockedReasonLabel(selectedBoundary.lockedReason) ||
                        "该内容不能安全映射到唯一原文边界。"}
                    </p>
                  </div>
                </div>
              )}

              {actionError && (
                <div className="error-block" role="alert">
                  <span className="notice-pip" />
                  <p>{actionError}</p>
                </div>
              )}

              {previewIsCurrent && (
                <div className="inline-notice is-success" role="status">
                  <span className="notice-pip" />
                  <div>
                    <strong>预览已通过</strong>
                    <p>{previewImpactSummary(previewResult)}</p>
                  </div>
                </div>
              )}

              <div className="boundary-calibration-actions">
                <button
                  className="button secondary"
                  type="button"
                  disabled={controlsBusy || !selectedOperation}
                  onClick={() => void runPreview()}
                >
                  <Icon name="eye" />
                  {localAction === "preview" ? "正在预览" : "预览修订"}
                </button>
                <button
                  className="button primary"
                  type="button"
                  disabled={controlsBusy || !previewIsCurrent}
                  onClick={() => void runApply()}
                >
                  <Icon name="check" />
                  {localAction === "apply" ? "正在应用" : "应用修订"}
                </button>
                <button
                  className="button ghost"
                  type="button"
                  disabled={controlsBusy}
                  onClick={cancel}
                >
                  取消
                </button>
              </div>
            </>
          ) : (
            <div className="inline-notice is-info">
              <span className="notice-pip" />
              <div>
                <strong>选择边界，或点击画布中的条目</strong>
                <p>
                  点击条目可建立内部拆分候选；边界会显示它所属的
                  Section、Clause 和 Item。
                </p>
              </div>
            </div>
          )}
        </aside>
      </div>
    </section>
  );
}
