import { asArray, asRecord } from "../../shared/api/client";
import type { JsonRecord } from "../../shared/api/contracts";

export interface ReviewSpan extends JsonRecord {
  semantic_unit_id: string;
  owner_structure_node_id: string;
  start_offset: number;
  end_offset: number;
}

export interface ReviewNode extends JsonRecord {
  review_node_id: string;
  source_semantic_unit_ids: string[];
  source_owner_structure_node_ids: string[];
  source_spans: ReviewSpan[];
  source_order: number;
  semantic_class: string;
  requirement_subtype?: string | null;
  include_in_view: boolean;
  role?: string | null;
  parent_review_node_id?: string | null;
  decision_mode: string;
  reason_codes?: string[];
}

export interface ReviewModelNode extends JsonRecord {
  semantic_unit_id: string;
  review_text?: string;
  text?: string;
  role?: string | null;
  parent_semantic_unit_id?: string | null;
  decision_mode?: string;
}

export interface ReviewSessionView {
  sessionId: string;
  revision: number;
  status: string;
  reviewerLabel: string;
  nodes: ReviewNode[];
  confirmedIds: string[];
  modelByUnit: Map<string, ReviewModelNode>;
}

export function reviewSessionView(payload: JsonRecord | null): ReviewSessionView | null {
  const review = asRecord(payload?.llm_review ?? payload);
  const persistence = asRecord(review.persistence);
  const reviewed = asRecord(review.reviewed_result);
  const prediction = asRecord(review.model_prediction);
  const nodes = asArray<ReviewNode>(reviewed.nodes);
  const sessionId = String(persistence.session_id || "");
  if (!sessionId || !nodes.length) return null;
  const modelNodes = asArray<ReviewModelNode>(prediction.nodes);
  return {
    sessionId,
    revision: Number(reviewed.revision || persistence.revision || 0),
    status: String(reviewed.status || "DRAFT"),
    reviewerLabel: String(reviewed.reviewer_label || ""),
    nodes,
    confirmedIds: asArray<string>(reviewed.confirmed_review_node_ids).map(String),
    modelByUnit: new Map(
      modelNodes
        .filter((node) => node.semantic_unit_id)
        .map((node) => [String(node.semantic_unit_id), node]),
    ),
  };
}

function codePoints(value: string): string[] {
  return Array.from(value);
}

function sliceCodePoints(value: string, start: number, end: number): string {
  return codePoints(value).slice(start, end).join("");
}

export function codePointOffset(value: string, codeUnitOffset: number): number {
  let safe = Math.max(0, Math.min(value.length, codeUnitOffset));
  if (
    safe > 0 &&
    safe < value.length &&
    value.charCodeAt(safe - 1) >= 0xd800 &&
    value.charCodeAt(safe - 1) <= 0xdbff &&
    value.charCodeAt(safe) >= 0xdc00 &&
    value.charCodeAt(safe) <= 0xdfff
  ) {
    safe -= 1;
  }
  return Array.from(value.slice(0, safe)).length;
}

export function reviewNodeText(
  node: ReviewNode,
  modelByUnit: Map<string, ReviewModelNode>,
): string {
  return asArray<ReviewSpan>(node.source_spans)
    .map((span) => {
      const model = modelByUnit.get(String(span.semantic_unit_id));
      const text = String(model?.review_text ?? model?.text ?? "");
      return sliceCodePoints(
        text,
        Number(span.start_offset || 0),
        Number(span.end_offset || 0),
      );
    })
    .join("")
    .trim();
}

function lineage(spans: ReviewSpan[]) {
  const units: string[] = [];
  const owners: string[] = [];
  for (const span of spans) {
    if (units.includes(span.semantic_unit_id)) continue;
    units.push(span.semantic_unit_id);
    owners.push(span.owner_structure_node_id);
  }
  return { units, owners };
}

function editedId(
  base: string,
  revision: number,
  start: number,
  end: number,
  piece: number,
): string {
  const safeBase = base.replace(/[^A-Za-z0-9_.-]/g, "-").slice(0, 120);
  return `${safeBase}-edit-r${revision + 1}-${start}-${end}-${piece}`;
}

export function splitReviewNode(
  nodes: ReviewNode[],
  reviewNodeId: string,
  selectionStart: number,
  selectionEnd: number,
  revision: number,
): { nodes: ReviewNode[]; focusId: string } {
  const index = nodes.findIndex((node) => node.review_node_id === reviewNodeId);
  const selected = nodes[index];
  if (!selected) throw new Error("请选择要拆分的需求候选。");
  if (
    !selected.include_in_view ||
    selected.semantic_class !== "REQUIREMENT" ||
    selected.decision_mode !== "DIRECT"
  ) {
    throw new Error("只有拟进入下一步的需求候选可以拆分。");
  }
  if (nodes.some((node) => node.parent_review_node_id === reviewNodeId)) {
    throw new Error("该条目仍有下级内容，不能直接拆分。");
  }
  const spans = asArray<ReviewSpan>(selected.source_spans);
  if (spans.length !== 1) {
    throw new Error("跨来源片段条目需先核对来源边界，不能在浏览器中直接拆分。");
  }
  const span = spans[0];
  const length = Number(span.end_offset) - Number(span.start_offset);
  const start = Math.max(0, Math.min(length, selectionStart));
  const end = Math.max(start, Math.min(length, selectionEnd));
  if (start === end || (start === 0 && end === length)) {
    throw new Error("请选中条目中的一段非空文字，形成明确拆分边界。");
  }
  const groups = [
    start > 0
      ? [{ ...span, end_offset: Number(span.start_offset) + start }]
      : [],
    [
      {
        ...span,
        start_offset: Number(span.start_offset) + start,
        end_offset: Number(span.start_offset) + end,
      },
    ],
    end < length
      ? [{ ...span, start_offset: Number(span.start_offset) + end }]
      : [],
  ].filter((group) => group.length) as ReviewSpan[][];
  const pieces = groups.map((pieceSpans, pieceIndex) => {
    const pieceLineage = lineage(pieceSpans);
    return {
      ...selected,
      review_node_id: editedId(
        selected.review_node_id,
        revision,
        pieceSpans[0].start_offset,
        pieceSpans[pieceSpans.length - 1].end_offset,
        pieceIndex,
      ),
      source_semantic_unit_ids: pieceLineage.units,
      source_owner_structure_node_ids: pieceLineage.owners,
      source_spans: pieceSpans,
    };
  });
  return {
    nodes: [...nodes.slice(0, index), ...pieces, ...nodes.slice(index + 1)],
    focusId: pieces[Math.min(1, pieces.length - 1)].review_node_id,
  };
}

function sameShape(left: ReviewNode, right: ReviewNode): boolean {
  return [
    "semantic_class",
    "requirement_subtype",
    "include_in_view",
    "role",
    "parent_review_node_id",
    "decision_mode",
  ].every((field) => (left[field] ?? null) === (right[field] ?? null));
}

function coalesceSpans(spans: ReviewSpan[]): ReviewSpan[] {
  const result: ReviewSpan[] = [];
  for (const span of spans) {
    const previous = result[result.length - 1];
    if (
      previous &&
      previous.semantic_unit_id === span.semantic_unit_id &&
      previous.owner_structure_node_id === span.owner_structure_node_id &&
      previous.end_offset === span.start_offset
    ) {
      previous.end_offset = span.end_offset;
    } else {
      result.push({ ...span });
    }
  }
  return result;
}

export function mergeReviewNode(
  nodes: ReviewNode[],
  reviewNodeId: string,
): ReviewNode[] {
  const index = nodes.findIndex((node) => node.review_node_id === reviewNodeId);
  const selected = nodes[index];
  const next = nodes[index + 1];
  if (!selected || !next) {
    throw new Error("当前条目后面没有可合并的相邻条目。");
  }
  if (
    nodes.some(
      (node) =>
        node.parent_review_node_id === selected.review_node_id ||
        node.parent_review_node_id === next.review_node_id,
    )
  ) {
    throw new Error("带有下级内容的条目不能直接合并。");
  }
  if (
    !sameShape(selected, next) ||
    !selected.include_in_view ||
    selected.semantic_class !== "REQUIREMENT" ||
    selected.decision_mode !== "DIRECT"
  ) {
    throw new Error("相邻条目的父级或进入下一步方式不同，不能直接合并。");
  }
  const spans = coalesceSpans([
    ...asArray<ReviewSpan>(selected.source_spans),
    ...asArray<ReviewSpan>(next.source_spans),
  ]);
  const mergedLineage = lineage(spans);
  const merged: ReviewNode = {
    ...selected,
    source_spans: spans,
    source_semantic_unit_ids: mergedLineage.units,
    source_owner_structure_node_ids: mergedLineage.owners,
  };
  return [...nodes.slice(0, index), merged, ...nodes.slice(index + 2)];
}

export function excludeReviewNode(
  nodes: ReviewNode[],
  reviewNodeId: string,
): ReviewNode[] {
  const selected = nodes.find((node) => node.review_node_id === reviewNodeId);
  if (!selected) throw new Error("请选择要移出的条目。");
  if (
    !selected.include_in_view ||
    selected.semantic_class !== "REQUIREMENT" ||
    selected.decision_mode !== "DIRECT"
  ) {
    throw new Error("当前条目本来就不作为需求候选进入下一步。");
  }
  if (nodes.some((node) => node.parent_review_node_id === reviewNodeId)) {
    throw new Error("该条目仍有下级内容，不能直接移出。");
  }
  return nodes.map((node) =>
    node.review_node_id === reviewNodeId
      ? {
          ...node,
          include_in_view: false,
          role: null,
          parent_review_node_id: null,
          decision_mode: "NONE",
        }
      : node,
  );
}

export function reincludeReviewNode(
  nodes: ReviewNode[],
  reviewNodeId: string,
  modelByUnit: Map<string, ReviewModelNode>,
): ReviewNode[] {
  const selected = nodes.find((node) => node.review_node_id === reviewNodeId);
  if (!selected) throw new Error("请选择要重新加入的条目。");
  if (selected.include_in_view) throw new Error("当前条目已经在下一步清单中。");
  const model = selected.source_semantic_unit_ids
    .map((id) => modelByUnit.get(id))
    .find(Boolean);
  if (!model) {
    throw new Error("缺少原模型投影，不能安全恢复该条目。");
  }
  const parentTarget = String(model.parent_semantic_unit_id || "");
  const parent = parentTarget
    ? nodes.find(
        (node) =>
          node.include_in_view &&
          node.source_semantic_unit_ids.includes(parentTarget),
      )
    : null;
  if (parentTarget && !parent) {
    throw new Error("原父项当前不可用，先恢复父项后再重新加入。");
  }
  return nodes.map((node) =>
    node.review_node_id === reviewNodeId
      ? {
          ...node,
          include_in_view: true,
          role: model.role ?? null,
          parent_review_node_id: parent?.review_node_id || null,
          decision_mode: String(model.decision_mode || "DIRECT"),
        }
      : node,
  );
}
