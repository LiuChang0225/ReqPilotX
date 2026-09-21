import { useEffect, useRef, useState, type CSSProperties } from "react";
import { cx } from "../../shared/lib/format";
import { Dialog } from "../../shared/ui/Dialog";
import { Icon } from "../../shared/ui/Icon";
import { useWorkbenchHost } from "../workspace/WorkbenchHostContext";
import {
  sourceCropUrl,
  tableSegments,
  type StructuredTableModel,
  type StructuredTableRecordFocus,
  type TableCell,
} from "./restorationModel";

interface StructuredTableProps {
  table: StructuredTableModel;
  tables: StructuredTableModel[];
  displayLabel?: string;
  displayLabelAuthority?: "AGENT_DISPLAY_LABEL";
  fitContainer?: boolean;
  projectId: string;
  documentId: string;
  sourceSha256: string;
  onPageChange: (page: number) => void;
  recordFocus?: StructuredTableRecordFocus | null;
  compactRecord?: boolean;
  initialRowLimit?: number;
}

function regionNumber(
  region: Record<string, unknown>,
  keys: string[],
  fallback: number,
): number {
  for (const key of keys) {
    const value = Number(region[key]);
    if (Number.isFinite(value)) return value;
  }
  return fallback;
}

function isHeaderCell(table: StructuredTableModel, cell: TableCell): boolean {
  if (cell.header) return true;
  return table.headerRegions.some((region) => {
    const rowStart = regionNumber(region, ["row_start", "start_row", "row"], 0);
    const rowSpan = Math.max(
      1,
      regionNumber(region, ["row_span", "rowSpan"], 1),
    );
    const rowEnd = regionNumber(
      region,
      ["row_end", "end_row"],
      rowStart + rowSpan - 1,
    );
    const columnStart = regionNumber(
      region,
      ["column_start", "start_column", "column"],
      0,
    );
    const columnSpan = Math.max(
      1,
      regionNumber(region, ["column_span", "columnSpan"], 1),
    );
    const columnEnd = regionNumber(
      region,
      ["column_end", "end_column"],
      columnStart + columnSpan - 1,
    );
    return (
      cell.row >= rowStart &&
      cell.row <= rowEnd &&
      cell.column >= columnStart &&
      cell.column <= columnEnd
    );
  });
}

function alignmentStyle(cell: TableCell): CSSProperties {
  const horizontal = cell.horizontalAlignment
    ? ({
        LEFT: "left",
        CENTER: "center",
        RIGHT: "right",
        JUSTIFY: "justify",
      }[cell.horizontalAlignment] as CSSProperties["textAlign"])
    : undefined;
  const vertical = cell.verticalAlignment
    ? ({
        TOP: "top",
        MIDDLE: "middle",
        BOTTOM: "bottom",
      }[cell.verticalAlignment] as CSSProperties["verticalAlign"])
    : undefined;
  return {
    textAlign: horizontal || "left",
    verticalAlign: vertical || "top",
    paddingLeft: `${10 + Math.min(120, cell.indentLevel * 12)}px`,
  };
}

function cellCoversIndex(start: number, span: number, index: number): boolean {
  return start <= index && index < start + Math.max(1, span);
}

function compactVisibleRowSpan(
  cell: TableCell,
  renderedRowIndexes: number[],
): number | undefined {
  if (cell.rowSpan <= 1) return undefined;
  const startIndex = renderedRowIndexes.indexOf(cell.row);
  if (startIndex < 0) return undefined;
  const sourceEnd = cell.row + cell.rowSpan;
  let expectedRow = cell.row;
  let visibleSpan = 0;
  for (let index = startIndex; index < renderedRowIndexes.length; index += 1) {
    const rowIndex = renderedRowIndexes[index];
    if (rowIndex !== expectedRow || rowIndex >= sourceEnd) break;
    visibleSpan += 1;
    expectedRow += 1;
  }
  return visibleSpan > 1 ? visibleSpan : undefined;
}

function compactApplicableHeaderCellIds(
  table: StructuredTableModel,
  declaredHeaderCellIds: Set<string>,
  selectedRows: Set<number>,
  includeInferredHeaders: boolean,
): Set<string> {
  const declared = table.cells.filter((cell) =>
    declaredHeaderCellIds.has(cell.id),
  );
  const candidates = declared.length
    ? declared
    : includeInferredHeaders
      ? table.cells.filter((cell) => isHeaderCell(table, cell))
      : [];
  if (!candidates.length) return new Set();
  const applicableRows = new Set<number>();
  // A record may include a governing statement above its column headers.
  // Resolve each selected row's local header group, not just the first row's.
  for (const selectedRow of selectedRows.size ? selectedRows : [Number.POSITIVE_INFINITY]) {
    const candidateRows = new Set(candidates.map(cell => cell.row).filter(row => row < selectedRow));
    if (!candidateRows.size) continue;
    const nearestRow = Math.max(...candidateRows);
    applicableRows.add(nearestRow);
    for (let row = nearestRow - 1; candidateRows.has(row); row -= 1) applicableRows.add(row);
  }
  return new Set(
    candidates
      .filter((cell) => applicableRows.has(cell.row))
      .map((cell) => cell.id),
  );
}

function compactProjectedCellsByRow(
  table: StructuredTableModel,
  selectedRows: Set<number>,
  recordCellIds: Set<string>,
  applicableHeaderCellIds: Set<string>,
): Map<number, TableCell[]> {
  const projected = new Map<number, TableCell[]>();
  const append = (row: number, cell: TableCell) => {
    const values = projected.get(row) || [];
    values.push(cell);
    projected.set(row, values);
  };
  table.cells.forEach((cell) => {
    if (applicableHeaderCellIds.has(cell.id)) append(cell.row, cell);
  });
  const selected = Array.from(selectedRows).sort((left, right) => left - right);
  table.cells.forEach((cell) => {
    if (!recordCellIds.has(cell.id) || applicableHeaderCellIds.has(cell.id)) return;
    const coveredRows = selected.filter((row) =>
      cellCoversIndex(cell.row, cell.rowSpan, row),
    );
    if (!coveredRows.length) return;
    const projectedRow = coveredRows[0];
    let projectedSpan = 1;
    while (
      projectedSpan < coveredRows.length &&
      coveredRows[projectedSpan] === projectedRow + projectedSpan
    ) {
      projectedSpan += 1;
    }
    append(projectedRow, {
      ...cell,
      row: projectedRow,
      rowSpan: projectedSpan,
    });
  });
  projected.forEach((cells) => {
    cells.sort((left, right) => left.column - right.column);
  });
  return projected;
}

function mergeSafeRowLimit(
  table: StructuredTableModel,
  requestedLimit: number,
  rowCount: number,
): number {
  if (!Number.isFinite(requestedLimit) || requestedLimit >= rowCount) {
    return rowCount;
  }
  let limit = Math.max(1, Math.floor(requestedLimit));
  let changed = true;
  while (changed && limit < rowCount) {
    changed = false;
    table.cells.forEach((cell) => {
      const end = cell.row + Math.max(1, cell.rowSpan);
      if (cell.row < limit && end > limit) {
        limit = Math.min(rowCount, end);
        changed = true;
      }
    });
  }
  return limit;
}

function TableCellContent({
  cell,
  projectId,
  documentId,
  sourceSha256,
  onPageChange,
}: {
  cell: TableCell;
  projectId: string;
  documentId: string;
  sourceSha256: string;
  onPageChange: (page: number) => void;
}) {
  const host = useWorkbenchHost();
  return (
    <>
      {cell.text && <div className="table-cell-text">{cell.text}</div>}
      {cell.images.map((image) => {
        const url = image.available
          ? sourceCropUrl({
              projectId,
              documentId,
              sourceSha256,
              page: image.page,
              bbox: image.bbox,
            })
          : "";
        return url ? (
          <figure className="table-cell-image" key={image.id}>
            <img
              src={host.resourceUrl(url)}
              alt="原 PDF 表格单元格中的图片"
              width={560}
              height={320}
              loading="lazy"
              decoding="async"
            />
            {image.page > 0 && (
              <figcaption>
                <button
                  className="source-page-jump"
                  type="button"
                  onClick={() => onPageChange(image.page)}
                >
                  图片来源 · 第 {image.page} 页
                </button>
              </figcaption>
            )}
          </figure>
        ) : (
          <div className="table-cell-image is-unavailable" key={image.id}>
            <span>图片位置已识别，当前无法生成预览。</span>
            {image.page > 0 && (
              <button
                className="source-page-jump"
                type="button"
                onClick={() => onPageChange(image.page)}
              >
                查看第 {image.page} 页
              </button>
            )}
          </div>
        );
      })}
    </>
  );
}

function TableSegment({
  table,
  index,
  projectId,
  documentId,
  sourceSha256,
  onPageChange,
  fitContainer,
  recordFocus,
  compactRecord,
  includeInferredHeaders,
  rowLimit,
}: {
  table: StructuredTableModel;
  index: number;
  projectId: string;
  documentId: string;
  sourceSha256: string;
  onPageChange: (page: number) => void;
  fitContainer: boolean;
  recordFocus: StructuredTableRecordFocus | null;
  compactRecord: boolean;
  includeInferredHeaders: boolean;
  rowLimit: number;
}) {
  const host = useWorkbenchHost();
  const rows = new Map<number, TableCell[]>();
  table.cells
    .slice()
    .sort((left, right) => left.row - right.row || left.column - right.column)
    .forEach((cell) => {
      const row = rows.get(cell.row) || [];
      row.push(cell);
      rows.set(cell.row, row);
    });
  const inferredRowCount = table.cells.reduce(
    (maximum, cell) => Math.max(maximum, cell.row + cell.rowSpan),
    0,
  );
  const rowCount = Math.max(table.rowCount, inferredRowCount);
  const totalColumnWidth = table.columnWidthsPoints.reduce(
    (total, value) => total + value,
    0,
  );
  const readableColumnWidth = Math.min(1600, table.columnCount * 96);
  const minimumWidth =
    fitContainer && table.columnCount <= 3
      ? 0
      : table.geometryPreserved && table.widthPoints
        ? Math.max(
            520,
            readableColumnWidth,
            Math.min(1600, table.widthPoints),
          )
        : Math.max(520, Math.min(1600, table.columnCount * 118));
  const rowBindings =
    recordFocus?.rowBindings.filter(
      (binding) => binding.tableId === table.id,
    ) || [];
  const columnBindings =
    recordFocus?.columnBindings.filter(
      (binding) => binding.tableId === table.id,
    ) || [];
  const selectedRows = new Set(rowBindings.map((binding) => binding.index));
  const selectedColumns = new Set(
    columnBindings.map((binding) => binding.index),
  );
  const resolveDisplayCells = (ids: readonly string[]) => {
    const resolved = new Set(ids);
    for (const cell of table.cells) {
      if (cell.sourceCellIds?.some(id => resolved.has(id))) resolved.add(cell.id);
    }
    return resolved;
  };
  const rowCellIds = resolveDisplayCells(
    rowBindings.flatMap((binding) => binding.cellIds),
  );
  const columnCellIds = resolveDisplayCells(
    columnBindings.flatMap((binding) => binding.cellIds),
  );
  const headerCellIds = resolveDisplayCells(
    recordFocus?.headerCellIdsByTable[table.id] || [],
  );
  const sharedCellIds = resolveDisplayCells(
    recordFocus?.sharedCellIdsByTable[table.id] || [],
  );
  const applicableHeaderCellIds =
    compactRecord && recordFocus
      ? compactApplicableHeaderCellIds(
          table,
          headerCellIds,
          selectedRows,
          includeInferredHeaders,
        )
      : headerCellIds;
  const compactRecordCellIds = new Set([...rowCellIds, ...sharedCellIds]);
  const compactCellsByRow =
    compactRecord && recordFocus
      ? compactProjectedCellsByRow(
          table,
          selectedRows,
          compactRecordCellIds,
          applicableHeaderCellIds,
        )
      : new Map<number, TableCell[]>();
  const compactRowIndexes = new Set<number>([
    ...selectedRows,
    ...Array.from(compactCellsByRow.keys()),
  ]);
  const allRowIndexes =
    compactRecord && recordFocus
      ? Array.from(compactRowIndexes).sort((left, right) => left - right)
      : Array.from({ length: rowCount }, (_, rowIndex) => rowIndex);
  const renderedRowIndexes =
    compactRecord && recordFocus
      ? allRowIndexes
      : allRowIndexes.slice(0, mergeSafeRowLimit(table, rowLimit, rowCount));
  const compactCellsForRow = (rowIndex: number): TableCell[] => {
    const values = rows.get(rowIndex) || [];
    if (!compactRecord || !recordFocus) return values;
    return compactCellsByRow.get(rowIndex) || [];
  };
  const visibleFloatingImages = (table.floatingImages || []).filter(
    (image) =>
      !compactRecord ||
      !recordFocus ||
      Boolean(
        image.sourceRowIndexes?.some((rowIndex) => selectedRows.has(rowIndex)),
      ),
  );

  return (
    <section
      className={cx("table-segment", compactRecord && "is-record-compact")}
      data-geometry-preserved={table.geometryPreserved}
    >
      {index > 0 && (
        <div className="table-continuation-divider">
          <span>
            {compactRecord ? "当前条目 · 原表" : "续表"} · 第 {index + 1} 段
            {table.page > 0 ? ` · 第 ${table.page} 页` : ""}
          </span>
          {table.page > 0 && (
            <button
              className="source-page-jump"
              type="button"
              onClick={() => onPageChange(table.page)}
            >
              回到原文
            </button>
          )}
        </div>
      )}
      <table style={{ minWidth: minimumWidth ? `${minimumWidth}px` : "100%" }}>
        {table.geometryPreserved && totalColumnWidth > 0 && (
          <colgroup>
            {table.columnWidthsPoints.map((width, columnIndex) => (
              <col
                key={`${table.id}-column-${columnIndex}`}
                style={{ width: `${(width / totalColumnWidth) * 100}%` }}
              />
            ))}
          </colgroup>
        )}
        <tbody>
          {renderedRowIndexes.map((rowIndex) => {
            const rowHeight = table.geometryPreserved
              ? table.rowHeightsPoints[rowIndex]
              : 0;
            const focusedRecordRow =
              recordFocus?.mode === "ROW" && selectedRows.has(rowIndex);
            return (
              <tr
                key={`${table.id}-row-${rowIndex}`}
                className={cx(
                  focusedRecordRow && "is-record-focus-row",
                  recordFocus?.mode === "MATRIX_INTERSECTION" &&
                    selectedRows.has(rowIndex) &&
                    "is-record-axis-row",
                )}
                aria-current={focusedRecordRow ? "true" : undefined}
                style={rowHeight ? { height: `${Math.max(22, rowHeight)}px` } : undefined}
              >
                {compactCellsForRow(rowIndex).map((cell) => {
                  const header = isHeaderCell(table, cell);
                  const Cell = header ? "th" : "td";
                  const coversSelectedRow = Array.from(selectedRows).some(
                    (selectedRow) =>
                      cellCoversIndex(cell.row, cell.rowSpan, selectedRow),
                  );
                  const coversSelectedColumn = Array.from(
                    selectedColumns,
                  ).some((selectedColumn) =>
                    cellCoversIndex(
                      cell.column,
                      cell.columnSpan,
                      selectedColumn,
                    ),
                  );
                  const onSelectedRow =
                    rowCellIds.has(cell.id) || coversSelectedRow;
                  const onSelectedColumn =
                    columnCellIds.has(cell.id) || coversSelectedColumn;
                  const focusedCell =
                    recordFocus?.mode === "ROW"
                      ? rowCellIds.has(cell.id)
                      : recordFocus?.mode === "MATRIX_INTERSECTION"
                        ? onSelectedRow && onSelectedColumn
                        : false;
                  return (
                    <Cell
                      key={cell.id}
                      className={cx(
                        focusedCell && "is-record-focus-cell",
                        recordFocus?.mode === "MATRIX_INTERSECTION" &&
                          onSelectedRow &&
                          "is-record-axis-row",
                        recordFocus?.mode === "MATRIX_INTERSECTION" &&
                          onSelectedColumn &&
                          "is-record-axis-column",
                        applicableHeaderCellIds.has(cell.id) &&
                          "is-record-context-header",
                        sharedCellIds.has(cell.id) &&
                          "is-record-context-shared",
                      )}
                      aria-current={focusedCell ? "true" : undefined}
                      scope={header ? (rowIndex === 0 ? "col" : "row") : undefined}
                      rowSpan={
                        compactRecord
                          ? compactVisibleRowSpan(cell, renderedRowIndexes)
                          : cell.rowSpan > 1
                            ? cell.rowSpan
                            : undefined
                      }
                      colSpan={cell.columnSpan > 1 ? cell.columnSpan : undefined}
                      style={alignmentStyle(cell)}
                    >
                      <TableCellContent
                        cell={cell}
                        projectId={projectId}
                        documentId={documentId}
                        sourceSha256={sourceSha256}
                        onPageChange={onPageChange}
                      />
                    </Cell>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
      {visibleFloatingImages.length ? (
        <section className="table-floating-images" aria-label="跨单元格图片">
          <strong>跨单元格图片</strong>
          <div>
            {visibleFloatingImages.map((image) => {
              const url = image.available
                ? sourceCropUrl({
                    projectId,
                    documentId,
                    sourceSha256,
                    page: image.page,
                    bbox: image.bbox,
                  })
                : "";
              return (
                <figure key={image.id}>
                  {url ? (
                    <img
                  src={host.resourceUrl(url)}
                      alt="原 PDF 中跨越多个表格单元格的图片"
                      width={560}
                      height={320}
                      loading="lazy"
                      decoding="async"
                    />
                  ) : (
                    <span>图片位置已保留，当前无法生成预览。</span>
                  )}
                  <figcaption>
                    <button
                      className="source-page-jump"
                      type="button"
                      onClick={() => onPageChange(image.page)}
                    >
                      图片来源 · 第 {image.page} 页
                    </button>
                  </figcaption>
                </figure>
              );
            })}
          </div>
        </section>
      ) : null}
    </section>
  );
}

export function StructuredTable({
  table,
  tables,
  displayLabel,
  displayLabelAuthority,
  fitContainer = false,
  projectId,
  documentId,
  sourceSha256,
  onPageChange,
  recordFocus = null,
  compactRecord = false,
  initialRowLimit = 0,
}: StructuredTableProps) {
  const [expanded, setExpanded] = useState(false);
  const [expandedWhole, setExpandedWhole] = useState(false);
  const dialogContentRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!expanded) return;
    const frame = requestAnimationFrame(() => dialogContentRef.current?.querySelector<HTMLElement>(".is-record-focus-row, .is-record-focus-cell")?.scrollIntoView?.({ block: "center", inline: "nearest" }));
    return () => cancelAnimationFrame(frame);
  }, [expanded, expandedWhole, recordFocus?.recordId]);
  const [visibleRowLimit, setVisibleRowLimit] = useState(
    initialRowLimit > 0 ? initialRowLimit : Number.POSITIVE_INFINITY,
  );
  useEffect(() => {
    setVisibleRowLimit(
      initialRowLimit > 0 ? initialRowLimit : Number.POSITIVE_INFINITY,
    );
  }, [initialRowLimit, table.id]);
  const segments = tableSegments(table, tables);
  const populatedSegments = segments.filter((segment) => segment.cells.length);
  if (!populatedSegments.length) {
    return (
      <p className="table-empty">
        表格事实已识别，但本次没有返回可重建的单元格网格。
      </p>
    );
  }
  const rowCount = populatedSegments.reduce(
    (total, segment) => total + segment.rowCount,
    0,
  );
  const columnCount = Math.max(
    ...populatedSegments.map((segment) => segment.columnCount),
  );
  const mergePreserved = populatedSegments.every((segment) =>
    ["PRESERVED", "NO_MERGES_DETECTED"].includes(segment.mergeSemantics),
  );
  const geometryPreserved =
    mergePreserved &&
    populatedSegments.every((segment) => segment.geometryPreserved);
  const visualGrid =
    mergePreserved &&
    populatedSegments.every(
      (segment) =>
        segment.reconstructionMode === "REMOTE_STRUCTURED_GRID" &&
        segment.cells.length > 0,
    );
  const visualRegion = populatedSegments.every(
    (segment) => segment.reconstructionMode === "VISUAL_REGION_ONLY",
  );
  const notes = Array.from(
    new Set(populatedSegments.flatMap((segment) => segment.notes)),
  );
  const firstPage = populatedSegments.find((segment) => segment.page)?.page || 0;
  const lastPagedSegment = populatedSegments
    .slice()
    .reverse()
    .find((segment) => segment.pageEnd || segment.page);
  const lastPage =
    lastPagedSegment?.pageEnd || lastPagedSegment?.page || firstPage;
  const focusTableIds = new Set([
    ...(recordFocus?.rowBindings.map((binding) => binding.tableId) || []),
    ...(recordFocus?.columnBindings.map((binding) => binding.tableId) || []),
  ]);
  const activeRecordFocus =
    recordFocus &&
    (populatedSegments.some((segment) => focusTableIds.has(segment.id)) ||
      populatedSegments.some(
        (segment) =>
          Boolean(recordFocus.logicalTableId) &&
          segment.logicalTableId === recordFocus.logicalTableId,
      ))
      ? recordFocus
      : null;
  const fidelityText =
    compactRecord && activeRecordFocus && geometryPreserved
      ? "依据原表关系重组"
      : geometryPreserved
        ? "结构与比例已还原"
        : visualRegion
          ? "原表区域"
          : visualGrid
            ? "视觉网格已识别"
            : "结构待对照";
  const hasHiddenRows =
    initialRowLimit > 0 &&
    populatedSegments.some((segment) => segment.rowCount > visibleRowLimit);
  const renderTable = (
    pageChange: (page: number) => void,
    fit: boolean,
    compact: boolean,
  ) => {
    const indexedSegments = populatedSegments.map((segment, index) => ({
      segment,
      index,
    }));
    const canonicalHeaderSegmentId = activeRecordFocus
      ? indexedSegments.find(({ segment }) => {
          const explicitHeaders =
            activeRecordFocus.headerCellIdsByTable[segment.id] || [];
          return (
            explicitHeaders.length > 0 ||
            segment.cells.some((cell) => isHeaderCell(segment, cell))
          );
        })?.segment.id || ""
      : "";
    const compactSegmentIds = activeRecordFocus
      ? new Set([
          ...focusTableIds,
          ...Object.entries(activeRecordFocus.headerCellIdsByTable)
            .filter(([, cellIds]) => cellIds.length > 0)
            .map(([tableId]) => tableId),
          ...Object.entries(activeRecordFocus.sharedCellIdsByTable)
            .filter(([, cellIds]) => cellIds.length > 0)
            .map(([tableId]) => tableId),
          canonicalHeaderSegmentId,
        ])
      : new Set<string>();
    const displayedSegments =
      compact && activeRecordFocus
        ? indexedSegments.filter(({ segment }) =>
            compactSegmentIds.has(segment.id),
          )
        : indexedSegments;
    return (
      <div className="structured-table-scroll">
      {displayedSegments.map(({ segment, index }) => (
        <TableSegment
          key={segment.id}
          table={segment}
          index={index}
          projectId={projectId}
          documentId={documentId}
          sourceSha256={sourceSha256}
          onPageChange={pageChange}
          fitContainer={fit}
          recordFocus={activeRecordFocus}
          compactRecord={compact}
          includeInferredHeaders={
            compact && segment.id === canonicalHeaderSegmentId
          }
          rowLimit={visibleRowLimit}
        />
      ))}
      </div>
    );
  };
  const progressiveRows = hasHiddenRows ? (
    <div className="structured-table-progressive">
      <span>
        为保持长表流畅，当前每段显示前 {Math.floor(visibleRowLimit)} 行；合并单元格不会被截断。
      </span>
      <button
        className="source-page-jump"
        type="button"
        onClick={() =>
          setVisibleRowLimit((current) => current + initialRowLimit)
        }
      >
        再加载 {initialRowLimit} 行
      </button>
    </div>
  ) : null;
  const fidelityNote = (
    <p
      className={cx(
        "table-fidelity-note",
        (geometryPreserved || visualGrid || visualRegion) && "is-preserved",
      )}
    >
      {compactRecord && activeRecordFocus && geometryPreserved
        ? "当前条目按原生表头、合并关系和来源行重组；完整原表的几何保持不变，可展开核对。"
        : geometryPreserved
        ? "合并关系和行列比例来自可证明的原生几何。"
        : visualRegion
          ? "原生单元格混入相邻内容，当前直接展示精确表格区域；待局部结构恢复后再发布单元格网格。"
        : visualGrid
        ? "行列与合并关系来自本次视觉网格识别；未声称恢复精确列宽和行高。"
          : "已展示可证明的单元格内容；请结合右侧原 PDF 核对几何差异。"}
    </p>
  );

  return (
    <>
      <figure
        className={cx(
          "structured-table",
          populatedSegments.length > 1 && "is-continuation",
          activeRecordFocus && "has-record-focus",
          fitContainer && "is-fit-container",
          compactRecord && activeRecordFocus && "is-record-compact",
        )}
        data-geometry-preserved={geometryPreserved}
        data-record-focus={activeRecordFocus?.recordId || undefined}
      >
        <figcaption>
          <div>
            <strong>
              {displayLabelAuthority === "AGENT_DISPLAY_LABEL" && displayLabel
                ? displayLabel
                : compactRecord && activeRecordFocus
                ? "当前表格条目"
                : populatedSegments.length > 1
                  ? "跨页表格"
                  : "表格结构"}
            </strong>
            <span>
              {displayLabelAuthority === "AGENT_DISPLAY_LABEL" ? "显示名（AI） · " : ""}
              {compactRecord && activeRecordFocus
                ? `表头 + 当前内容 · 原表 ${rowCount} 行 × ${columnCount} 列`
                : visualRegion
                  ? "原 PDF 表格区域 · 未伪造单元格"
                : `${rowCount} 行 × ${columnCount} 列`}
              {firstPage
                ? ` · 第 ${firstPage}${lastPage > firstPage ? `–${lastPage}` : ""} 页`
                : ""}
            </span>
          </div>
          <div className="table-caption-actions">
            {populatedSegments.length > 1 && (
              <span className="table-fidelity-badge is-continuation">
                {populatedSegments.length} 段续表
              </span>
            )}
            <span
              className={cx(
                "table-fidelity-badge",
                !geometryPreserved && !visualGrid && !visualRegion && "is-partial",
                (visualGrid || visualRegion) && "is-visual",
              )}
            >
              {fidelityText}
            </span>
            <button
              className="source-page-jump"
              type="button"
              onClick={() => { setExpandedWhole(!compactRecord); setExpanded(true); }}
            >
              <Icon name="maximize" size={13} />
              {compactRecord && activeRecordFocus ? "放大本条" : "展开表格"}
            </button>
            {firstPage > 0 && (
              <button
                className="source-page-jump"
                type="button"
                onClick={() => onPageChange(firstPage)}
              >
                查看原文
              </button>
            )}
          </div>
        </figcaption>
        {activeRecordFocus && (
          <section
            className="table-record-focus"
            aria-label="当前表格条目"
          >
            <div className="table-record-focus-title">
              <div>
                <span>表格条目</span>
                <strong>{activeRecordFocus.recordLabel}</strong>
              </div>
              <span className="table-record-focus-state">
                {recordFocus?.classificationLabel || "未做语义判断"}
              </span>
            </div>
            <div className="table-record-focus-context">
              <span>
                {activeRecordFocus.mode === "MATRIX_INTERSECTION"
                  ? "已高亮行列交点，淡色区域保留两个轴向。"
                  : "已在完整原表中高亮当前条目行。"}
              </span>
              {activeRecordFocus.headerTexts.length > 0 && (
                <span>
                  表头：{activeRecordFocus.headerTexts.join(" / ")}
                </span>
              )}
              {activeRecordFocus.sharedValues.length > 0 && (
                <span>
                  共用值：{activeRecordFocus.sharedValues.join(" / ")}
                </span>
              )}
              {activeRecordFocus.notes.length > 0 && (
                <span>备注：{activeRecordFocus.notes.join(" / ")}</span>
              )}
              {activeRecordFocus.imageCount > 0 && (
                <span>
                  关联图片 {activeRecordFocus.imageCount} 项，保留在原表单元格中。
                </span>
              )}
            </div>
          </section>
        )}
        {renderTable(
          onPageChange,
          fitContainer,
          compactRecord && Boolean(activeRecordFocus),
        )}
        {progressiveRows}
        {notes.length > 0 && <p className="table-notes">{notes.join(" · ")}</p>}
        {compactRecord && activeRecordFocus ? (
          <p className="table-fidelity-note is-preserved">
            首屏只呈现继承表头、共用值和当前内容；完整原表保持不变，可展开核对。
          </p>
        ) : (
          fidelityNote
        )}
      </figure>
      {expanded && (
        <Dialog
          open
          title={`${displayLabel || "当前表格"} · ${activeRecordFocus && !expandedWhole ? "当前条目原表" : "表格完整视图"}`}
          className="table-preview-dialog"
          onClose={() => setExpanded(false)}
        >
          <header>
            <div>
              <p className="section-label">{activeRecordFocus && !expandedWhole ? "当前条目原表" : "表格完整视图"}</p>
              <h2>{displayLabel || "当前表格"}</h2>
              <p>
                {displayLabelAuthority === "AGENT_DISPLAY_LABEL" ? "显示名（AI） · " : ""}
                {rowCount} 行 × {columnCount} 列
                {firstPage
                  ? ` · 第 ${firstPage}${lastPage > firstPage ? `–${lastPage}` : ""} 页`
                  : ""}
                {" · "}
                {fidelityText}
              </p>
            </div>
            {activeRecordFocus && <button type="button" className="button secondary compact" onClick={() => setExpandedWhole(value => !value)}>
              {expandedWhole ? "只看当前条目" : "查看完整原表"}</button>}
            <button
              className="icon-button"
              type="button"
              aria-label="关闭表格完整视图"
              onClick={() => setExpanded(false)}
            >
              <Icon name="close" />
            </button>
          </header>
          <div
            ref={dialogContentRef}
            className="table-dialog-content structured-table"
            data-geometry-preserved={geometryPreserved}
            data-record-focus={activeRecordFocus?.recordId || undefined}
          >
            {activeRecordFocus && (
              <section className="table-record-focus" aria-label="当前表格条目">
                <div className="table-record-focus-title">
                  <div>
                    <span>表格条目</span>
                    <strong>{activeRecordFocus.recordLabel}</strong>
                  </div>
                  <span className="table-record-focus-state">
                    {recordFocus?.classificationLabel || "未做语义判断"}
                  </span>
                </div>
              </section>
            )}
            {renderTable(
              (targetPage) => {
                setExpanded(false);
                onPageChange(targetPage);
              },
              false,
              Boolean(activeRecordFocus) && !expandedWhole,
            )}
            {progressiveRows}
            {notes.length > 0 && (
              <p className="table-notes">{notes.join(" · ")}</p>
            )}
            {fidelityNote}
          </div>
        </Dialog>
      )}
    </>
  );
}
