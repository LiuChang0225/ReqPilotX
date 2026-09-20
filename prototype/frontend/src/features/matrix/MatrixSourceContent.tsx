import { useEffect, useMemo, useState } from "react";
import { asArray, asRecord } from "../../shared/api/client";
import type {
  JsonRecord,
  RequirementMatrixLogicalTable,
  RequirementMatrixNode,
} from "../../shared/api/contracts";
import { compactText } from "../../shared/lib/format";
import { EmptyState } from "../../shared/ui/Feedback";
import { Icon } from "../../shared/ui/Icon";
import { StructuredTable } from "../restoration/StructuredTable";
import {
  normalizeStructuredTable,
  sourceCropUrl,
  sourceGeometryForContentBlock,
  type PageSnapshot,
  type TableCell,
  type StructuredTableAxisBinding,
  type StructuredTableModel,
  type StructuredTableRecordFocus,
} from "../restoration/restorationModel";
import { matrixNodeLabel } from "./requirementMatrixModel";
import { classificationLabels } from "./classificationFacets";

function records(value: unknown): JsonRecord[] {
  return asArray<JsonRecord>(value).filter(
    (item) => Boolean(item) && typeof item === "object",
  );
}

function unique(values: string[]): string[] {
  return Array.from(new Set(values.filter(Boolean)));
}

function axisBindings(
  value: unknown,
  indexKey: "row_index" | "column_index",
): StructuredTableAxisBinding[] {
  return records(value)
    .map((binding) => ({
      tableId: String(binding.table_id || ""),
      index: Number(binding[indexKey]),
      cellIds: unique(asArray<unknown>(binding.cell_ids).map(String)),
    }))
    .filter(
      (binding) =>
        Boolean(binding.tableId) &&
        Number.isInteger(binding.index) &&
        binding.index >= 0,
    );
}

function cellIdsByTable(
  values: Array<{ tableId: string; cellIds: string[] }>,
): Record<string, string[]> {
  const grouped: Record<string, string[]> = {};
  values.forEach((value) => {
    grouped[value.tableId] = unique([
      ...(grouped[value.tableId] || []),
      ...value.cellIds,
    ]);
  });
  return grouped;
}

interface MatrixHeaderBindingProjection {
  tableId: string;
  cellIds: string[];
  axis: string;
  text: string;
}

interface MatrixTableRecordFocus extends StructuredTableRecordFocus {
  localHeaderTableId: string;
}

function matchingLocalRepeatedHeaderCells(
  headers: readonly MatrixHeaderBindingProjection[],
  tables: readonly StructuredTableModel[],
  focusedTable: StructuredTableModel | undefined,
  focusedRowIndex: number,
): TableCell[] {
  if (!focusedTable || !Number.isFinite(focusedRowIndex) || focusedRowIndex <= 0) {
    return [];
  }
  const tableOrder = new Map(
    tables.map((table, index) => [table.id, index] as const),
  );
  const boundTableIds = unique(headers.map((header) => header.tableId))
    .filter((tableId) => tableOrder.has(tableId))
    .sort(
      (left, right) =>
        (tableOrder.get(left) || 0) - (tableOrder.get(right) || 0),
    );
  const firstBoundTableId = boundTableIds[0];
  if (!firstBoundTableId || firstBoundTableId === focusedTable.id) return [];
  const firstBoundTable = tables.find((table) => table.id === firstBoundTableId);
  if (!firstBoundTable) return [];

  const referenceIds = new Set(
    headers
      .filter((header) => header.tableId === firstBoundTableId)
      .flatMap((header) => header.cellIds),
  );
  if (!referenceIds.size) return [];
  const referenceCells = firstBoundTable.cells.filter(
    (cell) => referenceIds.has(cell.id) && Boolean(compactText(cell.text, "")),
  );
  if (referenceCells.length !== referenceIds.size) return [];

  const cellsByRow = (cells: readonly TableCell[]) => {
    const rows = new Map<number, TableCell[]>();
    cells.forEach((cell) => {
      if (!compactText(cell.text, "")) return;
      rows.set(cell.row, [...(rows.get(cell.row) || []), cell]);
    });
    return Array.from(rows.entries())
      .sort(([left], [right]) => left - right)
      .map(([row, rowCells]) => ({
        row,
        cells: rowCells.sort((left, right) => left.column - right.column),
      }));
  };
  const signature = (cells: readonly TableCell[]) =>
    JSON.stringify(
      cells.map((cell) => [cell.column, compactText(cell.text, "")]),
    );
  const referenceRows = cellsByRow(referenceCells);
  const prefixRows = cellsByRow(
    focusedTable.cells.filter((cell) => cell.row < focusedRowIndex),
  );
  if (!referenceRows.length || prefixRows.length < referenceRows.length) return [];

  for (
    let start = 0;
    start <= prefixRows.length - referenceRows.length;
    start += 1
  ) {
    const candidate = prefixRows.slice(start, start + referenceRows.length);
    const isContiguous = candidate.every(
      (row, index) => index === 0 || row.row === candidate[index - 1].row + 1,
    );
    if (
      isContiguous &&
      candidate.every(
        (row, index) =>
          signature(row.cells) === signature(referenceRows[index].cells),
      )
    ) {
      return candidate.flatMap((row) => row.cells);
    }
  }
  return [];
}

function headersInTableOrder(
  value: unknown,
  tables: readonly StructuredTableModel[],
): MatrixHeaderBindingProjection[] {
  const headers = records(value).map((header) => ({
    tableId: String(header.table_id || ""),
    cellIds: unique(asArray<unknown>(header.cell_ids).map(String)),
    axis: String(header.axis || "").toUpperCase(),
    text: compactText(header.text, ""),
  }));
  const tableOrder = new Map(
    tables.map((table, index) => [table.id, index] as const),
  );
  const cellPositions = new Map(
    tables.flatMap((table) =>
      table.cells.map(
        (cell) =>
          [
            `${table.id}\u0000${cell.id}`,
            {
              table: tableOrder.get(table.id) || 0,
              row: cell.row,
              column: cell.column,
            },
          ] as const,
      ),
    ),
  );
  const positioned = headers.map((header, originalIndex) => {
    const positions = header.cellIds.map((cellId) =>
      cellPositions.get(`${header.tableId}\u0000${cellId}`),
    );
    if (!positions.length || positions.some((position) => !position)) {
      return { header, originalIndex, position: null };
    }
    const ordered = positions
      .filter((position): position is NonNullable<typeof position> => Boolean(position))
      .sort(
        (left, right) =>
          left.table - right.table ||
          left.row - right.row ||
          left.column - right.column,
      );
    return { header, originalIndex, position: ordered[0] };
  });

  // Frozen records remain untouched. Only project a different display order when
  // every binding can be proven against the currently restored table geometry.
  if (positioned.some((item) => !item.position)) return headers;
  return positioned
    .slice()
    .sort(
      (left, right) =>
        left.position!.table - right.position!.table ||
        left.position!.row - right.position!.row ||
        left.position!.column - right.position!.column ||
        left.originalIndex - right.originalIndex,
    )
    .map((item) => item.header);
}

function matrixRecordFocus(
  node: RequirementMatrixNode,
  logicalTables: readonly RequirementMatrixLogicalTable[],
  tables: readonly StructuredTableModel[],
): MatrixTableRecordFocus | null {
  if (!node.table_record_id) return null;
  const logicalIds = new Set(node.logical_table_ids);
  const logicalTable =
    logicalTables.find((table) => logicalIds.has(table.logical_table_id)) ||
    logicalTables.find((table) => table.node_ids.includes(node.node_id));
  if (!logicalTable) return null;
  const projection = asRecord(logicalTable.projection);
  const record = records(projection.records).find(
    (candidate) =>
      String(candidate.record_id || candidate.id || "") ===
      node.table_record_id,
  );
  if (!record) return null;
  const rowBindings = axisBindings(record.row_bindings, "row_index");
  const columnBindings = axisBindings(record.column_bindings, "column_index");
  if (!rowBindings.length || !columnBindings.length) return null;
  const headers = headersInTableOrder(record.header_bindings, tables);
  const headerCellIdsByTable = cellIdsByTable(headers);
  let headerTexts = unique(headers.map((header) => header.text));
  const focusedTableId = rowBindings[0]?.tableId || "";
  const focusedTable = tables.find((table) => table.id === focusedTableId);
  const focusedRowIndex = Math.min(
    ...rowBindings
      .filter((binding) => binding.tableId === focusedTableId)
      .map((binding) => binding.index),
  );
  const localRepeatedHeaders = matchingLocalRepeatedHeaderCells(
    headers,
    tables,
    focusedTable,
    focusedRowIndex,
  );
  if (focusedTable && localRepeatedHeaders.length) {
    headerCellIdsByTable[focusedTable.id] = unique(
      localRepeatedHeaders.map((cell) => cell.id),
    );
    headerTexts = unique(
      localRepeatedHeaders.map((cell) => compactText(cell.text, "")),
    );
  }
  const shared = records(record.shared_merged_values).map((value) => ({
    tableId: String(value.table_id || ""),
    cellIds: [String(value.cell_id || "")].filter(Boolean),
    text: compactText(value.text, ""),
  }));
  const axes = new Set(headers.map((header) => header.axis));
  const explicitKind = String(
    record.record_kind || record.table_record_kind || record.type || "",
  ).toUpperCase();
  const mode =
    explicitKind === "MATRIX_INTERSECTION" ||
    (rowBindings.length === 1 &&
      columnBindings.length === 1 &&
      axes.has("ROW") &&
      axes.has("COLUMN"))
      ? "MATRIX_INTERSECTION"
      : "ROW";
  const imageIds = unique(
    [...records(record.figure_refs), ...records(record.image_refs)]
      .map((visual) =>
        String(visual.asset_id || visual.reference_id || visual.cell_id || ""),
      )
      .filter(Boolean),
  );
  return {
    recordId: node.table_record_id,
    recordLabel: matrixNodeLabel(node),
    mode,
    logicalTableId: logicalTable.logical_table_id,
    rowBindings,
    columnBindings,
    headerCellIdsByTable,
    headerTexts,
    sharedCellIdsByTable: cellIdsByTable(shared),
    sharedValues: unique(shared.map((value) => value.text)),
    notes: unique(
      records(record.notes).map((note) => compactText(note.text, "")),
    ),
    imageCount: imageIds.length,
    classificationLabel: node.candidate_review ? "候选判断" : asRecord(node.source_item).requirementness === "REQUIREMENT"
      ? ["需求", ...classificationLabels(node)].join(" · ") : asRecord(node.source_item).requirementness === "NON_REQUIREMENT" ? "非需求原文" : undefined,
    localHeaderTableId:
      focusedTable && localRepeatedHeaders.length ? focusedTable.id : "",
  };
}

function tableCandidates(
  node: RequirementMatrixNode,
  logicalTables: readonly RequirementMatrixLogicalTable[],
): JsonRecord[] {
  const candidates: JsonRecord[] = [];
  [...node.content_blocks, ...node.table_refs].forEach((block) => {
    const kind = String(block.kind || block.type || "").toLowerCase();
    if (kind === "table" || Array.isArray(block.cells)) candidates.push(block);
  });
  logicalTables.forEach((logicalTable) => {
    const projection = asRecord(logicalTable.projection);
    const tableFact = asRecord(projection.table_fact);
    if (Object.keys(tableFact).length) candidates.push(tableFact);
    candidates.push(...records(projection.tables));
    candidates.push(...records(projection.table_segments));
    candidates.push(...records(logicalTable.structured_segments));
  });
  return candidates;
}

function collectTables(
  node: RequirementMatrixNode,
  logicalTables: readonly RequirementMatrixLogicalTable[],
  pageSnapshot: PageSnapshot,
): StructuredTableModel[] {
  const physicalIds = new Set(
    logicalTables.flatMap((logicalTable) => logicalTable.physical_table_ids),
  );
  const logicalIds = new Set(node.logical_table_ids);
  const normalized = tableCandidates(node, logicalTables)
    .map(normalizeStructuredTable)
    .filter((table) => table.cells.length > 0);
  const pageTables = pageSnapshot.tables.filter(
    (table) =>
      physicalIds.has(table.id) ||
      logicalIds.has(table.logicalTableId) ||
      node.table_refs.some(
        (reference) =>
          String(reference.table_id || reference.id || "") === table.id,
      ),
  );
  const output = new Map<string, StructuredTableModel>();
  [...normalized, ...pageTables].forEach((table) => {
    if (!output.has(table.id) || output.get(table.id)!.cells.length < table.cells.length) {
      output.set(table.id, table);
    }
  });
  return Array.from(output.values()).sort(
    (left, right) =>
      left.segmentIndex - right.segmentIndex ||
      left.sourceOrder - right.sourceOrder,
  );
}

function blockKind(block: JsonRecord): string {
  return String(block.kind || block.type || "").toLowerCase();
}

function uniqueVisualBlocks(blocks: readonly JsonRecord[]): JsonRecord[] {
  const output = new Map<string, JsonRecord>();
  blocks.forEach((block, index) => {
    if (!["figure", "image"].includes(blockKind(block))) return;
    const location = asRecord(
      block.location || block.source_location || block.locator,
    );
    const identity = String(
      block.source_id ||
        block.source_node_id ||
        block.asset_id ||
        block.reference_id ||
        `${location.page_number || location.page || ""}:${JSON.stringify(location.bbox || [])}:${index}`,
    );
    if (!output.has(identity)) output.set(identity, block);
  });
  return Array.from(output.values());
}

export function MatrixSourceContent({
  node,
  contextPath,
  parentClause = null,
  logicalTables,
  recordAtomicNodes = [],
  pageSnapshot,
  projectId,
  documentId,
  sourceSha256,
  onPageChange,
}: {
  node: RequirementMatrixNode | null;
  contextPath: readonly RequirementMatrixNode[];
  parentClause?: RequirementMatrixNode | null;
  logicalTables: readonly RequirementMatrixLogicalTable[];
  recordAtomicNodes?: readonly RequirementMatrixNode[];
  pageSnapshot: PageSnapshot;
  projectId: string;
  documentId: string;
  sourceSha256: string;
  onPageChange: (page: number) => void;
}) {
  const recordSelection = Boolean(
    node?.table_record_id &&
      ["TABLE_RECORD", "ATOMIC_REQUIREMENT"].includes(
        String(node.display_role || "").toUpperCase(),
      ),
  );
  const contextNode = useMemo(() => {
    if (!node || recordSelection || node.display_role === "TABLE") {
      return node;
    }
    if (node.source_kind === "SOURCE_ITEM" && node.candidate_review) {
      // A document ancestor is navigation, not automatically an applicable
      // condition. Only show the original scope explicitly linked to this item.
      const scopeId = String(node.candidate_review.applicabilityScope || "");
      return contextPath.find((candidate) => candidate.node_id === scopeId) || node;
    }
    if (parentClause) return parentClause;
    if (node.source_item) return node;
    return (
      contextPath
        .slice()
        .reverse()
        .find(
          (candidate) =>
            candidate.node_id !== node.node_id &&
            ["TABLE_RECORD", "CLAUSE"].includes(candidate.display_role) &&
            (candidate.content_blocks.length > 0 ||
              Boolean(compactText(candidate.requirement_text || candidate.summary, ""))),
        ) || node
    );
  }, [contextPath, node, parentClause, recordSelection]);
  const tables = useMemo(
    () => {
      if (!node || !contextNode) return [];
      const output = new Map<string, StructuredTableModel>();
      const tableAncestors = contextPath.filter(
        (candidate) => candidate.display_role === "TABLE",
      );
      const sourceNodes = new Map<string, RequirementMatrixNode>();
      [contextNode, node, ...tableAncestors].forEach((candidate) =>
        sourceNodes.set(candidate.node_id, candidate),
      );
      sourceNodes.forEach((sourceNode) => {
        collectTables(sourceNode, logicalTables, pageSnapshot).forEach((table) => {
          const current = output.get(table.id);
          if (!current || current.cells.length < table.cells.length) {
            output.set(table.id, table);
          }
        });
      });
      return Array.from(output.values()).sort(
        (left, right) =>
          left.segmentIndex - right.segmentIndex ||
          left.sourceOrder - right.sourceOrder,
      );
    },
    [contextNode, contextPath, logicalTables, node, pageSnapshot],
  );
  const recordFocus = useMemo(
    () => (node ? matrixRecordFocus(node, logicalTables, tables) : null),
    [logicalTables, node, tables],
  );
  const displayedLogicalTables = logicalTables.filter((logical) =>
    logical.physical_table_ids.some((id) => tables.some((table) => table.id === id)),
  );
  const tableTitlePresentation = displayedLogicalTables.length === 1
    ? asRecord(asRecord(displayedLogicalTables[0].projection).title_presentation)
    : {};
  const agentTableDisplayLabel = tableTitlePresentation.authority === "AGENT_DISPLAY_LABEL"
    && typeof tableTitlePresentation.display_title === "string"
    ? tableTitlePresentation.display_title : undefined;
  const focusedTableId =
    recordFocus?.rowBindings[0]?.tableId ||
    recordFocus?.columnBindings[0]?.tableId ||
    "";
  const [showCompleteTable, setShowCompleteTable] = useState(false);
  const tableIdentity = tables.map((table) => table.id).join(":");
  useEffect(() => {
    setShowCompleteTable(false);
  }, [node?.node_id, tableIdentity]);
  const recordSegment =
    tables.find((table) => table.id === focusedTableId) || tables[0];
  const recordRowIndex = Math.min(
    ...(recordFocus?.rowBindings
      .filter((binding) => binding.tableId === recordSegment?.id)
      .map((binding) => binding.index) || []),
  );
  const recordSegmentHasLocalHeader = Boolean(
    recordSegment &&
      Number.isFinite(recordRowIndex) &&
      recordFocus?.localHeaderTableId === recordSegment.id,
  );
  const recordDisplayTables = recordSegmentHasLocalHeader && recordSegment
    ? [recordSegment]
    : tables;
  const logicalRecordCount = Math.max(
    0,
    ...logicalTables.map((table) => Number(table.record_count || 0)),
  );
  if (!node || !contextNode) {
    return (
      <EmptyState
        icon="tree"
        title="选择矩阵中的内容"
        description="这里会还原完整条款、表格与图片，并保留它们在文档中的上下文。"
      />
    );
  }
  const humanDisplayText = String(contextNode.display_text || "").trim();
  const textBlocks = contextNode.content_blocks.filter((block) => {
    if (humanDisplayText) return false;
    const kind = blockKind(block);
    return !["table", "figure", "image", "table_record"].includes(kind) &&
      Boolean(compactText(block.text || block.raw_text, ""));
  });
  const visualBlocks = recordSelection
    ? []
    : uniqueVisualBlocks([
        ...contextNode.content_blocks,
        ...contextNode.figure_refs,
      ]);
  const primaryText =
    humanDisplayText ||
    (contextNode.child_ids.length > 0
      ? ""
      : compactText(contextNode.requirement_text || contextNode.summary, ""));
  const hasPrimaryText = Boolean(primaryText);
  const showingParentContext = contextNode.node_id !== node.node_id;
  const selectedBoundaryText =
    String(node.display_text || "").trim() ||
    (node.child_ids.length > 0
      ? matrixNodeLabel(node)
      : compactText(
          node.requirement_text || node.summary || node.label,
          "当前内容边界",
        ));

  return (
    <article className="matrix-source-content">
      <header className="matrix-source-heading">
        <div>
          <span>
            {recordSelection
              ? "当前表格条目"
              : showingParentContext
                ? "完整父条款"
                : contextNode.source_title_role === "SOURCE_EXCERPT"
                  ? "原文片段（无独立标题）"
                : "完整来源内容"}
            {contextNode.source_title_role === "AGENT_ASSIGNED" ? " · Agent 拟名/编号" : ""}
          </span>
          <h2>{matrixNodeLabel(recordSelection ? node : contextNode)}</h2>
        </div>
        {node.page_start && (
          <button
            className="source-page-jump"
            type="button"
            onClick={() => onPageChange(node.page_start || 1)}
          >
            <Icon name="evidence" size={14} />
            第 {node.page_start}
            {node.page_end && node.page_end !== node.page_start
              ? `–${node.page_end}`
              : ""}{" "}
            页
          </button>
        )}
      </header>

      {recordSelection && parentClause && (
        <p className="matrix-record-parent-context">
          所属条款：{matrixNodeLabel(parentClause)}
        </p>
      )}

      {!recordSelection && showingParentContext && (
        <section className="matrix-selected-boundary" aria-label="当前选中内容边界">
          <span>当前选中</span>
          <strong>{matrixNodeLabel(node)}</strong>
          <p>{selectedBoundaryText}</p>
        </section>
      )}

      {showingParentContext && Boolean(node.source_excerpts?.length) && (
        <section className="matrix-source-excerpts" aria-label="本条派生依据的原文">
          <p className="section-label">本条依据的原文</p>
          {node.source_excerpts?.map((excerpt) => (
            <div key={excerpt.id}>
              <span>{excerpt.role === "CONTEXT" ? "共用上下文" : "直接依据"}</span>
              <blockquote>{excerpt.text}</blockquote>
              {excerpt.pages.map((page) => <button key={page} type="button" className="source-page-jump"
                onClick={() => onPageChange(page)}>原文第 {page} 页</button>)}
            </div>
          ))}
        </section>
      )}

      {!recordSelection && hasPrimaryText && (
        <section className="matrix-primary-copy">
          <p>{primaryText}</p>
        </section>
      )}

      {!recordSelection && textBlocks.map((block, index) => {
        const text = compactText(block.text || block.raw_text, "");
        if (!text || text === primaryText) return null;
        return (
          <p className="matrix-source-paragraph" key={String(block.id || index)}>
            {text}
          </p>
        );
      })}

      {tables.length > 0 && (
        <section className="matrix-table-focus" aria-label="表格还原与条目定位">
          <div className="matrix-table-focus-intro">
            <div>
              <span>{recordFocus ? "当前记录" : "完整表格"}</span>
              <strong>
                {recordFocus
                  ? "表头、共用值、备注与图片保持原样"
                  : "按原始行列与合并关系还原"}
              </strong>
            </div>
            <div className="matrix-table-summary">
              {logicalRecordCount > 0 && <span>{logicalRecordCount} 条记录</span>}
              {recordFocus?.imageCount ? (
                <em>{recordFocus.imageCount} 张图片</em>
              ) : recordFocus ? (
                <em>当前行已定位</em>
              ) : null}
            </div>
          </div>
          {recordFocus && recordSegment && (
            <section className="matrix-record-detail">
              <StructuredTable
                table={recordSegment}
                tables={recordDisplayTables}
                displayLabel={agentTableDisplayLabel || recordFocus.recordLabel}
                displayLabelAuthority={agentTableDisplayLabel ? "AGENT_DISPLAY_LABEL" : undefined}
                fitContainer
                projectId={projectId}
                documentId={documentId}
                sourceSha256={sourceSha256}
                onPageChange={onPageChange}
                recordFocus={recordFocus}
                compactRecord
              />
              {recordAtomicNodes.length ? (
                <section
                  className="matrix-record-atoms"
                  aria-label="记录内原子需求"
                >
                  <header>
                    <span>由当前记录细化的需求</span>
                    <strong>{recordAtomicNodes.length} 项</strong>
                  </header>
                  <div>
                    {recordAtomicNodes.map((atomicNode) => (
                      <article
                        className={
                          atomicNode.node_id === node.node_id
                            ? "is-current"
                            : undefined
                        }
                        key={atomicNode.node_id}
                      >
                        <strong>{matrixNodeLabel(atomicNode)}</strong>
                        <p>
                          {compactText(
                            atomicNode.requirement_text || atomicNode.summary,
                            "该原子项保留当前记录的完整表头与共用值。",
                          )}
                        </p>
                      </article>
                    ))}
                  </div>
                </section>
              ) : (
                <p className="matrix-record-retained">
                  当前原文条目保持整体；必要且获授权时可形成派生子项，原表内容保持完整。
                </p>
              )}
              <button
                className="matrix-complete-table-toggle"
                type="button"
                aria-expanded={showCompleteTable}
                onClick={() => setShowCompleteTable((current) => !current)}
              >
                <Icon
                  name={showCompleteTable ? "chevron-down" : "chevron"}
                  size={14}
                />
                {showCompleteTable ? "收起完整原表" : "查看完整原表"}
              </button>
            </section>
          )}
          {(!recordFocus || showCompleteTable) && (
            tables[0] && (
              <div className="matrix-complete-table">
                <StructuredTable
                  table={tables[0]}
                  tables={tables}
                  displayLabel={agentTableDisplayLabel || matrixNodeLabel(contextNode)}
                  displayLabelAuthority={agentTableDisplayLabel ? "AGENT_DISPLAY_LABEL" : undefined}
                  fitContainer
                  projectId={projectId}
                  documentId={documentId}
                  sourceSha256={sourceSha256}
                  onPageChange={onPageChange}
                  recordFocus={recordFocus}
                  compactRecord={false}
                  initialRowLimit={72}
                />
              </div>
            )
          )}
        </section>
      )}

      {visualBlocks.map((block, index) => {
        const geometry = sourceGeometryForContentBlock(block, pageSnapshot.blocks);
        const url = geometry
          ? sourceCropUrl({
              projectId,
              documentId,
              sourceSha256,
              page: geometry.page,
              bbox: geometry.bbox,
            })
          : "";
        return url ? (
          <figure className="matrix-source-figure" key={String(block.id || index)}>
            <img
              src={url}
              alt={compactText(block.caption || block.text, "原文图片")}
              width={1200}
              height={900}
              loading="lazy"
              decoding="async"
            />
            <figcaption>
              {compactText(block.caption || block.text, "原文图片")}
              {geometry && ` · 第 ${geometry.page} 页`}
            </figcaption>
          </figure>
        ) : null;
      })}

      {!hasPrimaryText && !textBlocks.length && !tables.length && !visualBlocks.length && (
        <EmptyState
          icon="document"
          title="当前节点没有独立内容块"
          description="它仍保留在层级中；可在原 PDF 中核对其范围和子节点。"
        />
      )}
    </article>
  );
}
