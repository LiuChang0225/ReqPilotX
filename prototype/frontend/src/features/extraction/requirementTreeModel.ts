import type {
  RequirementTreeNode,
  RequirementTreeSnapshot,
} from "../../shared/api/contracts";
import type { StructuredTableRecordFocus } from "../restoration/restorationModel";

export type RequirementTreeFilter = "all" | "attention" | "items";

export interface VisibleRequirementTreeNode {
  node: RequirementTreeNode;
  depth: number;
  hasChildren: boolean;
  expanded: boolean;
}

export interface ReviewCandidateRoleCounts {
  total: number;
  requirements: number;
  directContents: number;
  wholeTables: number;
  tableRecords: number;
  structureProxies: number;
}

export interface RequirementTreeTableFocus {
  nodeId: string;
  structureNodeId: string;
  tableRecordId: string;
  logicalTableIds: string[];
}

export function requirementTreeIndex(tree: RequirementTreeSnapshot | null) {
  return new Map((tree?.nodes || []).map((node) => [node.id, node]));
}

function logicalTableIds(node: RequirementTreeNode): string[] {
  return unique(
    [...records(node.table_refs), ...records(node.content_blocks)]
      .map((item) =>
        String(item.logical_table_id || item.table_id || item.id || ""),
      )
      .filter(Boolean),
  );
}

export function nearestRequirementTreeTableNode(
  node: RequirementTreeNode | null,
  tree: RequirementTreeSnapshot | null,
): RequirementTreeNode | null {
  if (!node || !tree) return null;
  const index = requirementTreeIndex(tree);
  let current: RequirementTreeNode | undefined = node;
  const visited = new Set<string>();
  while (current && !visited.has(current.id)) {
    visited.add(current.id);
    if (current.kind === "TABLE_RECORD" || current.kind === "TABLE") {
      return current;
    }
    current = current.parent_id
      ? index.get(current.parent_id)
      : undefined;
  }
  return null;
}

export function requirementTreeTableFocus(
  node: RequirementTreeNode | null,
  tree: RequirementTreeSnapshot | null,
  additionalLogicalTableIds: readonly string[] = [],
): RequirementTreeTableFocus | null {
  const tableNode = nearestRequirementTreeTableNode(node, tree);
  if (!tableNode) return null;
  return {
    nodeId: tableNode.id,
    structureNodeId: String(tableNode.structure_node_id || ""),
    tableRecordId: String(tableNode.table_record_id || ""),
    logicalTableIds: unique([
      ...logicalTableIds(tableNode),
      ...additionalLogicalTableIds.map(String).filter(Boolean),
    ]),
  };
}

export function matchingRequirementTreeTableNode(
  tree: RequirementTreeSnapshot | null,
  focus: RequirementTreeTableFocus | null,
): RequirementTreeNode | null {
  if (!tree || !focus) return null;
  const candidates = tree.nodes.filter(
    (node) => node.kind === "TABLE_RECORD" || node.kind === "TABLE",
  );
  const exact = candidates.find((node) => node.id === focus.nodeId);
  if (exact) return exact;
  if (focus.tableRecordId) {
    const record = candidates.find(
      (node) => String(node.table_record_id || "") === focus.tableRecordId,
    );
    if (record) return record;
  }
  const logicalIds = new Set(focus.logicalTableIds);
  if (logicalIds.size) {
    const logical = candidates.find((node) =>
      logicalTableIds(node).some((id) => logicalIds.has(id)),
    );
    if (logical) return logical;
  }
  if (!focus.structureNodeId) return null;
  return (
    candidates.find(
      (node) =>
        String(node.structure_node_id || "") === focus.structureNodeId &&
        node.kind === "TABLE_RECORD",
    ) ||
    candidates.find(
      (node) =>
        String(node.structure_node_id || "") === focus.structureNodeId,
    ) ||
    null
  );
}

export function isRequirementTreeReviewItem(
  node: RequirementTreeNode | null,
): boolean {
  if (!node) return false;
  return (
    [
      "STRUCTURE_PROXY",
      "DIRECT_CONTENT",
      "WHOLE_TABLE_REVIEW",
      "TABLE_RECORD",
      "REQUIREMENT",
    ].includes(
      node.projection_role || "",
    ) ||
    ["TABLE_RECORD", "REQUIREMENT"].includes(node.kind)
  );
}

export function isDirectContentReviewNode(
  node: RequirementTreeNode | null,
): boolean {
  return node?.projection_role === "DIRECT_CONTENT";
}

export function requirementTreeEditableStructureNodeIds(
  node: RequirementTreeNode | null,
): string[] {
  if (!node) return [];
  const explicit = unique(
    (Array.isArray(node.editable_structure_node_ids)
      ? node.editable_structure_node_ids
      : []
    )
      .map((value) => String(value || "").trim())
      .filter(Boolean),
  );
  if (explicit.length > 0) return explicit;
  // A DIRECT_CONTENT row is a projection over one or more physical CONTENT
  // nodes. Its structure_node_id is the reading owner, not an editable target.
  // Old snapshots without the explicit binding must remain read-only instead
  // of silently moving their parent clause.
  if (isDirectContentReviewNode(node)) return [];
  // Semantic/table projections point at a reading owner.  Only one-to-one
  // Structure rows may use their own identity as the edit target.
  if (!["STRUCTURE", "STRUCTURE_PROXY"].includes(node.projection_role || "")) {
    return [];
  }
  const structureNodeId = String(node.structure_node_id || "").trim();
  return structureNodeId && node.id === structureNodeId ? [structureNodeId] : [];
}

export function isWholeTableReviewNode(
  node: RequirementTreeNode | null,
): boolean {
  return node?.projection_role === "WHOLE_TABLE_REVIEW";
}

function normalizedTreeRoleToken(value: unknown): string {
  return String(value || "")
    .trim()
    .replace(/[\s_-]+/g, "")
    .toUpperCase();
}

const TOC_ROLE_TOKENS = new Set(["TOC", "TOCENTRY", "TABLEOFCONTENTS"]);
export function isRequirementTreeTocNode(
  node: RequirementTreeNode | null,
): boolean {
  if (!node || isPublishedRequirementNode(node)) return false;
  if (
    [node.kind, node.classification].some((value) =>
      TOC_ROLE_TOKENS.has(normalizedTreeRoleToken(value)),
    )
  ) {
    return true;
  }
  if (
    records(node.source_refs).some((ref) =>
      TOC_ROLE_TOKENS.has(normalizedTreeRoleToken(ref.source_kind)),
    )
  ) {
    return true;
  }
  return Array.isArray(node.toc_entries) && node.toc_entries.length > 0;
}

export function requirementTreeNodeDisplayLabel(
  node: RequirementTreeNode,
): string {
  const label = String(node.label || "").trim();
  if (label) return label;
  if (isWholeTableReviewNode(node)) return "完整表格";
  const summary = String(node.summary || "").trim();
  if (isDirectContentReviewNode(node)) {
    return summary || "条款内容";
  }
  return summary || "未命名内容";
}

export function isPublishedRequirementNode(
  node: RequirementTreeNode | null,
): boolean {
  if (!node) return false;
  // A projection role is authoritative when present. DIRECT_CONTENT may use a
  // requirement-shaped leaf for review, but remains a candidate until the
  // reviewer explicitly promotes it.
  if (node.projection_role) return node.projection_role === "REQUIREMENT";
  return node.kind === "REQUIREMENT";
}

export function isExactRequirementTreeContentLeaf(
  node: RequirementTreeNode | null,
): boolean {
  return Boolean(
    node &&
      (isDirectContentReviewNode(node) ||
        isWholeTableReviewNode(node) ||
        node.projection_role === "TABLE_RECORD" ||
        node.projection_role === "REQUIREMENT" ||
        node.kind === "TABLE_RECORD" ||
        (node.kind === "REQUIREMENT" && !node.projection_role)),
  );
}

export function requirementTreeReviewCandidateLeaves(
  tree: RequirementTreeSnapshot | null,
): RequirementTreeNode[] {
  if (!tree) return [];
  const index = requirementTreeIndex(tree);
  const hasReviewDescendant = (node: RequirementTreeNode): boolean => {
    const pending = [...node.child_ids];
    const visited = new Set<string>();
    while (pending.length) {
      const childId = pending.shift();
      if (!childId || visited.has(childId)) continue;
      visited.add(childId);
      const child = index.get(childId);
      if (!child) continue;
      if (isRequirementTreeReviewItem(child)) return true;
      pending.push(...child.child_ids);
    }
    return false;
  };
  return tree.nodes
    .filter(
      (node) =>
        node.include_in_view !== false &&
        isRequirementTreeReviewItem(node) &&
        !hasReviewDescendant(node),
    )
    .sort(
      (left, right) =>
        left.source_order - right.source_order || left.id.localeCompare(right.id),
    );
}

export function requirementTreeReviewCandidateRoleCounts(
  tree: RequirementTreeSnapshot | null,
): ReviewCandidateRoleCounts {
  const leaves = requirementTreeReviewCandidateLeaves(tree);
  return {
    total: leaves.length,
    requirements: leaves.filter((node) => isPublishedRequirementNode(node))
      .length,
    directContents: leaves.filter((node) => isDirectContentReviewNode(node))
      .length,
    wholeTables: leaves.filter((node) => isWholeTableReviewNode(node)).length,
    tableRecords: leaves.filter(
      (node) =>
        node.kind === "TABLE_RECORD" ||
        node.projection_role === "TABLE_RECORD",
    ).length,
    structureProxies: leaves.filter(
      (node) => node.projection_role === "STRUCTURE_PROXY",
    ).length,
  };
}

export function requirementTreeNodeStatusLabel(
  node: RequirementTreeNode,
): string {
  return requirementTreeNodeRoleLabel(node);
}

export function requirementTreeNodeRoleLabel(
  node: RequirementTreeNode,
): string {
  if (isPublishedRequirementNode(node)) {
    if (node.status === "REVIEWED" || node.semantic_state === "CONFIRMED") {
      return "已确认需求";
    }
    if (node.attention_count > 0 || node.status === "NEEDS_ATTENTION") {
      return "需求 · 需关注";
    }
    return "需求 · 待确认";
  }
  if (isRequirementTreeTocNode(node)) return "目录";
  if (isWholeTableReviewNode(node) || node.kind === "TABLE") {
    return "完整表格 · 未做语义判断";
  }
  if (
    node.kind === "TABLE_RECORD" ||
    node.projection_role === "TABLE_RECORD"
  ) {
    return "表格条目 · 未做语义判断";
  }
  if (
    ["STRUCTURE_PROXY", "DIRECT_CONTENT"].includes(
      node.projection_role || "",
    ) ||
    node.kind === "DIRECT_CONTENT" ||
    (node.kind === "REQUIREMENT" && !isPublishedRequirementNode(node))
  ) {
    return "条款内容 · 未做语义判断";
  }
  return "仅标题";
}

export function requirementTreeRowSecondaryLabel(
  node: RequirementTreeNode,
): string {
  return node.kind === "DOCUMENT" ? "" : requirementTreeNodeRoleLabel(node);
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is Record<string, unknown> =>
          Boolean(item) && typeof item === "object" && !Array.isArray(item),
      )
    : [];
}

function stringValues(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map(String).filter((item) => item.trim().length > 0)
    : [];
}

function unique(values: string[]): string[] {
  return Array.from(new Set(values));
}

function axisBindings(
  value: unknown,
  indexKey: "row_index" | "column_index",
): StructuredTableRecordFocus["rowBindings"] {
  return records(value)
    .map((binding) => ({
      tableId: String(binding.table_id || ""),
      index: Number(binding[indexKey]),
      cellIds: unique(stringValues(binding.cell_ids)),
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

function recordProjectionSource(
  node: RequirementTreeNode,
  tree: RequirementTreeSnapshot | null,
): Record<string, unknown> | null {
  const recordId = String(node.table_record_id || node.id || "");
  const direct = records(node.content_blocks).find(
    (block) =>
      String(block.kind || "").toLowerCase() === "table_record" &&
      String(block.record_id || "") === recordId,
  );
  if (direct) return direct;
  for (const projection of records(tree?.table_record_projections)) {
    const record = records(projection.records).find(
      (candidate) => String(candidate.record_id || "") === recordId,
    );
    if (record) {
      const tableFact =
        projection.table_fact &&
        typeof projection.table_fact === "object" &&
        !Array.isArray(projection.table_fact)
          ? (projection.table_fact as Record<string, unknown>)
          : {};
      return {
        ...record,
        logical_table_id:
          record.logical_table_id || tableFact.logical_table_id || "",
      };
    }
  }
  return null;
}

export function tableRecordFocus(
  node: RequirementTreeNode | null,
  tree: RequirementTreeSnapshot | null,
): StructuredTableRecordFocus | null {
  if (
    !node ||
    (node.kind !== "TABLE_RECORD" &&
      node.projection_role !== "TABLE_RECORD")
  ) {
    return null;
  }
  const record = recordProjectionSource(node, tree);
  if (!record) return null;
  const rowBindings = axisBindings(record.row_bindings, "row_index");
  const columnBindings = axisBindings(
    record.column_bindings,
    "column_index",
  );
  if (!rowBindings.length || !columnBindings.length) return null;

  const headers = records(record.header_bindings).map((header) => ({
    tableId: String(header.table_id || ""),
    cellIds: unique(stringValues(header.cell_ids)),
    axis: String(header.axis || "").toUpperCase(),
    text: String(header.text || "").trim(),
  }));
  const sharedValues = records(record.shared_merged_values).map((value) => ({
    tableId: String(value.table_id || ""),
    cellIds: [String(value.cell_id || "")].filter(Boolean),
    text: String(value.text || "").trim(),
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
  const visualRefs = [
    ...records(record.figure_refs),
    ...records(record.image_refs),
    ...records(node.figure_refs),
  ];
  const imageIds = unique(
    visualRefs
      .map((visual) =>
        String(
          visual.asset_id ||
            visual.reference_id ||
            visual.cell_id ||
            "",
        ),
      )
      .filter(Boolean),
  );

  return {
    recordId: String(record.record_id || node.table_record_id || node.id),
    recordLabel: node.label || node.summary || "未命名表格条目",
    mode,
    logicalTableId: String(record.logical_table_id || ""),
    rowBindings,
    columnBindings,
    headerCellIdsByTable: cellIdsByTable(headers),
    headerTexts: unique(
      headers.map((header) => header.text).filter(Boolean),
    ),
    sharedCellIdsByTable: cellIdsByTable(sharedValues),
    sharedValues: unique(
      sharedValues.map((value) => value.text).filter(Boolean),
    ),
    notes: unique(
      records(record.notes)
        .map((note) => String(note.text || "").trim())
        .filter(Boolean),
    ),
    imageCount: imageIds.length,
  };
}

function matchingNodeIds(
  tree: RequirementTreeSnapshot,
  query: string,
  filter: RequirementTreeFilter,
): Set<string> | null {
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized && filter === "all") return null;
  const index = requirementTreeIndex(tree);
  const matches = new Set<string>();
  for (const node of tree.nodes) {
    const queryMatch =
      !normalized ||
      `${node.label} ${node.summary} ${node.classification}`
        .toLocaleLowerCase()
        .includes(normalized);
    const filterMatch =
      filter === "all" ||
      (filter === "attention" && node.attention_count > 0) ||
      (filter === "items" && isRequirementTreeReviewItem(node));
    if (!queryMatch || !filterMatch) continue;
    matches.add(node.id);
    let parentId = node.parent_id;
    const visited = new Set<string>();
    while (parentId && !visited.has(parentId)) {
      visited.add(parentId);
      matches.add(parentId);
      parentId = index.get(parentId)?.parent_id || null;
    }
  }
  return matches;
}

function projectedChildIds(
  node: RequirementTreeNode,
  index: ReadonlyMap<string, RequirementTreeNode>,
  included: ReadonlySet<string> | null,
): string[] {
  return node.child_ids.filter(
    (childId) => index.has(childId) && (!included || included.has(childId)),
  );
}

export function expandableRequirementTreeIds(
  tree: RequirementTreeSnapshot | null,
  query = "",
  filter: RequirementTreeFilter = "all",
): Set<string> {
  if (!tree) return new Set();
  const index = requirementTreeIndex(tree);
  const included = matchingNodeIds(tree, query, filter);
  return new Set(
    tree.nodes
      .filter(
        (node) =>
          (!included || included.has(node.id)) &&
          projectedChildIds(node, index, included).length > 0,
      )
      .map((node) => node.id),
  );
}

export function clauseLevelExpandedIds(
  tree: RequirementTreeSnapshot | null,
  query = "",
  filter: RequirementTreeFilter = "all",
): Set<string> {
  if (!tree) return new Set();
  const expandable = expandableRequirementTreeIds(tree, query, filter);
  return new Set(
    tree.nodes
      .filter(
        (node) =>
          expandable.has(node.id) &&
          ![
            "CLAUSE",
            "LIST_GROUP",
            "TABLE",
            "TABLE_RECORD",
            "REQUIREMENT",
          ].includes(node.kind),
      )
      .map((node) => node.id),
  );
}

export function subtreeExpandableRequirementTreeIds(
  tree: RequirementTreeSnapshot | null,
  nodeId: string,
  query = "",
  filter: RequirementTreeFilter = "all",
): Set<string> {
  if (!tree || !nodeId) return new Set();
  const index = requirementTreeIndex(tree);
  const expandable = expandableRequirementTreeIds(tree, query, filter);
  const result = new Set<string>();
  const pending = [nodeId];
  const visited = new Set<string>();
  while (pending.length) {
    const currentId = pending.pop();
    if (!currentId || visited.has(currentId)) continue;
    visited.add(currentId);
    const node = index.get(currentId);
    if (!node) continue;
    if (expandable.has(currentId)) result.add(currentId);
    pending.push(...node.child_ids);
  }
  return result;
}

export function requirementTreeAncestorIds(
  tree: RequirementTreeSnapshot | null,
  nodeId: string,
): Set<string> {
  if (!tree || !nodeId) return new Set();
  const index = requirementTreeIndex(tree);
  const result = new Set<string>();
  const visited = new Set<string>();
  let parentId = index.get(nodeId)?.parent_id || null;
  while (parentId && !visited.has(parentId)) {
    visited.add(parentId);
    result.add(parentId);
    parentId = index.get(parentId)?.parent_id || null;
  }
  return result;
}

export function flattenRequirementTree(
  tree: RequirementTreeSnapshot | null,
  expandedIds: ReadonlySet<string>,
  query = "",
  filter: RequirementTreeFilter = "all",
): VisibleRequirementTreeNode[] {
  if (!tree) return [];
  const index = requirementTreeIndex(tree);
  const included = matchingNodeIds(tree, query, filter);
  const rows: VisibleRequirementTreeNode[] = [];
  const walk = (nodeId: string, depth: number, active: Set<string>) => {
    if (active.has(nodeId)) return;
    const node = index.get(nodeId);
    if (!node || (included && !included.has(nodeId))) return;
    active.add(nodeId);
    const childIds = projectedChildIds(node, index, included);
    const expanded = expandedIds.has(nodeId);
    rows.push({
      node,
      depth,
      hasChildren: childIds.length > 0,
      expanded,
    });
    if (expanded) {
      childIds.forEach((childId) => walk(childId, depth + 1, active));
    }
    active.delete(nodeId);
  };
  tree.root_node_ids.forEach((rootId) => walk(rootId, 0, new Set()));
  return rows;
}

export function initialVisibleSelectionId(
  rows: readonly VisibleRequirementTreeNode[],
): string {
  return (
    rows.find((row) => isRequirementTreeReviewItem(row.node))?.node.id ||
    rows[0]?.node.id ||
    ""
  );
}

export function parentClause(
  node: RequirementTreeNode | null,
  tree: RequirementTreeSnapshot | null,
): RequirementTreeNode | null {
  if (!node || !tree) return node;
  // Structure containers own a complete reading scope. In particular a
  // STRUCTURE_PROXY is not a synthetic leaf: it is a clause/section whose
  // direct content happened to be reviewable without semantic classification.
  // Moving it to its parent makes the title, middle content and PDF focus refer
  // to three different scopes (for example selecting 7.5.2 rendered 7.5).
  if (
    node.projection_role === "STRUCTURE_PROXY" ||
    ["SECTION", "CLAUSE", "LIST_GROUP", "GROUP"].includes(node.kind)
  ) {
    return node;
  }
  // A complete table is itself the useful context. Records nested below it
  // return to this owner so shared headers and merged cells remain visible.
  if (node.kind === "TABLE" || isWholeTableReviewNode(node)) return node;
  if (!isRequirementTreeReviewItem(node)) return node;
  const index = requirementTreeIndex(tree);
  let parentId = node.parent_id;
  let tableFallback: RequirementTreeNode | null = null;
  const visited = new Set<string>();
  while (parentId && !visited.has(parentId)) {
    visited.add(parentId);
    const parent = index.get(parentId);
    if (!parent) return null;
    if (parent.kind === "TABLE" && !tableFallback) {
      tableFallback = parent;
      if (
        node.kind === "TABLE_RECORD" ||
        node.projection_role === "TABLE_RECORD"
      ) {
        return parent;
      }
    }
    if (
      ["CLAUSE", "SECTION", "LIST_GROUP"].includes(parent.kind) ||
      (parent.kind === "GROUP" && parent.parent_id !== null)
    ) {
      return parent;
    }
    parentId = parent.parent_id;
  }
  // Table-only sources may not have a clause ancestor. Keep their complete
  // table as context instead of falling back to the record leaf.
  return tableFallback || node;
}

export function initialExpandedIds(
  tree: RequirementTreeSnapshot | null,
): Set<string> {
  if (!tree) return new Set();
  return new Set(
    tree.nodes
      .filter((node) => node.depth < 2 && node.child_ids.length > 0)
      .map((node) => node.id),
  );
}
