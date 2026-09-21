import { useMemo, type RefObject } from "react";
import { LoadingBlock, StatusBadge } from "../../shared/ui/Feedback";
import { Icon } from "../../shared/ui/Icon";
import { StructuredTable } from "../restoration/StructuredTable";
import {
  contentTypeLabel,
  pageBlockEvidenceChannel,
  pageBlocksForEvidenceView,
  pageEvidenceDisposition,
  provenanceLabel,
  sourceCropUrl,
  type PageEvidenceView,
  type PageBlock,
  type PageIssue,
  type PageSnapshot,
} from "../restoration/restorationModel";
import type { ParsingDiagnosticsSnapshot } from "./parsingDiagnosticsModel";

type PageEvidenceVersionView = "STAGED_CURRENT" | "FORMAL_PREVIOUS";

export type ExtractionVisualRouteIdentity =
  | "QWEN_OCR"
  | "BAILIAN_LAYOUT"
  | "AZURE_DOCUMENT_INTELLIGENCE"
  | "GENERIC_VISUAL";

export interface ExtractionVisualReceiptSummary {
  attemptedPageCount: number;
  readyPageCount: number;
  degradedPageNumbers: number[];
  routeIdentity: ExtractionVisualRouteIdentity;
  adoptedProviderIds: string[];
  adoptedProviderIdByPage: Record<number, string>;
  tableParsing?: {
    candidateCount: number;
    attemptCount: number;
    readyCount: number;
    failedCount: number;
    budgetSkippedCount: number;
    requestCeilingPerPage: number;
    requestCeilingPerDocument: number;
  };
}

interface ParseDiagnosisLensProps {
  projectId: string;
  documentId: string;
  sourceSha256: string;
  sourceFileUrl: string;
  pdfUrl: string;
  pdfFrameRef: RefObject<HTMLIFrameElement | null>;
  page: number;
  pageCount: number;
  pageSnapshot: PageSnapshot;
  pageLoading: boolean;
  pageAvailable: boolean;
  pageError: string;
  pageEvidenceView?: PageEvidenceVersionView;
  hasPreviousFormalPageFacts?: boolean;
  stagedPagePending?: boolean;
  stagedReadyPageCount?: number;
  stagedTotalPages?: number;
  stagedReadyPageRanges?: string;
  diagnostics: ParsingDiagnosticsSnapshot;
  diagnosticsLoading: boolean;
  diagnosticsError: string;
  workflowBlockerMessage: string;
  visualReceiptSummary?: ExtractionVisualReceiptSummary | null;
  onRetryFailedVisualPages?: (() => void) | null;
  retryFailedVisualPagesDisabled?: boolean;
  focusedFactId: string;
  riskOnly: boolean;
  showSupportLayer: boolean;
  visualRecoveryFailed?: boolean;
  contentView: PageEvidenceView;
  onRiskOnlyChange: (value: boolean) => void;
  onShowSupportLayerChange: (value: boolean) => void;
  onContentViewChange: (value: PageEvidenceView) => void;
  onPageEvidenceViewChange?: (value: PageEvidenceVersionView) => void;
  onPageChange: (page: number) => void;
  onFactFocus: (factId: string) => void;
}


export function ParseDiagnosisLens({
  projectId,
  documentId,
  sourceSha256,
  sourceFileUrl,
  pdfUrl,
  pdfFrameRef,
  page,
  pageCount,
  pageSnapshot,
  pageLoading,
  pageAvailable,
  pageError,
  pageEvidenceView = "FORMAL_PREVIOUS",
  hasPreviousFormalPageFacts = false,
  stagedPagePending = false,
  stagedReadyPageCount = 0,
  stagedTotalPages = 0,
  stagedReadyPageRanges = "",
  diagnostics,
  diagnosticsLoading,
  diagnosticsError,
  workflowBlockerMessage,
  visualReceiptSummary = null,
  onRetryFailedVisualPages = null,
  retryFailedVisualPagesDisabled = false,
  focusedFactId,
  riskOnly,
  showSupportLayer,
  visualRecoveryFailed = false,
  contentView,
  onRiskOnlyChange,
  onShowSupportLayerChange,
  onContentViewChange,
  onPageEvidenceViewChange = () => undefined,
  onPageChange,
  onFactFocus,
}: ParseDiagnosisLensProps) {
  const disposition = useMemo(
    () => pageEvidenceDisposition(pageSnapshot, { visualRecoveryFailed }),
    [pageSnapshot, visualRecoveryFailed],
  );
  const blocks = useMemo(
    () =>
      pageBlocksForEvidenceView(pageSnapshot, contentView, showSupportLayer, {
        visualRecoveryFailed,
      }),
    [contentView, pageSnapshot, showSupportLayer, visualRecoveryFailed],
  );
  const contentViewCounts = useMemo(
    () => ({
      EFFECTIVE: pageBlocksForEvidenceView(
        pageSnapshot,
        "EFFECTIVE",
        false,
        { visualRecoveryFailed },
      ).length,
      REMOTE_VISUAL: pageBlocksForEvidenceView(pageSnapshot, "REMOTE_VISUAL")
        .length,
      NATIVE: pageBlocksForEvidenceView(pageSnapshot, "NATIVE").length,
      COMPARE: pageBlocksForEvidenceView(pageSnapshot, "COMPARE").length,
    }),
    [pageSnapshot, visualRecoveryFailed],
  );
  const stagedView = pageEvidenceView === "STAGED_CURRENT";
  const actionableIssues = stagedView
    ? []
    : pageSnapshot.issues.filter(
        (issue) =>
          issue.blocking || ["warning", "error"].includes(issue.severity),
      );
  const technicalIssues = pageSnapshot.issues.filter(
    (issue) => !stagedView && !actionableIssues.includes(issue),
  );
  const boundedPageCount = Math.max(pageCount, 1);
  const visibleBlocks = stagedView || !riskOnly ? blocks : [];
  const unassignedRisks = stagedView
    ? []
    : diagnostics.risks.filter((risk) => risk.pageNumber < 1);
  const qwenOcrReceipt = visualReceiptSummary?.routeIdentity === "QWEN_OCR";
  const adoptedProviderId =
    visualReceiptSummary?.adoptedProviderIdByPage[page]?.toLowerCase() || "";
  const qwenOcrPageReceipt = Boolean(
    qwenOcrReceipt &&
      (!adoptedProviderId ||
        adoptedProviderId.includes("qwen-ocr") ||
        adoptedProviderId.includes("bailian-ocr")),
  );
  const visualReceiptLabel = qwenOcrReceipt
    ? "Qwen OCR 结构恢复"
    : "页面视觉处理";

  return (
    <section className="parse-diagnosis-lens" aria-label="解析核对">
      <header className="parse-risk-summary">
        <div className="parse-risk-summary-copy">
          <span>{stagedView ? "本轮逐页结果" : "解析事实诊断"}</span>
          {stagedView ? (
            <strong>
              本轮暂存结果 · {stagedReadyPageCount}/{Math.max(stagedTotalPages, 1)} 页可浏览
            </strong>
          ) : workflowBlockerMessage ? (
            <strong>当前事实只读，下一步等待可用结果</strong>
          ) : diagnosticsLoading ? (
            <strong>正在读取严重风险摘要</strong>
          ) : diagnostics.hardBlockerCount > 0 ? (
            <strong>{diagnostics.hardBlockerCount} 项硬阻断需要处理</strong>
          ) : diagnostics.severeWarningCount > 0 ? (
            <strong>
              {diagnostics.severeWarningCount} 项严重风险，涉及 {diagnostics.riskPages.length} 页
            </strong>
          ) : diagnostics.status === "READY" ? (
            <strong>未标记严重解析异常</strong>
          ) : (
            <strong>文档级风险摘要暂不可用</strong>
          )}
          <small>
            {stagedView
              ? `这些是只读临时事实；结构与风险摘要尚未生成，任务成功后才会整体发布。${
                  stagedReadyPageRanges
                    ? ` 当前可浏览 ${stagedReadyPageRanges}。`
                    : ""
                }`
              : workflowBlockerMessage
              ? workflowBlockerMessage
              : diagnosticsError
              ? `${diagnosticsError}；仍可按页浏览现有事实。`
              : diagnostics.status === "READY" && diagnostics.riskCount === 0
                ? "这表示现有诊断未报告严重问题，不等于要求人工逐页确认正确性。"
                : "摘要只采用服务端已有 warning、error 与 blocking 事实，不在浏览器重新判断阈值。"}
          </small>
        </div>
        <div className="parse-risk-summary-actions">
          {hasPreviousFormalPageFacts && (
            <div
              className="parse-evidence-version-switch"
              role="group"
              aria-label="解析结果版本"
            >
              <button
                type="button"
                className={stagedView ? "is-active" : ""}
                aria-pressed={stagedView}
                aria-label="查看本轮结果"
                onClick={() => onPageEvidenceViewChange("STAGED_CURRENT")}
              >
                本轮结果
              </button>
              <button
                type="button"
                className={!stagedView ? "is-active" : ""}
                aria-pressed={!stagedView}
                aria-label="查看上一正式结果"
                onClick={() => onPageEvidenceViewChange("FORMAL_PREVIOUS")}
              >
                上一正式结果
              </button>
            </div>
          )}
          {stagedView ? (
            <StatusBadge tone="neutral">本轮临时 · 只读</StatusBadge>
          ) : workflowBlockerMessage ? (
            <StatusBadge tone="warning">当前结果不可继续</StatusBadge>
          ) : diagnostics.acknowledged && diagnostics.severeWarningCount > 0 ? (
            <StatusBadge tone="success">风险已知晓</StatusBadge>
          ) : diagnostics.hardBlockerCount > 0 ? (
            <StatusBadge tone="danger">不可带风险跳过</StatusBadge>
          ) : diagnostics.severeWarningCount > 0 ? (
            <StatusBadge tone="warning">可知晓后继续</StatusBadge>
          ) : (
            <StatusBadge tone="neutral">只读核对</StatusBadge>
          )}
          {!stagedView && (
            <button
              className={riskOnly ? "is-active" : ""}
              type="button"
              aria-pressed={riskOnly}
              onClick={() => onRiskOnlyChange(!riskOnly)}
              disabled={diagnostics.riskCount < 1 && actionableIssues.length < 1}
            >
              {riskOnly ? "显示全部解析" : "只看严重风险"}
            </button>
          )}
        </div>
        {unassignedRisks.length > 0 && (
          <section
            className="parse-unassigned-risks"
            aria-label="无法定位到具体页的严重风险"
          >
            <header>
              <strong>{unassignedRisks.length} 项严重风险没有可靠页码</strong>
              <small>这些事实仍参与阻断与知晓判断，不能因无法定位而隐藏。</small>
            </header>
            <div>
              {unassignedRisks.map((risk) => (
                <article key={risk.id}>
                  <StatusBadge tone={risk.blocking ? "danger" : "warning"}>
                    {risk.blocking ? "硬阻断" : "严重风险"}
                  </StatusBadge>
                  <code>{risk.code}</code>
                  <span>{risk.message}</span>
                </article>
              ))}
            </div>
          </section>
        )}
        {!stagedView && visualReceiptSummary && (
          <section
            className="parse-unassigned-risks"
            role="status"
            aria-label={`${visualReceiptLabel}终态回执`}
          >
            <header>
              <strong>
                {visualReceiptLabel} {visualReceiptSummary.readyPageCount}/
                {visualReceiptSummary.attemptedPageCount} 页就绪，
                {visualReceiptSummary.degradedPageNumbers.length} 页降级
              </strong>
              <small>
                {qwenOcrReceipt
                  ? "这是 Qwen OCR 冻结路线的页面终态；文字定位、文档结构与表格结构必须分别核对回执，页就绪不代表图片内容理解完成。"
                  : "这是冻结运行路线的页面终态；页就绪只说明对应页面处理完成，不代表图片内容理解或每类结构均已验证。"}
              </small>
            </header>
            {visualReceiptSummary.tableParsing && (
              <small>
                表格处理 {visualReceiptSummary.tableParsing.attemptCount} 次尝试 · {visualReceiptSummary.tableParsing.readyCount} 就绪 · {visualReceiptSummary.tableParsing.failedCount} 失败 · {visualReceiptSummary.tableParsing.budgetSkippedCount} 项因预算跳过。处理上限为每页 {visualReceiptSummary.tableParsing.requestCeilingPerPage}、全文 {visualReceiptSummary.tableParsing.requestCeilingPerDocument}；尝试次数不等于实际远程请求数。
              </small>
            )}
            {visualReceiptSummary.degradedPageNumbers.length > 0 && (
              <nav
                className="parse-risk-page-nav"
                aria-label={`${visualReceiptLabel}降级页面`}
              >
                <span>降级页</span>
                {visualReceiptSummary.degradedPageNumbers.map((failedPage) => (
                  <button
                    key={failedPage}
                    type="button"
                    className={failedPage === page ? "is-active" : ""}
                    aria-label={`查看 ${visualReceiptLabel}降级的第 ${failedPage} 页`}
                    onClick={() => onPageChange(failedPage)}
                  >
                    P{failedPage}
                  </button>
                ))}
              </nav>
            )}
            {onRetryFailedVisualPages &&
              visualReceiptSummary.degradedPageNumbers.length > 0 && (
                <button
                  className="button primary"
                  type="button"
                  disabled={retryFailedVisualPagesDisabled}
                  onClick={onRetryFailedVisualPages}
                >
                  只重试降级的 {visualReceiptSummary.degradedPageNumbers.length} 页
                </button>
              )}
          </section>
        )}
        {!stagedView && diagnostics.riskPages.length > 0 && (
          <nav className="parse-risk-page-nav" aria-label="严重风险页面">
            <span>风险页</span>
            {diagnostics.riskPages.map((riskPage) => (
              <button
                key={riskPage.pageNumber}
                type="button"
                className={riskPage.pageNumber === page ? "is-active" : ""}
                aria-label={`查看第 ${riskPage.pageNumber} 页的 ${
                  riskPage.hardBlockerCount + riskPage.severeWarningCount
                } 项解析风险`}
                onClick={() => onPageChange(riskPage.pageNumber)}
              >
                P{riskPage.pageNumber}
                <strong>
                  {riskPage.hardBlockerCount + riskPage.severeWarningCount}
                </strong>
              </button>
            ))}
          </nav>
        )}
      </header>

      <div className="parse-diagnosis-workbench">
        <section className="parse-original-pane" aria-label={`原 PDF · 第 ${page} 页`}>
          <header>
            <div>
              <span>原 PDF</span>
              <strong>第 {page} 页</strong>
              <small>与右侧解析事实保持同页</small>
            </div>
            <PageNavigation
              page={page}
              pageCount={boundedPageCount}
              onPageChange={onPageChange}
            />
            <a
              className="button ghost"
              href={`/pdf-viewer?fit=page&pdf=${encodeURIComponent(sourceFileUrl)}&page=${page}`}
              target="_blank"
              rel="noreferrer"
            >
              新窗口
              <Icon name="external" />
            </a>
          </header>
          <iframe
            ref={pdfFrameRef}
            className="parse-original-frame"
            title={`原 PDF · 第 ${page} 页`}
            src={pdfUrl}
            loading="eager"
          />
        </section>

        <section className="parse-result-pane" aria-label={`第 ${page} 页解析结果`}>
          <header>
            <div>
              <span>{stagedView ? "本轮暂存内容" : pageEvidenceViewLabel(contentView)}</span>
              <strong>
                {stagedView ? "本轮结果" : hasPreviousFormalPageFacts ? "上一正式结果" : "解析结果"}
                · 第 {page} 页
              </strong>
              <small>
                当前显示 {contentViewCounts[contentView]} 块
                {stagedView
                  ? " · 临时只读"
                  : ` · OCR ${disposition.remoteVisualBlockCount} · 本地 ${disposition.nativeBlockCount} · ${actionableIssues.length} 项风险`}
              </small>
            </div>
            <label className="parse-support-toggle">
              <input
                type="checkbox"
                checked={showSupportLayer}
                onChange={(event) =>
                  onShowSupportLayerChange(event.target.checked)
                }
              />
              显示扫描支撑层
            </label>
          </header>

          <div className="parse-result-scroll">
            {!stagedPagePending && !pageLoading && !pageError && pageAvailable && (
              <>
                <div className="parse-source-view-toolbar">
                  <div
                    className="parse-source-view-switch"
                    role="group"
                    aria-label="解析来源视图"
                  >
                    {(
                      [
                        ["EFFECTIVE", "最终采用"],
                        ["REMOTE_VISUAL", "OCR 原始"],
                        ["NATIVE", "本地原始"],
                        ["COMPARE", "差异对照"],
                      ] as const
                    ).map(([view, label]) => (
                      <button
                        key={view}
                        type="button"
                        className={contentView === view ? "is-active" : ""}
                        aria-label={label}
                        aria-pressed={contentView === view}
                        onClick={() => onContentViewChange(view)}
                      >
                        <span>{label}</span>
                        <strong aria-hidden="true">{contentViewCounts[view]}</strong>
                      </button>
                    ))}
                  </div>
                  <small aria-live="polite">
                    当前显示 {contentViewCounts[contentView]} 块 · OCR {disposition.remoteVisualBlockCount} · 本地 {disposition.nativeBlockCount}
                    {disposition.unknownBlockCount
                      ? ` · 未标明 ${disposition.unknownBlockCount}`
                      : ""}
                  </small>
                </div>
                <PageAdoptionBanner
                  disposition={disposition}
                  contentView={contentView}
                />
                <QwenOcrStructureScopeBanner
                  snapshot={pageSnapshot}
                  remoteVisualBlockCount={disposition.remoteVisualBlockCount}
                  enabled={qwenOcrPageReceipt}
                />
              </>
            )}
            {stagedView && (
              <section className="parse-staged-page-banner" role="status">
                <Icon name="evidence" />
                <div>
                  <strong>本轮结果仅供即时浏览</strong>
                  <span>不会写入结构整理，也不会替换上一正式结果；全文成功后才整体发布。</span>
                </div>
              </section>
            )}
            {stagedPagePending ? (
              <div className="parse-page-unavailable" role="status">
                <Icon name="evidence" />
                <strong>本轮第 {page} 页尚未处理</strong>
                <p>当前视图不会用上一正式结果填充本页。可查看已完成页，或切换整套上一正式结果。</p>
              </div>
            ) : pageLoading ? (
              <LoadingBlock label={`正在读取第 ${page} 页解析事实`} />
            ) : pageError ? (
              <div className="parse-page-unavailable" role="status">
                <Icon name="evidence" />
                <strong>{stagedView ? "本轮暂存页读取失败" : "本页解析事实暂不可用"}</strong>
                <p>{pageError} 原 PDF 仍可浏览，系统不会因此自动重新解析。</p>
              </div>
            ) : !pageAvailable ? (
              <div className="parse-page-unavailable" role="status">
                <Icon name="evidence" />
                <strong>没有可读取的逐页解析工件</strong>
                <p>可继续查看原文或显式重新解析整个文档；进入本页不会创建后台任务。</p>
              </div>
            ) : (
              <>
                {actionableIssues.length > 0 && (
                  <section className="parse-page-risks" aria-label="当前页严重风险">
                    <header>
                      <span>当前页风险</span>
                      <strong>{actionableIssues.length} 项</strong>
                    </header>
                    {actionableIssues.map((issue) => (
                      <PageIssueCard
                        key={issue.id}
                        issue={issue}
                        focused={focusedFactId === issue.id}
                        onFocus={() => onFactFocus(issue.id)}
                      />
                    ))}
                  </section>
                )}

                {visibleBlocks.length > 0 ? (
                  <div className="parse-block-list" aria-label="当前页全部解析事实">
                    {visibleBlocks.map((block) => (
                      <PageBlockCard
                        key={block.id}
                        block={block}
                        snapshot={pageSnapshot}
                        projectId={projectId}
                        documentId={documentId}
                        sourceSha256={sourceSha256}
                        focused={focusedFactId === block.id}
                        onFocus={() => onFactFocus(block.id)}
                        onPageChange={onPageChange}
                        supportingGeometryOnly={
                          contentView === "EFFECTIVE" &&
                          ["TABLE", "FIGURE"].includes(block.type) &&
                          (disposition.effectiveTextPolicy === "HIDE" ||
                            (disposition.effectiveTextPolicy ===
                              "REMOTE_VISUAL" &&
                              pageBlockEvidenceChannel(block) === "NATIVE"))
                        }
                      />
                    ))}
                  </div>
                ) : !actionableIssues.length ? (
                  <div className="parse-page-unavailable is-empty" role="status">
                    <strong>本页没有可显示的解析内容</strong>
                    <p>这只描述当前工件，不推断原页一定为空。</p>
                  </div>
                ) : null}

                {technicalIssues.length > 0 && (
                  <details className="parse-technical-details">
                    <summary>技术详情 · {technicalIssues.length} 项低优先级记录</summary>
                    {technicalIssues.map((issue) => (
                      <PageIssueCard
                        key={issue.id}
                        issue={issue}
                        focused={focusedFactId === issue.id}
                        onFocus={() => onFactFocus(issue.id)}
                      />
                    ))}
                  </details>
                )}
              </>
            )}
          </div>
        </section>
      </div>
    </section>
  );
}

function QwenOcrStructureScopeBanner({
  snapshot,
  remoteVisualBlockCount,
  enabled,
}: {
  snapshot: PageSnapshot;
  remoteVisualBlockCount: number;
  enabled: boolean;
}) {
  const visualTableCellCount = snapshot.tables.reduce(
    (count, table) =>
      count +
      table.cells.filter((cell) => cell.textSource === "visual_table_cell_fact")
        .length,
    0,
  );
  const hasUngroundedImageMarkup = snapshot.issues.some(
    (issue) =>
      issue.code === "pdf_visual_figure_content_not_evaluated",
  );
  const hasQwenOcrFacts =
    remoteVisualBlockCount > 0 ||
    visualTableCellCount > 0 ||
    hasUngroundedImageMarkup;
  if (!enabled || !hasQwenOcrFacts) return null;

  const nativeImageAssetCount = snapshot.blocks.filter(
    (block) =>
      block.type === "FIGURE" && pageBlockEvidenceChannel(block) === "NATIVE",
  ).length;
  return (
    <section
      className={`parse-adoption-banner ${
        hasUngroundedImageMarkup ? "is-unresolved" : "is-native"
      }`}
      role="status"
      aria-label="Qwen OCR 结构恢复范围"
    >
      <Icon name="evidence" />
      <div>
        {visualTableCellCount > 0 ? (
          <>
            <strong>Qwen OCR 文字定位与表格回执可核对</strong>
            <span>
              当前有 {visualTableCellCount} 个单元格来自与本地表格裁剪精确绑定的 table_parsing 事实；它不会把普通图形当成表格。
            </span>
          </>
        ) : (
          <>
            <strong>当前仅有 Qwen OCR 文字定位回执</strong>
            <span>
              advanced_recognition 不能证明文档结构或表格结构已经恢复；当前正式逐页工件没有独立的 document_parsing 或 table_parsing 成功回执。
            </span>
          </>
        )}
        {nativeImageAssetCount > 0 && (
          <span>
            图片资产与矢量图由 PyMuPDF 精确保留（本页 {nativeImageAssetCount} 项）；Qwen OCR 不冒充图片内容理解。
          </span>
        )}
        {hasUngroundedImageMarkup && (
          <span>
            图片内容未评估，需页面布局/视觉理解升级；没有升级成功回执时阻断下一步。
          </span>
        )}
      </div>
    </section>
  );
}


function PageNavigation({
  page,
  pageCount,
  onPageChange,
}: {
  page: number;
  pageCount: number;
  onPageChange: (page: number) => void;
}) {
  return (
    <div className="parse-page-navigation" aria-label="解析页码">
      <button
        type="button"
        aria-label="上一页"
        disabled={page <= 1}
        onClick={() => onPageChange(Math.max(1, page - 1))}
      >
        ‹
      </button>
      <span>
        {page} / {pageCount}
      </span>
      <button
        type="button"
        aria-label="下一页"
        disabled={page >= pageCount}
        onClick={() => onPageChange(Math.min(pageCount, page + 1))}
      >
        ›
      </button>
    </div>
  );
}

function pageEvidenceViewLabel(view: PageEvidenceView): string {
  if (view === "REMOTE_VISUAL") return "OCR 原始事实";
  if (view === "NATIVE") return "本地原始事实";
  if (view === "COMPARE") return "OCR / 本地差异对照";
  return "最终采用结果";
}

function PageAdoptionBanner({
  disposition,
  contentView,
}: {
  disposition: ReturnType<typeof pageEvidenceDisposition>;
  contentView: PageEvidenceView;
}) {
  const rawViewSuffix =
    contentView === "EFFECTIVE"
      ? ""
      : ` 当前正在查看“${pageEvidenceViewLabel(contentView)}”，它不会改变最终采用通道。`;
  if (disposition.effectiveTextPolicy === "HIDE") {
    return (
      <section
        className="parse-adoption-banner is-unresolved"
        role="status"
        aria-label="最终采用通道"
        aria-live="polite"
      >
        <Icon name="evidence" />
        <div>
          <strong>本页 OCR 正文未就绪，最终采用结果已停止展示文字</strong>
          <span>
            OCR 回执为失败、空结果或处理中；系统不会用本地文字静默回退。表格拓扑和图形位置仍可作为非文字支撑核对。
            {disposition.reasonCodes.length
              ? ` 原因：${disposition.reasonCodes.join("、")}。`
              : ""}
            {rawViewSuffix}
          </span>
        </div>
      </section>
    );
  }
  if (disposition.adoptedTextChannel === "REMOTE_VISUAL") {
    return (
      <section
        className="parse-adoption-banner is-remote"
        role="status"
        aria-label="最终采用通道"
        aria-live="polite"
      >
        <Icon name="evidence" />
        <div>
          <strong>本页正文采用 OCR</strong>
          <span>
            OCR 成功回执与 {disposition.remoteVisualBlockCount} 个视觉事实一致；最终采用视图不再混入本地文字，也不会把 OCR 的表格或图片框冒充采用结果。正文文字来自 OCR，表格拓扑与图片资产来自本地支撑，不作为正文文字。OCR 框已换算到 PDF 页坐标，但仍属于模型定位、尚未经过人工标注精度校准。
            {rawViewSuffix}
          </span>
        </div>
      </section>
    );
  }
  if (disposition.adoptedTextChannel === "NATIVE") {
    return (
      <section
        className="parse-adoption-banner is-native"
        role="status"
        aria-label="最终采用通道"
        aria-live="polite"
      >
        <Icon name="evidence" />
        <div>
          <strong>本页正文采用本地解析</strong>
          <span>
            页面回执明确未运行 OCR，因此最终采用视图只展示本地事实。
            {rawViewSuffix}
          </span>
        </div>
      </section>
    );
  }
  return (
    <section
      className="parse-adoption-banner is-unresolved"
      role="status"
      aria-label="最终采用通道"
      aria-live="polite"
    >
      <Icon name="evidence" />
      <div>
        <strong>本页采用通道无法确认，最终采用结果已停止展示文字</strong>
        <span>
          页面覆盖回执与实际来源块不完整或不一致；系统不会擅自把任一通道冒充最终正文。
          {rawViewSuffix}
        </span>
      </div>
    </section>
  );
}


function PageIssueCard({
  issue,
  focused,
  onFocus,
}: {
  issue: PageIssue;
  focused: boolean;
  onFocus: () => void;
}) {
  return (
    <article className={`parse-issue-card${focused ? " is-focused" : ""}`}>
      <header>
        <StatusBadge tone={issue.blocking ? "danger" : "warning"}>
          {issue.blocking ? "硬阻断" : issue.label}
        </StatusBadge>
        <code>{issue.code}</code>
      </header>
      <p>{issue.message}</p>
      <button
        className="button ghost"
        type="button"
        disabled={!issue.locatable}
        onClick={onFocus}
      >
        {issue.locatable ? "定位原文" : "没有可靠位置"}
      </button>
    </article>
  );
}


function PageBlockCard({
  block,
  snapshot,
  projectId,
  documentId,
  sourceSha256,
  focused,
  onFocus,
  onPageChange,
  supportingGeometryOnly = false,
}: {
  block: PageBlock;
  snapshot: PageSnapshot;
  projectId: string;
  documentId: string;
  sourceSha256: string;
  focused: boolean;
  onFocus: () => void;
  onPageChange: (page: number) => void;
  supportingGeometryOnly?: boolean;
}) {
  const table = snapshot.tables.find(
    (candidate) =>
      candidate.id === block.tableId || candidate.sourceNodeId === block.id,
  );
  const sourceKind = String(block.sourceKind || "").toUpperCase();
  const hasSourceImage =
    block.type !== "PAGE_RASTER" &&
    (block.type === "FIGURE" || ["IMAGE", "FIGURE"].includes(sourceKind));
  const supportLayers = block.supportLayers || [];
  const supportLayerCount =
    block.supportLayerCount || supportLayers.length || 1;
  const supportLayerIds = block.supportLayerIds?.length
    ? block.supportLayerIds
    : supportLayers.map((layer) => layer.id);
  const supportLayerSourceRefs = block.supportLayerSourceRefs || [];
  const supportAuditRows = Array.from(
    {
      length: Math.max(
        supportLayerCount,
        supportLayerIds.length,
        supportLayerSourceRefs.length,
      ),
    },
    (_, index) => {
      const id = supportLayerIds[index] || `物理层 ${index + 1}`;
      const layer =
        supportLayers.find((candidate) => candidate.id === id) ||
        (supportLayers.length === supportLayerCount
          ? supportLayers[index]
          : undefined);
      return {
        id,
        layer,
        sourceRef: supportLayerSourceRefs[index] || {},
      };
    },
  );
  const cropUrl =
    hasSourceImage && block.locatable
      ? sourceCropUrl({
          projectId,
          documentId,
          sourceSha256,
          page: block.pageNumber,
          bbox: block.bbox,
        })
      : "";
  return (
    <article
      className={`parse-block-card is-${block.type.toLowerCase()}${
        focused ? " is-focused" : ""
      } is-channel-${pageBlockEvidenceChannel(block).toLowerCase().replace("_", "-")}`}
      data-parse-fact={block.id}
    >
      <header>
        <div>
          <span className="parse-block-label">{block.label}</span>
          <strong>{contentTypeLabel(block.type, block.role)}</strong>
          <small className="parse-block-source">
            {provenanceLabel(block.provenance)}
          </small>
          {supportingGeometryOnly && (
            <small className="parse-block-adoption-role">
              结构/资产支撑 · 不作为正文文字
            </small>
          )}
        </div>
        <button
          className="button ghost"
          type="button"
          disabled={!block.locatable}
          onClick={onFocus}
        >
          {block.locatable ? "定位原文" : "没有可靠位置"}
        </button>
      </header>
      {block.type === "PAGE_RASTER" ? (
        <section
          className="parse-scan-support"
          role="note"
          aria-label="扫描页支撑说明"
        >
          <strong>扫描页支撑 · {supportLayerCount} 个物理层</strong>
          <span>
            这些层共同还原一张扫描页，不属于最终采用内容，也不是 {supportLayerCount}
            张独立图片。
          </span>
          <small>
            页面内容请直接核对左侧原 PDF 整页预览；这里不会再次裁切并展示整页图像。
          </small>
          {supportAuditRows.length > 0 && (
            <details className="parse-scan-support-audit">
              <summary>查看 {supportLayerCount} 个物理层审计信息</summary>
              <ul>
                {supportAuditRows.map(({ id, layer, sourceRef }, index) => {
                  const sourceId = String(
                    sourceRef.source_id ||
                      sourceRef.source_node_id ||
                      sourceRef.asset_id ||
                      "",
                  );
                  return (
                    <li key={`${id}-${index}`}>
                      <code>{id}</code>
                      <span>
                        {layer?.sourceKind || layer?.type || "原生页面图层"}
                        {layer
                          ? ` · ${provenanceLabel(layer.provenance)}`
                          : ""}
                        {sourceId ? ` · source ${sourceId}` : ""}
                        {layer?.bbox.length === 4
                          ? ` · bbox ${layer.bbox
                              .map((value) => Number(value.toFixed(2)))
                              .join(", ")}`
                          : " · 无可靠坐标"}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </details>
          )}
        </section>
      ) : supportingGeometryOnly && block.type === "TABLE" ? (
        <div className="parse-supporting-geometry" role="note">
          <strong>本地表格拓扑</strong>
          <span>
            {table
              ? `${table.rowCount} 行 × ${table.columnCount} 列${
                  table.geometryPreserved ? " · 几何已保留" : " · 几何待核对"
                }`
              : block.locatable
                ? "表格位置已保留 · 拓扑待核对"
                : "表格事实已保留 · 位置与拓扑待核对"}
          </span>
          <small>单元格文字待视觉绑定；可切换到“本地原始”核对原始文字。</small>
        </div>
      ) : table ? (
        <StructuredTable
          table={table}
          tables={[table]}
          displayLabel={block.label}
          fitContainer
          projectId={projectId}
          documentId={documentId}
          sourceSha256={sourceSha256}
          onPageChange={onPageChange}
          initialRowLimit={18}
        />
      ) : supportingGeometryOnly && block.type === "FIGURE" ? (
        <figure>
          {cropUrl && (
            <img src={cropUrl} alt={`${block.label} 解析图形`} loading="lazy" />
          )}
          <figcaption>本地图形资产与位置已保留；题注文字不作为最终正文。</figcaption>
        </figure>
      ) : cropUrl ? (
        <figure>
          <img src={cropUrl} alt={`${block.label} 解析图形`} loading="lazy" />
          {block.text && <figcaption>{block.text}</figcaption>}
        </figure>
      ) : block.text ? (
        <p>{block.text}</p>
      ) : (
        <p className="parse-block-empty">
          已保留该解析事实，但没有可显示文字。
        </p>
      )}
    </article>
  );
}
