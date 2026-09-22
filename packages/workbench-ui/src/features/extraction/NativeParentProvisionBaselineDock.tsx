import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "../../shared/api/client";
import type {
  NativeParentProvisionBaselinePublicV1,
  NativeParentProvisionEntryV1,
} from "../../shared/api/contracts";
import { cx } from "../../shared/lib/format";
import { EmptyState, InlineNotice, LoadingBlock } from "../../shared/ui/Feedback";
import { Icon } from "../../shared/ui/Icon";

interface NativeParentProvisionBaselineDockProps {
  projectId: string;
  documentId: string;
  sourceFileUrl: string;
}

function entryLabel(entry: NativeParentProvisionEntryV1): string {
  const source = entry.verbatim_text.replace(/\s+/g, " ").trim();
  return entry.explicit_label || source.slice(0, 72) || "未命名原文条目";
}

function requirementnessLabel(value: string): string {
  return value === "REQUIREMENT" ? "需求" : "非需求";
}

function requirementKindLabel(value: string | null): string {
  const labels: Record<string, string> = {
    FUNCTIONAL: "功能需求",
    QUALITY: "质量需求",
    CONSTRAINT: "约束",
    OTHER_REQUIREMENT: "其他需求",
  };
  return value ? labels[value] || value : "不适用";
}

function technicalityLabel(value: string | null): string {
  const labels: Record<string, string> = {
    TECHNICAL: "技术性",
    NON_TECHNICAL: "非技术性",
    MIXED: "混合",
  };
  return value ? labels[value] || value : "不适用";
}

export function NativeParentProvisionBaselineDock({
  projectId,
  documentId,
  sourceFileUrl,
}: NativeParentProvisionBaselineDockProps) {
  const [result, setResult] =
    useState<NativeParentProvisionBaselinePublicV1 | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [activePage, setActivePage] = useState(1);
  const pdfFrameRef = useRef<HTMLIFrameElement | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void api
      .nativeParentProvisionResult(projectId, documentId, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setResult(value);
        const first = value.entries[0];
        setSelectedId(first?.parent_provision_id || "");
        setActivePage(first?.source_ranges[0]?.page || 1);
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        setError(
          caught instanceof ApiError
            ? caught.message
            : "需求基线结果暂时无法读取。",
        );
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [documentId, projectId]);

  const selected =
    result?.entries.find((entry) => entry.parent_provision_id === selectedId) ||
    result?.entries[0] ||
    null;
  const overlays = useMemo(
    () =>
      (selected?.source_ranges || [])
        .filter(
          (range) =>
            range.page === activePage && range.bbox && range.source_bounds,
        )
        .map((range, index) => ({
          id: `${selected?.parent_provision_id || "parent"}:${range.segment_id}:${index}`,
          type: "TEXT",
          label: `原文 ${index + 1}`,
          bbox: range.bbox,
          sourceBounds: range.source_bounds,
          selected: true,
          focus: true,
          precision: range.precision,
          channel: "NATIVE",
        })),
    [activePage, selected],
  );
  const syncPdf = useCallback(() => {
    pdfFrameRef.current?.contentWindow?.postMessage(
      {
        type: "REQPILOTX_PDF_NAVIGATE",
        page: activePage,
        overlays,
      },
      window.location.origin,
    );
  }, [activePage, overlays]);

  useEffect(() => syncPdf(), [syncPdf]);
  useEffect(() => {
    const listener = (event: MessageEvent) => {
      if (
        event.origin !== window.location.origin ||
        event.source !== pdfFrameRef.current?.contentWindow
      ) {
        return;
      }
      const message = event.data as { type?: unknown; page?: unknown };
      if (message.type === "pdf-viewer-ready") syncPdf();
      if (message.type === "pdf-page-changed") {
        const page = Math.trunc(Number(message.page || activePage));
        if (page > 0) setActivePage(page);
      }
    };
    window.addEventListener("message", listener);
    return () => window.removeEventListener("message", listener);
  }, [activePage, syncPdf]);

  if (loading && !result) return <LoadingBlock label="正在读取需求基线" />;
  if (error) {
    return (
      <InlineNotice title="需求基线暂不可用" tone="warning">
        <span>{error}</span>
      </InlineNotice>
    );
  }
  if (!result || !selected) {
    return (
      <EmptyState
        title="没有可显示的父条款"
        description="Host 尚未发布完整的来源窗口聚合结果。"
      />
    );
  }

  return (
    <section className="native-parent-baseline" aria-label="Agent 需求基线">
      <header className="native-parent-baseline-header">
        <div>
          <strong>Agent 需求基线</strong>
          <span>
            {result.summary.parent_provision_count} 个父条款 · 全部已绑定原文
          </span>
        </div>
        <span className="native-parent-proof">
          {result.professional_policy_attested ? "专业证明已闭合" : "证明未闭合"}
        </span>
      </header>
      <div className="native-parent-baseline-grid">
        <aside className="native-parent-list" aria-label="父条款列表">
          {result.entries.map((entry) => (
            <button
              key={entry.parent_provision_id}
              type="button"
              className={cx(
                "native-parent-list-item",
                entry.parent_provision_id === selected.parent_provision_id &&
                  "is-selected",
              )}
              onClick={() => {
                setSelectedId(entry.parent_provision_id);
                setActivePage(entry.source_ranges[0]?.page || activePage);
              }}
            >
              <span>{entry.source_order + 1}</span>
              <strong>{entryLabel(entry)}</strong>
              <em
                className={cx(
                  "native-parent-kind",
                  entry.requirementness === "REQUIREMENT" && "is-requirement",
                )}
              >
                {requirementnessLabel(entry.requirementness)}
              </em>
            </button>
          ))}
        </aside>
        <article className="native-parent-detail">
          <div className="native-parent-detail-title">
            <span>原文父条款 {selected.source_order + 1}</span>
            <h2>{entryLabel(selected)}</h2>
          </div>
          <div className="native-parent-classification">
            <div>
              <span>判断</span>
              <strong>{requirementnessLabel(selected.requirementness)}</strong>
            </div>
            <div>
              <span>需求类型</span>
              <strong>{requirementKindLabel(selected.requirement_kind)}</strong>
            </div>
            <div>
              <span>技术属性</span>
              <strong>{technicalityLabel(selected.technicality)}</strong>
            </div>
            <div>
              <span>置信度</span>
              <strong>{Math.round(selected.confidence * 100)}%</strong>
            </div>
          </div>
          <section className="native-parent-verbatim">
            <header>
              <strong>逐字原文</strong>
              <span>{selected.source_ranges.length} 个来源范围</span>
            </header>
            <p>{selected.verbatim_text}</p>
          </section>
          <div className="native-parent-pages" aria-label="来源页面">
            {Array.from(
              new Set(selected.source_ranges.map((range) => range.page)),
            ).map((page) => (
              <button
                type="button"
                key={page}
                className={cx(activePage === page && "is-active")}
                onClick={() => setActivePage(page)}
              >
                P{page}
              </button>
            ))}
          </div>
          <details className="native-parent-reasons">
            <summary>查看 Agent 判断依据</summary>
            <span>内容角色：{selected.content_role}</span>
            <span>依据代码：{selected.reason_codes.join(" · ")}</span>
            {selected.ancestor_labels.length ? (
              <span>上级语境：{selected.ancestor_labels.join(" / ")}</span>
            ) : null}
          </details>
        </article>
        <section className="native-parent-source" aria-label="原文 PDF 对照">
          <header>
            <div>
              <strong>原文 PDF</strong>
              <span>P{activePage} · {overlays.length} 个证据框</span>
            </div>
            <a
              className="button ghost compact"
              href={`/pdf-viewer?fit=page&pdf=${encodeURIComponent(sourceFileUrl)}&page=${activePage}`}
              target="_blank"
              rel="noreferrer"
              aria-label="在新窗口打开原文 PDF"
            >
              <Icon name="external" size={14} />
            </a>
          </header>
          <iframe
            ref={pdfFrameRef}
            title={`原文 PDF · 第 ${activePage} 页`}
            src={
              `/pdf-viewer?embedded=1&fit=page` +
              `&pdf=${encodeURIComponent(sourceFileUrl)}` +
              `&page=1`
            }
            onLoad={syncPdf}
          />
        </section>
      </div>
    </section>
  );
}
