import { describe, expect, it } from "vitest";
import {
  normalizeCaptureQualityManifest,
  normalizeVisualPageCapture,
} from "./contracts";

function manifest(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "capture-quality-manifest/v1",
    project_id: "project-1",
    sor_document_id: "document-1",
    source_sha256: "a".repeat(64),
    base_capture_id: "capture-1",
    base_source_ir_id: "source-ir-1",
    effective_bundle_id: "bundle-1",
    status: "PARTIAL",
    page_count: 2,
    native_ready_page_count: 1,
    visual_required_page_count: 1,
    visual_ready_page_count: 0,
    failed_page_count: 0,
    active_visual_generation_id: null,
    pages: [
      {
        page_number: 1,
        page_bbox: [0, 0, 595, 842],
        readable_mode: "NATIVE",
        capture_status: "NOT_REQUIRED",
        native_character_count: 240,
        native_cjk_character_count: 180,
        native_selectable: true,
        risk_codes: [],
        required_capability: "NONE",
      },
      {
        page_number: 2,
        page_bbox: [0, 0, 595, 842],
        readable_mode: "NONE",
        capture_status: "PENDING",
        native_character_count: 0,
        native_cjk_character_count: 0,
        native_selectable: false,
        risk_codes: ["PDF_VISUAL_TEXT_PROVIDER_MISSING"],
        required_capability: "LAYOUT",
      },
    ],
    ...overrides,
  };
}

describe("source capture frontend contracts", () => {
  it("accepts an exact partial page manifest without inventing readability", () => {
    const normalized = normalizeCaptureQualityManifest(manifest());
    expect(normalized).not.toBeNull();
    expect(normalized?.pages.map((page) => page.readable_mode)).toEqual([
      "NATIVE",
      "NONE",
    ]);
  });

  it("rejects page gaps, count drift, and false READY claims", () => {
    expect(
      normalizeCaptureQualityManifest(
        manifest({ pages: [manifest().pages[0]] }),
      ),
    ).toBeNull();
    expect(
      normalizeCaptureQualityManifest(manifest({ native_ready_page_count: 2 })),
    ).toBeNull();
    expect(
      normalizeCaptureQualityManifest(manifest({ status: "READY" })),
    ).toBeNull();
  });

  it("rejects a manifest without its source and effective-bundle identities", () => {
    expect(
      normalizeCaptureQualityManifest(manifest({ effective_bundle_id: "" })),
    ).toBeNull();
    expect(
      normalizeCaptureQualityManifest(manifest({ base_source_ir_id: "" })),
    ).toBeNull();
    expect(
      normalizeCaptureQualityManifest(manifest({ sor_document_id: "" })),
    ).toBeNull();
  });

  it("accepts only the strict zero-page READY manifest used by non-PDF sources", () => {
    const zeroPage = manifest({
      status: "READY",
      page_count: 0,
      native_ready_page_count: 0,
      visual_required_page_count: 0,
      visual_ready_page_count: 0,
      failed_page_count: 0,
      pages: [],
    });
    expect(normalizeCaptureQualityManifest(zeroPage)).toMatchObject({
      status: "READY",
      page_count: 0,
      pages: [],
    });
    expect(
      normalizeCaptureQualityManifest({ ...zeroPage, status: "PARTIAL" }),
    ).toBeNull();
    expect(
      normalizeCaptureQualityManifest({
        ...zeroPage,
        visual_required_page_count: 1,
      }),
    ).toBeNull();
  });

  it("normalizes a source-bound visual page and canonical visual snapshot id", () => {
    const normalized = normalizeVisualPageCapture({
      schema_version: "visual-page-capture/v1",
      generation_id: "generation-1",
      visual_snapshot_id: "visual-snapshot-2",
      source_sha256: "a".repeat(64),
      base_capture_id: "capture-1",
      page_number: 2,
      page_bbox: [0, 0, 595, 842],
      page_image_sha256: "b".repeat(64),
      width_px: 1190,
      height_px: 1684,
      render_scale: 2,
      renderer_id: "pymupdf",
      renderer_version: "1",
      provider_id: "bailian",
      provider_version: "qwen-ocr-v1",
      capture_status: "READY",
      content_state: "SELECTABLE_TEXT",
      runs: [
        {
          run_id: "run-1",
          observation_id: "observation-1",
          text: "电涡流传感器",
          bbox: [40, 80, 240, 110],
          content_type: "TEXT",
          reading_order: 1,
          confidence: 0.92,
        },
      ],
    });
    expect(normalized).not.toBeNull();
    expect(normalized?.visual_snapshot_id).toBe("visual-snapshot-2");
    expect(normalized?.runs[0]?.source_ref).toMatchObject({
      kind: "VISUAL_TEXT",
      visual_snapshot_id: "visual-snapshot-2",
      observation_id: "observation-1",
      start_offset: 0,
      end_offset: 6,
      page_number: 2,
      page_image_sha256: "b".repeat(64),
    });
  });

  it("keeps a provider-verified empty page visual without inventing text", () => {
    const base = {
      schema_version: "visual-page-capture/v1",
      generation_id: "generation-empty",
      visual_snapshot_id: "visual-empty",
      source_sha256: "a".repeat(64),
      base_capture_id: "capture-1",
      page_number: 3,
      page_bbox: [0, 0, 595, 842],
      page_image_sha256: "b".repeat(64),
      width_px: 1190,
      height_px: 1684,
      render_scale: 2,
      renderer_id: "pymupdf",
      renderer_version: "1",
      provider_id: "bailian",
      provider_version: "qwen-ocr-v1",
      capture_status: "READY",
    };

    expect(
      normalizeVisualPageCapture({
        ...base,
        content_state: "VERIFIED_EMPTY",
        runs: [],
      }),
    ).toMatchObject({
      content_state: "VERIFIED_EMPTY",
      readable_mode: "VISUAL",
      runs: [],
    });
    expect(
      normalizeVisualPageCapture({
        ...base,
        content_state: "SELECTABLE_TEXT",
        runs: [],
      }),
    ).toBeNull();
    expect(
      normalizeVisualPageCapture({
        ...base,
        content_state: "VERIFIED_EMPTY",
        runs: [{ text: "不应存在" }],
      }),
    ).toBeNull();
  });
});
