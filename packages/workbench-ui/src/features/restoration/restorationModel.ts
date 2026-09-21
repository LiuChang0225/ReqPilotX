import { asArray, asRecord } from "../../shared/api/client";
import type {
  JsonRecord,
  PageProcessingReceipt,
} from "../../shared/api/contracts";
import { compactText } from "../../shared/lib/format";

export const filterableContentTypes = [
  "TEXT",
  "TABLE",
  "FIGURE",
  "HEADER",
  "FOOTER",
  "WATERMARK",
  "UNKNOWN",
] as const;

export type ContentType =
  | (typeof filterableContentTypes)[number]
  | "PAGE_RASTER";

export interface PageBlock {
  id: string;
  type: ContentType;
  role: string;
  label: string;
  text: string;
  bbox: number[];
  locatable: boolean;
  locationStatus: string;
  sourceOrder: number;
  pageNumber: number;
  tableId: string;
  provenance: JsonRecord;
  diagnostics: JsonRecord[];
  defaultVisible?: boolean;
  overlayDefault?: boolean;
  sourceKind?: string;
  sourceLocation?: JsonRecord;
  /** Original payload retained for scan-layer audit; never treated as content. */
  raw?: JsonRecord;
  /** Present only on the logical PAGE_RASTER support block. */
  supportLayerCount?: number;
  /** Physical source-node identities retained by the server presenter. */
  supportLayerIds?: string[];
  /** Source-bound lineage for physical layers in a collapsed server artifact. */
  supportLayerSourceRefs?: JsonRecord[];
  /** Physical PDF image layers folded into the logical support block. */
  supportLayers?: PageBlock[];
}

export interface PageIssue {
  id: string;
  code: string;
  message: string;
  severity: string;
  bbox: number[];
  locatable: boolean;
  blocking: boolean;
  stage: string;
  label: string;
  overlay: boolean;
  pageNumber: number;
  sourceId: string;
  locationStatus: string;
  sourceLocation: JsonRecord;
}

export interface TableCellImage {
  id: string;
  assetId: string;
  mediaType: string;
  page: number;
  bbox: number[];
  available: boolean;
  sourceRowIndexes?: number[];
}

export interface TableCell {
  id: string;
  sourceCellIds?: string[];
  row: number;
  column: number;
  rowSpan: number;
  columnSpan: number;
  text: string;
  horizontalAlignment: "LEFT" | "CENTER" | "RIGHT" | "JUSTIFY" | "";
  verticalAlignment: "TOP" | "MIDDLE" | "BOTTOM" | "";
  indentLevel: number;
  page: number;
  header: boolean;
  textSource: string;
  images: TableCellImage[];
}

export interface StructuredTableModel {
  id: string;
  sourceNodeId: string;
  sourceOrder: number;
  rowCount: number;
  columnCount: number;
  headerRegions: JsonRecord[];
  cells: TableCell[];
  floatingImages?: TableCellImage[];
  notes: string[];
  page: number;
  pageEnd: number;
  reconstructionMode: string;
  mergeSemantics: string;
  geometryPreserved: boolean;
  widthPoints: number;
  heightPoints: number;
  columnWidthsPoints: number[];
  rowHeightsPoints: number[];
  logicalTableId: string;
  continuationStatus: string;
  segmentIndex: number;
  segmentCount: number;
}

export interface StructuredTableAxisBinding {
  tableId: string;
  index: number;
  cellIds: string[];
}

export interface StructuredTableRecordFocus {
  recordId: string;
  recordLabel: string;
  mode: "ROW" | "MATRIX_INTERSECTION";
  logicalTableId: string;
  rowBindings: StructuredTableAxisBinding[];
  columnBindings: StructuredTableAxisBinding[];
  headerCellIdsByTable: Record<string, string[]>;
  headerTexts: string[];
  sharedCellIdsByTable: Record<string, string[]>;
  sharedValues: string[];
  notes: string[];
  imageCount: number;
  classificationLabel?: string;
}

export interface PageSnapshot {
  blocks: PageBlock[];
  supportBlocks?: PageBlock[];
  /** Immutable physical scan layers retained for old-artifact audit. */
  physicalSupportBlocks?: PageBlock[];
  issues: PageIssue[];
  tables: StructuredTableModel[];
  bounds: number[];
  counts: JsonRecord;
  coverage: JsonRecord;
  textAuthority?: PageTextAuthorityReceipt;
  sourceSha256: string;
}

export interface PageTextAuthorityReceipt {
  declared: boolean;
  channel: "REMOTE_VISUAL" | "NATIVE" | "UNKNOWN";
  status: "READY" | "EMPTY" | "FAILED" | "PENDING" | "UNKNOWN";
  reasonCodes: string[];
}

export type PageEvidenceView =
  | "EFFECTIVE"
  | "REMOTE_VISUAL"
  | "NATIVE"
  | "COMPARE";

export type PageEvidenceChannel = "REMOTE_VISUAL" | "NATIVE" | "UNKNOWN";

export type PageEvidenceDispositionBasis =
  | "TEXT_AUTHORITY_OCR_READY"
  | "TEXT_AUTHORITY_NATIVE_READY"
  | "TEXT_AUTHORITY_NOT_READY"
  | "TEXT_AUTHORITY_BLOCK_MISMATCH"
  | "REMOTE_VISUAL_DEGRADED"
  | "REMOTE_TEXT_OBSERVED"
  | "REMOTE_TEXT_NOT_EVALUATED"
  | "REMOTE_COVERAGE_BLOCK_MISMATCH"
  | "REMOTE_TEXT_NOT_OBSERVED"
  | "COVERAGE_UNAVAILABLE";

export interface PageEvidenceDisposition {
  adoptedTextChannel: PageEvidenceChannel | "UNRESOLVED";
  basis: PageEvidenceDispositionBasis;
  certain: boolean;
  effectiveTextPolicy: "REMOTE_VISUAL" | "NATIVE" | "COMPARE" | "HIDE";
  reasonCodes: string[];
  nativeBlockCount: number;
  remoteVisualBlockCount: number;
  unknownBlockCount: number;
}

export interface SourceCropGeometry {
  page: number;
  bbox: number[];
}

export interface PageEvidenceSelectionOptions {
  /** Fallback for pre-text-authority artifacts only. */
  visualRecoveryFailed?: boolean;
}

function firstDefined(...values: unknown[]): unknown {
  return values.find((value) => value !== undefined && value !== null);
}

export function numberFrom(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function processingReceiptForPage(
  receipts: PageProcessingReceipt[],
  page: number,
): PageProcessingReceipt | null {
  const pageNumber = Math.trunc(numberFrom(page));
  if (pageNumber < 1) return null;
  return (
    receipts.find(
      (receipt) => Math.trunc(numberFrom(receipt.page_number)) === pageNumber,
    ) || null
  );
}

function wholeNumber(value: unknown, fallback = 0): number {
  return Math.max(0, Math.trunc(numberFrom(value, fallback)));
}

function positiveInt(value: unknown): number {
  const parsed = Math.trunc(numberFrom(value));
  return parsed > 0 ? parsed : 0;
}

export function bboxOf(value: unknown): number[] {
  if (!Array.isArray(value) || value.length !== 4) return [];
  const bbox = value.map(Number);
  return bbox.every(Number.isFinite) &&
    bbox[2] > bbox[0] &&
    bbox[3] > bbox[1]
    ? bbox
    : [];
}

function positiveNumberArray(value: unknown): number[] {
  return asArray<unknown>(value)
    .map(Number)
    .filter((item) => Number.isFinite(item) && item > 0);
}

function stringArray(value: unknown): string[] {
  return asArray<unknown>(value)
    .map((item) => compactText(item, ""))
    .filter(Boolean);
}

function normalizeType(value: unknown): ContentType {
  const next = String(value || "UNKNOWN").toUpperCase();
  if (next === "PAGE_RASTER") return next;
  return filterableContentTypes.includes(
    next as (typeof filterableContentTypes)[number],
  )
    ? (next as (typeof filterableContentTypes)[number])
    : "UNKNOWN";
}

function isScanSupportBlock(block: PageBlock): boolean {
  return (
    block.type === "PAGE_RASTER" ||
    String(block.role || "").trim().toUpperCase() === "SCAN_LAYER"
  );
}

function unionBbox(blocks: PageBlock[]): number[] {
  const boxes = blocks
    .map((block) => block.bbox)
    .filter((bbox) => bbox.length === 4);
  if (!boxes.length) return [];
  return [
    Math.min(...boxes.map((bbox) => bbox[0])),
    Math.min(...boxes.map((bbox) => bbox[1])),
    Math.max(...boxes.map((bbox) => bbox[2])),
    Math.max(...boxes.map((bbox) => bbox[3])),
  ];
}

function logicalScanSupportBlock({
  page,
  pageBounds,
  physicalLayers,
}: {
  page: number;
  pageBounds: number[];
  physicalLayers: PageBlock[];
}): PageBlock | null {
  if (!physicalLayers.length) return null;
  const bbox =
    pageBounds.length === 4 ? [...pageBounds] : unionBbox(physicalLayers);
  const channels = new Set(physicalLayers.map(pageBlockEvidenceChannel));
  const channel = channels.size === 1 ? [...channels][0] : "UNKNOWN";
  const layerIds = Array.from(
    new Set(
      physicalLayers.flatMap((layer) =>
        layer.supportLayerIds?.length ? layer.supportLayerIds : [layer.id],
      ),
    ),
  );
  const layerSourceRefs = physicalLayers.flatMap((layer) =>
    layer.supportLayerSourceRefs?.length
      ? layer.supportLayerSourceRefs
      : Object.keys(asRecord(layer.raw?.source_ref)).length
        ? [asRecord(layer.raw?.source_ref)]
        : [],
  );
  const supportLayerCount = Math.max(
    physicalLayers.length,
    layerIds.length,
    layerSourceRefs.length,
    ...physicalLayers.map((layer) => layer.supportLayerCount || 0),
  );
  return {
    id: `page-${page}-scan-support`,
    type: "PAGE_RASTER",
    role: "SCAN_LAYER",
    label: `SCAN-P${page}`,
    text: "",
    bbox,
    locatable: bbox.length === 4,
    locationStatus: bbox.length === 4 ? "LOCATED" : "",
    sourceOrder: Math.min(...physicalLayers.map((layer) => layer.sourceOrder)),
    pageNumber: page,
    tableId: "",
    provenance: {
      channel,
      physical_layer_count: supportLayerCount,
    },
    diagnostics: [],
    defaultVisible: false,
    overlayDefault: false,
    sourceKind: "PAGE_SUPPORT_GROUP",
    sourceLocation: {
      page_number: page,
      bbox,
      physical_layer_ids: layerIds,
    },
    supportLayerCount,
    supportLayerIds: layerIds,
    supportLayerSourceRefs: layerSourceRefs,
    supportLayers: physicalLayers,
  };
}

function normalizeCellImage(
  raw: JsonRecord,
  tableId: string,
  cellIndex: number,
  imageIndex: number,
): TableCellImage {
  const location = asRecord(
    firstDefined(raw.location, raw.locator, raw.native_locator),
  );
  const bbox = bboxOf(location.bbox ?? raw.bbox);
  return {
    id: String(
      firstDefined(
        raw.id,
        raw.source_node_id,
        raw.asset_id,
        `${tableId}-cell-${cellIndex + 1}-image-${imageIndex + 1}`,
      ),
    ),
    assetId: String(raw.asset_id || ""),
    mediaType: String(raw.media_type || "image/png"),
    page: positiveInt(firstDefined(raw.page, raw.page_number, location.page_number)),
    bbox,
    sourceRowIndexes: asArray(raw.source_row_indexes)
      .map((value) => Number(value))
      .filter((value) => Number.isInteger(value) && value >= 0),
    available: Boolean(
      firstDefined(asRecord(raw.rendering).available, raw.available, bbox.length === 4),
    ),
  };
}

export function normalizeStructuredTable(
  raw: JsonRecord,
  index = 0,
): StructuredTableModel {
  const location = asRecord(
    firstDefined(raw.location, raw.locator, raw.native_locator),
  );
  const reconstruction = asRecord(
    firstDefined(raw.reconstruction, raw.reconstruction_status),
  );
  const geometry = asRecord(firstDefined(raw.geometry, raw.grid_geometry));
  const continuation = asRecord(
    firstDefined(raw.continuation, raw.continuation_status),
  );
  const id = String(
    firstDefined(raw.id, raw.table_id, raw.source_node_id, `table-${index + 1}`),
  );
  const cells = asArray<JsonRecord>(raw.cells).map<TableCell>((cell, cellIndex) => {
    const cellLocation = asRecord(
      firstDefined(cell.location, cell.locator, cell.native_locator),
    );
    const horizontal = String(cell.horizontal_alignment || "").toUpperCase();
    const vertical = String(cell.vertical_alignment || "").toUpperCase();
    return {
      id: String(firstDefined(cell.id, cell.cell_id, `${id}-cell-${cellIndex + 1}`)),
      sourceCellIds: asArray<string>(firstDefined(cell.source_cell_ids, cell.sourceCellIds)).map(String),
      row: wholeNumber(firstDefined(cell.row, cell.row_index)),
      column: wholeNumber(firstDefined(cell.column, cell.column_index)),
      rowSpan: Math.max(
        1,
        wholeNumber(firstDefined(cell.row_span, cell.rowSpan), 1),
      ),
      columnSpan: Math.max(
        1,
        wholeNumber(
          firstDefined(cell.column_span, cell.col_span, cell.colSpan),
          1,
        ),
      ),
      text: compactText(
        firstDefined(cell.text, cell.raw_text, cell.display_value, cell.formula),
        "",
      ),
      horizontalAlignment: ["LEFT", "CENTER", "RIGHT", "JUSTIFY"].includes(
        horizontal,
      )
        ? (horizontal as TableCell["horizontalAlignment"])
        : "",
      verticalAlignment: ["TOP", "MIDDLE", "BOTTOM"].includes(vertical)
        ? (vertical as TableCell["verticalAlignment"])
        : "",
      indentLevel: Math.min(20, wholeNumber(cell.indent_level)),
      page: positiveInt(
        firstDefined(cell.page, cell.page_number, cellLocation.page_number),
      ),
      header: Boolean(firstDefined(cell.is_header, cell.header, false)),
      textSource: String(
        firstDefined(cell.text_source, cell.textSource, "native_table_cell"),
      ),
      images: asArray<JsonRecord>(cell.images).map((image, imageIndex) =>
        normalizeCellImage(image, id, cellIndex, imageIndex),
      ),
    };
  });
  const inferredRows = cells.reduce(
    (maximum, cell) => Math.max(maximum, cell.row + cell.rowSpan),
    0,
  );
  const inferredColumns = cells.reduce(
    (maximum, cell) => Math.max(maximum, cell.column + cell.columnSpan),
    0,
  );
  const rowCount = wholeNumber(raw.row_count, inferredRows) || inferredRows;
  const columnCount =
    wholeNumber(raw.column_count, inferredColumns) || inferredColumns;
  const columnWidthsPoints = positiveNumberArray(
    firstDefined(geometry.column_widths_points, geometry.columnWidthsPoints),
  );
  const rowHeightsPoints = positiveNumberArray(
    firstDefined(geometry.row_heights_points, geometry.rowHeightsPoints),
  );
  const geometryStatus = String(
    firstDefined(
      geometry.status,
      reconstruction.cell_geometry,
      "NOT_AVAILABLE",
    ),
  ).toUpperCase();

  return {
    id,
    sourceNodeId: String(firstDefined(raw.source_node_id, id)),
    sourceOrder: numberFrom(firstDefined(raw.source_order, raw.order), index),
    rowCount,
    columnCount,
    headerRegions: asArray<JsonRecord>(raw.header_regions),
    cells,
    floatingImages: asArray<JsonRecord>(raw.floating_images).map((image, imageIndex) =>
      normalizeCellImage(image, id, -1, imageIndex),
    ),
    notes: stringArray(raw.notes),
    page: positiveInt(
      firstDefined(raw.page_start, raw.page, location.page_number, cells[0]?.page),
    ),
    pageEnd: positiveInt(
      firstDefined(
        raw.page_end,
        raw.page_start,
        location.page_number,
        cells.at(-1)?.page,
      ),
    ),
    reconstructionMode: String(
      firstDefined(reconstruction.mode, "STRUCTURED_TEXT_GRID"),
    ).toUpperCase(),
    mergeSemantics: String(
      firstDefined(reconstruction.merge_semantics, "UNKNOWN"),
    ).toUpperCase(),
    geometryPreserved:
      geometryStatus === "PRESERVED" &&
      columnWidthsPoints.length === columnCount &&
      rowHeightsPoints.length === rowCount,
    widthPoints: Math.max(
      0,
      numberFrom(firstDefined(geometry.width_points, geometry.widthPoints)),
    ),
    heightPoints: Math.max(
      0,
      numberFrom(firstDefined(geometry.height_points, geometry.heightPoints)),
    ),
    columnWidthsPoints,
    rowHeightsPoints,
    logicalTableId: String(firstDefined(continuation.logical_table_id, raw.logical_table_id, id)),
    continuationStatus: String(
      firstDefined(continuation.status, "SINGLE"),
    ).toUpperCase(),
    segmentIndex: wholeNumber(continuation.segment_index),
    segmentCount: Math.max(1, wholeNumber(continuation.segment_count, 1)),
  };
}

export function normalizeRestorationPage(
  payload: JsonRecord | null,
  page: number,
): PageSnapshot {
  const comparison = asRecord(
    payload?.page_comparison ??
      payload?.pageComparison ??
      asRecord(payload?.result).page_comparison,
  );
  const rawPage =
    asArray<JsonRecord>(comparison.pages).find(
      (item) => numberFrom(item.page_number ?? item.page) === page,
    ) ||
    asRecord(payload?.page) ||
    payload ||
    {};
  const pageBounds = bboxOf(
    rawPage.bounds ?? rawPage.page_bounds ?? asRecord(rawPage.result).bounds,
  );
  const normalizedBlocks = asArray<JsonRecord>(
    rawPage.blocks ??
      rawPage.content_blocks ??
      asRecord(rawPage.result).blocks,
  )
    .map<PageBlock>((block, index) => {
      const bbox = bboxOf(block.bbox);
      const type = normalizeType(block.type ?? block.block_type);
      return {
        id: String(
          firstDefined(
            block.id,
            block.source_node_id,
            `page-${page}-block-${index + 1}`,
          ),
        ),
        type,
        role: String(block.role || ""),
        label: compactText(block.label, `${type}-${index + 1}`),
        text: compactText(block.text ?? block.raw_text, ""),
        bbox,
        locatable: block.locatable !== false && bbox.length === 4,
        locationStatus: compactText(block.location_status, ""),
        sourceOrder: numberFrom(block.source_order, index),
        pageNumber: positiveInt(block.page_number) || page,
        tableId: String(block.table_id || ""),
        provenance: asRecord(block.provenance),
        diagnostics: asArray<JsonRecord>(block.diagnostics),
        defaultVisible: block.default_visible !== false,
        overlayDefault: block.overlay_default !== false,
        sourceKind: String(block.source_kind || ""),
        sourceLocation: asRecord(block.source_location),
        raw: block,
        supportLayerCount: positiveInt(block.physical_layer_count) || undefined,
        supportLayerIds: stringArray(block.physical_layer_ids),
        supportLayerSourceRefs: asArray<JsonRecord>(
          block.physical_layer_source_refs,
        ),
      };
    })
    .filter(
      (block) =>
        block.type === "PAGE_RASTER" ||
        block.type !== "UNKNOWN" ||
        block.text ||
        block.bbox.length,
    )
    .sort((left, right) => left.sourceOrder - right.sourceOrder);
  const physicalSupportBlocks = normalizedBlocks.filter(isScanSupportBlock);
  const blocks = normalizedBlocks.filter(
    (block) => !isScanSupportBlock(block),
  );
  const logicalSupport = logicalScanSupportBlock({
    page,
    pageBounds,
    physicalLayers: physicalSupportBlocks,
  });
  const supportBlocks = logicalSupport ? [logicalSupport] : [];
  const issues = asArray<JsonRecord>(rawPage.issues).map<PageIssue>(
    (issue, index) => {
      const bbox = bboxOf(issue.bbox);
      return {
        id: String(issue.id ?? issue.code ?? `page-${page}-issue-${index + 1}`),
        code: compactText(issue.code, `ISSUE-${index + 1}`),
        message: compactText(
          issue.message ?? issue.message_key,
          "未提供异常说明",
        ),
        severity: compactText(issue.severity, "warning").toLowerCase(),
        bbox,
        locatable: issue.locatable !== false && bbox.length === 4,
        blocking: issue.blocking === true,
        stage: compactText(issue.stage, ""),
        label: compactText(issue.label, `ISS-${index + 1}`),
        overlay: issue.overlay === true,
        pageNumber: positiveInt(issue.page_number) || page,
        sourceId: String(issue.source_id || ""),
        locationStatus: compactText(issue.location_status, ""),
        sourceLocation: asRecord(issue.source_location),
      };
    },
  );
  const rawTextAuthority = asRecord(
    asRecord(rawPage.coverage).text_authority ?? rawPage.text_authority,
  );
  const rawTextAuthorityChannel = String(
    rawTextAuthority.channel || "",
  ).toUpperCase();
  const rawTextAuthorityStatus = String(
    rawTextAuthority.status || "",
  ).toUpperCase();
  const textAuthority: PageTextAuthorityReceipt = {
    declared: Object.keys(rawTextAuthority).length > 0,
    channel: ["REMOTE_VISUAL", "NATIVE"].includes(rawTextAuthorityChannel)
      ? (rawTextAuthorityChannel as "REMOTE_VISUAL" | "NATIVE")
      : "UNKNOWN",
    status: ["READY", "EMPTY", "FAILED", "PENDING"].includes(
      rawTextAuthorityStatus,
    )
      ? (rawTextAuthorityStatus as PageTextAuthorityReceipt["status"])
      : "UNKNOWN",
    reasonCodes: stringArray(rawTextAuthority.reason_codes),
  };

  return {
    blocks,
    supportBlocks,
    physicalSupportBlocks,
    issues,
    tables: asArray<JsonRecord>(payload?.tables)
      .map(normalizeStructuredTable)
      .sort(
        (left, right) =>
          left.segmentIndex - right.segmentIndex ||
          left.sourceOrder - right.sourceOrder,
      ),
    bounds: pageBounds,
    counts: asRecord(rawPage.counts),
    coverage: asRecord(rawPage.coverage),
    textAuthority,
    sourceSha256: String(asRecord(payload?.source_identity).source_sha256 || ""),
  };
}

export function contentTypeLabel(type: string, role = ""): string {
  if (type === "TEXT" && role === "TOC") return "目录";
  return (
    {
      TEXT: "文字",
      TABLE: "表格",
      FIGURE: "图形",
      HEADER: "页眉",
      FOOTER: "页脚",
      WATERMARK: "水印",
      PAGE_RASTER: "扫描页支撑",
      ISSUE: "异常",
      UNKNOWN: "未分类",
    }[type] || "未分类"
  );
}

export function provenanceLabel(provenance: JsonRecord): string {
  const channel = String(provenance.channel || "").toUpperCase();
  if (channel === "NATIVE") return "本地解析";
  if (channel === "REMOTE_VISUAL") return "OCR 视觉识别";
  return "来源未标明";
}

export function pageBlockEvidenceChannel(
  block: PageBlock,
): PageEvidenceChannel {
  const channel = String(block.provenance.channel || "").toUpperCase();
  if (channel === "NATIVE" || channel === "REMOTE_VISUAL") return channel;
  return "UNKNOWN";
}

/**
 * Decide only the page's display channel. This is not a new extraction
 * authority: it requires the server coverage receipt and the returned blocks
 * to agree.  An absent or contradictory receipt hides adopted text; users may
 * still inspect both immutable channels through the explicit compare view.
 */
export function pageEvidenceDisposition(
  snapshot: PageSnapshot,
  options: PageEvidenceSelectionOptions = {},
): PageEvidenceDisposition {
  const channels = snapshot.blocks.map(pageBlockEvidenceChannel);
  const nativeBlockCount = channels.filter((channel) => channel === "NATIVE").length;
  const remoteVisualBlockCount = channels.filter(
    (channel) => channel === "REMOTE_VISUAL",
  ).length;
  const unknownBlockCount = channels.filter(
    (channel) => channel === "UNKNOWN",
  ).length;
  const remoteTextBlockCount = snapshot.blocks.filter(
    (block) =>
      pageBlockEvidenceChannel(block) === "REMOTE_VISUAL" &&
      block.type === "TEXT",
  ).length;
  const remoteText = asRecord(snapshot.coverage.remote_text);
  const hasCoverageReceipt =
    Object.prototype.hasOwnProperty.call(remoteText, "evaluated") &&
    Object.prototype.hasOwnProperty.call(remoteText, "status") &&
    Object.prototype.hasOwnProperty.call(remoteText, "observation_count");
  const evaluated = remoteText.evaluated === true;
  const status = String(remoteText.status || "").toUpperCase();
  const observationCount = wholeNumber(remoteText.observation_count);
  const result = {
    nativeBlockCount,
    remoteVisualBlockCount,
    unknownBlockCount,
  };

  if (snapshot.textAuthority?.declared) {
    const authority = snapshot.textAuthority;
    if (
      authority.channel === "REMOTE_VISUAL" &&
      authority.status === "READY" &&
      remoteTextBlockCount > 0
    ) {
      return {
        ...result,
        adoptedTextChannel: "REMOTE_VISUAL",
        basis: "TEXT_AUTHORITY_OCR_READY",
        certain: true,
        effectiveTextPolicy: "REMOTE_VISUAL",
        reasonCodes: authority.reasonCodes,
      };
    }
    if (
      authority.channel === "NATIVE" &&
      authority.status === "READY"
    ) {
      return {
        ...result,
        adoptedTextChannel: "NATIVE",
        basis: "TEXT_AUTHORITY_NATIVE_READY",
        certain: true,
        effectiveTextPolicy: "NATIVE",
        reasonCodes: authority.reasonCodes,
      };
    }
    return {
      ...result,
      adoptedTextChannel: "UNRESOLVED",
      basis:
        authority.status === "READY"
          ? "TEXT_AUTHORITY_BLOCK_MISMATCH"
          : "TEXT_AUTHORITY_NOT_READY",
      certain: false,
      effectiveTextPolicy: "HIDE",
      reasonCodes: authority.reasonCodes,
    };
  }
  if (options.visualRecoveryFailed) {
    return {
      ...result,
      adoptedTextChannel: "UNRESOLVED",
      basis: "REMOTE_VISUAL_DEGRADED",
      certain: false,
      effectiveTextPolicy: "HIDE",
      reasonCodes: ["remote_visual_degraded"],
    };
  }

  if (!hasCoverageReceipt) {
    return {
      ...result,
      adoptedTextChannel: "UNRESOLVED",
      basis: "COVERAGE_UNAVAILABLE",
      certain: false,
      effectiveTextPolicy: "HIDE",
      reasonCodes: [],
    };
  }
  if (
    evaluated &&
    status === "OBSERVED" &&
    observationCount > 0 &&
    observationCount === remoteTextBlockCount &&
    remoteVisualBlockCount > 0 &&
    unknownBlockCount === 0
  ) {
    return {
      ...result,
      adoptedTextChannel: "REMOTE_VISUAL",
      basis: "REMOTE_TEXT_OBSERVED",
      certain: true,
      effectiveTextPolicy: "REMOTE_VISUAL",
      reasonCodes: [],
    };
  }
  if (
    !evaluated &&
    status === "NOT_EVALUATED" &&
    observationCount === 0 &&
    remoteVisualBlockCount === 0 &&
    unknownBlockCount === 0
  ) {
    return {
      ...result,
      adoptedTextChannel: "NATIVE",
      basis: "REMOTE_TEXT_NOT_EVALUATED",
      certain: true,
      effectiveTextPolicy: "NATIVE",
      reasonCodes: [],
    };
  }
  if (evaluated && status === "NOT_OBSERVED" && observationCount === 0) {
    return {
      ...result,
      adoptedTextChannel: "UNRESOLVED",
      basis: "REMOTE_TEXT_NOT_OBSERVED",
      certain: false,
      effectiveTextPolicy: "HIDE",
      reasonCodes: [],
    };
  }
  return {
    ...result,
    adoptedTextChannel: "UNRESOLVED",
    basis: "REMOTE_COVERAGE_BLOCK_MISMATCH",
    certain: false,
    effectiveTextPolicy: "HIDE",
    reasonCodes: [],
  };
}

const NATIVE_SUPPORTING_FACT_TYPES = new Set<ContentType>([
  "TABLE",
  "FIGURE",
]);

const REMOTE_ADOPTED_FACT_TYPES = new Set<ContentType>([
  "TEXT",
  "HEADER",
  "FOOTER",
  "WATERMARK",
]);

const PAGE_FURNITURE_FACT_TYPES = new Set<ContentType>([
  "HEADER",
  "FOOTER",
  "WATERMARK",
]);

function isProvenPageFurniture(block: PageBlock): boolean {
  if (PAGE_FURNITURE_FACT_TYPES.has(block.type)) return true;
  return block.diagnostics.some((diagnostic) =>
    /^pdf_repeated_(?:header|footer)_image_furniture$/i.test(
      String(diagnostic.code || ""),
    ),
  );
}

/** One selector feeds both cards and PDF overlays so the two surfaces cannot drift. */
export function pageBlocksForEvidenceView(
  snapshot: PageSnapshot,
  view: PageEvidenceView,
  showSupportLayer = false,
  options: PageEvidenceSelectionOptions = {},
): PageBlock[] {
  const disposition = pageEvidenceDisposition(snapshot, options);
  const contentBlocks = snapshot.blocks.filter((block) => {
    const channel = pageBlockEvidenceChannel(block);
    if (view === "REMOTE_VISUAL") return channel === "REMOTE_VISUAL";
    if (view === "NATIVE") return channel === "NATIVE";
    if (view === "COMPARE") return true;
    if (disposition.effectiveTextPolicy === "REMOTE_VISUAL") {
      return (
        (channel === "REMOTE_VISUAL" &&
          REMOTE_ADOPTED_FACT_TYPES.has(block.type)) ||
        (channel === "NATIVE" &&
          NATIVE_SUPPORTING_FACT_TYPES.has(block.type) &&
          !isProvenPageFurniture(block))
      );
    }
    if (disposition.effectiveTextPolicy === "NATIVE") {
      return channel === "NATIVE" && !isProvenPageFurniture(block);
    }
    if (disposition.effectiveTextPolicy === "HIDE") {
      return (
        channel === "NATIVE" &&
        NATIVE_SUPPORTING_FACT_TYPES.has(block.type) &&
        !isProvenPageFurniture(block)
      );
    }
    return true;
  });
  return (showSupportLayer
    ? [...contentBlocks, ...(snapshot.supportBlocks || [])]
    : contentBlocks
  ).sort((left, right) => left.sourceOrder - right.sourceOrder);
}

export function tableForBlock(
  block: PageBlock,
  tables: StructuredTableModel[],
): StructuredTableModel | null {
  return (
    tables.find((table) => table.id === block.tableId) ||
    tables.find((table) => table.sourceNodeId === block.id) ||
    null
  );
}

export function tableSegments(
  table: StructuredTableModel,
  tables: StructuredTableModel[],
): StructuredTableModel[] {
  const logicalId = table.logicalTableId || table.id;
  const segments = tables.filter(
    (candidate) => (candidate.logicalTableId || candidate.id) === logicalId,
  );
  return (segments.length ? segments : [table]).slice().sort(
    (left, right) =>
      left.segmentIndex - right.segmentIndex ||
      left.sourceOrder - right.sourceOrder ||
      left.id.localeCompare(right.id),
  );
}

export function sourceGeometryForContentBlock(
  block: JsonRecord,
  pageBlocks: PageBlock[] = [],
): SourceCropGeometry | null {
  const location = asRecord(
    firstDefined(block.location, block.locator, block.native_locator),
  );
  const page =
    [
      location.page_number,
      location.page,
      block.page_number,
      block.page_start,
      block.page,
    ]
      .map(positiveInt)
      .find((candidate) => candidate > 0) || 0;
  const locationBbox = bboxOf(location.bbox);
  const bbox = locationBbox.length === 4 ? locationBbox : bboxOf(block.bbox);
  if (page > 0 && bbox.length === 4) return { page, bbox };

  const sourceId = String(block.source_id || block.source_node_id || "");
  if (!sourceId) return null;
  const restoredBlock = pageBlocks.find(
    (candidate) =>
      candidate.id === sourceId &&
      candidate.pageNumber > 0 &&
      candidate.bbox.length === 4,
  );
  return restoredBlock
    ? { page: restoredBlock.pageNumber, bbox: restoredBlock.bbox }
    : null;
}

export function sourceCropUrl({
  projectId,
  documentId,
  sourceSha256,
  page,
  bbox,
}: {
  projectId: string;
  documentId: string;
  sourceSha256: string;
  page: number;
  bbox: number[];
}): string {
  if (
    !projectId ||
    !documentId ||
    !/^[0-9a-f]{64}$/i.test(sourceSha256) ||
    page < 1 ||
    bbox.length !== 4
  ) {
    return "";
  }
  const [x0, y0, x1, y1] = bbox;
  if (![x0, y0, x1, y1].every(Number.isFinite) || x1 <= x0 || y1 <= y0) {
    return "";
  }
  const query = new URLSearchParams({
    source_sha256: sourceSha256.toLowerCase(),
    page: String(page),
    x0: x0.toFixed(3),
    y0: y0.toFixed(3),
    x1: x1.toFixed(3),
    y1: y1.toFixed(3),
  });
  return `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/source-crop?${query.toString()}`;
}
