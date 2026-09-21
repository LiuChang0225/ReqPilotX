import { useMemo, useState } from "react";
import { ChevronDown, FileScan, RotateCcw, X } from "lucide-react";
import { cx } from "../../shared/lib/format";
import type {
  CaptureQualityManifestV1,
  CaptureQualityPageV1,
} from "./contracts";
import { capturePageNeedsVisualWork } from "./contracts";

export interface CaptureStatusProps {
  manifest: CaptureQualityManifestV1 | null;
  loading?: boolean;
  error?: string;
  currentPage?: number;
  busyPage?: number | null;
  bulkRunning?: boolean;
  bulkStopping?: boolean;
  bulkProgress?: { completed: number; total: number; failed: number } | null;
  onPage?: (page: number) => void;
  onResumePage?: (page: number) => void;
  onResumeAll?: (pages: number[]) => void;
  onStopAll?: () => void;
  readOnly?: boolean;
  className?: string;
}

function readablePageCount(manifest: CaptureQualityManifestV1): number {
  return manifest.pages.filter(
    (page) => page.readable_mode !== "NONE",
  ).length;
}

function summaryLabel(
  manifest: CaptureQualityManifestV1 | null,
  loading: boolean,
  error: string,
): string {
  if (loading) return "正在检查可读性";
  if (!manifest || error) return "可读性待确认";
  const readable = readablePageCount(manifest);
  if (manifest.status === "READY" && readable >= manifest.page_count) {
    return `可读 ${manifest.page_count}/${manifest.page_count}`;
  }
  if (manifest.status === "BLOCKED") return "存在不可读页面";
  return `可读 ${readable}/${manifest.page_count}`;
}

function pageStatusLabel(page: CaptureQualityPageV1): string {
  if (page.readable_mode === "NATIVE") return "原生文字可选";
  if (page.readable_mode === "VISUAL") return "视觉文字检查完成";
  if (page.readable_mode === "HYBRID") return "混合文字可选";
  if (page.capture_status === "RUNNING") return "正在识别";
  if (page.capture_status === "FAILED") {
    const failureCode = String(page.failure_code || "").toLowerCase();
    if (failureCode === "visual_image_semantics_not_supported") {
      return "当前仅支持文字层；该页为无文字图像/图纸，暂不能完整阅读";
    }
    if (failureCode.includes("provider_not_configured")) {
      return "视觉模型未配置，请先完成配置";
    }
    if (
      failureCode.includes("authentication_failed") ||
      failureCode.includes("authorization_failed") ||
      failureCode.includes("credentials_unavailable") ||
      failureCode.includes("configuration_invalid")
    ) {
      return "视觉模型认证或配置不可用，请先修正 AI 阅读 PDF 页面解析配置";
    }
    return "识别失败，可重试";
  }
  return "当前不可选择";
}

function pageCaptureIsRetryable(page: CaptureQualityPageV1): boolean {
  const failureCode = String(page.failure_code || "").toLowerCase();
  return !(
    failureCode === "visual_image_semantics_not_supported" ||
    failureCode.includes("provider_not_configured") ||
    failureCode.includes("authentication_failed") ||
    failureCode.includes("authorization_failed") ||
    failureCode.includes("credentials_unavailable") ||
    failureCode.includes("configuration_invalid")
  );
}

export function CaptureStatus({
  manifest,
  loading = false,
  error = "",
  currentPage = 1,
  busyPage = null,
  bulkRunning = false,
  bulkStopping = false,
  bulkProgress = null,
  onPage,
  onResumePage,
  onResumeAll,
  onStopAll,
  readOnly = false,
  className,
}: CaptureStatusProps) {
  const [open, setOpen] = useState(false);
  const attentionPages = useMemo(
    () =>
      (manifest?.pages || []).filter(
        (page) =>
          capturePageNeedsVisualWork(page) || page.capture_status === "FAILED",
      ),
    [manifest],
  );
  const readable = manifest ? readablePageCount(manifest) : 0;
  const retryablePages = useMemo(
    () => attentionPages.filter(pageCaptureIsRetryable),
    [attentionPages],
  );
  const riskPages = useMemo(
    () => (manifest?.pages || []).filter((page) => page.risk_codes.length > 0),
    [manifest],
  );
  const attentionPagesWithoutRisk = useMemo(
    () => attentionPages.filter((page) => page.risk_codes.length === 0),
    [attentionPages],
  );
  const visualReadyPages = useMemo(
    () =>
      (manifest?.pages || []).filter(
        (page) =>
          page.readable_mode === "VISUAL" && page.capture_status === "READY",
      ),
    [manifest],
  );
  const providerLabels = useMemo(
    () =>
      [
        ...new Set(
          (manifest?.pages || [])
            .filter((page) => page.provider_id)
            .map(
              (page) =>
                `${page.provider_id}${page.provider_version ? ` / ${page.provider_version}` : ""}`,
            ),
        ),
      ],
    [manifest],
  );
  const label = summaryLabel(manifest, loading, error);
  const tone = !manifest || error
    ? "unknown"
    : manifest.status === "READY" && attentionPages.length === 0
      ? "ready"
      : manifest.status === "BLOCKED"
        ? "blocked"
        : "partial";

  return (
    <div className={cx("capture-status", className)}>
      <button
        className={cx("capture-status-trigger", `is-${tone}`)}
        type="button"
        aria-expanded={open}
        aria-label={`文档可读性：${label}`}
        onClick={() => setOpen((value) => !value)}
      >
        <FileScan size={15} aria-hidden="true" />
        <span>{label}</span>
        <ChevronDown size={13} aria-hidden="true" />
      </button>
      {open ? (
        <section className="capture-status-popover" aria-label="文档可读性详情">
          <header>
            <div>
              <small>DOCUMENT CAPTURE</small>
              <h3>文档可读性</h3>
            </div>
            <button
              type="button"
              aria-label="关闭文档可读性详情"
              onClick={() => setOpen(false)}
            >
              <X size={16} />
            </button>
          </header>
          {loading ? (
            <p className="capture-status-message">正在核对每一页的可选择文字层…</p>
          ) : error || !manifest ? (
            <p className="capture-status-message is-warning">
              {error || "暂时无法确认每一页是否都能被完整读取。AI 回答不应被视为全文覆盖。"}
            </p>
          ) : (
            <>
              <dl className="capture-status-metrics">
                <div><dt>可读</dt><dd>{readable}/{manifest.page_count}</dd></div>
                <div><dt>需视觉识别</dt><dd>{manifest.visual_required_page_count}</dd></div>
                <div><dt>已完成</dt><dd>{manifest.visual_ready_page_count}</dd></div>
                <div><dt>失败</dt><dd>{manifest.failed_page_count}</dd></div>
              </dl>
              {attentionPages.length ? (
                <div className="capture-status-pages">
                  <div className="capture-status-explanation">
                    <strong>{attentionPages.length} 页当前无法完整选择</strong>
                    <p>
                      {readOnly
                        ? "这里只读显示中立捕获状态；进入解析诊断不会自动上传页面或启动识别。"
                        : "继续识别会把这些页面图像发送给已配置的视觉模型，只补齐阅读来源，不会启动需求提取。"}
                    </p>
                    {!readOnly && bulkRunning ? (
                      <div className="capture-bulk-progress" role="status">
                        <span>
                          {bulkStopping
                            ? "将在当前页识别完成后停止"
                            : `已处理 ${bulkProgress?.completed || 0}/${bulkProgress?.total || attentionPages.length}${bulkProgress?.failed ? ` · ${bulkProgress.failed} 页失败` : ""}`}
                        </span>
                        <button
                          type="button"
                          disabled={bulkStopping}
                          onClick={onStopAll}
                        >
                          {bulkStopping ? "正在停止…" : "停止批量识别"}
                        </button>
                      </div>
                    ) : !readOnly && retryablePages.length ? (
                      <button
                        type="button"
                        disabled={!onResumeAll}
                        onClick={() =>
                          onResumeAll?.(
                            retryablePages.map((page) => page.page_number),
                          )
                        }
                      >
                        识别全部 {retryablePages.length} 页
                      </button>
                    ) : null}
                  </div>
                  {!readOnly ? attentionPages.map((page) => (
                    <article
                      key={page.page_number}
                      className={cx(page.page_number === currentPage && "is-current")}
                    >
                      <button
                        className="capture-page-link"
                        type="button"
                        onClick={() => onPage?.(page.page_number)}
                      >
                        第 {page.page_number} 页
                      </button>
                      <span>{pageStatusLabel(page)}</span>
                      {!readOnly ? (
                        pageCaptureIsRetryable(page) ? <button
                          className="capture-page-resume"
                          type="button"
                          disabled={
                            bulkRunning ||
                            busyPage === page.page_number ||
                            !onResumePage
                          }
                          onClick={() => onResumePage?.(page.page_number)}
                        >
                          <RotateCcw size={12} aria-hidden="true" />
                          {busyPage === page.page_number
                            ? "识别中…"
                            : page.capture_status === "FAILED"
                              ? "重新识别"
                              : "识别本页"}
                        </button>
                        : <span className="capture-page-readonly">无需重复识别</span>
                      ) : (
                        <span className="capture-page-readonly">只读</span>
                      )}
                    </article>
                  )) : null}
                </div>
              ) : (
                <p className="capture-status-message is-ready">
                  当前文档所有需要识别的页面均已完成文字检查；有文字页面已保存可追溯文字层，无文字图片/图纸语义未解析。
                </p>
              )}
              {readOnly && (
                attentionPagesWithoutRisk.length > 0 ||
                riskPages.length > 0 ||
                visualReadyPages.length > 0
              ) ? (
                <div className="capture-status-page-groups">
                  {attentionPagesWithoutRisk.length > 0 ? (
                    <section aria-label="待补齐页面">
                      <strong>待补齐</strong>
                      <div>
                        {attentionPagesWithoutRisk.map((page) => (
                          <button
                            key={`attention-${page.page_number}`}
                            type="button"
                            className={cx(page.page_number === currentPage && "is-current")}
                            title={pageStatusLabel(page)}
                            onClick={() => onPage?.(page.page_number)}
                          >
                            P{page.page_number}
                          </button>
                        ))}
                      </div>
                    </section>
                  ) : null}
                  {riskPages.length > 0 ? (
                    <section aria-label="捕获风险页">
                      <strong>风险页</strong>
                      <div>
                        {riskPages.map((page) => (
                          <button
                            key={`risk-${page.page_number}`}
                            type="button"
                            className={cx(page.page_number === currentPage && "is-current")}
                            title={page.risk_codes.join(" · ")}
                            onClick={() => onPage?.(page.page_number)}
                          >
                            P{page.page_number}
                          </button>
                        ))}
                      </div>
                    </section>
                  ) : null}
                  {visualReadyPages.length > 0 ? (
                    <section aria-label="视觉识别完成页">
                      <strong>视觉完成</strong>
                      <div>
                        {visualReadyPages.map((page) => (
                          <button
                            key={`visual-${page.page_number}`}
                            type="button"
                            className={cx(page.page_number === currentPage && "is-current")}
                            onClick={() => onPage?.(page.page_number)}
                          >
                            P{page.page_number}
                          </button>
                        ))}
                      </div>
                    </section>
                  ) : null}
                </div>
              ) : null}
              <details className="capture-status-technical">
                <summary>技术来源</summary>
                <dl>
                  <div><dt>来源哈希</dt><dd>{manifest.source_sha256.slice(0, 12)}…</dd></div>
                  <div><dt>基础捕获</dt><dd>{manifest.base_capture_id || "—"}</dd></div>
                  <div><dt>视觉批次</dt><dd>{manifest.active_visual_generation_id || "尚未生成"}</dd></div>
                  <div><dt>模型来源</dt><dd>{providerLabels.join("、") || "尚无视觉模型记录"}</dd></div>
                </dl>
              </details>
            </>
          )}
        </section>
      ) : null}
    </div>
  );
}
