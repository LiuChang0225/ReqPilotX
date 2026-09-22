import { asArray, asRecord } from "../../shared/api/client";
import type { JsonRecord } from "../../shared/api/contracts";
import type {
  CaptureQualityPageV1,
  CaptureReadableMode,
  VisualPageCaptureV1,
  VisualTextRunV1,
  VisualTextSourceRefV1,
} from "./contracts";

export interface PdfSelectableLayerV1 extends JsonRecord {
  schema_version: "pdf-selectable-layer/v1";
  source_sha256: string;
  page_number: number;
  page_bbox: number[];
  page_image_sha256: string;
  readable_mode: CaptureReadableMode;
  visual_snapshot_id: string;
  provider_id: string;
  provider_version: string;
  runs: VisualTextRunV1[];
}

export interface PdfVisualSelectionV1 {
  text: string;
  sourceRefs: VisualTextSourceRefV1[];
}

function bbox(value: unknown): number[] {
  const values = asArray<unknown>(value).map(Number);
  return values.length === 4 &&
    values.every(Number.isFinite) &&
    values[2]! > values[0]! &&
    values[3]! > values[1]!
    ? values
    : [];
}

function mode(value: unknown): CaptureReadableMode {
  const normalized = String(value || "NONE").toUpperCase();
  return ["NATIVE", "VISUAL", "HYBRID", "NONE"].includes(normalized)
    ? (normalized as CaptureReadableMode)
    : "NONE";
}

function normalizeRun(
  value: unknown,
  layer: {
    snapshotId: string;
    pageNumber: number;
    pageBounds: number[];
    pageImageSha256: string;
    providerId: string;
    providerVersion: string;
  },
  index: number,
): VisualTextRunV1 | null {
  const record = asRecord(value);
  const text = String(record.text || "");
  const observationId = String(record.observation_id || "");
  const runBounds = bbox(record.bbox);
  if (!text || !observationId || runBounds.length !== 4) return null;
  const rawConfidence = Number(record.confidence);
  return {
    ...record,
    run_id: String(record.run_id || `${layer.snapshotId}:${observationId}:${index}`),
    observation_id: observationId,
    text,
    bbox: runBounds,
    content_type: String(record.content_type || "TEXT").toUpperCase(),
    reading_order: Math.max(0, Math.trunc(Number(record.reading_order ?? index) || 0)),
    confidence: Number.isFinite(rawConfidence) ? rawConfidence : null,
    source_ref: {
      kind: "VISUAL_TEXT",
      visual_snapshot_id: layer.snapshotId,
      observation_id: observationId,
      start_offset: 0,
      end_offset: Array.from(text).length,
      page_number: layer.pageNumber,
      bbox: runBounds,
      page_bbox: layer.pageBounds,
      page_image_sha256: layer.pageImageSha256,
      provider_id: layer.providerId,
      provider_version: layer.providerVersion,
    },
  };
}

export function pdfSelectableLayerFromVisualCapture(
  capture: VisualPageCaptureV1,
  readableMode: CaptureReadableMode = capture.readable_mode,
): PdfSelectableLayerV1 {
  return {
    schema_version: "pdf-selectable-layer/v1",
    source_sha256: capture.source_sha256,
    page_number: capture.page_number,
    page_bbox: capture.page_bbox,
    page_image_sha256: capture.page_image_sha256,
    readable_mode: readableMode,
    visual_snapshot_id: capture.visual_snapshot_id,
    provider_id: capture.provider_id,
    provider_version: capture.provider_version,
    runs: capture.runs,
  };
}

export function pdfSelectableLayerFromCapturePage(
  page: CaptureQualityPageV1,
  sourceSha256: string,
  visualCapture?: VisualPageCaptureV1 | null,
): PdfSelectableLayerV1 {
  return {
    schema_version: "pdf-selectable-layer/v1",
    source_sha256: sourceSha256,
    page_number: page.page_number,
    page_bbox: visualCapture?.page_bbox || page.page_bbox,
    page_image_sha256:
      visualCapture?.page_image_sha256 || page.page_image_sha256 || "",
    readable_mode: page.readable_mode,
    visual_snapshot_id:
      visualCapture?.visual_snapshot_id || page.visual_snapshot_id || "",
    provider_id: visualCapture?.provider_id || page.provider_id || "",
    provider_version:
      visualCapture?.provider_version || page.provider_version || "",
    runs: visualCapture?.runs || [],
  };
}

export function normalizePdfSelectableLayer(
  value: unknown,
): PdfSelectableLayerV1 | null {
  const record = asRecord(value);
  if (String(record.schema_version || "") !== "pdf-selectable-layer/v1") {
    return null;
  }
  const sourceSha256 = String(record.source_sha256 || "").toLowerCase();
  const pageImageSha256 = String(record.page_image_sha256 || "").toLowerCase();
  const snapshotId = String(record.visual_snapshot_id || "");
  const pageNumber = Math.max(0, Math.trunc(Number(record.page_number) || 0));
  const pageBounds = bbox(record.page_bbox);
  const readableMode = mode(record.readable_mode);
  const visualLayerRequired = ["VISUAL", "HYBRID"].includes(readableMode);
  if (
    !/^[0-9a-f]{64}$/.test(sourceSha256) ||
    pageNumber < 1 ||
    (visualLayerRequired &&
      (!/^[0-9a-f]{64}$/.test(pageImageSha256) ||
        !snapshotId ||
        pageBounds.length !== 4))
  ) {
    return null;
  }
  const providerId = String(record.provider_id || "");
  const providerVersion = String(record.provider_version || "");
  const runs = asArray<unknown>(record.runs)
    .map((run, index) =>
      normalizeRun(
        run,
        {
          snapshotId,
          pageNumber,
          pageBounds,
          pageImageSha256,
          providerId,
          providerVersion,
        },
        index,
      ),
    )
    .filter((run): run is VisualTextRunV1 => Boolean(run))
    .sort(
      (left, right) =>
        left.reading_order - right.reading_order ||
        left.run_id.localeCompare(right.run_id),
    );
  return {
    ...record,
    schema_version: "pdf-selectable-layer/v1",
    source_sha256: sourceSha256,
    page_number: pageNumber,
    page_bbox: pageBounds,
    page_image_sha256: pageImageSha256,
    readable_mode: readableMode,
    visual_snapshot_id: snapshotId,
    provider_id: providerId,
    provider_version: providerVersion,
    runs,
  };
}
