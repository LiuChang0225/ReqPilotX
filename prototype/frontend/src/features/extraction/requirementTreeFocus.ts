import { asArray } from "../../shared/api/client";
import type {
  JsonRecord,
  RequirementTreeNode,
} from "../../shared/api/contracts";
import type { PageSnapshot } from "../restoration/restorationModel";

export interface RequirementTreeFocusOverlay {
  id: string;
  type: string;
  label: string;
  bbox: number[];
  sourceBounds: number[];
  selected: true;
  focus: true;
  precision: string;
}

export function geometryFocusRegionsForNode(
  payload: JsonRecord | null,
  nodeId: string,
  expectedGeometryIdentity?: {
    geometryIndexId: string;
    geometryIndexSha256: string;
  },
): JsonRecord[] | null {
  if (!payload || !nodeId || String(payload.node_id || "") !== nodeId) {
    return null;
  }
  if (expectedGeometryIdentity) {
    const geometryIdentity = payload.geometry_identity as JsonRecord | undefined;
    if (
      !geometryIdentity ||
      String(geometryIdentity.geometry_index_id || "") !==
        expectedGeometryIdentity.geometryIndexId ||
      String(geometryIdentity.geometry_index_sha256 || "") !==
        expectedGeometryIdentity.geometryIndexSha256
    ) {
      return null;
    }
  }
  return asArray<JsonRecord>(payload.regions);
}

function bboxOf(value: unknown): number[] {
  const bbox = asArray<unknown>(value).map(Number);
  return bbox.length === 4 &&
    bbox.every(Number.isFinite) &&
    bbox[2] > bbox[0] &&
    bbox[3] > bbox[1]
    ? bbox
    : [];
}

function positivePage(value: unknown, fallback: number): number {
  const page = Number(value);
  return Number.isInteger(page) && page > 0 ? page : fallback;
}

function overlayType(role: string): string {
  if (role === "TABLE") return "TABLE";
  if (role === "FIGURE") return "FIGURE";
  return "TEXT";
}

function overlayLabel(role: string, node: RequirementTreeNode): string {
  if (role === "TABLE") return "条款表格";
  if (role === "TABLE_CAPTION") return "表题";
  if (role === "FIGURE") return "条款图片";
  if (role === "REQUIREMENT_TEXT") return "需求来源";
  return node.kind === "REQUIREMENT" ? "需求来源" : "条款正文";
}

export function requirementTreeFocusOverlays({
  selectedNode,
  contextNode,
  pageSnapshot,
  pdfPage,
  focusRegions,
}: {
  selectedNode: RequirementTreeNode | null;
  contextNode: RequirementTreeNode | null;
  pageSnapshot: PageSnapshot;
  pdfPage: number;
  focusRegions?: JsonRecord[] | null;
}): RequirementTreeFocusOverlay[] {
  const focusNode = selectedNode || contextNode;
  if (!focusNode) return [];
  const sourceBounds = bboxOf(pageSnapshot.bounds);
  if (sourceBounds.length !== 4) return [];

  const preferred = focusRegions || [];
  let textLabelUsed = false;
  const overlays: RequirementTreeFocusOverlay[] = [];
  const seen = new Set<string>();
  preferred.forEach((region, index) => {
    const page = positivePage(region.page_number, pdfPage);
    const bbox = bboxOf(region.bbox);
    if (page !== pdfPage || bbox.length !== 4) return;
    const role = String(region.role || "CLAUSE_TEXT").toUpperCase();
    const precision = String(
      region.precision || "BLOCK_APPROXIMATE",
    ).toUpperCase();
    const key = `${page}:${bbox.join(",")}:${role}`;
    if (seen.has(key)) return;
    seen.add(key);
    const isText = role === "CLAUSE_TEXT" || role === "REQUIREMENT_TEXT";
    const label = isText && textLabelUsed
      ? ""
      : overlayLabel(role, focusNode);
    if (isText) textLabelUsed = true;
    overlays.push({
      id: String(region.id || `focus-${index + 1}`),
      type: overlayType(role),
      label,
      bbox,
      sourceBounds,
      selected: true,
      focus: true,
      precision,
    });
  });
  return overlays;
}
