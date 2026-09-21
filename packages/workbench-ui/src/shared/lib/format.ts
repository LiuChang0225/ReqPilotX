import type {
  ExtractionItem,
  ExtractionResult,
  JsonRecord,
  Requirement,
  StructureNode,
  StructurePayload,
} from "../api/contracts";
import { asArray, asRecord } from "../api/client";

export function cx(
  ...parts: Array<string | false | null | undefined>
): string {
  return parts.filter(Boolean).join(" ");
}

export function compactText(value: unknown, fallback = "—"): string {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text || fallback;
}

export function truncate(value: unknown, length = 140): string {
  const text = compactText(value, "");
  if (text.length <= length) return text;
  return `${text.slice(0, Math.max(0, length - 1)).trimEnd()}…`;
}

export function formatDate(value: unknown): string {
  const date = new Date(String(value || ""));
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatBytes(value: unknown): string {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let amount = bytes;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  return `${amount >= 10 || unit === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[unit]}`;
}

export function requirementId(requirement: Requirement): string {
  return String(requirement.id || requirement.requirement_id || "");
}

export function requirementText(requirement: Requirement): string {
  return compactText(
    requirement.requirement_text ??
      requirement.text ??
      requirement.source_text ??
      requirement.title,
    "未提供条目文本",
  );
}

export function requirementDecision(requirement: Requirement): string {
  return compactText(
    requirement.manual_decision ??
      requirement.final_decision ??
      requirement.decision,
    "UNASSIGNED",
  );
}

export function requirementAiDecision(requirement: Requirement): string {
  return compactText(
    requirement.ai_recommendation ??
      requirement.ai_decision ??
      requirement.analysis_status,
    "PENDING",
  );
}

export function activeExtractionResult(
  payload: { result?: ExtractionResult | null; partial_result?: ExtractionResult | null } | null,
): { result: ExtractionResult | null; partial: boolean } {
  if (!payload) return { result: null, partial: false };
  if (payload.result) return { result: payload.result, partial: false };
  if (payload.partial_result) {
    return { result: payload.partial_result, partial: true };
  }
  return { result: null, partial: false };
}

export function extractionItems(result: ExtractionResult | null): ExtractionItem[] {
  if (!result) return [];
  const preferred = asArray<ExtractionItem>(result.review_groups);
  if (preferred.length) return preferred;
  const requirements = asArray<ExtractionItem>(result.requirement_groups);
  if (requirements.length) return requirements;
  return asArray<ExtractionItem>(result.items);
}

export function extractionSessionId(result: ExtractionResult | null): string {
  if (!result) return "";
  const llmReview = asRecord(result.llm_review);
  const persistence = asRecord(llmReview.persistence);
  return compactText(
    result.review_session_id ??
      result.session_id ??
      asRecord(result.review).session_id ??
      persistence.session_id,
    "",
  );
}

export interface StructureTreeRow extends StructureNode {
  depth: number;
  childCount: number;
}

export function flattenStructure(payload: StructurePayload | null): StructureTreeRow[] {
  const nodes = asArray<StructureNode>(payload?.structure_agent?.effective_nodes);
  if (!nodes.length) return [];
  const children = new Map<string, StructureNode[]>();
  for (const node of nodes) {
    const parent = String(node.parent_structure_node_id || "");
    const list = children.get(parent) || [];
    list.push(node);
    children.set(parent, list);
  }
  for (const list of children.values()) {
    list.sort((left, right) => Number(left.source_order || 0) - Number(right.source_order || 0));
  }
  const output: StructureTreeRow[] = [];
  const visited = new Set<string>();
  const visit = (node: StructureNode, depth: number) => {
    if (visited.has(node.structure_node_id)) return;
    visited.add(node.structure_node_id);
    const nodeChildren = children.get(node.structure_node_id) || [];
    output.push({ ...node, depth, childCount: nodeChildren.length });
    for (const child of nodeChildren) visit(child, depth + 1);
  };
  const roots = [
    ...(children.get("") || []),
    ...nodes.filter(
      (node) =>
        node.parent_structure_node_id &&
        !nodes.some(
          (candidate) =>
            candidate.structure_node_id === node.parent_structure_node_id,
        ),
    ),
  ];
  for (const root of roots) visit(root, 0);
  for (const node of nodes) visit(node, 0);
  return output;
}

export function firstArrayOfRecords(
  payload: JsonRecord | null | undefined,
  keys: string[],
): JsonRecord[] {
  for (const key of keys) {
    const value = payload?.[key];
    if (Array.isArray(value)) return value.filter(Boolean) as JsonRecord[];
  }
  return [];
}

export function jsonNumber(record: JsonRecord | null | undefined, keys: string[]): number {
  for (const key of keys) {
    const value = Number(record?.[key]);
    if (Number.isFinite(value)) return value;
  }
  return 0;
}

export function graphemeSegments(value: string): string[] {
  if (typeof Intl.Segmenter === "function") {
    const segmenter = new Intl.Segmenter("zh-CN", { granularity: "grapheme" });
    return Array.from(segmenter.segment(value), (entry) => entry.segment);
  }
  return Array.from(value);
}

export function splitTextAtGrapheme(
  value: string,
  requestedOffset: number,
): [string, string] {
  const segments = graphemeSegments(value);
  const offset = Math.max(1, Math.min(segments.length - 1, requestedOffset));
  return [
    segments.slice(0, offset).join("").trim(),
    segments.slice(offset).join("").trim(),
  ];
}
