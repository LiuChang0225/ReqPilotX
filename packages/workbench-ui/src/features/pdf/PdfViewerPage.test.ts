import { describe, expect, it, vi } from "vitest";
import {
  copyPdfSelectionText,
  copyPdfSelectionWithExecCommand,
  isTrustedPdfViewerMessage,
  pdfParentOrigin,
  normalizePdfOverlays,
  pdfWheelNavigation,
  pdfWheelTargetPage,
  pdfSelectionActionPayload,
  readingPdfSelectionEnabled,
  selectableTextLayersForMode,
  visualSourceRefsForRange,
  visualTextRunStyle,
} from "./PdfViewerPage";
import type { PdfSelectableLayerV1 } from "../document-capture/pdfLayer";

describe("PDF reader selection actions", () => {
  it("requires an explicit exact parent origin for an embedded cross-origin host", () => {
    const parent = {} as MessageEventSource, origin = "https://pair.example";
    expect(pdfParentOrigin(new URLSearchParams({embedded:"1",parentOrigin:origin}),window.location.origin)).toBe(origin);
    for (const value of ["*", "null", "javascript:alert(1)", `${origin}/path`, `https://user:password@example.invalid`]) {
      expect(pdfParentOrigin(new URLSearchParams({embedded:"1",parentOrigin:value}),window.location.origin)).toBe(window.location.origin);
    }
    expect(isTrustedPdfViewerMessage({origin,source:parent},parent,origin)).toBe(true);
    expect(isTrustedPdfViewerMessage({origin,source:null},parent,origin)).toBe(false);
    expect(isTrustedPdfViewerMessage({origin:"https://other.example",source:parent},parent,origin)).toBe(false);
  });
  it("preserves the evidence channel without replacing the overlay content type", () => {
    expect(
      normalizePdfOverlays([
        {
          id: "ocr-text",
          type: "TEXT",
          label: "TXT-002",
          bbox: [10, 20, 100, 50],
          channel: "REMOTE_VISUAL",
        },
        {
          id: "native-table",
          type: "TABLE",
          label: "TBL-001",
          bbox: [10, 60, 100, 100],
          channel: "NATIVE",
        },
      ]),
    ).toMatchObject([
      { id: "ocr-text", type: "TEXT", channel: "REMOTE_VISUAL" },
      { id: "native-table", type: "TABLE", channel: "NATIVE" },
    ]);
  });

  it("arbitrates native and visual text layers without duplicate rendering", () => {
    expect(selectableTextLayersForMode("NATIVE", true)).toEqual({
      native: true,
      visual: false,
    });
    expect(selectableTextLayersForMode("VISUAL", true)).toEqual({
      native: false,
      visual: true,
    });
    expect(selectableTextLayersForMode("NONE", true)).toEqual({
      native: false,
      visual: false,
    });
    expect(selectableTextLayersForMode("VISUAL", false)).toEqual({
      native: true,
      visual: false,
    });
  });

  it("turns a visual range across runs into ordered exact source refs", () => {
    const root = document.createElement("div");
    const first = document.createElement("span");
    first.dataset.pdfVisualRun = "run-1";
    first.textContent = "前😀置";
    const second = document.createElement("span");
    second.dataset.pdfVisualRun = "run-2";
    second.textContent = "条件值";
    root.append(first, second);
    document.body.append(root);
    const range = document.createRange();
    range.setStart(first.firstChild!, 1);
    range.setEnd(second.firstChild!, 2);

    const refs = visualSourceRefsForRange(range, root, visualLayer());

    expect(refs).toHaveLength(2);
    expect(refs.map((ref) => ref.observation_id)).toEqual(["obs-1", "obs-2"]);
    expect(refs[0]).toMatchObject({ start_offset: 1, end_offset: 3 });
    expect(refs[1]).toMatchObject({ start_offset: 0, end_offset: 2 });
    expect(visualSourceRefsForRange(range, root, visualLayer(), 1)).toEqual([]);
    root.remove();
  });

  it("fails closed when a visual range has no source run or crosses more than 16 runs", () => {
    const root = document.createElement("div");
    const unknown = document.createElement("span");
    unknown.dataset.pdfVisualRun = "missing-run";
    unknown.textContent = "无来源";
    root.append(unknown);
    document.body.append(root);
    const missingRange = document.createRange();
    missingRange.selectNodeContents(unknown);
    expect(
      visualSourceRefsForRange(missingRange, root, visualLayer()),
    ).toEqual([]);

    const layer = visualLayer();
    layer.runs = Array.from({ length: 17 }, (_, index) => ({
      ...layer.runs[0]!,
      run_id: `many-${index}`,
      observation_id: `many-observation-${index}`,
      text: String(index % 10),
      reading_order: index,
      source_ref: {
        ...layer.runs[0]!.source_ref,
        observation_id: `many-observation-${index}`,
        end_offset: 1,
      },
    }));
    root.replaceChildren();
    const elements = layer.runs.map((run) => {
      const element = document.createElement("span");
      element.dataset.pdfVisualRun = run.run_id;
      element.textContent = run.text;
      root.append(element);
      return element;
    });
    const oversizedRange = document.createRange();
    oversizedRange.setStart(elements[0]!.firstChild!, 0);
    oversizedRange.setEnd(elements[16]!.firstChild!, 1);
    expect(visualSourceRefsForRange(oversizedRange, root, layer)).toEqual([]);
    root.remove();
  });

  it("requires an explicit reading-only query gate", () => {
    expect(
      readingPdfSelectionEnabled(
        new URLSearchParams("embedded=1&selection=reading"),
      ),
    ).toBe(true);
    expect(
      readingPdfSelectionEnabled(new URLSearchParams("embedded=1")),
    ).toBe(false);
    expect(
      readingPdfSelectionEnabled(new URLSearchParams("selection=reading")),
    ).toBe(false);
  });

  it("turns one deliberate wheel gesture into one page direction in fit-page mode", () => {
    expect(
      pdfWheelNavigation({
        deltaY: 100,
        accumulator: 0,
        fitMode: "page",
        scrollTop: 0,
        scrollHeight: 800,
        clientHeight: 800,
      }),
    ).toEqual({ accumulator: 0, direction: 1, preventDefault: true });
    expect(
      pdfWheelNavigation({
        deltaY: -100,
        accumulator: 0,
        fitMode: "page",
        scrollTop: 0,
        scrollHeight: 800,
        clientHeight: 800,
      }),
    ).toEqual({ accumulator: 0, direction: -1, preventDefault: true });
  });

  it("accumulates trackpad movement and resets when direction changes", () => {
    const first = pdfWheelNavigation({
      deltaY: 35,
      accumulator: 0,
      fitMode: "page",
      scrollTop: 0,
      scrollHeight: 800,
      clientHeight: 800,
    });
    expect(first).toEqual({
      accumulator: 35,
      direction: 0,
      preventDefault: true,
    });
    expect(
      pdfWheelNavigation({
        deltaY: -35,
        accumulator: first.accumulator,
        fitMode: "page",
        scrollTop: 0,
        scrollHeight: 800,
        clientHeight: 800,
      }),
    ).toEqual({ accumulator: -35, direction: 0, preventDefault: true });
  });

  it("keeps zoomed page scrolling until the current page boundary", () => {
    expect(
      pdfWheelNavigation({
        deltaY: 100,
        accumulator: 0,
        fitMode: "custom",
        scrollTop: 300,
        scrollHeight: 1600,
        clientHeight: 800,
      }),
    ).toEqual({ accumulator: 0, direction: 0, preventDefault: false });
    expect(
      pdfWheelNavigation({
        deltaY: 100,
        accumulator: 0,
        fitMode: "custom",
        scrollTop: 800,
        scrollHeight: 1600,
        clientHeight: 800,
      }),
    ).toEqual({ accumulator: 0, direction: 1, preventDefault: true });
  });

  it("does not intercept browser zoom gestures and normalizes line deltas", () => {
    expect(
      pdfWheelNavigation({
        deltaY: 100,
        accumulator: 0,
        fitMode: "page",
        scrollTop: 0,
        scrollHeight: 800,
        clientHeight: 800,
        ctrlKey: true,
      }),
    ).toEqual({ accumulator: 0, direction: 0, preventDefault: false });
    expect(
      pdfWheelNavigation({
        deltaY: 6,
        deltaMode: 1,
        accumulator: 0,
        fitMode: "page",
        scrollTop: 0,
        scrollHeight: 800,
        clientHeight: 800,
      }),
    ).toEqual({ accumulator: 0, direction: 1, preventDefault: true });
  });

  it("keeps wheel navigation inside the document page range", () => {
    expect(pdfWheelTargetPage(1, 29, -1)).toBeNull();
    expect(pdfWheelTargetPage(29, 29, 1)).toBeNull();
    expect(pdfWheelTargetPage(10, 29, -1)).toBe(9);
    expect(pdfWheelTargetPage(10, 29, 1)).toBe(11);
  });

  it("accepts viewer control messages only from the same-origin parent", () => {
    const trustedParent = {} as MessageEventSource;
    expect(
      isTrustedPdfViewerMessage(
        { origin: window.location.origin, source: trustedParent },
        trustedParent,
      ),
    ).toBe(true);
    expect(
      isTrustedPdfViewerMessage(
        { origin: window.location.origin, source: null },
        trustedParent,
      ),
    ).toBe(false);
    expect(
      isTrustedPdfViewerMessage(
        { origin: "https://attacker.example", source: trustedParent },
        trustedParent,
      ),
    ).toBe(false);
  });

  it.each([
    {
      label: "multiline paragraph",
      text: Array.from(
        { length: 12 },
        (_, index) => `第${index + 1}行包含较长的视觉识别段落和工程条件`,
      ).join("\n"),
      maxFontSize: 40,
    },
    {
      label: "long wrapped paragraph",
      text: "这是一段需要在整块视觉识别区域内自动换行的工程说明。".repeat(45),
      maxFontSize: 32,
    },
    {
      label: "TSV table",
      text: Array.from(
        { length: 10 },
        (_, row) =>
          Array.from({ length: 8 }, (_, column) => `R${row + 1}C${column + 1}`).join(
            "\t",
          ),
      ).join("\n"),
      maxFontSize: 50,
    },
  ])("fits a $label run inside its bbox", ({ text, maxFontSize }) => {
    const style = visualTextRunStyle(
      [60, 80, 540, 600],
      [0, 0, 600, 800],
      1200,
      1600,
      text,
    );
    const fontSize = Number.parseFloat(String(style.fontSize));

    expect(fontSize).toBeGreaterThan(0);
    expect(fontSize).toBeLessThanOrEqual(maxFontSize);
    expect(style).toMatchObject({
      left: "10%",
      top: "10%",
      width: "80%",
      height: "65%",
      boxSizing: "border-box",
      overflow: "hidden",
      lineHeight: 1.14,
      whiteSpace: "pre-wrap",
      overflowWrap: "anywhere",
      wordBreak: "break-word",
    });
  });

  it.each(["ASK", "EXPLAIN", "TRANSLATE", "SUMMARIZE"] as const)(
    "keeps visual source identity on the %s selection action",
    (action) => {
      const layer = visualLayer();
      const payload = pdfSelectionActionPayload(
        action,
        {
          id: "visual-selection-1",
          text: "电涡流",
          sourceRefs: [layer.runs[0]!.source_ref],
        },
        15,
        layer,
      );
      expect(payload).toMatchObject({
        type: "pdf-selection-action",
        action,
        page: 15,
        sourceRefs: [layer.runs[0]!.source_ref],
        sourceSha256: layer.source_sha256,
        pageImageSha256: layer.page_image_sha256,
        visualSnapshotId: layer.visual_snapshot_id,
      });
    },
  );

  it("prefers the Clipboard API and falls back locally when it is rejected", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const legacyCopy = vi.fn().mockReturnValue(true);
    expect(
      await copyPdfSelectionText("选中的原文", { writeText }, legacyCopy),
    ).toBe(true);
    expect(writeText).toHaveBeenCalledWith("选中的原文");
    expect(legacyCopy).not.toHaveBeenCalled();

    const rejected = vi.fn().mockRejectedValue(new Error("denied"));
    expect(
      await copyPdfSelectionText(
        "选中的原文",
        { writeText: rejected },
        legacyCopy,
      ),
    ).toBe(true);
    expect(legacyCopy).toHaveBeenCalledWith("选中的原文");

    const failedFallback = vi.fn().mockReturnValue(false);
    expect(
      await copyPdfSelectionText("选中的原文", undefined, failedFallback),
    ).toBe(false);
  });

  it("cleans up the legacy textarea and restores focus and selection", () => {
    const button = document.createElement("button");
    const source = document.createElement("span");
    source.textContent = "保留这段选择";
    document.body.append(button, source);
    button.focus();
    const selection = document.getSelection();
    const range = document.createRange();
    range.selectNodeContents(source);
    selection?.removeAllRanges();
    selection?.addRange(range);
    const originalExecCommand = document.execCommand;
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: vi.fn().mockReturnValue(true),
    });
    try {
      expect(copyPdfSelectionWithExecCommand("选中的原文")).toBe(true);
      expect(document.querySelector("textarea[aria-hidden='true']")).toBeNull();
      expect(document.activeElement).toBe(button);
      expect(document.getSelection()?.toString()).toBe("保留这段选择");
    } finally {
      selection?.removeAllRanges();
      button.remove();
      source.remove();
      Object.defineProperty(document, "execCommand", {
        configurable: true,
        value: originalExecCommand,
      });
    }
  });
});

function visualLayer(): PdfSelectableLayerV1 {
  const common = {
    page_number: 15,
    bbox: [20, 20, 120, 40],
    page_bbox: [0, 0, 595, 842],
    page_image_sha256: "b".repeat(64),
    provider_id: "bailian",
    provider_version: "qwen-ocr-v1",
  };
  return {
    schema_version: "pdf-selectable-layer/v1",
    source_sha256: "a".repeat(64),
    page_number: 15,
    page_bbox: common.page_bbox,
    page_image_sha256: common.page_image_sha256,
    readable_mode: "VISUAL",
    visual_snapshot_id: "visual-15",
    provider_id: common.provider_id,
    provider_version: common.provider_version,
    runs: [
      {
        run_id: "run-1",
        observation_id: "obs-1",
        text: "前😀置",
        bbox: [20, 20, 120, 40],
        content_type: "TEXT",
        reading_order: 1,
        confidence: 0.9,
        source_ref: {
          kind: "VISUAL_TEXT",
          visual_snapshot_id: "visual-15",
          observation_id: "obs-1",
          start_offset: 0,
          end_offset: 3,
          ...common,
        },
      },
      {
        run_id: "run-2",
        observation_id: "obs-2",
        text: "条件值",
        bbox: [20, 50, 120, 70],
        content_type: "TEXT",
        reading_order: 2,
        confidence: 0.88,
        source_ref: {
          kind: "VISUAL_TEXT",
          visual_snapshot_id: "visual-15",
          observation_id: "obs-2",
          start_offset: 0,
          end_offset: 3,
          ...common,
          bbox: [20, 50, 120, 70],
        },
      },
    ],
  };
}
