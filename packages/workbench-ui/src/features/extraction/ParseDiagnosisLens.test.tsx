import { createRef, useState } from "react";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  normalizeRestorationPage,
  type PageEvidenceView,
  type PageSnapshot,
} from "../restoration/restorationModel";
import {
  ParseDiagnosisLens,
  type ExtractionVisualReceiptSummary,
  type ExtractionVisualRouteIdentity,
} from "./ParseDiagnosisLens";
import {
  normalizeParsingDiagnostics,
  type ParsingDiagnosticsSnapshot,
} from "./parsingDiagnosticsModel";


afterEach(cleanup);

describe("ParseDiagnosisLens", () => {
  it("defaults to the adopted OCR text channel and keeps raw channels explicitly switchable", async () => {
    const user = userEvent.setup();
    render(
      <DiagnosisHarness
        pageSnapshotOverride={ocrPageSnapshot()}
        onPageChange={vi.fn()}
        onFactFocus={vi.fn()}
      />,
    );

    const channelSwitch = screen.getByRole("group", {
      name: "解析来源视图",
    });
    expect(
      within(channelSwitch).getByRole("button", { name: "最终采用" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.getByRole("status", { name: "最终采用通道" }),
    ).toHaveTextContent("本页正文采用 OCR");
    expect(
      screen.getByRole("status", { name: "最终采用通道" }),
    ).toHaveTextContent("模型定位、尚未经过人工标注精度校准");
    expect(screen.getByText("OCR adopted text")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "FIG-001 解析图形" })).toBeInTheDocument();
    expect(screen.getByText("结构/资产支撑 · 不作为正文文字")).toBeInTheDocument();
    expect(screen.getByText(/题注文字不作为最终正文/)).toBeInTheDocument();
    expect(screen.queryByText("native figure evidence")).not.toBeInTheDocument();
    expect(screen.queryByText("native duplicate text")).not.toBeInTheDocument();
    expect(screen.getAllByText(/当前显示 2 块/)).toHaveLength(2);

    await user.click(
      within(channelSwitch).getByRole("button", { name: "OCR 原始" }),
    );
    expect(screen.getByText("OCR adopted text")).toBeInTheDocument();
    expect(screen.queryByText("native figure evidence")).not.toBeInTheDocument();
    expect(screen.getAllByText(/当前显示 1 块/)).toHaveLength(2);

    await user.click(
      within(channelSwitch).getByRole("button", { name: "本地原始" }),
    );
    expect(screen.getByText("native duplicate text")).toBeInTheDocument();
    expect(screen.getByText("native figure evidence")).toBeInTheDocument();
    expect(screen.queryByText("OCR adopted text")).not.toBeInTheDocument();

    await user.click(
      within(channelSwitch).getByRole("button", { name: "差异对照" }),
    );
    expect(screen.getByText("native duplicate text")).toBeInTheDocument();
    expect(screen.getByText("OCR adopted text")).toBeInTheDocument();
    expect(screen.getByText("native figure evidence")).toBeInTheDocument();
  });

  it("does not silently adopt native text when the OCR page receipt is degraded", async () => {
    const user = userEvent.setup();
    render(
      <DiagnosisHarness
        visualRecoveryFailed
        onPageChange={vi.fn()}
        onFactFocus={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("status", { name: "最终采用通道" }),
    ).toHaveTextContent("本页 OCR 正文未就绪");
    expect(
      screen.queryByText("系统应在 5 秒内完成自检。"),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "本地原始" }));
    expect(screen.getByText("系统应在 5 秒内完成自检。")).toBeInTheDocument();
  });

  it("hides both text channels when the adopted authority cannot be verified", async () => {
    const user = userEvent.setup();
    render(
      <DiagnosisHarness
        pageSnapshotOverride={ambiguousTextAuthorityPageSnapshot()}
        onPageChange={vi.fn()}
        onFactFocus={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("status", { name: "最终采用通道" }),
    ).toHaveTextContent("最终采用结果已停止展示文字");
    expect(screen.queryByText("native ambiguous text")).not.toBeInTheDocument();
    expect(screen.queryByText("OCR ambiguous text")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "差异对照" }));
    expect(screen.getByText("native ambiguous text")).toBeInTheDocument();
    expect(screen.getByText("OCR ambiguous text")).toBeInTheDocument();
  });

  it("keeps native table topology but withholds native cell text in the adopted OCR view", async () => {
    const user = userEvent.setup();
    render(
      <DiagnosisHarness
        pageSnapshotOverride={ocrTablePageSnapshot()}
        onPageChange={vi.fn()}
        onFactFocus={vi.fn()}
      />,
    );

    expect(screen.getByRole("note")).toHaveTextContent("本地表格拓扑");
    expect(screen.getByText("结构/资产支撑 · 不作为正文文字")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("1 行 × 1 列");
    expect(screen.getByRole("note")).toHaveTextContent("文字待视觉绑定");
    expect(screen.queryByText("native secret cell text")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "本地原始" }));
    expect(screen.getByText("native secret cell text")).toBeInTheDocument();
  });

  it("calls table structure ready only when exact table-parsing cell facts exist", () => {
    render(
      <DiagnosisHarness
        pageSnapshotOverride={ocrTablePageSnapshot("visual_table_cell_fact")}
        visualReceiptSummary={visualReceiptSummary("QWEN_OCR")}
        onPageChange={vi.fn()}
        onFactFocus={vi.fn()}
      />,
    );

    const scope = screen.getByRole("status", {
      name: "Qwen OCR 结构恢复范围",
    });
    expect(scope).toHaveTextContent("Qwen OCR 文字定位与表格回执可核对");
    expect(scope).toHaveTextContent("1 个单元格");
    expect(scope).toHaveTextContent("table_parsing 事实");
    expect(scope).not.toHaveTextContent("实际远程请求");
  });

  it("keeps original PDF and all same-page facts side by side", async () => {
    const user = userEvent.setup();
    const onPageChange = vi.fn();
    const onFactFocus = vi.fn();
    const view = render(
      <DiagnosisHarness
        onPageChange={onPageChange}
        onFactFocus={onFactFocus}
      />,
    );

    expect(screen.getByLabelText("原 PDF · 第 2 页")).toBeInTheDocument();
    expect(screen.getByLabelText("第 2 页解析结果")).toBeInTheDocument();
    expect(screen.getByText("系统应在 5 秒内完成自检。")).toBeInTheDocument();
    expect(screen.getByText("Visual content may be incomplete.")).toBeInTheDocument();
    expect(screen.getByText("Technical trace only.")).not.toBeVisible();
    expect(
      screen.queryByRole("note", { name: "扫描页支撑说明" }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: "显示扫描支撑层" }));
    const support = screen.getByRole("note", { name: "扫描页支撑说明" });
    expect(support).toHaveTextContent("2 个物理层");
    expect(support).toHaveTextContent("不属于最终采用内容");
    expect(support).toHaveTextContent("左侧原 PDF 整页预览");
    expect(screen.getByText("查看 2 个物理层审计信息")).toBeInTheDocument();
    expect(
      view.container.querySelectorAll(
        '[data-parse-fact="page-2-scan-support"]',
      ),
    ).toHaveLength(1);
    expect(
      view.container.querySelectorAll('.parse-block-card.is-page_raster img'),
    ).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "只看严重风险" }));
    expect(
      screen.queryByText("系统应在 5 秒内完成自检。"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Visual content may be incomplete.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(onPageChange).toHaveBeenCalledWith(3);
  });

  it("states a real workflow blocker without hiding the page evidence", () => {
    render(
      <DiagnosisHarness
        workflowBlockerMessage="当前正文事实不足以生成结构。"
        onPageChange={vi.fn()}
        onFactFocus={vi.fn()}
      />,
    );

    expect(
      screen.getByText("当前事实只读，下一步等待可用结果"),
    ).toBeInTheDocument();
    expect(screen.getByText("当前结果不可继续")).toBeInTheDocument();
    expect(screen.getByText("当前正文事实不足以生成结构。")).toBeInTheDocument();
    expect(screen.getByText("系统应在 5 秒内完成自检。")).toBeInTheDocument();
  });

  it("keeps an unassigned hard blocker visible instead of hiding it behind page navigation", () => {
    render(
      <DiagnosisHarness
        diagnosticsSnapshot={normalizeParsingDiagnostics({
          schema_version: "parsing-diagnostics/v1",
          status: "BLOCKED",
          source_sha256: "a".repeat(64),
          active_parse_artifact_id: "b".repeat(64),
          parse_identity_sha256: "c".repeat(64),
          page_count: 4,
          hard_blocker_count: 1,
          severe_warning_count: 0,
          risk_count: 1,
          severe_issue_ids: [],
          blocking_issue_ids: ["unreadable-source"],
          unassigned_risk_count: 1,
          risk_pages: [],
          risks: [
            {
              id: "unreadable-source",
              code: "SOURCE_UNREADABLE",
              severity: "error",
              blocking: true,
              stage: "SOURCE_IR",
              message: "Source content is unreadable.",
              page_number: 0,
            },
          ],
        })}
        onPageChange={vi.fn()}
        onFactFocus={vi.fn()}
      />,
    );

    expect(
      screen.getByLabelText("无法定位到具体页的严重风险"),
    ).toBeInTheDocument();
    expect(screen.getByText("SOURCE_UNREADABLE")).toBeInTheDocument();
    expect(screen.getByText("Source content is unreadable.")).toBeInTheDocument();
  });

  it("counts one multi-page finding once and keeps every risk page navigable", async () => {
    const user = userEvent.setup();
    const onPageChange = vi.fn();
    render(
      <DiagnosisHarness
        diagnosticsSnapshot={normalizeParsingDiagnostics({
          schema_version: "parsing-diagnostics/v1",
          status: "READY_WITH_RISKS",
          source_sha256: "a".repeat(64),
          active_parse_artifact_id: "b".repeat(64),
          parse_identity_sha256: "c".repeat(64),
          page_count: 4,
          hard_blocker_count: 0,
          severe_warning_count: 1,
          risk_count: 1,
          severe_issue_ids: ["finding-1:0"],
          blocking_issue_ids: [],
          unassigned_risk_count: 0,
          risk_pages: [
            {
              page_number: 2,
              hard_blocker_count: 0,
              severe_warning_count: 1,
              risk_ids: ["finding-1:0"],
              issue_codes: ["STRUCTURE_GAP"],
            },
            {
              page_number: 4,
              hard_blocker_count: 0,
              severe_warning_count: 1,
              risk_ids: ["finding-1:0"],
              issue_codes: ["STRUCTURE_GAP"],
            },
          ],
          risks: [
            {
              id: "finding-1:0",
              finding_id: "finding-1",
              code: "STRUCTURE_GAP",
              severity: "warning",
              blocking: false,
              stage: "STRUCTURE",
              message: "structure_gap",
              page_number: 2,
              page_numbers: [2, 4],
              locations: [
                { page_number: 2 },
                { page_number: 4 },
              ],
            },
          ],
        })}
        onPageChange={onPageChange}
        onFactFocus={vi.fn()}
      />,
    );

    expect(screen.getByText("1 项严重风险，涉及 2 页")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "查看第 2 页的 1 项解析风险" }),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "查看第 4 页的 1 项解析风险" }),
    );
    expect(onPageChange).toHaveBeenCalledWith(4);
  });

  it("shows the terminal Qwen OCR structure receipt and keeps failed pages navigable without borrowing reading capture status", async () => {
    const user = userEvent.setup();
    const onPageChange = vi.fn();

    render(
      <DiagnosisHarness
        pageCount={30}
        visualReceiptSummary={{
          attemptedPageCount: 27,
          readyPageCount: 23,
          degradedPageNumbers: [15, 18, 26, 27],
          routeIdentity: "QWEN_OCR",
          adoptedProviderIds: ["aliyun-bailian-qwen-ocr"],
          adoptedProviderIdByPage: { 2: "aliyun-bailian-qwen-ocr" },
          tableParsing: {
            candidateCount: 8,
            attemptCount: 6,
            readyCount: 5,
            failedCount: 1,
            budgetSkippedCount: 2,
            requestCeilingPerPage: 8,
            requestCeilingPerDocument: 64,
          },
        }}
        onPageChange={onPageChange}
        onFactFocus={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("status", { name: "Qwen OCR 结构恢复终态回执" }),
    ).toHaveTextContent("Qwen OCR 结构恢复 23/27 页就绪，4 页降级");
    expect(
      screen.getByRole("status", { name: "Qwen OCR 结构恢复终态回执" }),
    ).toHaveTextContent("文字定位、文档结构与表格结构必须分别核对回执");
    expect(
      screen.getByRole("status", { name: "Qwen OCR 结构恢复终态回执" }),
    ).toHaveTextContent("表格处理 6 次尝试 · 5 就绪 · 1 失败 · 2 项因预算跳过");
    expect(
      screen.queryByRole("button", { name: /文档可读性/ }),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "查看 Qwen OCR 结构恢复降级的第 18 页" }),
    );
    expect(onPageChange).toHaveBeenCalledWith(18);
  });

  it.each([
    ["BAILIAN_LAYOUT", "bailian-page-layout"],
    ["AZURE_DOCUMENT_INTELLIGENCE", "azure-document-intelligence"],
  ] as const)(
    "uses a generic terminal receipt for %s instead of claiming Qwen OCR",
    (routeIdentity, providerId) => {
      render(
        <DiagnosisHarness
          pageSnapshotOverride={ocrPageSnapshot()}
          visualReceiptSummary={visualReceiptSummary(routeIdentity, providerId)}
          onPageChange={vi.fn()}
          onFactFocus={vi.fn()}
        />,
      );

      expect(
        screen.getByRole("status", { name: "页面视觉处理终态回执" }),
      ).toHaveTextContent("页面视觉处理 4/4 页就绪，0 页降级");
      expect(
        screen.queryByRole("status", { name: "Qwen OCR 结构恢复终态回执" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("status", { name: "Qwen OCR 结构恢复范围" }),
      ).not.toBeInTheDocument();
    },
  );

  it("attributes native image assets to PyMuPDF instead of Qwen OCR", () => {
    render(
      <DiagnosisHarness
        pageSnapshotOverride={ocrPageSnapshot()}
        visualReceiptSummary={visualReceiptSummary("QWEN_OCR")}
        onPageChange={vi.fn()}
        onFactFocus={vi.fn()}
      />,
    );

    const scope = screen.getByRole("status", {
      name: "Qwen OCR 结构恢复范围",
    });
    expect(scope).toHaveTextContent("图片资产与矢量图");
    expect(scope).toHaveTextContent("PyMuPDF 精确保留");
    expect(scope).toHaveTextContent("Qwen OCR 不冒充图片内容理解");
    expect(scope).toHaveTextContent("当前仅有 Qwen OCR 文字定位回执");
    expect(scope).toHaveTextContent("文档结构或表格结构");
  });

  it("keeps a native header image inspectable only in the raw native view", async () => {
    const snapshot = ocrPageSnapshot();
    snapshot.blocks.unshift({
      id: "native-header-logo",
      type: "HEADER",
      sourceKind: "image",
      role: "",
      label: "HDR-001",
      text: "",
      bbox: [40, 20, 120, 60],
      locatable: true,
      locationStatus: "LOCATED",
      sourceOrder: 0,
      pageNumber: 2,
      tableId: "",
      provenance: { channel: "NATIVE" },
      diagnostics: [],
    });
    const user = userEvent.setup();

    render(
      <DiagnosisHarness
        pageSnapshotOverride={snapshot}
        onPageChange={vi.fn()}
        onFactFocus={vi.fn()}
      />,
    );

    expect(
      screen.queryByRole("img", { name: "HDR-001 解析图形" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "本地原始" }));
    expect(
      screen.getByRole("img", { name: "HDR-001 解析图形" }),
    ).toBeInTheDocument();
  });

  it("never marks an ungrounded scanned-page image region as ready", () => {
    render(
      <DiagnosisHarness
        pageSnapshotOverride={ocrScannedFigureMarkupSnapshot()}
        visualReceiptSummary={visualReceiptSummary("QWEN_OCR")}
        onPageChange={vi.fn()}
        onFactFocus={vi.fn()}
      />,
    );

    const scope = screen.getByRole("status", {
      name: "Qwen OCR 结构恢复范围",
    });
    expect(scope).toHaveTextContent("图片内容未评估");
    expect(scope).toHaveTextContent("需页面布局/视觉理解升级");
    expect(scope).toHaveTextContent("没有升级成功回执时阻断下一步");
    expect(scope).not.toHaveTextContent("图片理解就绪");
  });
});

function DiagnosisHarness({
  workflowBlockerMessage = "",
  diagnosticsSnapshot = diagnostics(),
  pageCount = 4,
  visualReceiptSummary,
  pageSnapshotOverride,
  visualRecoveryFailed = false,
  onPageChange,
  onFactFocus,
}: {
  workflowBlockerMessage?: string;
  diagnosticsSnapshot?: ParsingDiagnosticsSnapshot;
  pageCount?: number;
  visualReceiptSummary?: ExtractionVisualReceiptSummary;
  pageSnapshotOverride?: PageSnapshot;
  visualRecoveryFailed?: boolean;
  onPageChange: (page: number) => void;
  onFactFocus: (factId: string) => void;
}) {
  const [riskOnly, setRiskOnly] = useState(false);
  const [showSupportLayer, setShowSupportLayer] = useState(false);
  const [contentView, setContentView] = useState<PageEvidenceView>("EFFECTIVE");
  return (
    <ParseDiagnosisLens
      projectId="project-1"
      documentId="document-1"
      sourceSha256={"a".repeat(64)}
      sourceFileUrl="/api/source.pdf"
      pdfUrl="/pdf-viewer?embedded=1"
      pdfFrameRef={createRef<HTMLIFrameElement>()}
      page={2}
      pageCount={pageCount}
      pageSnapshot={pageSnapshotOverride || pageSnapshot()}
      pageLoading={false}
      pageAvailable
      pageError=""
      diagnostics={diagnosticsSnapshot}
      diagnosticsLoading={false}
      diagnosticsError=""
      workflowBlockerMessage={workflowBlockerMessage}
      visualReceiptSummary={visualReceiptSummary}
      focusedFactId=""
      riskOnly={riskOnly}
      showSupportLayer={showSupportLayer}
      visualRecoveryFailed={visualRecoveryFailed}
      contentView={contentView}
      onRiskOnlyChange={setRiskOnly}
      onShowSupportLayerChange={setShowSupportLayer}
      onContentViewChange={setContentView}
      onPageChange={onPageChange}
      onFactFocus={onFactFocus}
    />
  );
}

function visualReceiptSummary(
  routeIdentity: ExtractionVisualRouteIdentity,
  providerId = "aliyun-bailian-qwen-ocr",
): ExtractionVisualReceiptSummary {
  return {
    attemptedPageCount: 4,
    readyPageCount: 4,
    degradedPageNumbers: [],
    routeIdentity,
    adoptedProviderIds: [providerId],
    adoptedProviderIdByPage: { 2: providerId },
  };
}


function pageSnapshot() {
  return normalizeRestorationPage(
    {
      source_identity: { source_sha256: "a".repeat(64) },
      page_comparison: {
        pages: [
          {
            page_number: 2,
            bounds: [0, 0, 595, 842],
            blocks: [
              {
                id: "text-1",
                type: "TEXT",
                label: "TXT-001",
                text: "系统应在 5 秒内完成自检。",
                page_number: 2,
                source_order: 1,
                bbox: [40, 80, 520, 120],
                locatable: true,
                provenance: { channel: "NATIVE" },
              },
              {
                id: "page-raster-base",
                type: "PAGE_RASTER",
                role: "SCAN_LAYER",
                label: "PGR-BASE",
                page_number: 2,
                source_order: 2,
                bbox: [27, 0, 568, 842],
                locatable: true,
                default_visible: false,
                overlay_default: false,
                source_kind: "image",
                provenance: { channel: "NATIVE", xref: 11 },
              },
              {
                id: "page-raster-mask",
                type: "FIGURE",
                role: "SCAN_LAYER",
                label: "PGR-MASK",
                page_number: 2,
                source_order: 3,
                bbox: [27, 0, 568, 842],
                locatable: true,
                default_visible: false,
                overlay_default: false,
                source_kind: "image_mask",
                provenance: { channel: "NATIVE", xref: 12 },
              },
            ],
            issues: [
              {
                id: "warning-1",
                label: "ISS-001",
                code: "visual_gap",
                message: "Visual content may be incomplete.",
                severity: "warning",
                blocking: false,
                overlay: true,
                page_number: 2,
                bbox: [40, 80, 520, 120],
                locatable: true,
              },
              {
                id: "info-1",
                label: "ISS-002",
                code: "trace_recorded",
                message: "Technical trace only.",
                severity: "info",
                blocking: false,
                overlay: false,
                page_number: 2,
                locatable: false,
              },
            ],
            coverage: {
              remote_text: {
                evaluated: false,
                status: "NOT_EVALUATED",
                observation_count: 0,
              },
              remote_visual: { evaluated_types: [] },
            },
          },
        ],
      },
      tables: [],
    },
    2,
  );
}

function ocrPageSnapshot() {
  return normalizeRestorationPage(
    {
      source_identity: { source_sha256: "a".repeat(64) },
      page_comparison: {
        pages: [
          {
            page_number: 2,
            bounds: [0, 0, 595, 842],
            blocks: [
              {
                id: "native-text",
                type: "TEXT",
                label: "TXT-001",
                text: "native duplicate text",
                page_number: 2,
                source_order: 1,
                bbox: [40, 80, 520, 120],
                provenance: { channel: "NATIVE" },
              },
              {
                id: "native-figure",
                type: "FIGURE",
                label: "FIG-001",
                text: "native figure evidence",
                page_number: 2,
                source_order: 2,
                bbox: [40, 140, 520, 300],
                provenance: { channel: "NATIVE" },
              },
              {
                id: "remote-text",
                type: "TEXT",
                label: "TXT-002",
                text: "OCR adopted text",
                page_number: 2,
                source_order: 3,
                bbox: [40, 80, 520, 120],
                provenance: { channel: "REMOTE_VISUAL" },
              },
            ],
            issues: [],
            coverage: {
              remote_text: {
                evaluated: true,
                status: "OBSERVED",
                observation_count: 1,
              },
              remote_visual: { evaluated_types: ["TEXT"] },
            },
          },
        ],
      },
      tables: [],
    },
    2,
  );
}

function ocrScannedFigureMarkupSnapshot() {
  return normalizeRestorationPage(
    {
      source_identity: { source_sha256: "a".repeat(64) },
      page_comparison: {
        pages: [
          {
            page_number: 2,
            bounds: [0, 0, 595, 842],
            blocks: [
              {
                id: "page-raster",
                type: "PAGE_RASTER",
                label: "SCAN-001",
                page_number: 2,
                source_order: 1,
                bbox: [0, 0, 595, 842],
                provenance: { channel: "NATIVE" },
              },
              {
                id: "remote-text",
                type: "TEXT",
                label: "TXT-002",
                text: "OCR scanned text",
                page_number: 2,
                source_order: 2,
                bbox: [40, 80, 520, 120],
                provenance: { channel: "REMOTE_VISUAL" },
              },
            ],
            issues: [
              {
                id: "unassessed-image-content",
                code: "pdf_visual_figure_content_not_evaluated",
                message: "扫描页图片占位没有可验证几何，必须升级复杂视觉。",
                severity: "error",
                blocking: true,
                locatable: false,
              },
            ],
            coverage: {
              text_authority: {
                channel: "REMOTE_VISUAL",
                status: "READY",
                reason_codes: ["visual_text_snapshot_ready"],
              },
              remote_text: {
                evaluated: true,
                status: "OBSERVED",
                observation_count: 1,
              },
            },
          },
        ],
      },
      tables: [],
    },
    2,
  );
}

function ambiguousTextAuthorityPageSnapshot() {
  return normalizeRestorationPage(
    {
      source_identity: { source_sha256: "a".repeat(64) },
      page_comparison: {
        pages: [
          {
            page_number: 2,
            bounds: [0, 0, 595, 842],
            blocks: [
              {
                id: "native-ambiguous-text",
                type: "TEXT",
                label: "TXT-001",
                text: "native ambiguous text",
                page_number: 2,
                source_order: 1,
                bbox: [40, 80, 520, 120],
                provenance: { channel: "NATIVE" },
              },
              {
                id: "remote-ambiguous-text",
                type: "TEXT",
                label: "TXT-002",
                text: "OCR ambiguous text",
                page_number: 2,
                source_order: 2,
                bbox: [40, 80, 520, 120],
                provenance: { channel: "REMOTE_VISUAL" },
              },
            ],
            issues: [],
            coverage: {
              remote_text: {
                evaluated: true,
                status: "OBSERVED",
                observation_count: 2,
              },
            },
          },
        ],
      },
      tables: [],
    },
    2,
  );
}

function ocrTablePageSnapshot(
  textSource: "native_table_cell" | "visual_table_cell_fact" =
    "native_table_cell",
) {
  return normalizeRestorationPage(
    {
      source_identity: { source_sha256: "a".repeat(64) },
      page_comparison: {
        pages: [
          {
            page_number: 2,
            bounds: [0, 0, 595, 842],
            blocks: [
              {
                id: "native-table",
                type: "TABLE",
                label: "TBL-001",
                table_id: "table-2",
                page_number: 2,
                source_order: 1,
                bbox: [40, 140, 520, 300],
                provenance: { channel: "NATIVE" },
              },
              {
                id: "remote-text",
                type: "TEXT",
                label: "TXT-002",
                text: "OCR adopted table-area text",
                page_number: 2,
                source_order: 2,
                bbox: [40, 140, 520, 300],
                provenance: { channel: "REMOTE_VISUAL" },
              },
            ],
            issues: [],
            coverage: {
              text_authority: {
                channel: "REMOTE_VISUAL",
                status: "READY",
                reason_codes: ["visual_text_snapshot_ready"],
              },
              remote_text: {
                evaluated: true,
                status: "OBSERVED",
                observation_count: 1,
              },
            },
          },
        ],
      },
      tables: [
        {
          id: "table-2",
          source_node_id: "native-table",
          row_count: 1,
          column_count: 1,
          page_start: 2,
          page_end: 2,
          cells: [
            {
              id: "native-cell",
              row: 0,
              column: 0,
              text: "native secret cell text",
              text_source: textSource,
              page: 2,
            },
          ],
          reconstruction: {
            mode: "STRUCTURED_TEXT_GRID",
            merge_semantics: "PRESERVED",
            cell_geometry: "PRESERVED",
          },
          geometry: { status: "PRESERVED" },
          continuation: {
            logical_table_id: "table-2",
            status: "SINGLE",
            segment_index: 0,
            segment_count: 1,
          },
        },
      ],
    },
    2,
  );
}


function diagnostics() {
  return normalizeParsingDiagnostics({
    schema_version: "parsing-diagnostics/v1",
    status: "READY_WITH_RISKS",
    source_sha256: "a".repeat(64),
    active_parse_artifact_id: "b".repeat(64),
    parse_identity_sha256: "c".repeat(64),
    page_count: 4,
    hard_blocker_count: 0,
    severe_warning_count: 1,
    risk_count: 1,
    severe_issue_ids: ["warning-1"],
    blocking_issue_ids: [],
    unassigned_risk_count: 0,
    risk_pages: [
      {
        page_number: 2,
        hard_blocker_count: 0,
        severe_warning_count: 1,
        risk_ids: ["warning-1"],
        issue_codes: ["visual_gap"],
      },
    ],
    risks: [
      {
        id: "warning-1",
        code: "visual_gap",
        severity: "warning",
        blocking: false,
        stage: "SOURCE_IR",
        message: "Visual content may be incomplete.",
        page_number: 2,
      },
    ],
  });
}
