import { asArray, asRecord } from "../../shared/api/client";
import type { JsonRecord } from "../../shared/api/contracts";

export type CaptureManifestStatus = "READY" | "PARTIAL" | "BLOCKED";
export type CaptureReadableMode = "NATIVE" | "VISUAL" | "HYBRID" | "NONE";
export type CapturePageStatus =
  | "NOT_REQUIRED"
  | "PENDING"
  | "RUNNING"
  | "READY"
  | "FAILED";
export type CaptureRequiredCapability = "NONE" | "OCR" | "LAYOUT";
export type VisualContentState = "SELECTABLE_TEXT" | "VERIFIED_EMPTY";

export interface CaptureQualityPageV1 extends JsonRecord {
  page_number: number;
  page_bbox: number[];
  readable_mode: CaptureReadableMode;
  capture_status: CapturePageStatus;
  native_character_count: number;
  native_cjk_character_count: number;
  native_selectable: boolean;
  risk_codes: string[];
  required_capability: CaptureRequiredCapability;
  visual_snapshot_id?: string | null;
  page_image_sha256?: string | null;
  provider_id?: string | null;
  provider_version?: string | null;
  failure_code?: string | null;
}

export interface CaptureQualityManifestV1 extends JsonRecord {
  schema_version: "capture-quality-manifest/v1" | (string & {});
  project_id: string;
  sor_document_id: string;
  source_sha256: string;
  base_capture_id: string;
  base_source_ir_id: string;
  effective_bundle_id: string;
  status: CaptureManifestStatus;
  page_count: number;
  native_ready_page_count: number;
  visual_required_page_count: number;
  visual_ready_page_count: number;
  failed_page_count: number;
  active_visual_generation_id?: string | null;
  pages: CaptureQualityPageV1[];
}

export interface VisualTextSourceRefV1 extends JsonRecord {
  kind: "VISUAL_TEXT";
  visual_snapshot_id: string;
  observation_id: string;
  start_offset: number;
  end_offset: number;
  page_number?: number;
  bbox?: number[];
  page_bbox?: number[];
  page_image_sha256?: string;
  provider_id?: string;
  provider_version?: string;
}

export interface VisualTextRunV1 extends JsonRecord {
  run_id: string;
  observation_id: string;
  text: string;
  bbox: number[];
  content_type: string;
  reading_order: number;
  confidence: number | null;
  source_ref: VisualTextSourceRefV1;
}

export interface VisualPageCaptureV1 extends JsonRecord {
  schema_version: "visual-page-capture/v1" | (string & {});
  generation_id: string;
  visual_snapshot_id: string;
  source_sha256: string;
  base_capture_id: string;
  page_number: number;
  page_bbox: number[];
  page_image_sha256: string;
  width_px: number;
  height_px: number;
  render_scale: number;
  renderer_id: string;
  renderer_version: string;
  provider_id: string;
  provider_version: string;
  capture_status: CapturePageStatus;
  content_state: VisualContentState;
  failure_code?: string | null;
  readable_mode: CaptureReadableMode;
  runs: VisualTextRunV1[];
}

const SHA256_PATTERN = /^[0-9a-f]{64}$/;

function wholeNumber(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : 0;
}

function finiteNumber(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
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

function stringList(value: unknown): string[] {
  return [...new Set(asArray<unknown>(value).map(String).filter(Boolean))];
}

function readableMode(value: unknown): CaptureReadableMode {
  const normalized = String(value || "NONE").toUpperCase();
  return ["NATIVE", "VISUAL", "HYBRID", "NONE"].includes(normalized)
    ? (normalized as CaptureReadableMode)
    : "NONE";
}

function pageStatus(value: unknown): CapturePageStatus {
  const normalized = String(value || "PENDING").toUpperCase();
  return ["NOT_REQUIRED", "PENDING", "RUNNING", "READY", "FAILED"].includes(
    normalized,
  )
    ? (normalized as CapturePageStatus)
    : "PENDING";
}

function requiredCapability(value: unknown): CaptureRequiredCapability {
  const normalized = String(value || "NONE").toUpperCase();
  return ["NONE", "OCR", "LAYOUT"].includes(normalized)
    ? (normalized as CaptureRequiredCapability)
    : "NONE";
}

export function normalizeCaptureQualityManifest(
  payload: unknown,
): CaptureQualityManifestV1 | null {
  const record = asRecord(payload);
  if (String(record.schema_version || "") !== "capture-quality-manifest/v1") {
    return null;
  }
  const sourceSha256 = String(record.source_sha256 || "").toLowerCase();
  const projectId = String(record.project_id || "");
  const documentId = String(record.sor_document_id || "");
  const baseCaptureId = String(record.base_capture_id || "");
  const baseSourceIrId = String(record.base_source_ir_id || "");
  const effectiveBundleId = String(record.effective_bundle_id || "");
  const status = String(record.status || "").toUpperCase();
  if (
    !SHA256_PATTERN.test(sourceSha256) ||
    !projectId ||
    !documentId ||
    !baseCaptureId ||
    !baseSourceIrId ||
    !effectiveBundleId ||
    !["READY", "PARTIAL", "BLOCKED"].includes(status)
  ) {
    return null;
  }
  const pages = asArray<JsonRecord>(record.pages)
    .map<CaptureQualityPageV1 | null>((raw) => {
      const pageNumber = wholeNumber(raw.page_number);
      if (pageNumber < 1) return null;
      const visualSnapshotId = String(raw.visual_snapshot_id || "") || null;
      const pageImageSha256 = String(raw.page_image_sha256 || "").toLowerCase();
      const pageBounds = bbox(raw.page_bbox);
      const normalizedReadableMode = readableMode(raw.readable_mode);
      const normalizedPageStatus = pageStatus(raw.capture_status);
      const nativeSelectable = raw.native_selectable === true;
      if (
        pageBounds.length !== 4 ||
        (normalizedReadableMode === "NATIVE" && !nativeSelectable) ||
        (normalizedReadableMode === "VISUAL" &&
          (normalizedPageStatus !== "READY" ||
            !visualSnapshotId ||
            !SHA256_PATTERN.test(pageImageSha256))) ||
        (normalizedReadableMode === "NONE" && normalizedPageStatus === "READY")
      ) {
        return null;
      }
      return {
        ...raw,
        page_number: pageNumber,
        page_bbox: pageBounds,
        readable_mode: normalizedReadableMode,
        capture_status: normalizedPageStatus,
        native_character_count: wholeNumber(raw.native_character_count),
        native_cjk_character_count: wholeNumber(raw.native_cjk_character_count),
        native_selectable: nativeSelectable,
        risk_codes: stringList(raw.risk_codes),
        required_capability: requiredCapability(raw.required_capability),
        visual_snapshot_id: visualSnapshotId,
        page_image_sha256: SHA256_PATTERN.test(pageImageSha256)
          ? pageImageSha256
          : null,
        provider_id: String(raw.provider_id || "") || null,
        provider_version: String(raw.provider_version || "") || null,
        failure_code: String(raw.failure_code || "") || null,
      };
    })
    .filter((page): page is CaptureQualityPageV1 => Boolean(page))
    .sort((left, right) => left.page_number - right.page_number);
  const pageCount = wholeNumber(record.page_count);
  const zeroPageReadyManifest =
    pageCount === 0 &&
    pages.length === 0 &&
    status === "READY" &&
    wholeNumber(record.native_ready_page_count) === 0 &&
    wholeNumber(record.visual_required_page_count) === 0 &&
    wholeNumber(record.visual_ready_page_count) === 0 &&
    wholeNumber(record.failed_page_count) === 0;
  const exactPageCoverage =
    zeroPageReadyManifest ||
    (pageCount > 0 &&
      pages.length === pageCount &&
      pages.every((page, index) => page.page_number === index + 1));
  const nativeReadyPageCount = pages.filter(
    (page) => page.readable_mode === "NATIVE",
  ).length;
  const visualRequiredPageCount = pages.filter(
    (page) => page.required_capability !== "NONE",
  ).length;
  const visualReadyPageCount = pages.filter(
    (page) =>
      page.readable_mode === "VISUAL" && page.capture_status === "READY",
  ).length;
  const failedPageCount = pages.filter(
    (page) => page.capture_status === "FAILED",
  ).length;
  const allPagesReadable = pages.every((page) =>
    ["NATIVE", "VISUAL"].includes(page.readable_mode),
  );
  if (
    !exactPageCoverage ||
    nativeReadyPageCount !== wholeNumber(record.native_ready_page_count) ||
    visualRequiredPageCount !== wholeNumber(record.visual_required_page_count) ||
    visualReadyPageCount !== wholeNumber(record.visual_ready_page_count) ||
    failedPageCount !== wholeNumber(record.failed_page_count) ||
    (status === "READY") !== allPagesReadable ||
    (pageCount === 0 && !zeroPageReadyManifest)
  ) {
    return null;
  }
  return {
    ...record,
    schema_version: "capture-quality-manifest/v1",
    project_id: projectId,
    sor_document_id: documentId,
    source_sha256: sourceSha256,
    base_capture_id: baseCaptureId,
    base_source_ir_id: baseSourceIrId,
    effective_bundle_id: effectiveBundleId,
    status: status as CaptureManifestStatus,
    page_count: pageCount,
    native_ready_page_count: nativeReadyPageCount,
    visual_required_page_count: visualRequiredPageCount,
    visual_ready_page_count: visualReadyPageCount,
    failed_page_count: failedPageCount,
    active_visual_generation_id:
      String(record.active_visual_generation_id || "") || null,
    pages,
  };
}

export function normalizeVisualPageCapture(
  payload: unknown,
): VisualPageCaptureV1 | null {
  const record = asRecord(payload);
  if (String(record.schema_version || "") !== "visual-page-capture/v1") {
    return null;
  }
  const sourceSha256 = String(record.source_sha256 || "").toLowerCase();
  const pageImageSha256 = String(record.page_image_sha256 || "").toLowerCase();
  const snapshotId = String(
    record.visual_snapshot_id || record.snapshot_id || "",
  );
  const generationId = String(record.generation_id || "");
  const baseCaptureId = String(record.base_capture_id || "");
  const pageNumber = wholeNumber(record.page_number);
  const pageBounds = bbox(record.page_bbox);
  const captureStatus = pageStatus(record.capture_status);
  const contentState = String(record.content_state || "").toUpperCase();
  if (
    !SHA256_PATTERN.test(sourceSha256) ||
    !generationId ||
    !baseCaptureId ||
    pageNumber < 1 ||
    pageBounds.length !== 4 ||
    (captureStatus === "READY" &&
      (!SHA256_PATTERN.test(pageImageSha256) || !snapshotId)) ||
    !["SELECTABLE_TEXT", "VERIFIED_EMPTY"].includes(contentState)
  ) {
    return null;
  }
  const providerId = String(record.provider_id || "");
  const providerVersion = String(record.provider_version || "");
  const runs = asArray<JsonRecord>(record.runs)
    .map<VisualTextRunV1 | null>((raw, index) => {
      const text = String(raw.text || "");
      const observationId = String(raw.observation_id || "");
      const runBbox = bbox(raw.bbox);
      if (!text || !observationId || runBbox.length !== 4) return null;
      const confidence = finiteNumber(raw.confidence);
      return {
        ...raw,
        run_id: String(raw.run_id || `${snapshotId}:${observationId}:${index}`),
        observation_id: observationId,
        text,
        bbox: runBbox,
        content_type: String(raw.content_type || "TEXT").toUpperCase(),
        reading_order: wholeNumber(raw.reading_order ?? index),
        confidence,
        source_ref: {
          kind: "VISUAL_TEXT",
          visual_snapshot_id: snapshotId,
          observation_id: observationId,
          start_offset: 0,
          end_offset: Array.from(text).length,
          page_number: pageNumber,
          bbox: runBbox,
          page_bbox: pageBounds,
          page_image_sha256: pageImageSha256,
          provider_id: providerId,
          provider_version: providerVersion,
        },
      };
    })
    .filter((run): run is VisualTextRunV1 => Boolean(run))
    .sort(
      (left, right) =>
        left.reading_order - right.reading_order ||
        left.run_id.localeCompare(right.run_id),
    );
  if (
    captureStatus === "READY" &&
    ((contentState === "SELECTABLE_TEXT" && runs.length === 0) ||
      (contentState === "VERIFIED_EMPTY" &&
        asArray<unknown>(record.runs).length !== 0))
  ) {
    return null;
  }
  return {
    ...record,
    schema_version: "visual-page-capture/v1",
    generation_id: generationId,
    visual_snapshot_id: snapshotId,
    source_sha256: sourceSha256,
    base_capture_id: baseCaptureId,
    page_number: pageNumber,
    page_bbox: pageBounds,
    page_image_sha256: pageImageSha256,
    width_px: wholeNumber(record.width_px),
    height_px: wholeNumber(record.height_px),
    render_scale: finiteNumber(record.render_scale) || 1,
    renderer_id: String(record.renderer_id || ""),
    renderer_version: String(record.renderer_version || ""),
    provider_id: providerId,
    provider_version: providerVersion,
    capture_status: captureStatus,
    content_state: contentState as VisualContentState,
    failure_code: String(record.failure_code || "") || null,
    readable_mode: captureStatus === "READY" ? "VISUAL" : "NONE",
    runs,
  };
}

export function capturePageNeedsVisualWork(page: CaptureQualityPageV1): boolean {
  return (
    page.required_capability !== "NONE" &&
    page.capture_status !== "READY" &&
    page.capture_status !== "NOT_REQUIRED"
  );
}
