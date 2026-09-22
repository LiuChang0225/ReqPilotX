import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CaptureQualityManifestV1 } from "./contracts";
import { CaptureStatus } from "./CaptureStatus";

afterEach(cleanup);

describe("CaptureStatus", () => {
  it("keeps partial coverage honest and offers bulk plus per-page continuation", async () => {
    const user = userEvent.setup();
    const onResumeAll = vi.fn();
    const onResumePage = vi.fn();
    render(
      <CaptureStatus
        manifest={partialManifest()}
        currentPage={2}
        onResumeAll={onResumeAll}
        onResumePage={onResumePage}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "文档可读性：可读 1/3" }),
    );
    expect(screen.getByText("2 页当前无法完整选择")).toBeInTheDocument();
    expect(
      screen.getByText(/页面图像发送给已配置的视觉模型/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "识别全部 2 页" }));
    expect(onResumeAll).toHaveBeenCalledWith([2, 3]);
    await user.click(screen.getAllByRole("button", { name: "识别本页" })[0]!);
    expect(onResumePage).toHaveBeenCalledWith(2);
  });

  it("does not describe a failed page as readable", async () => {
    const user = userEvent.setup();
    const manifest = partialManifest();
    manifest.pages[1] = {
      ...manifest.pages[1]!,
      capture_status: "FAILED",
      failure_code: "VISUAL_PROVIDER_TIMEOUT",
    };
    manifest.failed_page_count = 1;
    render(<CaptureStatus manifest={manifest} onResumePage={vi.fn()} />);
    await user.click(
      screen.getByRole("button", { name: "文档可读性：可读 1/3" }),
    );
    expect(screen.getByText("识别失败，可重试")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重新识别" }),
    ).toBeInTheDocument();
  });

  it("explains deterministic image-only failure without offering a retry loop", async () => {
    const user = userEvent.setup();
    const manifest = partialManifest();
    manifest.pages[1] = {
      ...manifest.pages[1]!,
      capture_status: "FAILED",
      failure_code: "visual_image_semantics_not_supported",
    };
    manifest.failed_page_count = 1;
    const onResumePage = vi.fn();
    render(<CaptureStatus manifest={manifest} onResumePage={onResumePage} />);

    await user.click(
      screen.getByRole("button", { name: "文档可读性：可读 1/3" }),
    );
    expect(
      screen.getByText(
        "当前仅支持文字层；该页为无文字图像/图纸，暂不能完整阅读",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("无需重复识别")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "识别全部 1 页" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新识别" })).not.toBeInTheDocument();
    expect(onResumePage).not.toHaveBeenCalled();
  });

  it("requires a configuration fix for authentication failures instead of retrying forever", async () => {
    const user = userEvent.setup();
    const manifest = partialManifest();
    manifest.pages[1] = {
      ...manifest.pages[1]!,
      capture_status: "FAILED",
      failure_code: "pdf_visual_text_authentication_failed",
    };
    manifest.failed_page_count = 1;
    const onResumePage = vi.fn();
    render(<CaptureStatus manifest={manifest} onResumePage={onResumePage} />);

    await user.click(
      screen.getByRole("button", { name: "文档可读性：可读 1/3" }),
    );
    expect(
      screen.getByText(
        "视觉模型认证或配置不可用，请先修正 AI 阅读 PDF 页面解析配置",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("无需重复识别")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新识别" })).not.toBeInTheDocument();
    expect(onResumePage).not.toHaveBeenCalled();
  });

  it("keeps the read-only diagnosis view compact without repeating per-page cards", async () => {
    const user = userEvent.setup();
    const onPage = vi.fn();
    render(
      <CaptureStatus
        manifest={partialManifest()}
        currentPage={2}
        readOnly
        onPage={onPage}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "文档可读性：可读 1/3" }),
    );
    expect(
      screen.getByText(/进入解析诊断不会自动上传页面或启动识别/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "识别本页" })).not.toBeInTheDocument();
    expect(screen.queryByText("只读")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "P2" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "P3" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "P2" }));
    expect(onPage).toHaveBeenCalledWith(2);
  });
});

function partialManifest(): CaptureQualityManifestV1 {
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
    visual_ready_page_count: 0,
    failed_page_count: 0,
    active_visual_generation_id: null,
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
      ...[2, 3].map((page) => ({
        page_number: page,
        page_bbox: [0, 0, 595, 842],
        readable_mode: "NONE" as const,
        capture_status: "PENDING" as const,
        native_character_count: 0,
        native_cjk_character_count: 0,
        native_selectable: false,
        risk_codes: ["VISUAL_TEXT_REQUIRED"],
        required_capability: "LAYOUT" as const,
      })),
    ],
  };
}
