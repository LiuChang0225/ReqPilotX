import type { JsonRecord } from "../../shared/api/contracts";

export const BOUNDARY_CALIBRATION_PAGE_SCHEMA_VERSION =
  "boundary-calibration-page/v1" as const;

export type BoundaryCalibrationMode = "RESEGMENT" | "SPLIT";

export interface BoundaryCalibrationBinding {
  revisionNumber: number;
  effectiveSnapshotId: string;
  geometryIndexSha256: string;
}

export interface BoundaryCalibrationLineRegion {
  lineId: string;
  bbox: number[];
  startOffset: number | null;
  endOffset: number | null;
}

export interface BoundaryCalibrationSegment {
  nodeId: string;
  parentNodeId: string | null;
  kind: string;
  label: string;
  path: string[];
  sourceId: string;
  sourceKind: string;
  startOffset: number;
  endOffset: number;
  bbox: number[];
  lineRegions: BoundaryCalibrationLineRegion[];
  editable: boolean;
  lockedReason: string;
}

export interface BoundaryCalibrationAnchor {
  offset: number;
  bbox: number[];
  lineId: string;
}

export interface BoundaryCalibrationBoundary {
  boundaryId: string;
  mode: BoundaryCalibrationMode;
  beforeNodeId: string;
  afterNodeId: string;
  targetNodeId: string;
  sourceId: string;
  currentOffset: number;
  editable: boolean;
  lockedReason: string;
  anchors: BoundaryCalibrationAnchor[];
}

export interface BoundaryCalibrationPlan {
  schemaVersion: typeof BOUNDARY_CALIBRATION_PAGE_SCHEMA_VERSION;
  page: {
    number: number;
    width: number;
    height: number;
  };
  binding: BoundaryCalibrationBinding;
  segments: BoundaryCalibrationSegment[];
  boundaries: BoundaryCalibrationBoundary[];
}

export interface BoundarySplitOperation extends JsonRecord {
  operation: "boundary_split";
  structure_node_id: string;
  parts: Array<{
    source_spans: Array<{
      source_ref: {
        kind: string;
        source_id: string;
      };
      start_offset: number;
      end_offset: number;
    }>;
  }>;
}

export interface BoundaryResegmentOperation extends JsonRecord {
  operation: "boundary_resegment";
  preceding_structure_node_id: string;
  following_structure_node_id: string;
  new_boundary_offset: number;
}

export type BoundaryCalibrationOperation =
  | BoundarySplitOperation
  | BoundaryResegmentOperation;

export interface BoundaryCalibrationSelection {
  boundary_id: string;
  selected_offset: number;
  binding: {
    revision_number: number;
    effective_snapshot_id: string;
    geometry_index_sha256: string;
  };
}

type UnknownRecord = Record<string, unknown>;

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as UnknownRecord)
    : {};
}

function asRecords(value: unknown): UnknownRecord[] {
  return Array.isArray(value) ? value.map(asRecord) : [];
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function wholeNumber(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : fallback;
}

function positiveNumber(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function bbox(value: unknown): number[] {
  if (!Array.isArray(value) || value.length !== 4) return [];
  const normalized = value.map(Number);
  return normalized.every(Number.isFinite) &&
    normalized[2] > normalized[0] &&
    normalized[3] > normalized[1]
    ? normalized
    : [];
}

function pathLabels(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map(text).filter(Boolean);
  }
  const single = text(value);
  return single ? [single] : [];
}

function lineRegion(
  raw: UnknownRecord,
  index: number,
): BoundaryCalibrationLineRegion | null {
  const regionBbox = bbox(raw.bbox);
  if (!regionBbox.length) return null;
  const startOffset =
    raw.start_offset === undefined
      ? null
      : wholeNumber(raw.start_offset, Number.NaN);
  const endOffset =
    raw.end_offset === undefined
      ? null
      : wholeNumber(raw.end_offset, Number.NaN);
  return {
    lineId: text(raw.line_id ?? raw.id) || `line-${index + 1}`,
    bbox: regionBbox,
    startOffset:
      startOffset !== null && Number.isFinite(startOffset)
        ? startOffset
        : null,
    endOffset:
      endOffset !== null && Number.isFinite(endOffset) ? endOffset : null,
  };
}

function segment(
  raw: UnknownRecord,
): BoundaryCalibrationSegment | null {
  const nodeId = text(raw.node_id);
  const sourceId = text(raw.source_id);
  const segmentBbox = bbox(raw.bbox);
  const startOffset = wholeNumber(raw.start_offset, Number.NaN);
  const endOffset = wholeNumber(raw.end_offset, Number.NaN);
  if (
    !nodeId ||
    !sourceId ||
    !segmentBbox.length ||
    !Number.isFinite(startOffset) ||
    !Number.isFinite(endOffset) ||
    endOffset <= startOffset
  ) {
    return null;
  }
  const sourceRef = asRecord(raw.source_ref);
  const lockedReason = text(raw.locked_reason);
  return {
    nodeId,
    parentNodeId: text(raw.parent_node_id) || null,
    kind: text(raw.kind).toUpperCase() || "CONTENT",
    label: text(raw.label) || nodeId,
    path: pathLabels(raw.path),
    sourceId,
    sourceKind:
      text(raw.source_kind ?? sourceRef.kind).toLowerCase() || "source_node",
    startOffset,
    endOffset,
    bbox: segmentBbox,
    lineRegions: asRecords(raw.line_regions)
      .map(lineRegion)
      .filter(
        (item): item is BoundaryCalibrationLineRegion => item !== null,
      ),
    editable: raw.editable === true && !lockedReason,
    lockedReason,
  };
}

function anchor(
  raw: UnknownRecord,
  index: number,
): BoundaryCalibrationAnchor | null {
  const offset = wholeNumber(raw.offset, Number.NaN);
  const anchorBbox = bbox(raw.bbox);
  if (!Number.isFinite(offset) || offset < 0 || !anchorBbox.length) {
    return null;
  }
  return {
    offset,
    bbox: anchorBbox,
    lineId: text(raw.line_id ?? raw.id) || `anchor-${index + 1}`,
  };
}

function visualAnchorY(value: BoundaryCalibrationAnchor): number {
  return value.bbox[1];
}

function boundary(
  raw: UnknownRecord,
): BoundaryCalibrationBoundary | null {
  const boundaryId = text(raw.boundary_id);
  const sourceId = text(raw.source_id);
  const rawMode = text(raw.mode).toUpperCase();
  if (
    !boundaryId ||
    !sourceId ||
    (rawMode !== "SPLIT" && rawMode !== "RESEGMENT")
  ) {
    return null;
  }
  const mode = rawMode as BoundaryCalibrationMode;
  const beforeNodeId = text(
    raw.before_node_id ?? raw.preceding_node_id,
  );
  const afterNodeId = text(
    raw.after_node_id ?? raw.following_node_id,
  );
  const targetNodeId = text(raw.target_node_id);
  const currentOffset = wholeNumber(raw.current_offset, Number.NaN);
  const uniqueAnchors = new Map<number, BoundaryCalibrationAnchor>();
  asRecords(raw.anchors)
    .map(anchor)
    .filter(
      (item): item is BoundaryCalibrationAnchor => item !== null,
    )
    .forEach((item) => {
      if (!uniqueAnchors.has(item.offset)) uniqueAnchors.set(item.offset, item);
    });
  const anchors = Array.from(uniqueAnchors.values()).sort(
    (left, right) =>
      visualAnchorY(left) - visualAnchorY(right) || left.offset - right.offset,
  );
  const hasRequiredNodeIds =
    mode === "SPLIT"
      ? Boolean(targetNodeId)
      : Boolean(beforeNodeId && afterNodeId);
  const rawLockedReason = text(raw.locked_reason);
  const editable =
    raw.editable === true &&
    !rawLockedReason &&
    Number.isFinite(currentOffset) &&
    anchors.length > 0 &&
    hasRequiredNodeIds;
  let lockedReason = rawLockedReason;
  if (!lockedReason && !hasRequiredNodeIds) {
    lockedReason = "边界缺少可验证的结构节点绑定";
  } else if (!lockedReason && !anchors.length) {
    lockedReason = "当前边界没有可吸附的原文行";
  } else if (!lockedReason && !Number.isFinite(currentOffset)) {
    lockedReason = "当前边界缺少字符偏移";
  } else if (!lockedReason && raw.editable !== true) {
    lockedReason = "当前边界只读";
  }
  return {
    boundaryId,
    mode,
    beforeNodeId,
    afterNodeId,
    targetNodeId,
    sourceId,
    currentOffset: Number.isFinite(currentOffset) ? currentOffset : 0,
    editable,
    lockedReason,
    anchors,
  };
}

/**
 * Normalizes the intentionally loose API JSON into a fail-closed view model.
 * Invalid geometry, spans, anchors, or operation bindings are never guessed.
 */
export function normalizeBoundaryCalibrationPlan(
  payload: unknown,
): BoundaryCalibrationPlan | null {
  const raw = asRecord(payload);
  if (text(raw.schema_version) !== BOUNDARY_CALIBRATION_PAGE_SCHEMA_VERSION) {
    return null;
  }
  const rawPage = asRecord(raw.page);
  const pageNumber = wholeNumber(rawPage.number, 0);
  const width = positiveNumber(rawPage.width);
  const height = positiveNumber(rawPage.height);
  if (pageNumber < 1 || !width || !height) return null;
  const rawBinding = asRecord(raw.binding);
  return {
    schemaVersion: BOUNDARY_CALIBRATION_PAGE_SCHEMA_VERSION,
    page: {
      number: pageNumber,
      width,
      height,
    },
    binding: {
      revisionNumber: Math.max(
        0,
        wholeNumber(rawBinding.revision_number, 0),
      ),
      effectiveSnapshotId: text(rawBinding.effective_snapshot_id),
      geometryIndexSha256: text(rawBinding.geometry_index_sha256),
    },
    segments: asRecords(raw.segments)
      .map(segment)
      .filter(
        (item): item is BoundaryCalibrationSegment => item !== null,
      ),
    boundaries: asRecords(raw.boundaries)
      .map(boundary)
      .filter(
        (item): item is BoundaryCalibrationBoundary => item !== null,
      ),
  };
}

export function initialBoundaryOffset(
  value: BoundaryCalibrationBoundary,
): number {
  if (!value.anchors.length) return value.currentOffset;
  return value.anchors.reduce((closest, candidate) =>
    Math.abs(candidate.offset - value.currentOffset) <
    Math.abs(closest.offset - value.currentOffset)
      ? candidate
      : closest,
  ).offset;
}

export function boundaryAnchorAtOffset(
  value: BoundaryCalibrationBoundary,
  offset: number,
): BoundaryCalibrationAnchor | null {
  return value.anchors.find((item) => item.offset === offset) || null;
}

export function nearestBoundaryAnchor(
  value: BoundaryCalibrationBoundary,
  pageY: number,
): BoundaryCalibrationAnchor | null {
  if (!Number.isFinite(pageY) || !value.anchors.length) return null;
  return value.anchors.reduce((closest, candidate) =>
    Math.abs(visualAnchorY(candidate) - pageY) <
    Math.abs(visualAnchorY(closest) - pageY)
      ? candidate
      : closest,
  );
}

export function stepBoundaryAnchor(
  value: BoundaryCalibrationBoundary,
  offset: number,
  direction: -1 | 1,
): BoundaryCalibrationAnchor | null {
  if (!value.anchors.length) return null;
  const exactIndex = value.anchors.findIndex((item) => item.offset === offset);
  const index =
    exactIndex >= 0
      ? exactIndex
      : value.anchors.findIndex(
          (item) => item.offset === initialBoundaryOffset(value),
        );
  const nextIndex = Math.max(
    0,
    Math.min(value.anchors.length - 1, index + direction),
  );
  return value.anchors[nextIndex] || null;
}

function segmentIndex(
  plan: BoundaryCalibrationPlan,
): Map<string, BoundaryCalibrationSegment> {
  return new Map(plan.segments.map((item) => [item.nodeId, item]));
}

/**
 * Builds an operation only from an anchor supplied by the source-bound plan.
 * A raw pixel or arbitrary character offset can never become a patch.
 */
export function buildCanonicalBoundaryOperation(
  plan: BoundaryCalibrationPlan,
  boundaryId: string,
  selectedOffset: number,
): BoundaryCalibrationOperation | null {
  const selectedBoundary = plan.boundaries.find(
    (item) => item.boundaryId === boundaryId,
  );
  if (
    !selectedBoundary?.editable ||
    !Number.isInteger(selectedOffset) ||
    !boundaryAnchorAtOffset(selectedBoundary, selectedOffset)
  ) {
    return null;
  }
  const nodes = segmentIndex(plan);
  if (selectedBoundary.mode === "SPLIT") {
    const target = nodes.get(selectedBoundary.targetNodeId);
    if (
      !target?.editable ||
      target.sourceId !== selectedBoundary.sourceId ||
      selectedOffset <= target.startOffset ||
      selectedOffset >= target.endOffset
    ) {
      return null;
    }
    const sourceRef = {
      kind: target.sourceKind || "source_node",
      source_id: target.sourceId,
    };
    return {
      operation: "boundary_split",
      structure_node_id: target.nodeId,
      parts: [
        {
          source_spans: [
            {
              source_ref: sourceRef,
              start_offset: target.startOffset,
              end_offset: selectedOffset,
            },
          ],
        },
        {
          source_spans: [
            {
              source_ref: sourceRef,
              start_offset: selectedOffset,
              end_offset: target.endOffset,
            },
          ],
        },
      ],
    };
  }

  const preceding = nodes.get(selectedBoundary.beforeNodeId);
  const following = nodes.get(selectedBoundary.afterNodeId);
  if (
    !preceding?.editable ||
    !following?.editable ||
    !preceding.parentNodeId ||
    preceding.parentNodeId !== following.parentNodeId ||
    preceding.sourceId !== selectedBoundary.sourceId ||
    following.sourceId !== selectedBoundary.sourceId ||
    preceding.endOffset !== selectedBoundary.currentOffset ||
    following.startOffset !== selectedBoundary.currentOffset ||
    preceding.endOffset !== following.startOffset ||
    selectedOffset === selectedBoundary.currentOffset ||
    selectedOffset <= preceding.startOffset ||
    selectedOffset >= following.endOffset
  ) {
    return null;
  }
  return {
    operation: "boundary_resegment",
    preceding_structure_node_id: preceding.nodeId,
    following_structure_node_id: following.nodeId,
    new_boundary_offset: selectedOffset,
  };
}

export function boundarySelection(
  plan: BoundaryCalibrationPlan,
  boundaryId: string,
  selectedOffset: number,
): BoundaryCalibrationSelection {
  return {
    boundary_id: boundaryId,
    selected_offset: selectedOffset,
    binding: {
      revision_number: plan.binding.revisionNumber,
      effective_snapshot_id: plan.binding.effectiveSnapshotId,
      geometry_index_sha256: plan.binding.geometryIndexSha256,
    },
  };
}
