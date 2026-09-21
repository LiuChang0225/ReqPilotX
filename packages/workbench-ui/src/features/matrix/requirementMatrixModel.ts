import { EMPTY_CLASSIFICATION_FILTER, matchesClassificationFacets, type ClassificationFacetFilter } from "./classificationFacets";
import type {
  RequirementMatrixNode,
  RequirementMatrixSnapshot,
} from "../../shared/api/contracts";
import { compactText } from "../../shared/lib/format";

export type RequirementMatrixLens = "review" | "analysis";
export type RequirementMatrixFilter =
  | "all"
  | "analysis"
  | "pending"
  | "attention"
  | "tables"
  | "excluded";

export interface RequirementMatrixRow {
  node: RequirementMatrixNode;
  depth: number;
  hasChildren: boolean;
  expanded: boolean;
  contentNature: string;
  analysisStatus: string;
  aiSuggestion: string;
  humanDecision: string;
}

export interface RequirementMatrixMatchCounts {
  matched: number;
  retainedAncestors: number;
}

export type MatrixConclusionSource =
  | "NONE"
  | "AI_DEFAULT"
  | "HUMAN_OVERRIDE"
  | "HUMAN_NATIVE"
  | "STALE_REFERENCE";

export interface MatrixConclusionContract {
  analysisTarget: boolean;
  effectiveConclusion: string;
  conclusionSource: MatrixConclusionSource;
  overridesAi: boolean;
}

export type MatrixStatusTone =
  | "neutral"
  | "success"
  | "warning"
  | "danger"
  | "info";

const ACTIVE_AI_ANALYSIS_STATUSES = new Set([
  "QUEUED",
  "PENDING",
  "STARTING",
  "RUNNING",
  "CANCEL_REQUESTED",
  "PUBLISHING",
]);

export function isAiAnalysisActiveStatus(status: unknown): boolean {
  return ACTIVE_AI_ANALYSIS_STATUSES.has(String(status || "").toUpperCase());
}

function normalized(value: unknown): string {
  return String(value || "").trim().toLocaleLowerCase();
}

export function matrixNodeLabel(node: RequirementMatrixNode): string {
  const explicit = compactText(node.display_title, "");
  if (explicit) return explicit;
  const label = compactText(
    node.label || node.requirement_text || node.summary,
    "未命名内容",
  );
  if (String(node.display_role).toUpperCase() === "TABLE_RECORD") {
    return compactText(label.split(" · ", 1)[0], "表格条目");
  }
  const code = compactText(node.requirement_code, "");
  if (code && label === code) {
    return compactText(node.requirement_text || node.summary, "需求条目");
  }
  return label;
}

export function matrixSourceNumber(node: RequirementMatrixNode): string {
  const explicit = compactText(node.source_number, "");
  if (explicit) return explicit;
  const legacyCode = compactText(node.requirement_code, "");
  return /^(?:\d+(?:\.\d+)*|[A-Za-z]|[一二三四五六七八九十百千]+)[、.)）]?$/.test(
    legacyCode,
  )
    ? legacyCode
    : "";
}

export function matrixDisplayPreview(node: RequirementMatrixNode): string {
  const explicit = compactText(node.display_text, "");
  if (explicit && explicit !== matrixNodeLabel(node)) return explicit;
  if (String(node.display_role).toUpperCase() === "TABLE_RECORD") {
    const label = compactText(node.label, "");
    const parts = label.split(" · ");
    return parts.length > 1 ? compactText(parts.slice(1).join(" · "), "") : "";
  }
  const summary = compactText(node.summary, "");
  if (node.child_ids.length > 0) return "";
  const title = matrixNodeLabel(node);
  const sourceNumber = matrixSourceNumber(node);
  const headingOnly = new Set(
    [
      title,
      compactText(node.label, ""),
      sourceNumber && title ? `${sourceNumber} ${title}` : "",
      sourceNumber && title ? `${sourceNumber}${title}` : "",
    ].filter(Boolean),
  );
  return summary.includes(" | ") || headingOnly.has(summary)
    ? ""
    : summary;
}

export function matrixHierarchyLabel(node: RequirementMatrixNode): string {
  if (node.source_kind === "DERIVED_REQUIREMENT") return "派生需求";
  if (node.source_kind === "SINGULAR_REQUIREMENT") return "派生条目";
  const sourceLabels: Record<string, string> = {
    DOCUMENT: "文档",
    DOCUMENT_ROOT: "文档",
    DOCUMENT_UNIT: "文档",
    ATTACHMENT: "附件",
    COVER: "封面",
    TABLE_OF_CONTENTS: "目录",
    DOCUMENT_CONTROL: "文控信息",
    GROUP: "章节组",
    SECTION: "章节",
    SUBSECTION: "子章节",
    APPENDIX: "附录",
    ANNEX: "附录",
    CLAUSE: "条款",
    SUBCLAUSE: "子条款",
    LIST_GROUP: "条目组",
    LIST_ITEM: "条目",
    DIRECT_CONTENT: "正文",
    CONTENT: "正文",
    TABLE: "表格",
    TABLE_RECORD: "表格条目",
    REQUIREMENT: "原子需求",
    FIGURE: "图片",
    IMAGE: "图片",
  };
  const sourceKind = String(node.source_kind || "").toUpperCase();
  if (sourceKind === "SOURCE_ITEM") {
    const form = (node.source_item as { source_form?: string } | undefined)?.source_form;
    if (form === "TABLE_BRANCH") return "表格分支";
    if (node.display_role === "TABLE") return "表格";
    if (node.display_role === "TABLE_RECORD") return "表格条目";
    if (form === "IMAGE") return "图片";
    if (node.display_role === "CLAUSE" || form === "NUMBERED_CLAUSE" || form === "NATIVE_LIST") return "条目";
  }
  if (sourceKind === "SECTION") {
    return Number(node.depth || 0) <= 1 ? "章节" : "子章节";
  }
  if (sourceKind === "CLAUSE") {
    return Number(node.depth || 0) <= 2 ? "条款" : "子条款";
  }
  if (sourceLabels[sourceKind]) return sourceLabels[sourceKind];
  const labels: Record<string, string> = {
    STRUCTURE: "章节",
    CLAUSE: "条款",
    TABLE: "表格",
    TABLE_RECORD: "表格条目",
    REQUIREMENT: "原子需求",
    ATOMIC_REQUIREMENT: "原子需求",
    CONTEXT: "上下文",
  };
  return labels[String(node.display_role || "").toUpperCase()] || "内容";
}

export function matrixContentNature(node: RequirementMatrixNode): string {
  return matrixHierarchyLabel(node);
}

export function matrixAnalysisStatus(node: RequirementMatrixNode): string {
  if (node.matrix_visibility === "HUMAN_EXCLUDED" || node.disposition === "EXCLUDE") {
    return "已排除";
  }
  if (node.disposition === "RETAIN") return "仅保留";
  if (!matrixAnalysisEligible(node)) return "不需分析";
  const source = matrixConclusionSource(node);
  if (source === "STALE_REFERENCE") return "需重新分析";
  if (source === "AI_DEFAULT") return "AI 结论已采用";
  if (source === "HUMAN_OVERRIDE") return "人工已修改";
  if (source === "HUMAN_NATIVE") return "人工已录入";
  if (matrixHasIncompleteTerminalConclusion(node)) return "结论不完整";
  const policy = ((node.source_item || node.decomposition) as { analysis_policy?: { status?: string } } | undefined)?.analysis_policy;
  if (policy?.status === "SKIPPED") return "未执行符合性分析";
  return "待分析";
}

export function matrixAnalysisTone(value: string): MatrixStatusTone {
  if (
    [
      "AI 结论已采用",
      "人工已修改",
      "人工已录入",
      "人工已确认",
      "AI 已分析",
    ].includes(value)
  ) {
    return "success";
  }
  if (["已排除", "仅保留", "不需分析"].includes(value)) return "neutral";
  if (value === "结论不完整") return "warning";
  if (value.includes("失败") || value.includes("受阻")) return "danger";
  if (value.includes("待") || value.includes("重新")) return "warning";
  return "info";
}

export function matrixDecisionLabel(value: unknown, fallback: string): string {
  const raw = compactText(value, fallback);
  const labels: Record<string, string> = {
    ACCEPT: "接受",
    REJECT: "拒绝",
    DEVIATION_ACCEPT: "偏差接受",
    NEED_CLARIFICATION: "需澄清",
    COMPLIANT: "符合",
    PARTIALLY_COMPLIANT: "部分符合",
    NON_COMPLIANT: "不符合",
    INSUFFICIENT_INFORMATION: "信息不足",
    PENDING: "待确认",
    UNASSIGNED: "待确认",
    NONE: "待确认",
  };
  return labels[raw.toUpperCase()] || raw;
}

export function matrixAiSuggestion(node: RequirementMatrixNode): string {
  if (!matrixAnalysisEligible(node)) return "—";
  if (!matrixAllowsCurrentConclusion(node)) return "待生成";
  const assessment = node.ai_assessment;
  return matrixDecisionLabel(
    assessment?.decision || assessment?.compliance_status,
    "待生成",
  );
}

export function matrixHumanDecision(node: RequirementMatrixNode): string {
  if (node.matrix_visibility === "HUMAN_EXCLUDED") return "—";
  if (!matrixAnalysisEligible(node)) return "—";
  const source = matrixConclusionSource(node);
  if (source === "HUMAN_OVERRIDE" || source === "HUMAN_NATIVE") {
    return matrixDecisionLabel(node.manual_decision?.decision, "人工已修改");
  }
  if (source === "AI_DEFAULT") return "未人工修改";
  return source === "STALE_REFERENCE" ? "等待重新分析" : "等待分析";
}

export function matrixAnalysisEligible(node: RequirementMatrixNode): boolean {
  return (
    node.matrix_visibility === "VISIBLE" &&
    node.disposition === "ANALYZE" &&
    node.analysis_eligibility === "ELIGIBLE"
  );
}

export function matrixAnalysisCapable(node: RequirementMatrixNode): boolean {
  return node.analysis_eligibility === "ELIGIBLE";
}

function matrixContractText(value: unknown): string {
  return String(value || "").trim();
}

function matrixResultState(node: RequirementMatrixNode): string {
  const explicit = matrixContractText(node.analysis_result_state).toUpperCase();
  if (explicit) return explicit;
  if (node.stale_ai_assessment != null || node.stale_manual_decision != null) {
    return "STALE_REFERENCE";
  }
  if (
    node.ai_assessment != null ||
    node.manual_decision != null ||
    matrixContractText(node.compliance_status)
  ) {
    return "CURRENT";
  }
  return "NONE";
}

function matrixAllowsCurrentConclusion(node: RequirementMatrixNode): boolean {
  const status = matrixContractText(node.analysis_status).toUpperCase();
  return status === "ASSESSED" || status === "DECIDED";
}

function terminalConclusion(value: unknown): string {
  const text = matrixContractText(value);
  return new Set(["UNKNOWN", "PENDING", "PENDING_EVIDENCE", "NEEDS_EVIDENCE", "NEEDS_CLARIFICATION", "NOT_ASSESSED",
    "UNASSESSED", "UNDETERMINED"]).has(text.toUpperCase()) ? "" : text;
}

export function matrixEffectiveConclusion(node: RequirementMatrixNode): string {
  if (
    matrixResultState(node) !== "CURRENT" ||
    !matrixAllowsCurrentConclusion(node)
  ) {
    return "";
  }
  const manual = matrixContractText(node.manual_decision?.decision);
  if (manual) return terminalConclusion(manual);
  return terminalConclusion(matrixContractText(node.ai_assessment?.decision) || node.ai_assessment?.compliance_status);
}

export function matrixConclusionSource(
  node: RequirementMatrixNode,
): MatrixConclusionSource {
  const resultState = matrixResultState(node);
  if (resultState === "STALE_REFERENCE") return "STALE_REFERENCE";
  if (resultState !== "CURRENT" || !matrixAllowsCurrentConclusion(node) || !matrixEffectiveConclusion(node)) {
    return "NONE";
  }
  const aiConclusion =
    terminalConclusion(matrixContractText(node.ai_assessment?.decision) || node.ai_assessment?.compliance_status);
  if (terminalConclusion(node.manual_decision?.decision)) {
    return aiConclusion ? "HUMAN_OVERRIDE" : "HUMAN_NATIVE";
  }
  if (aiConclusion) {
    return "AI_DEFAULT";
  }
  return "NONE";
}

export function matrixConclusionContract(
  node: RequirementMatrixNode,
): MatrixConclusionContract {
  const source = matrixConclusionSource(node);
  return {
    analysisTarget: matrixAnalysisEligible(node),
    effectiveConclusion: matrixEffectiveConclusion(node),
    conclusionSource: source,
    overridesAi: source === "HUMAN_OVERRIDE",
  };
}

function matrixHasIncompleteTerminalConclusion(
  node: RequirementMatrixNode,
): boolean {
  return (
    matrixAnalysisEligible(node) &&
    matrixResultState(node) === "CURRENT" &&
    matrixAllowsCurrentConclusion(node) &&
    !matrixEffectiveConclusion(node)
  );
}

export function matrixNeedsAnalysis(node: RequirementMatrixNode): boolean {
  return (
    matrixAnalysisEligible(node) &&
    (node.analysis_status === "NOT_STARTED" ||
      matrixConclusionSource(node) === "STALE_REFERENCE")
  );
}

function needsAttention(node: RequirementMatrixNode): boolean {
  if (matrixHasIncompleteTerminalConclusion(node)) return true;
  const assessment = node.ai_assessment;
  const manual = node.manual_decision;
  return [
    assessment?.decision,
    assessment?.compliance_status,
    manual?.decision,
    node.compliance_status,
  ]
    .map((value) => String(value || "").toUpperCase())
    .some((value) =>
      [
        "NON_COMPLIANT",
        "INSUFFICIENT_INFORMATION",
        "NEED_CLARIFICATION",
        "REJECT",
      ].includes(value),
    );
}

function filterMatches(
  filter: RequirementMatrixFilter,
  node: RequirementMatrixNode,
): boolean {
  if (filter === "excluded") return node.matrix_visibility === "HUMAN_EXCLUDED";
  if (node.matrix_visibility === "SYSTEM_NON_BUSINESS") return false;
  if (filter === "all") return true;
  if (node.matrix_visibility !== "VISIBLE") return false;
  if (node.candidate_review) {
    if (filter === "analysis") return node.candidate_review.requirementness === "REQUIREMENT";
    if (filter === "pending") return node.candidate_review.status !== "reviewed";
    if (filter === "attention") return ["needs-review", "changed"].includes(String(node.candidate_review.status)) ||
      (Array.isArray(node.candidate_review.openIssues) && node.candidate_review.openIssues.length > 0);
  }
  if (filter === "analysis") return matrixAnalysisEligible(node);
  if (filter === "pending") {
    return matrixNeedsAnalysis(node);
  }
  if (filter === "attention") return needsAttention(node);
  const role = String(node.display_role || "").toUpperCase();
  return role === "TABLE" || role === "TABLE_RECORD";
}

function queryMatches(query: string, node: RequirementMatrixNode): boolean {
  if (!query) return true;
  return normalized(
    [
      node.source_number,
      node.candidate_review?.sourceId,
      node.display_title,
      node.display_text,
      node.source_kind,
      node.requirement_code,
      node.label,
      node.summary,
      node.requirement_text,
      matrixContentNature(node),
      matrixAnalysisStatus(node),
      matrixAiSuggestion(node),
      matrixHumanDecision(node),
    ].join(" "),
  ).includes(query);
}

export function matrixNodeMatchesView(
  node: RequirementMatrixNode,
  query: string,
  filter: RequirementMatrixFilter,
  facets: ClassificationFacetFilter = EMPTY_CLASSIFICATION_FILTER,
): boolean {
  return filterMatches(filter, node) && queryMatches(normalized(query), node) && matchesClassificationFacets(node, facets);
}

function sortedNodeIds(
  ids: readonly string[],
  index: ReadonlyMap<string, RequirementMatrixNode>,
): string[] {
  // The frozen tree's arrays are the reviewed business order.  Equal source
  // anchors are common for table rows; sorting them by opaque IDs corrupts the
  // sequence even though the hierarchy itself is correct.
  return Array.from(new Set(ids)).filter((id) => index.has(id));
}

function includedNodeIds(
  matrix: RequirementMatrixSnapshot,
  query: string,
  filter: RequirementMatrixFilter,
  facets: ClassificationFacetFilter = EMPTY_CLASSIFICATION_FILTER,
): Set<string> {
  const index = new Map(matrix.nodes.map((node) => [node.node_id, node]));
  const included = new Set<string>();
  matrix.nodes.forEach((node) => {
    if (!matrixNodeMatchesView(node, query, filter, facets)) return;
    included.add(node.node_id);
    const visited = new Set<string>();
    let parentId = node.parent_id;
    while (parentId && !visited.has(parentId)) {
      visited.add(parentId);
      const parent = index.get(parentId);
      if (!parent) break;
      included.add(parent.node_id);
      parentId = parent.parent_id;
    }
  });
  return included;
}

export function requirementMatrixMatchCounts(
  matrix: RequirementMatrixSnapshot | null,
  query: string,
  filter: RequirementMatrixFilter,
  facets: ClassificationFacetFilter = EMPTY_CLASSIFICATION_FILTER,
): RequirementMatrixMatchCounts {
  if (!matrix) return { matched: 0, retainedAncestors: 0 };
  const normalizedQuery = normalized(query);
  const directMatches = new Set(
    matrix.nodes
      .filter(
        (node) =>
          matrixNodeMatchesView(node, normalizedQuery, filter, facets),
      )
      .map((node) => node.node_id),
  );
  const included = includedNodeIds(matrix, normalizedQuery, filter, facets);
  return {
    matched: directMatches.size,
    retainedAncestors: Array.from(included).filter(
      (nodeId) =>
        !directMatches.has(nodeId) &&
        matrix.nodes.find((node) => node.node_id === nodeId)
          ?.matrix_visibility !== "SYSTEM_NON_BUSINESS",
    ).length,
  };
}

export function initialMatrixExpandedIds(
  matrix: RequirementMatrixSnapshot | null,
): Set<string> {
  if (!matrix) return new Set();
  return new Set(
    matrix.nodes
      .filter((node) => node.child_ids.length > 0 && (Boolean(matrix.candidate_review) || node.depth < 2))
      .map((node) => node.node_id),
  );
}

export function matrixAncestorIds(
  matrix: RequirementMatrixSnapshot | null,
  nodeId: string,
): Set<string> {
  if (!matrix || !nodeId) return new Set();
  const index = new Map(matrix.nodes.map((node) => [node.node_id, node]));
  const ancestors = new Set<string>();
  const visited = new Set<string>();
  let parentId = index.get(nodeId)?.parent_id || null;
  while (parentId && !visited.has(parentId)) {
    visited.add(parentId);
    ancestors.add(parentId);
    parentId = index.get(parentId)?.parent_id || null;
  }
  return ancestors;
}

export function flattenRequirementMatrix(
  matrix: RequirementMatrixSnapshot | null,
  expandedIds: ReadonlySet<string>,
  query = "",
  filter: RequirementMatrixFilter = "all",
  facets: ClassificationFacetFilter = EMPTY_CLASSIFICATION_FILTER,
): RequirementMatrixRow[] {
  if (!matrix) return [];
  const nodeIndex = new Map(matrix.nodes.map((node) => [node.node_id, node]));
  const normalizedQuery = normalized(query);
  const included = includedNodeIds(matrix, normalizedQuery, filter, facets);
  const rows: RequirementMatrixRow[] = [];
  const visited = new Set<string>();

  const walk = (nodeId: string, depth: number, active: Set<string>) => {
    if (active.has(nodeId) || visited.has(nodeId)) return;
    const node = nodeIndex.get(nodeId);
    if (!node || !included.has(nodeId)) return;
    active.add(nodeId);
    visited.add(nodeId);
    const children = sortedNodeIds(node.child_ids, nodeIndex).filter((childId) =>
      included.has(childId),
    );
    const isSystemWrapper =
      node.matrix_visibility === "SYSTEM_NON_BUSINESS";
    const expanded = children.length > 0 && expandedIds.has(node.node_id);
    if (!isSystemWrapper) {
      rows.push({
        node,
        depth,
        hasChildren: children.length > 0,
        expanded,
        contentNature: matrixContentNature(node),
        analysisStatus: matrixAnalysisStatus(node),
        aiSuggestion: matrixAiSuggestion(node),
        humanDecision: matrixHumanDecision(node),
      });
    }
    if (isSystemWrapper || expanded) {
      children.forEach((childId) =>
        walk(childId, depth + (isSystemWrapper ? 0 : 1), active),
      );
    }
    active.delete(nodeId);
  };

  sortedNodeIds(matrix.root_node_ids, nodeIndex).forEach((rootId) =>
    walk(rootId, 0, new Set()),
  );
  matrix.nodes
    .filter(
      (node) =>
        !visited.has(node.node_id) &&
        included.has(node.node_id) &&
        (!node.parent_id ||
          !nodeIndex.has(node.parent_id) ||
          !included.has(node.parent_id)),
    )
    .sort(
      (left, right) =>
        left.source_order - right.source_order ||
        left.node_id.localeCompare(right.node_id),
    )
    .forEach((node) => walk(node.node_id, Math.max(0, node.depth), new Set()));
  return rows;
}

export function firstMatrixSelectionId(
  rows: readonly RequirementMatrixRow[],
): string {
  return rows[0]?.node.node_id || "";
}

export function suggestedMatrixExpandedIds(
  matrix: RequirementMatrixSnapshot | null,
  query: string,
  filter: RequirementMatrixFilter,
  facets: ClassificationFacetFilter = EMPTY_CLASSIFICATION_FILTER,
): Set<string> {
  if (!matrix || (!normalized(query) && filter === "all" && !facets.category && !facets.topic)) return new Set();
  const index = new Map(matrix.nodes.map((node) => [node.node_id, node]));
  const included = includedNodeIds(matrix, normalized(query), filter, facets);
  return new Set(
    matrix.nodes
      .filter(
        (node) =>
          included.has(node.node_id) &&
          node.child_ids.some(
            (childId) => index.has(childId) && included.has(childId),
          ),
      )
      .map((node) => node.node_id),
  );
}

export function matrixRowAccessibleName(row: RequirementMatrixRow): string {
  const sourceNumber = matrixSourceNumber(row.node);
  return `${sourceNumber ? `${sourceNumber}，` : ""}${matrixNodeLabel(row.node)}，${row.contentNature}，${row.analysisStatus}，AI 结论 ${row.aiSuggestion}，人工修改 ${row.humanDecision}`;
}

export function matchingCandidateExportIds(
  matrix: RequirementMatrixSnapshot | null,
  query: string,
  filter: RequirementMatrixFilter,
  facets: ClassificationFacetFilter,
): string[] {
  const expanded = new Set((matrix?.nodes || []).map(node => node.node_id));
  return flattenRequirementMatrix(matrix, expanded, query, filter, facets)
    .filter(row => row.node.candidate_review && matrixNodeMatchesView(row.node, query, filter, facets))
    .map(row => row.node.node_id);
}
