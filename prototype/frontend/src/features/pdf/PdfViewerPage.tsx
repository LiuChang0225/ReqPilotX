import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type WheelEvent as ReactWheelEvent,
} from "react";
import { GlobalWorkerOptions, TextLayer, getDocument } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { asArray, asRecord } from "../../shared/api/client";
import type { JsonRecord } from "../../shared/api/contracts";
import { cx } from "../../shared/lib/format";
import { Brand } from "../../shared/ui/Brand";
import { ErrorBlock, LoadingBlock } from "../../shared/ui/Feedback";
import { Icon } from "../../shared/ui/Icon";
import {
  normalizePdfSelectableLayer,
  type PdfSelectableLayerV1,
} from "../document-capture/pdfLayer";
import type { VisualTextSourceRefV1 } from "../document-capture/contracts";
import { pdfCanvasOutput } from "./canvasRender";
import {
  overlayLabelPlacement,
  overlayStyleFor,
  overlayFocusView,
} from "./overlayGeometry";

GlobalWorkerOptions.workerSrc = workerUrl;

interface PdfOverlay {
  id: string;
  type: string;
  label: string;
  bbox: number[];
  sourceBounds?: number[];
  selected: boolean;
  focus: boolean;
  precision: string;
  channel: "REMOTE_VISUAL" | "NATIVE" | "";
}

export type PdfSelectionAction = "ASK" | "EXPLAIN" | "TRANSLATE" | "SUMMARIZE";
type PdfSelectionCopyState = "IDLE" | "COPIED" | "FAILED";

interface PdfTextSelection {
  id: string;
  text: string;
  left: number;
  top: number;
  below: boolean;
  sourceRefs: VisualTextSourceRefV1[];
}

export function pdfSelectionActionPayload(
  action: PdfSelectionAction,
  selection: Pick<PdfTextSelection, "id" | "text" | "sourceRefs">,
  page: number,
  layer: PdfSelectableLayerV1 | null,
): JsonRecord {
  return {
    type: "pdf-selection-action",
    selectionId: selection.id,
    page,
    text: selection.text,
    ...(selection.sourceRefs.length
      ? {
          sourceRefs: selection.sourceRefs,
          source_refs: selection.sourceRefs,
          provenanceChannel: "REMOTE_VISUAL",
          sourceSha256: layer?.source_sha256,
          pageImageSha256: layer?.page_image_sha256,
          visualSnapshotId: layer?.visual_snapshot_id,
        }
      : {}),
    action,
  };
}

function finiteNumber(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function normalizePdfOverlays(value: unknown): PdfOverlay[] {
  return asArray<JsonRecord>(value)
    .map<PdfOverlay | null>((raw, index) => {
      const bbox = asArray<unknown>(raw.bbox).map(Number);
      if (
        bbox.length !== 4 ||
        !bbox.every(Number.isFinite) ||
        bbox[2] <= bbox[0] ||
        bbox[3] <= bbox[1]
      ) {
        return null;
      }
      const sourceBounds = asArray<unknown>(
        raw.sourceBounds ?? raw.source_bounds,
      ).map(Number);
      return {
        id: String(raw.id || `overlay-${index + 1}`),
        type: String(raw.type || "UNKNOWN").toUpperCase(),
        label: String(raw.label ?? raw.type ?? index + 1),
        bbox,
        sourceBounds:
          sourceBounds.length === 4 &&
          sourceBounds.every(Number.isFinite) &&
          sourceBounds[2] > sourceBounds[0] &&
          sourceBounds[3] > sourceBounds[1]
            ? sourceBounds
            : undefined,
        selected: raw.selected === true,
        focus: raw.focus === true,
        precision: String(raw.precision || "").toUpperCase(),
        channel: ["REMOTE_VISUAL", "NATIVE"].includes(
          String(raw.channel || "").toUpperCase(),
        )
          ? (String(raw.channel || "").toUpperCase() as PdfOverlay["channel"])
          : "",
      };
    })
    .filter((item): item is PdfOverlay => Boolean(item));
}

function decodeInlineOverlays(value: string | null): PdfOverlay[] {
  if (!value) return [];
  try {
    const decoded = value.startsWith("[")
      ? value
      : decodeURIComponent(escape(window.atob(value)));
    return normalizePdfOverlays(JSON.parse(decoded));
  } catch {
    return [];
  }
}

export function readingPdfSelectionEnabled(params: URLSearchParams): boolean {
  return (
    params.get("embedded") === "1" && params.get("selection") === "reading"
  );
}

export function copyPdfSelectionWithExecCommand(
  text: string,
  documentRef: Document = document,
): boolean {
  if (!text || !documentRef.body || typeof documentRef.execCommand !== "function") {
    return false;
  }
  const activeElement = documentRef.activeElement as HTMLElement | null;
  const selection = documentRef.getSelection();
  const savedRanges = selection
    ? Array.from({ length: selection.rangeCount }, (_, index) =>
        selection.getRangeAt(index).cloneRange(),
      )
    : [];
  const textarea = documentRef.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.setAttribute("aria-hidden", "true");
  textarea.style.position = "fixed";
  textarea.style.inset = "0 auto auto -9999px";
  textarea.style.opacity = "0";
  documentRef.body.appendChild(textarea);
  let copied = false;
  try {
    textarea.select();
    copied = documentRef.execCommand("copy") === true;
  } catch {
    copied = false;
  } finally {
    textarea.remove();
    if (selection) {
      selection.removeAllRanges();
      savedRanges.forEach((range) => selection.addRange(range));
    }
    activeElement?.focus({ preventScroll: true });
  }
  return copied;
}

export async function copyPdfSelectionText(
  text: string,
  clipboard: Pick<Clipboard, "writeText"> | undefined =
    typeof navigator === "undefined" ? undefined : navigator.clipboard,
  legacyCopy: (value: string) => boolean = copyPdfSelectionWithExecCommand,
): Promise<boolean> {
  if (clipboard?.writeText) {
    try {
      await clipboard.writeText(text);
      return true;
    } catch {
      // A sandboxed browser may reject the async Clipboard API. The following
      // user-gesture-bound fallback still keeps the selected source local.
    }
  }
  return legacyCopy(text);
}

function visualRunElement(
  node: Node,
  root: HTMLElement,
): HTMLElement | null {
  const element =
    node.nodeType === Node.ELEMENT_NODE
      ? (node as Element)
      : node.parentElement;
  const run = element?.closest<HTMLElement>("[data-pdf-visual-run]") || null;
  return run && root.contains(run) ? run : null;
}

function unicodeOffsetWithin(
  element: HTMLElement,
  container: Node,
  offset: number,
): number {
  const range = document.createRange();
  try {
    range.selectNodeContents(element);
    range.setEnd(container, offset);
    return Array.from(range.toString()).length;
  } catch {
    return 0;
  } finally {
    range.detach?.();
  }
}

export function visualSourceRefsForRange(
  range: Range,
  root: HTMLElement,
  layer: PdfSelectableLayerV1,
  maxSourceRefs = 16,
): VisualTextSourceRefV1[] {
  const startElement = visualRunElement(range.startContainer, root);
  const endElement = visualRunElement(range.endContainer, root);
  if (!startElement || !endElement) return [];
  const elements = Array.from(
    root.querySelectorAll<HTMLElement>("[data-pdf-visual-run]"),
  );
  const startIndex = elements.indexOf(startElement);
  const endIndex = elements.indexOf(endElement);
  if (startIndex < 0 || endIndex < startIndex) return [];
  const runById = new Map(layer.runs.map((run) => [run.run_id, run]));
  const sourceRefs: VisualTextSourceRefV1[] = [];
  for (let index = startIndex; index <= endIndex; index += 1) {
    const element = elements[index];
    if (!element) continue;
    const run = runById.get(String(element.dataset.pdfVisualRun || ""));
    if (!run) continue;
    const runLength = Array.from(run.text).length;
    const localStart =
      index === startIndex
        ? unicodeOffsetWithin(element, range.startContainer, range.startOffset)
        : 0;
    const localEnd =
      index === endIndex
        ? unicodeOffsetWithin(element, range.endContainer, range.endOffset)
        : runLength;
    const startOffset = Math.max(0, Math.min(runLength, localStart));
    const endOffset = Math.max(startOffset, Math.min(runLength, localEnd));
    if (endOffset <= startOffset) continue;
    sourceRefs.push({
      ...run.source_ref,
      start_offset: run.source_ref.start_offset + startOffset,
      end_offset: run.source_ref.start_offset + endOffset,
    });
  }
  return sourceRefs.length > 0 && sourceRefs.length <= maxSourceRefs
    ? sourceRefs
    : [];
}

export function selectableTextLayersForMode(
  mode: string,
  hasBoundLayer: boolean,
): { native: boolean; visual: boolean } {
  if (!hasBoundLayer) return { native: true, visual: false };
  const normalized = String(mode || "NONE").toUpperCase();
  return {
    native: normalized === "NATIVE" || normalized === "HYBRID",
    visual: normalized === "VISUAL" || normalized === "HYBRID",
  };
}

const VISUAL_TEXT_LINE_HEIGHT = 1.14;
const MAX_VISUAL_TEXT_FONT_PX = 64;
const PDF_WHEEL_PAGE_THRESHOLD = 90;

export interface PdfWheelNavigationInput {
  deltaY: number;
  deltaMode?: number;
  accumulator: number;
  fitMode: "page" | "width" | "custom";
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
  ctrlKey?: boolean;
  metaKey?: boolean;
}

export interface PdfWheelNavigationResult {
  accumulator: number;
  direction: -1 | 0 | 1;
  preventDefault: boolean;
}

export function pdfWheelNavigation(
  input: PdfWheelNavigationInput,
): PdfWheelNavigationResult {
  if (input.ctrlKey || input.metaKey || input.deltaY === 0) {
    return { accumulator: 0, direction: 0, preventDefault: false };
  }
  const multiplier =
    input.deltaMode === 1
      ? 16
      : input.deltaMode === 2
        ? Math.max(1, input.clientHeight)
        : 1;
  const delta = input.deltaY * multiplier;
  const scrollable = input.scrollHeight > input.clientHeight + 2;
  const atTop = input.scrollTop <= 2;
  const atBottom =
    input.scrollTop + input.clientHeight >= input.scrollHeight - 2;
  const eligible =
    input.fitMode === "page" ||
    !scrollable ||
    (delta < 0 ? atTop : atBottom);
  if (!eligible) {
    return { accumulator: 0, direction: 0, preventDefault: false };
  }
  const sameDirection =
    input.accumulator === 0 || Math.sign(input.accumulator) === Math.sign(delta);
  const accumulator = (sameDirection ? input.accumulator : 0) + delta;
  if (Math.abs(accumulator) < PDF_WHEEL_PAGE_THRESHOLD) {
    return { accumulator, direction: 0, preventDefault: true };
  }
  return {
    accumulator: 0,
    direction: accumulator > 0 ? 1 : -1,
    preventDefault: true,
  };
}

export function pdfWheelTargetPage(
  page: number,
  pageCount: number,
  direction: -1 | 0 | 1,
): number | null {
  if (!direction || pageCount < 1) return null;
  const target = page + direction;
  return target >= 1 && target <= pageCount ? target : null;
}

function visualCharacterWidth(character: string): number {
  if (character === "\t") return 2.4;
  if (/\s/u.test(character)) return 0.34;
  const codePoint = character.codePointAt(0) || 0;
  if (
    codePoint >= 0x2e80 ||
    (codePoint >= 0x1f000 && codePoint <= 0x1faff)
  ) {
    return 1;
  }
  if (/[.,:;!'"`|ijlI1()\[\]{}]/u.test(character)) return 0.32;
  if (/[MW@#%&]/u.test(character)) return 0.86;
  return 0.56;
}

function visualLineWidth(line: string): number {
  return Math.max(
    0.5,
    Array.from(line).reduce(
      (width, character) => width + visualCharacterWidth(character),
      0,
    ),
  );
}

function fittedVisualFontSize(
  text: string,
  pixelWidth: number,
  pixelHeight: number,
): number {
  const lines = String(text || " ")
    .replace(/\r\n?/gu, "\n")
    .split("\n");
  const lineWidths = lines.map(visualLineWidth);
  const totalCharacterArea = Math.max(
    1,
    lineWidths.reduce((total, width) => total + width, 0),
  );
  const explicitLineHeightFit =
    pixelHeight / (Math.max(1, lines.length) * VISUAL_TEXT_LINE_HEIGHT);
  const characterAreaFit = Math.sqrt(
    (Math.max(1, pixelWidth) * Math.max(1, pixelHeight)) /
      (totalCharacterArea * VISUAL_TEXT_LINE_HEIGHT),
  );
  let fontSize = Math.min(
    MAX_VISUAL_TEXT_FONT_PX,
    explicitLineHeightFit,
    characterAreaFit,
  );

  // Re-estimate soft wrapping with the candidate font. This second bound is
  // important for paragraph/table observations whose bbox covers many lines.
  for (let iteration = 0; iteration < 3; iteration += 1) {
    const lineCapacity = Math.max(0.5, pixelWidth / Math.max(0.5, fontSize));
    const renderedLineCount = lineWidths.reduce(
      (count, width) => count + Math.max(1, Math.ceil(width / lineCapacity)),
      0,
    );
    fontSize = Math.min(
      fontSize,
      pixelHeight /
        (Math.max(1, renderedLineCount) * VISUAL_TEXT_LINE_HEIGHT),
    );
  }

  return Math.max(0.5, fontSize);
}

export function visualTextRunStyle(
  runBbox: number[],
  pageBounds: number[],
  renderWidth: number,
  renderHeight: number,
  text: string,
): CSSProperties {
  const geometry = overlayStyleFor({ bbox: runBbox, sourceBounds: pageBounds });
  const pageWidth = Math.max(1, Number(pageBounds[2]) - Number(pageBounds[0]));
  const pageHeight = Math.max(1, Number(pageBounds[3]) - Number(pageBounds[1]));
  const runWidth = Math.max(1, Number(runBbox[2]) - Number(runBbox[0]));
  const runHeight = Math.max(1, Number(runBbox[3]) - Number(runBbox[1]));
  const pixelWidth = Math.max(1, (runWidth / pageWidth) * renderWidth);
  const pixelHeight = Math.max(1, (runHeight / pageHeight) * renderHeight);
  return {
    ...geometry,
    boxSizing: "border-box",
    overflow: "hidden",
    lineHeight: VISUAL_TEXT_LINE_HEIGHT,
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
    wordBreak: "break-word",
    fontSize: `${fittedVisualFontSize(text, pixelWidth, pixelHeight)}px`,
  };
}

export function isTrustedPdfViewerMessage(
  event: Pick<MessageEvent, "origin" | "source">,
  expectedParent: MessageEventSource | null = window.parent,
  expectedOrigin: string = window.location.origin,
): boolean {
  return (
    event.origin === expectedOrigin && event.source === expectedParent
  );
}

export function pdfParentOrigin(params: URLSearchParams, fallback: string): string {
  const supplied = params.get("parentOrigin");
  if (params.get("embedded") !== "1" || !supplied) return fallback;
  try {
    const value = new URL(supplied);
    return ["http:", "https:"].includes(value.protocol) && value.origin === supplied ? value.origin : fallback;
  } catch { return fallback; }
}

export function PdfViewerPage() {
  const params = useMemo(
    () => new URLSearchParams(window.location.search),
    [],
  );
  const sourceUrl = params.get("pdf") || "";
  const embedded = params.get("embedded") === "1";
  const parentOrigin = pdfParentOrigin(params, window.location.origin);
  const readingSelectionEnabled = readingPdfSelectionEnabled(params);
  const keyboardOwnedByParent = params.get("keyboard") === "parent";
  const initialFit = params.get("fit") || "page";
  const [pdf, setPdf] = useState<Awaited<ReturnType<typeof getDocument>["promise"]> | null>(
    null,
  );
  const [page, setPage] = useState(
    Math.max(1, finiteNumber(params.get("page"), 1)),
  );
  const [scale, setScale] = useState(initialFit === "width" ? 1.3 : 1);
  const [fitMode, setFitMode] = useState<"page" | "width" | "custom">(
    initialFit === "width" ? "width" : "page",
  );
  const [overlays, setOverlays] = useState<PdfOverlay[]>(() =>
    decodeInlineOverlays(params.get("overlays")),
  );
  const [selectableLayer, setSelectableLayer] =
    useState<PdfSelectableLayerV1 | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [renderSize, setRenderSize] = useState({ width: 0, height: 0 });
  const [textSelection, setTextSelection] = useState<PdfTextSelection | null>(
    null,
  );
  const [selectionCopyState, setSelectionCopyState] =
    useState<PdfSelectionCopyState>("IDLE");
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textLayerRef = useRef<HTMLDivElement>(null);
  const visualTextLayerRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const renderTaskRef = useRef<{ cancel: () => void } | null>(null);
  const textLayerTaskRef = useRef<TextLayer | null>(null);
  const renderGeneration = useRef(0);
  const selectionGeneration = useRef(0);
  const wheelAccumulatorRef = useRef(0);
  const wheelLastEventAtRef = useRef(0);
  const wheelCooldownUntilRef = useRef(0);
  const focusRequestRef = useRef(0);
  const [focusedOverlayIndex, setFocusedOverlayIndex] = useState(-1);
  const [pendingFocus, setPendingFocus] = useState<{ centerX: number; centerY: number } | null>(null);
  const overlayIdentity = overlays.map(overlay => overlay.id).join("|");
  useEffect(() => { focusRequestRef.current++; setFocusedOverlayIndex(-1); setPendingFocus(null); }, [page, overlayIdentity]);
  useEffect(() => {
    if (!pendingFocus || !renderSize.width || !stageRef.current || !canvasRef.current) return;
    const frame = requestAnimationFrame(() => {
      const stage = stageRef.current, wrap = canvasRef.current?.parentElement;
      if (!stage || !wrap) return;
      const outer = stage.getBoundingClientRect(), inner = wrap.getBoundingClientRect();
      stage.scrollTo({ left: Math.max(0, inner.left - outer.left + stage.scrollLeft + pendingFocus.centerX * renderSize.width - stage.clientWidth / 2),
        top: Math.max(0, inner.top - outer.top + stage.scrollTop + pendingFocus.centerY * renderSize.height - stage.clientHeight / 2) });
    });
    return () => cancelAnimationFrame(frame);
  }, [pendingFocus, renderSize]);
  const focusOverlay = async () => {
    if (!pdf || !stageRef.current || !overlays.length) return;
    const index = (focusedOverlayIndex + 1) % overlays.length;
    const ticket = ++focusRequestRef.current;
    const natural = (await pdf.getPage(page)).getViewport({ scale: 1 });
    if (ticket !== focusRequestRef.current || !stageRef.current) return;
    const target = overlayFocusView(overlays[index], natural.width, natural.height, stageRef.current.clientWidth, stageRef.current.clientHeight);
    if (!target) return;
    setFocusedOverlayIndex(index); setScale(target.scale); setFitMode("custom"); setPendingFocus(target);
  };

  useEffect(() => {
    document.title = "ReqPilotX · PDF";
    if (!sourceUrl) {
      setError(new Error("缺少 PDF 来源地址。"));
      setLoading(false);
      return;
    }
    let cancelled = false;
    const task = getDocument({
      url: sourceUrl,
      withCredentials: true,
      cMapPacked: true,
      useSystemFonts: true,
    });
    void task.promise
      .then((document) => {
        if (cancelled) return;
        setPdf(document);
        setPage((value) => Math.min(Math.max(1, value), document.numPages));
      })
      .catch((cause) => {
        if (!cancelled) {
          setError(cause instanceof Error ? cause : new Error(String(cause)));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      void task.destroy();
    };
  }, [sourceUrl]);

  const renderPage = useCallback(async () => {
    if (
      !pdf ||
      !canvasRef.current ||
      !textLayerRef.current ||
      !stageRef.current
    ) {
      return;
    }
    const generation = ++renderGeneration.current;
    renderTaskRef.current?.cancel();
    textLayerTaskRef.current?.cancel();
    textLayerTaskRef.current = null;
    setTextSelection(null);
    setSelectionCopyState("IDLE");
    const pageProxy = await pdf.getPage(page);
    if (generation !== renderGeneration.current) return;
    const natural = pageProxy.getViewport({ scale: 1 });
    const availableWidth = Math.max(320, stageRef.current.clientWidth - 32);
    const availableHeight = Math.max(320, stageRef.current.clientHeight - 32);
    const fittedScale =
      fitMode === "width"
        ? availableWidth / natural.width
        : fitMode === "page"
          ? Math.min(
              availableWidth / natural.width,
              availableHeight / natural.height,
            )
          : scale;
    const viewport = pageProxy.getViewport({
      scale: Math.max(0.25, Math.min(4, fittedScale)),
    });
    const canvas = canvasRef.current;
    const context = canvas.getContext("2d", { alpha: false });
    if (!context) throw new Error("浏览器无法创建 PDF 画布。");
    const output = pdfCanvasOutput(
      viewport.width,
      viewport.height,
      window.devicePixelRatio || 1,
    );
    canvas.width = output.width;
    canvas.height = output.height;
    canvas.style.width = `${output.cssWidth}px`;
    canvas.style.height = `${output.cssHeight}px`;
    canvas.parentElement?.style.setProperty(
      "--scale-factor",
      String(viewport.scale),
    );
    setRenderSize({ width: viewport.width, height: viewport.height });
    const task = pageProxy.render({
      canvasContext: context,
      viewport,
      transform: output.transform,
    });
    renderTaskRef.current = task;
    try {
      await task.promise;
    } catch (cause) {
      if ((cause as { name?: string }).name !== "RenderingCancelledException") {
        throw cause;
      }
    } finally {
      if (renderTaskRef.current === task) renderTaskRef.current = null;
    }
    if (generation !== renderGeneration.current) return;
    const textLayerContainer = textLayerRef.current;
    textLayerContainer.replaceChildren();
    const textLayer = new TextLayer({
      textContentSource: await pageProxy.getTextContent(),
      container: textLayerContainer,
      viewport,
    });
    textLayerTaskRef.current = textLayer;
    try {
      await textLayer.render();
    } catch (cause) {
      if ((cause as { name?: string }).name !== "AbortException") throw cause;
    } finally {
      if (textLayerTaskRef.current === textLayer) {
        textLayerTaskRef.current = null;
      }
    }
  }, [fitMode, page, pdf, scale]);

  useEffect(() => {
    if (!pdf) return;
    let active = true;
    setError(null);
    void renderPage().catch((cause) => {
      if (active) setError(cause instanceof Error ? cause : new Error(String(cause)));
    });
    return () => {
      active = false;
      renderGeneration.current += 1;
      renderTaskRef.current?.cancel();
      textLayerTaskRef.current?.cancel();
    };
  }, [pdf, renderPage]);

  useEffect(() => {
    if (!stageRef.current) return;
    const observer = new ResizeObserver(() => {
      if (fitMode !== "custom") {
        void renderPage().catch(() => undefined);
      }
    });
    observer.observe(stageRef.current);
    return () => observer.disconnect();
  }, [fitMode, renderPage]);

  const navigateFromViewer = useCallback(
    (requestedPage: number) => {
      const nextPage = Math.min(
        pdf?.numPages || Number.MAX_SAFE_INTEGER,
        Math.max(1, requestedPage),
      );
      if (nextPage === page) return;
      setOverlays([]);
      setTextSelection(null);
      setPage(nextPage);
      if (embedded) {
        window.parent.postMessage(
          { type: "pdf-page-changed", page: nextPage },
          parentOrigin,
        );
      }
    },
    [embedded, page, pdf?.numPages, parentOrigin],
  );

  const handleWheelNavigation = useCallback(
    (event: ReactWheelEvent<HTMLElement>) => {
      const stage = stageRef.current;
      if (!stage) return;
      const now = performance.now();
      if (now - wheelLastEventAtRef.current > 220) {
        wheelAccumulatorRef.current = 0;
      }
      wheelLastEventAtRef.current = now;
      const result = pdfWheelNavigation({
        deltaY: event.deltaY,
        deltaMode: event.deltaMode,
        accumulator: wheelAccumulatorRef.current,
        fitMode,
        scrollTop: stage.scrollTop,
        scrollHeight: stage.scrollHeight,
        clientHeight: stage.clientHeight,
        ctrlKey: event.ctrlKey,
        metaKey: event.metaKey,
      });
      wheelAccumulatorRef.current = result.accumulator;
      if (result.preventDefault) event.preventDefault();
      if (!result.direction) return;
      if (now < wheelCooldownUntilRef.current) {
        wheelCooldownUntilRef.current = now + 180;
        return;
      }
      const nextPage = pdfWheelTargetPage(
        page,
        pdf?.numPages || 0,
        result.direction,
      );
      if (nextPage === null) return;
      wheelCooldownUntilRef.current = now + 420;
      navigateFromViewer(nextPage);
      requestAnimationFrame(() => {
        if (!stageRef.current) return;
        stageRef.current.scrollTop = result.direction > 0
          ? 0
          : stageRef.current.scrollHeight;
      });
    },
    [fitMode, navigateFromViewer, page, pdf?.numPages],
  );

  useEffect(() => {
    wheelAccumulatorRef.current = 0;
  }, [fitMode, page]);

  useEffect(() => {
    const listener = (event: MessageEvent) => {
      if (!isTrustedPdfViewerMessage(event, window.parent, parentOrigin)) return;
      const message = asRecord(event.data);
      if (message.type === "REQPILOTX_PDF_OVERLAYS") {
        const targetPage = Math.max(1, finiteNumber(message.page, page));
        setOverlays(
          targetPage === page ? normalizePdfOverlays(message.overlays) : [],
        );
      }
      if (message.type === "REQPILOTX_PDF_SELECTABLE_LAYER") {
        const nextLayer = normalizePdfSelectableLayer(
          message.selectableLayer ?? message.selectable_layer,
        );
        setSelectableLayer(nextLayer?.page_number === page ? nextLayer : null);
      }
      if (message.type === "REQPILOTX_PDF_NAVIGATE") {
        const nextPage = Math.max(1, finiteNumber(message.page, page));
        if (Object.prototype.hasOwnProperty.call(message, "overlays")) {
          setOverlays(normalizePdfOverlays(message.overlays));
        } else if (nextPage !== page) {
          setOverlays([]);
        }
        if (
          Object.prototype.hasOwnProperty.call(message, "selectableLayer") ||
          Object.prototype.hasOwnProperty.call(message, "selectable_layer")
        ) {
          const nextLayer = normalizePdfSelectableLayer(
            message.selectableLayer ?? message.selectable_layer,
          );
          setSelectableLayer(
            nextLayer?.page_number === nextPage ? nextLayer : null,
          );
        } else if (nextPage !== page) {
          setSelectableLayer(null);
        }
        setPage(nextPage);
      }
    };
    window.addEventListener("message", listener);
    return () => window.removeEventListener("message", listener);
  }, [page, parentOrigin]);

  useEffect(() => {
    window.parent.postMessage(
      { type: "pdf-viewer-ready" },
      parentOrigin,
    );
  }, [parentOrigin]);

  useEffect(() => {
    window.parent.postMessage(
      {
        type: "pdf-viewer-state",
        page,
        pageCount: pdf?.numPages || 0,
        scale,
        fit: fitMode,
      },
      parentOrigin,
    );
  }, [fitMode, page, pdf?.numPages, scale, parentOrigin]);

  useEffect(() => {
    if (keyboardOwnedByParent) return;
    const listener = (event: KeyboardEvent) => {
      if (event.key === "ArrowLeft" || event.key === "PageUp") {
        navigateFromViewer(page - 1);
      }
      if (event.key === "ArrowRight" || event.key === "PageDown") {
        navigateFromViewer(page + 1);
      }
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [keyboardOwnedByParent, navigateFromViewer, page]);

  const zoom = (delta: number) => {
    setFitMode("custom");
    setScale((value) => Math.max(0.25, Math.min(4, value + delta)));
  };

  const captureTextSelection = useCallback(() => {
    const selection = window.getSelection();
    const textLayer = textLayerRef.current;
    const visualTextLayer = visualTextLayerRef.current;
    const canvasWrap = canvasRef.current?.parentElement;
    if (
      !selection ||
      !textLayer ||
      !visualTextLayer ||
      !canvasWrap ||
      selection.rangeCount !== 1 ||
      selection.isCollapsed
    ) {
      setTextSelection(null);
      return;
    }
    const range = selection.getRangeAt(0);
    const nativeSelection =
      textLayer.contains(range.startContainer) &&
      textLayer.contains(range.endContainer);
    const visualSelection =
      visualTextLayer.contains(range.startContainer) &&
      visualTextLayer.contains(range.endContainer);
    if (!nativeSelection && !visualSelection) {
      setTextSelection(null);
      return;
    }
    const text = selection.toString().trim();
    if (!text) {
      setTextSelection(null);
      return;
    }
    const selectionRect = range.getBoundingClientRect();
    const wrapRect = canvasWrap.getBoundingClientRect();
    const below = selectionRect.top - wrapRect.top < 58;
    const id = `pdf-selection-${page}-${++selectionGeneration.current}`;
    const visualSourceRefs =
      visualSelection && selectableLayer
        ? visualSourceRefsForRange(range, visualTextLayer, selectableLayer)
        : [];
    if (visualSelection && !visualSourceRefs.length) {
      setTextSelection(null);
      return;
    }
    const nextSelection = {
      id,
      text,
      left: Math.max(
        118,
        Math.min(
          renderSize.width - 118,
          selectionRect.left - wrapRect.left + selectionRect.width / 2,
        ),
      ),
      top: below
        ? selectionRect.bottom - wrapRect.top + 10
        : selectionRect.top - wrapRect.top - 10,
      below,
      sourceRefs: visualSourceRefs,
    };
    setTextSelection(nextSelection);
    setSelectionCopyState("IDLE");
    if (readingSelectionEnabled) {
      window.parent.postMessage(
        {
          type: "pdf-text-selected",
          selectionId: id,
          page,
          text,
          ...(nextSelection.sourceRefs.length
            ? {
                sourceRefs: nextSelection.sourceRefs,
                source_refs: nextSelection.sourceRefs,
                provenanceChannel: "REMOTE_VISUAL",
                sourceSha256: selectableLayer?.source_sha256,
                pageImageSha256: selectableLayer?.page_image_sha256,
                visualSnapshotId: selectableLayer?.visual_snapshot_id,
              }
            : {}),
        },
        parentOrigin,
      );
    }
  }, [page, readingSelectionEnabled, renderSize.width, selectableLayer, parentOrigin]);

  const postSelectionAction = useCallback(
    (action: PdfSelectionAction) => {
      if (!textSelection || !readingSelectionEnabled) return;
      window.parent.postMessage(
        pdfSelectionActionPayload(action, textSelection, page, selectableLayer),
        parentOrigin,
      );
    },
    [page, readingSelectionEnabled, selectableLayer, textSelection, parentOrigin],
  );

  const activeSelectableLayer =
    selectableLayer?.page_number === page ? selectableLayer : null;
  const selectableMode = activeSelectableLayer?.readable_mode || "NATIVE";
  const visibleTextLayers = selectableTextLayersForMode(
    selectableMode,
    Boolean(activeSelectableLayer),
  );
  const showNativeText = visibleTextLayers.native;
  const showVisualText = visibleTextLayers.visual;

  const copySelection = useCallback(async () => {
    if (!textSelection) return;
    setSelectionCopyState(
      (await copyPdfSelectionText(textSelection.text)) ? "COPIED" : "FAILED",
    );
  }, [textSelection]);

  return (
    <div className={cx("pdf-viewer-page", embedded && "is-embedded")}>
      {!embedded && (
        <header className="pdf-viewer-header">
          <Brand compact />
          <div>
            <p className="section-label">可信来源</p>
            <strong>PDF 查看器</strong>
          </div>
        </header>
      )}
      <nav className="pdf-toolbar" aria-label="PDF 控制">
        <button
          className="icon-button"
          type="button"
          aria-label="上一页"
          disabled={page <= 1}
          onClick={() => navigateFromViewer(page - 1)}
        >
          <Icon name="chevron" className="is-reversed" />
        </button>
        <label>
          <span className="sr-only">页码</span>
          <input
            type="number"
            min={1}
            max={pdf?.numPages}
            value={page}
            onChange={(event) =>
              navigateFromViewer(finiteNumber(event.target.value, 1))
            }
          />
        </label>
        <span>/ {pdf?.numPages || "—"}</span>
        <button
          className="icon-button"
          type="button"
          aria-label="下一页"
          disabled={Boolean(pdf && page >= pdf.numPages)}
          onClick={() => navigateFromViewer(page + 1)}
        >
          <Icon name="chevron" />
        </button>
        <span className="toolbar-divider" />
        <button
          className="icon-button text-button"
          type="button"
          aria-label="缩小"
          onClick={() => zoom(-0.15)}
        >
          −
        </button>
        <button
          className="pdf-fit-button"
          type="button"
          title={fitMode === "page" ? "按宽度放大，便于阅读文字" : "显示完整页面，查看上下文"}
          onClick={() =>
            setFitMode((value) => (value === "page" ? "width" : "page"))
          }
        >
          {fitMode === "page"
            ? "按宽度放大"
            : fitMode === "width"
              ? "显示整页"
              : "显示整页"}
        </button>
        <button
          className="icon-button text-button"
          type="button"
          aria-label="放大"
          onClick={() => zoom(0.15)}
        >
          +
        </button>
        {overlays.length ? <button type="button" className="pdf-fit-button" aria-label="聚焦原文框标" title="放大框标；再次点击切换下一框"
          onClick={() => void focusOverlay()}>{focusedOverlayIndex < 0 ? "聚焦框标" : `框 ${focusedOverlayIndex + 1}/${overlays.length} · 切换`}</button>
          : <span className="pdf-overlay-count">原文</span>}
      </nav>
      {loading && <LoadingBlock label="正在载入 PDF" />}
      {error && <ErrorBlock error={error} onRetry={() => void renderPage()} />}
      <main
        ref={stageRef}
        className="pdf-stage"
        aria-label="PDF 页面；适合页面时可使用鼠标滚轮翻页"
        onWheel={handleWheelNavigation}
      >
        <div
          className="pdf-canvas-wrap"
          style={{ width: renderSize.width, height: renderSize.height }}
        >
          <canvas ref={canvasRef} />
          <div
            ref={textLayerRef}
            className={cx(
              "pdf-text-layer",
              "is-native",
              !showNativeText && "is-suppressed",
            )}
            aria-label={`第 ${page} 页可选择文字`}
            onPointerUp={captureTextSelection}
            onKeyUp={captureTextSelection}
          />
          <div
            ref={visualTextLayerRef}
            className={cx(
              "pdf-text-layer",
              "is-visual",
              !showVisualText && "is-suppressed",
            )}
            aria-label={`第 ${page} 页视觉识别文字`}
            onPointerUp={captureTextSelection}
            onKeyUp={captureTextSelection}
          >
            {showVisualText
              ? activeSelectableLayer?.runs.map((run) => {
                  const style = visualTextRunStyle(
                    run.bbox,
                    activeSelectableLayer.page_bbox,
                    renderSize.width,
                    renderSize.height,
                    run.text,
                  );
                  return (
                    <span
                      key={run.run_id}
                      data-pdf-visual-run={run.run_id}
                      style={style}
                    >
                      {run.text}
                    </span>
                  );
                })
              : null}
          </div>
          <div className="pdf-overlay-layer" aria-hidden="true">
            {overlays.map((overlay) => (
              <span
                key={overlay.id}
                className={cx(
                  "pdf-overlay",
                  `is-${overlay.type.toLowerCase()}`,
                  overlay.channel &&
                    `is-channel-${overlay.channel.toLowerCase().replace("_", "-")}`,
                  `has-label-${overlayLabelPlacement(overlay)}`,
                  overlay.selected && "is-selected",
                  overlay.focus && "is-focus",
                  overlay.precision.includes("APPROXIMATE") &&
                    "is-approximate",
                )}
                style={overlayStyleFor(overlay)}
              >
                {overlay.label && <em>{overlay.label}</em>}
              </span>
            ))}
          </div>
          {readingSelectionEnabled && textSelection ? (
            <div
              className={cx(
                "pdf-selection-menu",
                textSelection.below && "is-below",
              )}
              role="toolbar"
              aria-label="选中文字操作"
              style={{ left: textSelection.left, top: textSelection.top }}
              onPointerDown={(event) => event.preventDefault()}
            >
              <button type="button" onClick={() => postSelectionAction("ASK")}>
                问 AI
              </button>
              <button type="button" onClick={() => void copySelection()}>
                {selectionCopyState === "COPIED" ? "已复制" : "复制"}
              </button>
              <button
                type="button"
                onClick={() => postSelectionAction("EXPLAIN")}
              >
                解释
              </button>
              <button
                type="button"
                onClick={() => postSelectionAction("TRANSLATE")}
              >
                翻译
              </button>
              <button
                type="button"
                onClick={() => postSelectionAction("SUMMARIZE")}
              >
                总结
              </button>
              <button
                className="pdf-selection-menu-close"
                type="button"
                aria-label="关闭选中文字操作"
                onClick={() => {
                  window.getSelection()?.removeAllRanges();
                  setTextSelection(null);
                }}
              >
                ×
              </button>
              {selectionCopyState === "FAILED" ? (
                <span className="pdf-selection-copy-error" role="status">
                  复制失败，请使用 ⌘C / Ctrl+C
                </span>
              ) : null}
            </div>
          ) : null}
        </div>
      </main>
    </div>
  );
}
