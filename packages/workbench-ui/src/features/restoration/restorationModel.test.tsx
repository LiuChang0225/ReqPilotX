import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, describe, expect, it, vi } from "vitest";
import type { JsonRecord } from "../../shared/api/contracts";
import { StructuredTable } from "./StructuredTable";
import {
  pageBlocksForEvidenceView,
  pageEvidenceDisposition,
  normalizeRestorationPage,
  processingReceiptForPage,
  sourceCropUrl,
  tableForBlock,
  type StructuredTableRecordFocus,
  type StructuredTableModel,
  type TableCell,
} from "./restorationModel";

const sourceSha256 = "a".repeat(64);

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function showModal() {
    Object.defineProperty(this, "open", {
      configurable: true,
      value: true,
      writable: true,
    });
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function close() {
    this.open = false;
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  };
});

function pagePayload(): JsonRecord {
  return {
    source_identity: { source_sha256: sourceSha256 },
    page_comparison: {
      pages: [
        {
          page_number: 3,
          bounds: [0, 0, 595, 842],
          blocks: [
            {
              id: "source-table",
              type: "TABLE",
              label: "TBL-003",
              page_number: 3,
              source_order: 2,
              table_id: "table-3",
              bbox: [40, 80, 540, 420],
              locatable: true,
              provenance: { channel: "NATIVE" },
            },
          ],
          issues: [],
        },
      ],
    },
    tables: [
      {
        id: "table-3",
        source_node_id: "source-table",
        row_count: 2,
        column_count: 2,
        page_start: 3,
        page_end: 3,
        header_regions: [
          {
            row_start: 0,
            row_end: 0,
            column_start: 0,
            column_end: 1,
          },
        ],
        cells: [
          {
            id: "cell-heading",
            row: 0,
            column: 0,
            column_span: 2,
            text: "项目要求",
            page: 3,
          },
          {
            id: "cell-name",
            row: 1,
            column: 0,
            text: "活塞杆直径",
            page: 3,
          },
          {
            id: "cell-value",
            row: 1,
            column: 1,
            text: "Φ16–22 mm",
            page: 3,
            images: [
              {
                id: "cell-image",
                asset_id: "asset-1",
                page: 3,
                bbox: [300, 180, 420, 260],
                available: true,
              },
            ],
          },
        ],
        notes: ["数值以正式图纸为准"],
        reconstruction: {
          mode: "STRUCTURED_TEXT_GRID",
          merge_semantics: "PRESERVED",
          cell_geometry: "PRESERVED",
        },
        geometry: {
          status: "PRESERVED",
          width_points: 500,
          height_points: 80,
          column_widths_points: [220, 280],
          row_heights_points: [32, 48],
        },
        continuation: {
          logical_table_id: "logical-table-3",
          status: "SINGLE",
          segment_index: 0,
          segment_count: 1,
        },
      },
    ],
  };
}

describe("restoration page model", () => {
  it("adopts OCR text only when page coverage and remote observations agree", () => {
    const payload = pagePayload();
    const page = (
      payload.page_comparison as { pages: JsonRecord[] }
    ).pages[0];
    page.blocks = [
      {
        id: "native-text",
        type: "TEXT",
        label: "TXT-001",
        text: "native duplicate",
        page_number: 3,
        source_order: 1,
        bbox: [40, 40, 500, 70],
        provenance: { channel: "NATIVE" },
      },
      ...(page.blocks as JsonRecord[]),
      {
        id: "remote-text",
        type: "TEXT",
        label: "TXT-002",
        text: "OCR adopted text",
        page_number: 3,
        source_order: 3,
        bbox: [40, 40, 500, 70],
        provenance: { channel: "REMOTE_VISUAL" },
      },
    ];
    page.coverage = {
      remote_text: {
        evaluated: true,
        status: "OBSERVED",
        observation_count: 1,
      },
      remote_visual: { evaluated_types: ["TEXT"] },
    };

    const snapshot = normalizeRestorationPage(payload, 3);

    expect(pageEvidenceDisposition(snapshot)).toMatchObject({
      adoptedTextChannel: "REMOTE_VISUAL",
      basis: "REMOTE_TEXT_OBSERVED",
      certain: true,
    });
    expect(
      pageBlocksForEvidenceView(snapshot, "EFFECTIVE").map((block) => block.id),
    ).toEqual(["source-table", "remote-text"]);
    expect(
      pageBlocksForEvidenceView(snapshot, "REMOTE_VISUAL").map(
        (block) => block.id,
      ),
    ).toEqual(["remote-text"]);
    expect(
      pageBlocksForEvidenceView(snapshot, "NATIVE").map((block) => block.id),
    ).toEqual(["native-text", "source-table"]);
  });

  it("keeps remote layout media audit-only while adopting native table and figure authority once", () => {
    const payload = pagePayload();
    const page = (
      payload.page_comparison as { pages: JsonRecord[] }
    ).pages[0];
    page.blocks = [
      {
        id: "native-header-logo",
        type: "FIGURE",
        source_kind: "image",
        label: "HDR-001",
        page_number: 3,
        source_order: 1,
        bbox: [40, 20, 120, 60],
        provenance: { channel: "NATIVE" },
        diagnostics: [
          {
            code: "pdf_repeated_header_image_furniture",
            severity: "info",
          },
        ],
      },
      ...(page.blocks as JsonRecord[]),
      {
        id: "native-figure",
        type: "FIGURE",
        label: "FIG-001",
        page_number: 3,
        source_order: 3,
        bbox: [40, 450, 260, 620],
        provenance: { channel: "NATIVE" },
      },
      {
        id: "remote-text",
        type: "TEXT",
        label: "TXT-002",
        text: "OCR adopted text",
        page_number: 3,
        source_order: 4,
        bbox: [40, 80, 500, 110],
        provenance: { channel: "REMOTE_VISUAL" },
      },
      {
        id: "remote-header",
        type: "HEADER",
        label: "HDR-002",
        text: "OCR header",
        page_number: 3,
        source_order: 5,
        bbox: [40, 20, 500, 60],
        provenance: { channel: "REMOTE_VISUAL" },
      },
      {
        id: "remote-table-duplicate",
        type: "TABLE",
        label: "TBL-002",
        page_number: 3,
        source_order: 6,
        bbox: [40, 80, 540, 420],
        provenance: { channel: "REMOTE_VISUAL" },
      },
      {
        id: "remote-figure-duplicate",
        type: "FIGURE",
        label: "FIG-002",
        page_number: 3,
        source_order: 7,
        bbox: [40, 450, 260, 620],
        provenance: { channel: "REMOTE_VISUAL" },
      },
    ];
    page.coverage = {
      remote_text: {
        evaluated: true,
        status: "OBSERVED",
        observation_count: 1,
      },
      remote_visual: {
        evaluated_types: ["TEXT", "HEADER", "TABLE", "FIGURE"],
      },
    };

    const snapshot = normalizeRestorationPage(payload, 3);

    expect(
      pageBlocksForEvidenceView(snapshot, "EFFECTIVE").map((block) => block.id),
    ).toEqual([
      "source-table",
      "native-figure",
      "remote-text",
      "remote-header",
    ]);
    expect(
      pageBlocksForEvidenceView(snapshot, "REMOTE_VISUAL").map(
        (block) => block.id,
      ),
    ).toEqual([
      "remote-text",
      "remote-header",
      "remote-table-duplicate",
      "remote-figure-duplicate",
    ]);
    expect(
      pageBlocksForEvidenceView(snapshot, "COMPARE").map((block) => block.id),
    ).toContain("native-header-logo");
  });

  it("adopts native facts only when coverage explicitly says OCR was not evaluated", () => {
    const payload = pagePayload();
    const page = (
      payload.page_comparison as { pages: JsonRecord[] }
    ).pages[0];
    page.coverage = {
      remote_text: {
        evaluated: false,
        status: "NOT_EVALUATED",
        observation_count: 0,
      },
      remote_visual: { evaluated_types: [] },
    };

    const snapshot = normalizeRestorationPage(payload, 3);

    expect(pageEvidenceDisposition(snapshot)).toMatchObject({
      adoptedTextChannel: "NATIVE",
      basis: "REMOTE_TEXT_NOT_EVALUATED",
      certain: true,
    });
    expect(pageBlocksForEvidenceView(snapshot, "EFFECTIVE")).toEqual(
      snapshot.blocks,
    );
  });

  it("keeps proven native page furniture in the raw view but out of the adopted body", () => {
    const payload = pagePayload();
    const page = (
      payload.page_comparison as { pages: JsonRecord[] }
    ).pages[0];
    (page.blocks as JsonRecord[]).unshift({
      id: "native-header-logo",
      type: "HEADER",
      source_kind: "image",
      label: "HDR-001",
      page_number: 3,
      source_order: 1,
      bbox: [40, 20, 120, 60],
      provenance: { channel: "NATIVE" },
    });
    page.coverage = {
      remote_text: {
        evaluated: false,
        status: "NOT_EVALUATED",
        observation_count: 0,
      },
      remote_visual: { evaluated_types: [] },
    };

    const snapshot = normalizeRestorationPage(payload, 3);

    expect(
      pageBlocksForEvidenceView(snapshot, "EFFECTIVE").map((block) => block.id),
    ).toEqual(["source-table"]);
    expect(
      pageBlocksForEvidenceView(snapshot, "NATIVE").map((block) => block.id),
    ).toEqual(["native-header-logo", "source-table"]);
  });

  it("hides adopted text when coverage contradicts blocks while preserving explicit raw views", () => {
    const payload = pagePayload();
    const page = (
      payload.page_comparison as { pages: JsonRecord[] }
    ).pages[0];
    page.coverage = {
      remote_text: {
        evaluated: true,
        status: "OBSERVED",
        observation_count: 2,
      },
      remote_visual: { evaluated_types: ["TEXT"] },
    };

    const snapshot = normalizeRestorationPage(payload, 3);

    expect(pageEvidenceDisposition(snapshot)).toMatchObject({
      adoptedTextChannel: "UNRESOLVED",
      basis: "REMOTE_COVERAGE_BLOCK_MISMATCH",
      effectiveTextPolicy: "HIDE",
      certain: false,
    });
    expect(
      pageBlocksForEvidenceView(snapshot, "EFFECTIVE").map((block) => block.id),
    ).toEqual(["source-table"]);
    expect(pageBlocksForEvidenceView(snapshot, "COMPARE")).toEqual(
      snapshot.blocks,
    );
  });

  it("hides every text channel when an OCR attempt failed instead of adopting native fallback text", () => {
    const payload = pagePayload();
    const page = (
      payload.page_comparison as { pages: JsonRecord[] }
    ).pages[0];
    page.blocks = [
      {
        id: "native-text-after-ocr-failure",
        type: "TEXT",
        label: "TXT-001",
        text: "must not become fallback authority",
        page_number: 3,
        source_order: 1,
        bbox: [40, 40, 500, 70],
        provenance: { channel: "NATIVE" },
      },
      ...(page.blocks as JsonRecord[]),
    ];
    page.coverage = {
      remote_text: {
        evaluated: false,
        status: "NOT_EVALUATED",
        observation_count: 0,
      },
      remote_visual: { evaluated_types: [] },
    };

    const snapshot = normalizeRestorationPage(payload, 3);

    expect(
      pageEvidenceDisposition(snapshot, { visualRecoveryFailed: true }),
    ).toMatchObject({
      adoptedTextChannel: "UNRESOLVED",
      basis: "REMOTE_VISUAL_DEGRADED",
      effectiveTextPolicy: "HIDE",
      certain: false,
    });
    expect(
      pageBlocksForEvidenceView(snapshot, "EFFECTIVE", false, {
        visualRecoveryFailed: true,
      }).map((block) => block.id),
    ).toEqual(["source-table"]);
    expect(
      pageBlocksForEvidenceView(snapshot, "NATIVE", false, {
        visualRecoveryFailed: true,
      }).map((block) => block.id),
    ).toEqual(["native-text-after-ocr-failure", "source-table"]);
  });

  it("uses explicit server text authority before legacy coverage heuristics", () => {
    const payload = pagePayload();
    const page = (
      payload.page_comparison as { pages: JsonRecord[] }
    ).pages[0];
    page.blocks = [
      ...(page.blocks as JsonRecord[]),
      {
        id: "remote-authoritative-text",
        type: "TEXT",
        label: "TXT-002",
        text: "server-authorized OCR",
        page_number: 3,
        source_order: 3,
        bbox: [40, 40, 500, 70],
        provenance: { channel: "REMOTE_VISUAL" },
      },
    ];
    page.coverage = {
      remote_text: {
        evaluated: false,
        status: "NOT_EVALUATED",
        observation_count: 0,
      },
      text_authority: {
        channel: "REMOTE_VISUAL",
        status: "READY",
        reason_codes: ["visual_text_snapshot_ready"],
      },
    };

    const snapshot = normalizeRestorationPage(payload, 3);

    expect(pageEvidenceDisposition(snapshot)).toMatchObject({
      adoptedTextChannel: "REMOTE_VISUAL",
      basis: "TEXT_AUTHORITY_OCR_READY",
      effectiveTextPolicy: "REMOTE_VISUAL",
      reasonCodes: ["visual_text_snapshot_ready"],
    });
  });

  it("fails closed when explicit OCR authority is not READY", () => {
    const payload = pagePayload();
    const page = (
      payload.page_comparison as { pages: JsonRecord[] }
    ).pages[0];
    page.coverage = {
      text_authority: {
        channel: "REMOTE_VISUAL",
        status: "FAILED",
        reason_codes: ["provider_timeout"],
      },
    };

    const snapshot = normalizeRestorationPage(payload, 3);

    expect(pageEvidenceDisposition(snapshot)).toMatchObject({
      adoptedTextChannel: "UNRESOLVED",
      basis: "TEXT_AUTHORITY_NOT_READY",
      effectiveTextPolicy: "HIDE",
      reasonCodes: ["provider_timeout"],
    });
    expect(
      pageBlocksForEvidenceView(snapshot, "EFFECTIVE").map((block) => block.id),
    ).toEqual(["source-table"]);
  });

  it("keeps the structured table contract instead of flattening it to text", () => {
    const snapshot = normalizeRestorationPage(pagePayload(), 3);
    const block = snapshot.blocks[0];
    const table = tableForBlock(block, snapshot.tables);

    expect(block.tableId).toBe("table-3");
    expect(table).not.toBeNull();
    expect(table?.geometryPreserved).toBe(true);
    expect(table?.cells[0]).toMatchObject({
      text: "项目要求",
      columnSpan: 2,
    });
    expect(snapshot.sourceSha256).toBe(sourceSha256);
  });

  it("builds the current extraction crop route with source identity", () => {
    expect(
      sourceCropUrl({
        projectId: "project",
        documentId: "document",
        sourceSha256,
        page: 3,
        bbox: [10, 20, 110, 220],
      }),
    ).toContain(
      "/api/projects/project/sor-documents/document/extraction/source-crop?",
    );
  });

  it("selects the exact page processing receipt without cross-page fallback", () => {
    const receipts = [
      {
        page_number: 2,
        state: "READY" as const,
        adopted_provider_id: "azure-document-intelligence",
      },
      {
        page_number: 3,
        state: "DEGRADED" as const,
        adopted_provider_id: "upstream:primary-parser",
        degraded_reason_codes: ["azure-gpt56-sol:unreachable"],
      },
    ];

    expect(processingReceiptForPage(receipts, 3)).toMatchObject({
      state: "DEGRADED",
      adopted_provider_id: "upstream:primary-parser",
    });
    expect(processingReceiptForPage(receipts, 4)).toBeNull();
  });

  it("folds physical scan layers into one logical page support while preserving audit metadata", () => {
    const payload = pagePayload();
    const page = (
      payload.page_comparison as { pages: JsonRecord[] }
    ).pages[0];
    page.blocks = [
      ...(page.blocks as JsonRecord[]),
      {
        id: "page-raster-base",
        type: "PAGE_RASTER",
        role: "SCAN_LAYER",
        label: "PGR-BASE",
        page_number: 3,
        source_order: 3,
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
        page_number: 3,
        source_order: 4,
        bbox: [27, 0, 568, 842],
        locatable: true,
        default_visible: false,
        overlay_default: false,
        source_kind: "image_mask",
        provenance: { channel: "NATIVE", xref: 12 },
      },
    ];
    page.issues = [
      {
        id: "issue-1",
        label: "ISS-001",
        code: "source_unreadable",
        message: "Source content is unreadable.",
        severity: "error",
        blocking: true,
        stage: "SOURCE_IR",
        overlay: true,
        page_number: 3,
        source_id: "source-table",
        location_status: "LOCATED",
        source_location: { page_number: 3 },
        bbox: [40, 80, 540, 420],
        locatable: true,
      },
    ];

    const snapshot = normalizeRestorationPage(payload, 3);

    expect(snapshot.blocks).toHaveLength(1);
    expect(snapshot.supportBlocks).toHaveLength(1);
    expect(snapshot.supportBlocks?.[0]).toMatchObject({
      id: "page-3-scan-support",
      type: "PAGE_RASTER",
      role: "SCAN_LAYER",
      label: "SCAN-P3",
      defaultVisible: false,
      overlayDefault: false,
      supportLayerCount: 2,
    });
    expect(snapshot.supportBlocks?.[0].bbox).toEqual([0, 0, 595, 842]);
    expect(snapshot.supportBlocks?.[0].supportLayers).toHaveLength(2);
    expect(snapshot.supportBlocks?.[0].supportLayers?.map((block) => block.id)).toEqual([
      "page-raster-base",
      "page-raster-mask",
    ]);
    expect(snapshot.physicalSupportBlocks?.map((block) => block.id)).toEqual([
      "page-raster-base",
      "page-raster-mask",
    ]);
    expect(snapshot.physicalSupportBlocks?.[1].raw).toMatchObject({
      type: "FIGURE",
      role: "SCAN_LAYER",
      source_kind: "image_mask",
    });
    expect(
      pageBlocksForEvidenceView(snapshot, "EFFECTIVE", false).map(
        (block) => block.id,
      ),
    ).toEqual(["source-table"]);
    expect(
      pageBlocksForEvidenceView(snapshot, "EFFECTIVE", true).map(
        (block) => block.id,
      ),
    ).toEqual(["source-table", "page-3-scan-support"]);
    expect(snapshot.issues[0]).toMatchObject({
      blocking: true,
      stage: "SOURCE_IR",
      overlay: true,
      pageNumber: 3,
      sourceId: "source-table",
      locationStatus: "LOCATED",
    });
  });

  it("does not fold ordinary figure or table facts into scan support", () => {
    const payload = pagePayload();
    const page = (
      payload.page_comparison as { pages: JsonRecord[] }
    ).pages[0];
    (page.blocks as JsonRecord[]).push(
      {
        id: "ordinary-figure",
        type: "FIGURE",
        role: "CONTENT",
        page_number: 3,
        source_order: 3,
        bbox: [80, 450, 320, 620],
        provenance: { channel: "NATIVE" },
      },
      {
        id: "ordinary-table",
        type: "TABLE",
        role: "CONTENT",
        page_number: 3,
        source_order: 4,
        bbox: [80, 640, 520, 800],
        provenance: { channel: "NATIVE" },
      },
    );

    const snapshot = normalizeRestorationPage(payload, 3);

    expect(snapshot.supportBlocks).toEqual([]);
    expect(snapshot.physicalSupportBlocks).toEqual([]);
    expect(snapshot.blocks.map((block) => block.id)).toEqual([
      "source-table",
      "ordinary-figure",
      "ordinary-table",
    ]);
  });

  it("retains server-collapsed physical layer count and source lineage", () => {
    const payload = pagePayload();
    const page = (
      payload.page_comparison as { pages: JsonRecord[] }
    ).pages[0];
    (page.blocks as JsonRecord[]).push({
      id: "logical-raster-from-server",
      type: "PAGE_RASTER",
      role: "SCAN_LAYER",
      page_number: 3,
      source_order: 3,
      bbox: [27, 0, 568, 842],
      physical_layer_count: 2,
      physical_layer_ids: ["native-image-base", "native-image-mask"],
      physical_layer_source_refs: [
        { kind: "source_node", source_id: "native-image-base" },
        { kind: "source_node", source_id: "native-image-mask" },
      ],
      provenance: { channel: "NATIVE" },
    });

    const snapshot = normalizeRestorationPage(payload, 3);
    const support = snapshot.supportBlocks?.[0];

    expect(support).toMatchObject({
      id: "page-3-scan-support",
      supportLayerCount: 2,
      supportLayerIds: ["native-image-base", "native-image-mask"],
      supportLayerSourceRefs: [
        { kind: "source_node", source_id: "native-image-base" },
        { kind: "source_node", source_id: "native-image-mask" },
      ],
    });
    expect(support?.supportLayers?.[0].raw).toMatchObject({
      physical_layer_count: 2,
      physical_layer_ids: ["native-image-base", "native-image-mask"],
    });
  });
});

describe("StructuredTable", () => {
  it("keeps a compact source row visible after its raw cells are merged for presentation", () => {
    const base = normalizeRestorationPage(pagePayload(), 3).tables[0];
    const table = { ...base, cells: base.cells.map(cell => cell.row === 1
      ? { ...cell, id: `merged-${cell.id}`, sourceCellIds: [cell.id, `${cell.id}-paint-fragment`] }
      : cell) };
    const focus: StructuredTableRecordFocus = {
      recordId: "source-record", recordLabel: "活塞杆直径", mode: "ROW", logicalTableId: "logical-table-3",
      rowBindings: [{ tableId: "table-3", index: 1, cellIds: ["cell-name", "cell-value"] }],
      columnBindings: [], headerCellIdsByTable: { "table-3": ["cell-heading"] }, headerTexts: [],
      sharedCellIdsByTable: {}, sharedValues: [], notes: [], imageCount: 1,
    };
    const view = render(<StructuredTable table={table} tables={[table]} recordFocus={focus} compactRecord
      projectId="project" documentId="document" sourceSha256={sourceSha256} onPageChange={vi.fn()} />);
    const row = view.container.querySelector('tr[aria-current="true"]');
    expect(row).not.toBeNull();
    expect(row).toHaveTextContent("活塞杆直径");
    expect(row?.querySelectorAll("td,th")).toHaveLength(2);
    expect(row?.querySelector("img")).not.toBeNull();
    expect(base.cells.some(cell => cell.id.startsWith("merged-"))).toBe(false);
    view.unmount();
  });

  it("keeps a 500-row table DOM bounded and advances only on demand without cutting a rowspan", async () => {
    const base = normalizeRestorationPage(pagePayload(), 3).tables[0];
    const cellTemplate = base.cells.find((cell) => cell.row === 1)!;
    const cells = Array.from({ length: 500 }, (_, row) => ({
      ...cellTemplate,
      id: `long-row-${row}`,
      row,
      rowSpan: row === 49 ? 3 : 1,
      text: `第 ${row} 行`,
      images: [],
    })).filter((cell) => ![50, 51].includes(cell.row));
    const table: StructuredTableModel = {
      ...base,
      id: "long-table",
      logicalTableId: "long-table",
      rowCount: 500,
      columnCount: 1,
      cells,
      headerRegions: [],
    };
    const user = userEvent.setup();
    const view = render(
      <StructuredTable
        table={table}
        tables={[table]}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        initialRowLimit={50}
      />,
    );
    const scope = within(view.container);

    expect(scope.getAllByRole("row")).toHaveLength(52);
    expect(scope.queryByText("第 499 行")).not.toBeInTheDocument();
    expect(scope.getByText(/合并单元格不会被截断/)).toBeInTheDocument();

    await user.click(scope.getByRole("button", { name: "再加载 50 行" }));
    expect(scope.getAllByRole("row")).toHaveLength(100);
    expect(scope.getByText("第 99 行")).toBeInTheDocument();
    view.unmount();
  });

  it("renders semantic cells, merge spans, geometry fidelity and source navigation", async () => {
    const snapshot = normalizeRestorationPage(pagePayload(), 3);
    const table = snapshot.tables[0];
    const onPageChange = vi.fn();
    const user = userEvent.setup();
    render(
      <StructuredTable
        table={table}
        tables={snapshot.tables}
        displayLabel="TBL-003"
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={onPageChange}
      />,
    );

    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "项目要求" })).toHaveAttribute(
      "colspan",
      "2",
    );
    expect(screen.getByText("活塞杆直径")).toBeInTheDocument();
    expect(screen.getByText("结构与比例已还原")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "展开表格" }));
    expect(
      screen.getByRole("dialog", { name: "TBL-003 · 表格完整视图" }),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "关闭表格完整视图" }),
    );

    await user.click(screen.getByRole("button", { name: "查看原文" }));
    expect(onPageChange).toHaveBeenCalledWith(3);
  });

  it("keeps the whole source table visible while highlighting one candidate row and its inherited context", () => {
    const snapshot = normalizeRestorationPage(pagePayload(), 3);
    const focus: StructuredTableRecordFocus = {
      recordId: "table-record-1",
      recordLabel: "活塞杆直径",
      mode: "ROW",
      logicalTableId: "logical-table-3",
      rowBindings: [
        {
          tableId: "table-3",
          index: 1,
          cellIds: ["cell-name", "cell-value"],
        },
      ],
      columnBindings: [
        { tableId: "table-3", index: 0, cellIds: ["cell-name"] },
        { tableId: "table-3", index: 1, cellIds: ["cell-value"] },
      ],
      headerCellIdsByTable: { "table-3": ["cell-heading"] },
      headerTexts: ["项目要求"],
      sharedCellIdsByTable: { "table-3": ["cell-heading"] },
      sharedValues: ["前后减振器"],
      notes: ["数值以正式图纸为准"],
      imageCount: 1,
    };

    const view = render(
      <StructuredTable
        table={snapshot.tables[0]}
        tables={snapshot.tables}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={focus}
      />,
    );

    const scope = within(view.container);

    expect(
      scope.getByRole("region", { name: "当前表格条目" }),
    ).toHaveTextContent("表格条目活塞杆直径未做语义判断");
    expect(scope.getByText("表头：项目要求")).toBeInTheDocument();
    expect(scope.getByText("共用值：前后减振器")).toBeInTheDocument();
    expect(scope.getByText("备注：数值以正式图纸为准")).toBeInTheDocument();
    expect(scope.getByText(/关联图片 1 项/)).toBeInTheDocument();
    expect(
      scope.getByRole("columnheader", { name: "项目要求" }),
    ).toHaveClass(
      "is-record-context-header",
      "is-record-context-shared",
    );
    expect(
      scope.getByRole("cell", { name: "活塞杆直径" }).closest("tr"),
    ).toHaveClass(
      "is-record-focus-row",
    );
    expect(scope.getByText("Φ16–22 mm").closest("td")).toHaveClass(
      "is-record-focus-cell",
    );
    expect(
      scope.getByRole("img", { name: "原 PDF 表格单元格中的图片" }),
    ).toBeInTheDocument();
  });

  it("shows only inherited context and the selected record before expanding the immutable whole table", async () => {
    const snapshot = normalizeRestorationPage(pagePayload(), 3);
    const table = {
      ...snapshot.tables[0],
      rowCount: 3,
      cells: [
        ...snapshot.tables[0].cells,
        {
          ...snapshot.tables[0].cells[1],
          id: "cell-unrelated-name",
          row: 2,
          text: "无关记录",
          images: [],
        },
        {
          ...snapshot.tables[0].cells[2],
          id: "cell-unrelated-value",
          row: 2,
          text: "完整原表仍保留",
          images: [],
        },
      ],
    };
    const focus: StructuredTableRecordFocus = {
      recordId: "table-record-compact",
      recordLabel: "活塞杆直径",
      mode: "ROW",
      logicalTableId: "logical-table-3",
      rowBindings: [
        {
          tableId: "table-3",
          index: 1,
          cellIds: ["cell-name", "cell-value"],
        },
      ],
      columnBindings: [
        { tableId: "table-3", index: 0, cellIds: ["cell-name"] },
        { tableId: "table-3", index: 1, cellIds: ["cell-value"] },
      ],
      headerCellIdsByTable: { "table-3": ["cell-heading"] },
      headerTexts: ["项目要求"],
      sharedCellIdsByTable: {},
      sharedValues: [],
      notes: [],
      imageCount: 1,
    };
    const user = userEvent.setup();
    const view = render(
      <StructuredTable
        table={table}
        tables={[table]}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={focus}
        compactRecord
      />,
    );
    const scope = within(view.container);

    expect(scope.getByText("当前表格条目")).toBeInTheDocument();
    expect(scope.getByText(/表头 \+ 当前内容/)).toBeInTheDocument();
    expect(scope.getAllByText("活塞杆直径")).toHaveLength(2);
    expect(
      scope.getByRole("img", { name: "原 PDF 表格单元格中的图片" }),
    ).toBeInTheDocument();
    expect(scope.queryByText("无关记录")).not.toBeInTheDocument();

    await user.click(scope.getByRole("button", { name: "放大本条" }));
    const focusedDialogs = screen.getAllByRole("dialog", { name: "当前表格 · 当前条目原表" });
    await user.click(within(focusedDialogs[focusedDialogs.length - 1]).getByRole("button", { name: "查看完整原表" }));
    const dialogs = screen.getAllByRole("dialog", {
      name: "当前表格 · 表格完整视图",
    });
    const dialog = dialogs[dialogs.length - 1];
    expect(within(dialog).getByText("无关记录")).toBeInTheDocument();
    expect(within(dialog).getByText("完整原表仍保留")).toBeInTheDocument();
  });

  it("preserves compact vertical merges and omits unrelated continuation segments", async () => {
    const base = normalizeRestorationPage(pagePayload(), 3).tables[0];
    const cellTemplate = base.cells[0];
    const header: StructuredTableModel = {
      ...base,
      id: "dvp-header",
      logicalTableId: "logical-dvp",
      page: 10,
      pageEnd: 10,
      rowCount: 1,
      columnCount: 4,
      segmentIndex: 0,
      segmentCount: 3,
      headerRegions: [{ row_start: 0, row_end: 0, column_start: 0, column_end: 3 }],
      columnWidthsPoints: [60, 120, 260, 260],
      rowHeightsPoints: [28],
      cells: ["试验编号", "试验名称", "试验方法", "技术要求"].map(
        (text, column) => ({
          ...cellTemplate,
          id: `dvp-header-${column}`,
          row: 0,
          column,
          rowSpan: 1,
          columnSpan: 1,
          text,
          header: true,
          images: [],
        }),
      ),
    };
    const unrelated: StructuredTableModel = {
      ...header,
      id: "dvp-unrelated",
      page: 11,
      pageEnd: 11,
      segmentIndex: 1,
      headerRegions: [],
      cells: [
        {
          ...cellTemplate,
          id: "dvp-unrelated-cell",
          row: 0,
          column: 0,
          rowSpan: 1,
          columnSpan: 4,
          text: "中间页无关内容",
          header: false,
          images: [],
        },
      ],
    };
    const lateCells = [
      {
        ...cellTemplate,
        id: "dvp-58-number",
        row: 0,
        column: 0,
        rowSpan: 14,
        columnSpan: 1,
        text: "58",
        header: false,
        images: [],
      },
      {
        ...cellTemplate,
        id: "dvp-58-name",
        row: 0,
        column: 1,
        rowSpan: 14,
        columnSpan: 1,
        text: "温湿度循环（仅防水件）",
        header: false,
        images: [],
      },
      ...Array.from({ length: 14 }, (_, row) => [
        {
          ...cellTemplate,
          id: `dvp-58-method-${row}`,
          row,
          column: 2,
          rowSpan: 1,
          columnSpan: 1,
          text: `方法 ${row + 1}`,
          header: false,
          images: [],
        },
        {
          ...cellTemplate,
          id: `dvp-58-criterion-${row}`,
          row,
          column: 3,
          rowSpan: 1,
          columnSpan: 1,
          text: `要求 ${row + 1}`,
          header: false,
          images: [],
        },
      ]).flat(),
    ];
    const late: StructuredTableModel = {
      ...header,
      id: "dvp-late",
      page: 14,
      pageEnd: 14,
      rowCount: 14,
      segmentIndex: 2,
      headerRegions: [],
      rowHeightsPoints: Array.from({ length: 14 }, () => 28),
      cells: lateCells,
    };
    const focus: StructuredTableRecordFocus = {
      recordId: "dvp-58",
      recordLabel: "58 温湿度循环（仅防水件）",
      mode: "ROW",
      logicalTableId: "logical-dvp",
      rowBindings: Array.from({ length: 14 }, (_, index) => ({
        tableId: "dvp-late",
        index,
        cellIds: [
          "dvp-58-number",
          "dvp-58-name",
          `dvp-58-method-${index}`,
          `dvp-58-criterion-${index}`,
        ],
      })),
      columnBindings: [],
      headerCellIdsByTable: {
        "dvp-header": header.cells.map((cell) => cell.id),
      },
      headerTexts: header.cells.map((cell) => cell.text),
      sharedCellIdsByTable: {},
      sharedValues: [],
      notes: [],
      imageCount: 0,
    };
    const user = userEvent.setup();
    const view = render(
      <StructuredTable
        table={header}
        tables={[header, unrelated, late]}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={focus}
        compactRecord
      />,
    );
    const scope = within(view.container);

    expect(scope.getAllByRole("table")).toHaveLength(2);
    expect(scope.queryByText("中间页无关内容")).not.toBeInTheDocument();
    expect(scope.getByRole("columnheader", { name: "试验编号" })).toBeVisible();
    expect(scope.getByRole("cell", { name: "58" })).toHaveAttribute(
      "rowspan",
      "14",
    );
    expect(
      scope.getByRole("cell", { name: "温湿度循环（仅防水件）" }),
    ).toHaveAttribute("rowspan", "14");
    expect(
      scope.getByText("当前条目 · 原表 · 第 3 段 · 第 14 页"),
    ).toBeVisible();

    await user.click(scope.getByRole("button", { name: "放大本条" }));
    const focusedDialogs = screen.getAllByRole("dialog", { name: "当前表格 · 当前条目原表" });
    await user.click(within(focusedDialogs[focusedDialogs.length - 1]).getByRole("button", { name: "查看完整原表" }));
    const dvpDialogs = screen.getAllByRole("dialog", {
      name: "当前表格 · 表格完整视图",
    });
    const dialog = dvpDialogs[dvpDialogs.length - 1];
    expect(within(dialog).getByText("中间页无关内容")).toBeInTheDocument();
    expect(within(dialog).getByRole("cell", { name: "58" })).toHaveAttribute(
      "rowspan",
      "14",
    );
  });

  it("does not leak sibling cells from a shared merge anchor row", () => {
    const base = normalizeRestorationPage(pagePayload(), 3).tables[0];
    const template = base.cells[0];
    const table: StructuredTableModel = {
      ...base,
      id: "connector-table",
      logicalTableId: "connector-logical",
      rowCount: 3,
      columnCount: 3,
      headerRegions: [
        { rowStart: 0, rowEnd: 0, columnStart: 0, columnEnd: 2 },
      ],
      columnWidthsPoints: [120, 180, 260],
      rowHeightsPoints: [28, 28, 28],
      cells: [
        ...["接口类型", "项目", "要求"].map((text, column) => ({
          ...template,
          id: `connector-header-${column}`,
          row: 0,
          column,
          rowSpan: 1,
          columnSpan: 1,
          text,
          header: true,
          images: [],
        })),
        {
          ...template,
          id: "shared-self-tooling",
          row: 1,
          column: 0,
          rowSpan: 2,
          columnSpan: 1,
          text: "自开模",
          header: false,
          images: [],
        },
        {
          ...template,
          id: "unrelated-mold-number",
          row: 1,
          column: 1,
          rowSpan: 1,
          columnSpan: 1,
          text: "模具号",
          header: false,
          images: [],
        },
        {
          ...template,
          id: "unrelated-mold-requirement",
          row: 1,
          column: 2,
          rowSpan: 1,
          columnSpan: 1,
          text: "提供模具号",
          header: false,
          images: [],
        },
        {
          ...template,
          id: "selected-item",
          row: 2,
          column: 1,
          rowSpan: 1,
          columnSpan: 1,
          text: "端子位置度/端子高度",
          header: false,
          images: [],
        },
        {
          ...template,
          id: "selected-requirement",
          row: 2,
          column: 2,
          rowSpan: 1,
          columnSpan: 1,
          text: "标识并确认满足",
          header: false,
          images: [],
        },
      ],
    };
    const focus: StructuredTableRecordFocus = {
      recordId: "connector-terminal-position",
      recordLabel: "端子位置度/端子高度",
      mode: "ROW",
      logicalTableId: "connector-logical",
      rowBindings: [
        {
          tableId: "connector-table",
          index: 2,
          cellIds: [
            "shared-self-tooling",
            "selected-item",
            "selected-requirement",
          ],
        },
      ],
      columnBindings: [],
      headerCellIdsByTable: {
        "connector-table": [
          "connector-header-0",
          "connector-header-1",
          "connector-header-2",
        ],
      },
      headerTexts: [],
      sharedCellIdsByTable: {
        "connector-table": ["shared-self-tooling"],
      },
      sharedValues: [],
      notes: [],
      imageCount: 0,
    };
    const view = render(
      <StructuredTable
        table={table}
        tables={[table]}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={focus}
        compactRecord
      />,
    );
    const scope = within(view.container);

    expect(scope.getByRole("cell", { name: "自开模" })).toBeVisible();
    expect(
      scope.getByRole("cell", { name: "端子位置度/端子高度" }),
    ).toBeVisible();
    expect(
      scope.getByRole("row", {
        name: "自开模 端子位置度/端子高度 标识并确认满足",
      }),
    ).toBeVisible();
    expect(scope.queryByText("模具号")).not.toBeInTheDocument();
    expect(scope.queryByText("提供模具号")).not.toBeInTheDocument();
  });

  it("rebuilds a selected leaf row from vertical merges and the nearest header group", () => {
    const base = normalizeRestorationPage(pagePayload(), 3).tables[0];
    const template = base.cells[0];
    const cell = (
      id: string,
      text: string,
      row: number,
      column: number,
      options: Partial<TableCell> = {},
    ): TableCell => ({
      ...template,
      id,
      text,
      row,
      column,
      rowSpan: 1,
      columnSpan: 1,
      header: false,
      images: [],
      ...options,
    });
    const table: StructuredTableModel = {
      ...base,
      id: "connector-evaluation",
      logicalTableId: "connector-evaluation-logical",
      rowCount: 8,
      columnCount: 3,
      headerRegions: [],
      columnWidthsPoints: [140, 180, 260],
      rowHeightsPoints: [28, 28, 28, 28, 28, 28, 28, 28],
      cells: [
        cell("table-title", "整车用电器与线束匹配评估表", 0, 0, {
          columnSpan: 3,
          header: true,
        }),
        cell("old-section", "整车应用基本信息", 1, 0, {
          columnSpan: 3,
          header: true,
        }),
        cell("old-header-0", "旧项目", 2, 0, { header: true }),
        cell("old-header-1", "旧值", 2, 1, { header: true }),
        cell("old-header-2", "旧说明", 2, 2, { header: true }),
        cell("old-data", "上一分区内容", 3, 0, { columnSpan: 3 }),
        cell("current-section", "用电器端基本信息", 4, 0, {
          columnSpan: 3,
          header: true,
        }),
        cell("current-header-0", "接口类型", 5, 0, { header: true }),
        cell("current-header-1", "项目", 5, 1, { header: true }),
        cell("current-header-2", "要求", 5, 2, { header: true }),
        cell("shared-interface", "非自开模", 6, 0, { rowSpan: 2 }),
        cell("previous-item", "板端或甩线端型号", 6, 1),
        cell("shared-requirement", "提供相关型号信息", 6, 2, {
          rowSpan: 2,
        }),
        cell("selected-terminal", "端子信息", 7, 1),
      ],
    };
    const allHeaderIds = table.cells
      .filter((value) => value.header)
      .map((value) => value.id);
    const focus: StructuredTableRecordFocus = {
      recordId: "terminal-information",
      recordLabel: "非自开模—端子信息",
      mode: "ROW",
      logicalTableId: table.logicalTableId,
      rowBindings: [
        {
          tableId: table.id,
          index: 7,
          cellIds: [
            "shared-interface",
            "shared-requirement",
            "selected-terminal",
          ],
        },
      ],
      columnBindings: [],
      headerCellIdsByTable: { [table.id]: allHeaderIds },
      headerTexts: [],
      sharedCellIdsByTable: {
        [table.id]: ["shared-interface", "shared-requirement"],
      },
      sharedValues: [],
      notes: [],
      imageCount: 0,
    };
    const view = render(
      <StructuredTable
        table={table}
        tables={[table]}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={focus}
        compactRecord
      />,
    );
    const scope = within(view.container);

    expect(scope.queryByText("整车用电器与线束匹配评估表")).toBeNull();
    expect(scope.queryByText("整车应用基本信息")).toBeNull();
    expect(scope.queryByText("旧项目")).toBeNull();
    expect(scope.queryByText("上一分区内容")).toBeNull();
    expect(scope.getByText("用电器端基本信息")).toBeVisible();
    expect(scope.getByRole("rowheader", { name: "接口类型" })).toBeVisible();
    expect(
      scope.getByRole("row", {
        name: "非自开模 端子信息 提供相关型号信息",
      }),
    ).toBeVisible();
    expect(scope.queryByText("板端或甩线端型号")).toBeNull();
  });

  it.each([false, true])("retains declared headers after a governing statement, including when headers are selected (%s)", (includeHeaderRow) => {
    const base = normalizeRestorationPage(pagePayload(), 3).tables[0];
    const cell = (id: string, text: string, row: number, column: number, columnSpan = 1): TableCell => ({
      ...base.cells[0], id, text, row, column, rowSpan: 1, columnSpan, header: false, images: [],
    });
    const table: StructuredTableModel = { ...base, id: "connector", logicalTableId: "connector", rowCount: 3, columnCount: 3,
      headerRegions: [], rowHeightsPoints: [28, 24, 30], columnWidthsPoints: [90, 160, 160],
      cells: [cell("governing", "Use this terminal assignment", 0, 0, 3),
        cell("terminal", "Terminal", 1, 0), cell("circuit", "Circuit", 1, 1), cell("orientation", "Mating-face view", 1, 2),
        cell("pin1", "1", 2, 0), cell("supply", "24 V supply", 2, 1), cell("view", "Latch up", 2, 2)] };
    const focus: StructuredTableRecordFocus = { recordId: "record", recordLabel: "Complete connector", logicalTableId: table.id, mode: "ROW",
      rowBindings: (includeHeaderRow ? [0, 1, 2] : [0, 2]).map(index => ({ tableId: table.id, index, cellIds: table.cells.filter(c => c.row === index).map(c => c.id) })),
      columnBindings: [], headerCellIdsByTable: { [table.id]: ["terminal", "circuit", "orientation"] }, headerTexts: [],
      sharedCellIdsByTable: {}, sharedValues: [], notes: [], imageCount: 0 };
    const view = render(<StructuredTable table={table} tables={[table]} projectId="project" documentId="document"
      sourceSha256={sourceSha256} onPageChange={vi.fn()} recordFocus={focus} compactRecord />);
    const scope = within(view.container);
    expect(scope.getAllByText("Terminal")).toHaveLength(1);
    expect(scope.getAllByText("Circuit")).toHaveLength(1);
    expect(scope.getAllByText("Mating-face view")).toHaveLength(1);
    expect(scope.getByRole("table").querySelectorAll("tr")).toHaveLength(3);
    expect(scope.getByRole("table").querySelector("tr")?.textContent).toBe("Use this terminal assignment");
    expect(scope.getByText("24 V supply")).toBeVisible();
  });

  it("keeps only floating images that overlap the selected compact record", async () => {
    const base = normalizeRestorationPage(pagePayload(), 3).tables[0];
    const table: StructuredTableModel = {
      ...base,
      floatingImages: [
        {
          id: "selected-floating-image",
          assetId: "selected-asset",
          mediaType: "image/png",
          page: 3,
          bbox: [10, 40, 80, 90],
          available: true,
          sourceRowIndexes: [1],
        },
        {
          id: "unrelated-floating-image",
          assetId: "unrelated-asset",
          mediaType: "image/png",
          page: 4,
          bbox: [10, 100, 80, 150],
          available: true,
          sourceRowIndexes: [2],
        },
      ],
    };
    const focus: StructuredTableRecordFocus = {
      recordId: "table-record-floating-image",
      recordLabel: "活塞杆直径",
      mode: "ROW",
      logicalTableId: table.logicalTableId,
      rowBindings: [
        {
          tableId: table.id,
          index: 1,
          cellIds: ["cell-name", "cell-value"],
        },
      ],
      columnBindings: [],
      headerCellIdsByTable: { [table.id]: ["cell-heading"] },
      headerTexts: ["项目要求"],
      sharedCellIdsByTable: {},
      sharedValues: [],
      notes: [],
      imageCount: 1,
    };
    const user = userEvent.setup();
    const view = render(
      <StructuredTable
        table={table}
        tables={[table]}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={focus}
        compactRecord
      />,
    );
    const scope = within(view.container);

    const compactFloating = scope.getByRole("region", {
      name: "跨单元格图片",
    });
    expect(
      within(compactFloating).getByRole("button", {
        name: "图片来源 · 第 3 页",
      }),
    ).toBeVisible();
    expect(
      scope.queryByRole("button", { name: "图片来源 · 第 4 页" }),
    ).not.toBeInTheDocument();

    await user.click(scope.getByRole("button", { name: "放大本条" }));
    const focusedDialogs = screen.getAllByRole("dialog", { name: "当前表格 · 当前条目原表" });
    await user.click(within(focusedDialogs[focusedDialogs.length - 1]).getByRole("button", { name: "查看完整原表" }));
    const dialogs = screen.getAllByRole("dialog", {
      name: "当前表格 · 表格完整视图",
    });
    const dialog = dialogs[dialogs.length - 1];
    const expandedFloating = within(dialog).getByRole("region", {
      name: "跨单元格图片",
    });
    expect(
      within(expandedFloating).getByRole("button", {
        name: "图片来源 · 第 3 页",
      }),
    ).toBeVisible();
    expect(
      within(expandedFloating).getByRole("button", {
        name: "图片来源 · 第 4 页",
      }),
    ).toBeVisible();
  });

  it("highlights only the selected matrix intersection while retaining row and column axes", () => {
    const snapshot = normalizeRestorationPage(pagePayload(), 3);
    const focus: StructuredTableRecordFocus = {
      recordId: "table-record-matrix",
      recordLabel: "活塞杆直径 × 参数值",
      mode: "MATRIX_INTERSECTION",
      logicalTableId: "logical-table-3",
      rowBindings: [
        {
          tableId: "table-3",
          index: 1,
          cellIds: ["cell-name", "cell-value"],
        },
      ],
      columnBindings: [
        {
          tableId: "table-3",
          index: 1,
          cellIds: ["cell-heading", "cell-value"],
        },
      ],
      headerCellIdsByTable: { "table-3": ["cell-heading", "cell-name"] },
      headerTexts: ["活塞杆直径", "参数值"],
      sharedCellIdsByTable: {},
      sharedValues: [],
      notes: [],
      imageCount: 1,
    };

    const view = render(
      <StructuredTable
        table={snapshot.tables[0]}
        tables={snapshot.tables}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={focus}
      />,
    );

    const scope = within(view.container);

    expect(scope.getByText("Φ16–22 mm").closest("td")).toHaveClass(
      "is-record-focus-cell",
      "is-record-axis-row",
      "is-record-axis-column",
    );
    expect(scope.getByRole("cell", { name: "活塞杆直径" })).toHaveClass(
      "is-record-axis-row",
    );
    expect(scope.getByRole("cell", { name: "活塞杆直径" })).not.toHaveClass(
      "is-record-focus-cell",
    );
    expect(scope.getByRole("table")).toHaveTextContent(
      "项目要求活塞杆直径Φ16–22 mm",
    );
  });
});
