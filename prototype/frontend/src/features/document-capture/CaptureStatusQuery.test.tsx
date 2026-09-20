import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../shared/api/client";
import { CaptureStatusQuery } from "./CaptureStatusQuery";

const captureApiMocks = vi.hoisted(() => ({
  quality: vi.fn(),
  visualPage: vi.fn(),
  resumeVisualPage: vi.fn(),
}));

vi.mock("./captureApi", () => ({ captureApi: captureApiMocks }));

beforeEach(() => {
  Object.values(captureApiMocks).forEach((mock) => mock.mockReset());
});

afterEach(cleanup);

describe("CaptureStatusQuery", () => {
  it("reads the neutral manifest without starting capture and exposes risk and visual pages", async () => {
    captureApiMocks.quality.mockResolvedValue(manifest());
    const onPage = vi.fn();
    const user = userEvent.setup();
    render(
      <CaptureStatusQuery
        projectId="project-1"
        documentId="document-1"
        sourceSha256={"a".repeat(64)}
        currentPage={1}
        onPage={onPage}
      />,
    );

    await user.click(
      await screen.findByRole("button", {
        name: "文档可读性：可读 2/3",
      }),
    );
    expect(
      screen.getByText(/进入解析诊断不会自动上传页面或启动识别/),
    ).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "捕获风险页" })).toHaveTextContent(
      "P2P3",
    );
    expect(
      screen.getByRole("region", { name: "视觉识别完成页" }),
    ).toHaveTextContent("P2");
    await user.click(screen.getAllByRole("button", { name: "P2" })[0]!);
    expect(onPage).toHaveBeenCalledWith(2);
    await user.click(screen.getByText("技术来源"));
    expect(screen.getByText("bailian / qwen-ocr-v1")).toBeInTheDocument();
    expect(captureApiMocks.quality).toHaveBeenCalledOnce();
    expect(captureApiMocks.visualPage).not.toHaveBeenCalled();
    expect(captureApiMocks.resumeVisualPage).not.toHaveBeenCalled();
  });

  it("states that capture is not prepared for the backend's explicit 409 reason", async () => {
    captureApiMocks.quality.mockRejectedValue(
      new ApiError("not prepared", 409, {
        detail: { reason_code: "source_capture_not_prepared" },
      }),
    );
    const user = userEvent.setup();
    render(
      <CaptureStatusQuery
        projectId="project-1"
        documentId="document-1"
        sourceSha256={"a".repeat(64)}
        currentPage={1}
      />,
    );

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "文档可读性：可读性待确认" }),
      ).toBeInTheDocument(),
    );
    await user.click(
      screen.getByRole("button", { name: "文档可读性：可读性待确认" }),
    );
    expect(
      screen.getByText("中立捕获尚未准备；当前只显示已有解析事实。"),
    ).toBeInTheDocument();
    expect(captureApiMocks.resumeVisualPage).not.toHaveBeenCalled();
  });

  it("does not disguise an unrelated 409 as a not-prepared state", async () => {
    captureApiMocks.quality.mockRejectedValue(
      new ApiError("capture binding conflict", 409, {
        detail: { reason_code: "source_capture_binding_conflict" },
      }),
    );
    const user = userEvent.setup();
    render(
      <CaptureStatusQuery
        projectId="project-1"
        documentId="document-1"
        sourceSha256={"a".repeat(64)}
        currentPage={1}
      />,
    );

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "文档可读性：可读性待确认" }),
      ).toBeInTheDocument(),
    );
    await user.click(
      screen.getByRole("button", { name: "文档可读性：可读性待确认" }),
    );
    expect(screen.getByText("capture binding conflict")).toBeInTheDocument();
    expect(
      screen.queryByText("中立捕获尚未准备；当前只显示已有解析事实。"),
    ).not.toBeInTheDocument();
  });
});

function manifest() {
  return {
    schema_version: "capture-quality-manifest/v1",
    project_id: "project-1",
    sor_document_id: "document-1",
    source_sha256: "a".repeat(64),
    base_capture_id: "capture-1",
    base_source_ir_id: "source-ir-1",
    effective_bundle_id: "bundle-1",
    status: "PARTIAL",
    page_count: 3,
    native_ready_page_count: 1,
    visual_required_page_count: 2,
    visual_ready_page_count: 1,
    failed_page_count: 0,
    active_visual_generation_id: "visual-generation-1",
    pages: [
      {
        page_number: 1,
        page_bbox: [0, 0, 595, 842],
        readable_mode: "NATIVE",
        capture_status: "NOT_REQUIRED",
        native_character_count: 120,
        native_cjk_character_count: 80,
        native_selectable: true,
        risk_codes: [],
        required_capability: "NONE",
      },
      {
        page_number: 2,
        page_bbox: [0, 0, 595, 842],
        readable_mode: "VISUAL",
        capture_status: "READY",
        native_character_count: 0,
        native_cjk_character_count: 0,
        native_selectable: false,
        risk_codes: ["VISUAL_TEXT_REQUIRED"],
        required_capability: "LAYOUT",
        visual_snapshot_id: "visual-2",
        page_image_sha256: "b".repeat(64),
        provider_id: "bailian",
        provider_version: "qwen-ocr-v1",
      },
      {
        page_number: 3,
        page_bbox: [0, 0, 595, 842],
        readable_mode: "NONE",
        capture_status: "PENDING",
        native_character_count: 0,
        native_cjk_character_count: 0,
        native_selectable: false,
        risk_codes: ["VISUAL_PROVIDER_REQUIRED"],
        required_capability: "OCR",
      },
    ],
  };
}
