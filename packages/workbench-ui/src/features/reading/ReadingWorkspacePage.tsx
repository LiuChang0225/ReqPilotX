import {
  type KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  BookOpenText,
  Clock3,
  FileQuestion,
  ListChecks,
  MessageSquarePlus,
  PanelRightOpen,
  SendHorizontal,
  Sparkles,
  TriangleAlert,
  Trash2,
  X,
} from "lucide-react";
import { ApiError, asRecord } from "../../shared/api/client";
import { cx } from "../../shared/lib/format";
import { AppLink, useNavigate, useBrowserLocation } from "../../shared/navigation";
import { useApplicationHost } from "../../host/ApplicationHost";
import { Brand } from "../../shared/ui/Brand";
import {
  EmptyState,
  ErrorBlock,
  LoadingBlock,
  StatusBadge,
} from "../../shared/ui/Feedback";
import { Icon } from "../../shared/ui/Icon";
import { Dialog } from "../../shared/ui/Dialog";
import { CaptureStatus } from "../document-capture/CaptureStatus";
import { captureApi } from "../document-capture/captureApi";
import {
  normalizeCaptureQualityManifest,
  normalizeVisualPageCapture,
  type CaptureQualityManifestV1,
  type VisualPageCaptureV1,
} from "../document-capture/contracts";
import { pdfSelectableLayerFromCapturePage } from "../document-capture/pdfLayer";
import {
  parseWorkspaceRoute,
  serializeWorkspaceRoute,
} from "../workspace/route";
import type {
  ReadingChatMessageV1,
  ReadingCitationV1,
  ReadingContextV1,
  ReadingConversationSummaryV1,
  ReadingDocument,
  ReadingNoteV1,
  ReadingProject,
  ReadingSelectableNodeV1,
  ReadingSourcePageNodeV1,
  ReadingSourceRef,
  ReadingStructureMapNodeV1,
  ReadingTaskType,
} from "./contracts";
import { readingApi } from "./readingApi";
import { useReadingHandoff, type ReadingHandoff } from "../workspace/readingHandoff";
import { ReadingHandoffAction } from "./ReadingHandoffAction";
import "./reading-availability.css";

type AuxiliaryView = "overview" | "map" | "notes";
type MobilePane = "source" | "chat";
type ReaderTextSize = "compact" | "standard" | "large";

interface ExactSelection {
  sourceNodeId: string;
  startOffset: number;
  endOffset: number;
  text: string;
}

interface OptimisticQuestion {
  id: string;
  content: string;
  sourceRefs: ReadingSourceRef[];
  taskType: ReadingTaskType;
}

interface ChatFailure {
  message: string;
  question: string;
  sourceRefs: ReadingSourceRef[];
  providerNotConfigured: boolean;
  stopped?: boolean;
  taskType: ReadingTaskType;
}

type PdfSelectionAction = "ASK" | "EXPLAIN" | "TRANSLATE" | "SUMMARIZE";

interface PdfSelectionRequest {
  selectionId: string;
  page: number;
  text: string;
  sourceRefs: ReadingSourceRef[];
  action?: PdfSelectionAction;
}

interface VisualSelection {
  text: string;
  sourceRefs: ReadingSourceRef[];
}

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const MAX_QUESTION_LENGTH = 8000;
const MAX_SELECTION_REFS = 16;
const MAX_SELECTION_LENGTH = 12000;
const quickPrompts = [
  {
    label: "快速读懂",
    description: "目的、范围、结构与阅读顺序",
    prompt: "快速读懂这份文档",
    taskType: "QUICK_READ" as const,
    icon: BookOpenText,
  },
  {
    label: "关键技术条件",
    description: "把条款转成可执行关注点",
    prompt: "提炼关键技术条件",
    taskType: "TECHNICAL_CONDITIONS" as const,
    icon: ListChecks,
  },
  {
    label: "章节导读",
    description: "结构、关系与建议阅读顺序",
    prompt: "按章节导读",
    taskType: "SECTION_GUIDE" as const,
    icon: FileQuestion,
  },
  {
    label: "找出易遗漏条件",
    description: "例外、前提、引用与待澄清项",
    prompt: "找出容易遗漏的条件",
    taskType: "MISSED_CONDITIONS" as const,
    icon: TriangleAlert,
  },
];

const defaultFollowUps = [
  "这段回答最重要的原文依据是什么？",
  "还有哪些容易忽略的条件？",
  "请换一种更简单的方式解释。",
];

function fileIsPdf(document: ReadingDocument | null): boolean {
  const format = String(
    document?.detected_format || document?.file_type || document?.file_name || "",
  ).toLowerCase();
  return format === "pdf" || format === ".pdf" || format.endsWith(".pdf");
}

function statusTone(status: unknown) {
  const value = String(status || "").toUpperCase();
  if (value === "READY") return "success" as const;
  if (["PARTIAL", "PENDING", "RUNNING", "NOT_READY", "AI_UNAVAILABLE"].includes(value)) {
    return "warning" as const;
  }
  if (["FAILED", "STALE"].includes(value)) return "danger" as const;
  return "neutral" as const;
}

function statusLabel(status: unknown): string {
  const value = String(status || "").toUpperCase();
  return (
    {
      READY: "可对话",
      CHECKING_AI: "正在检查连接",
      AI_UNAVAILABLE: "Agent 未就绪",
      PARTIAL: "部分可读",
      PENDING: "准备中",
      RUNNING: "准备中",
      NOT_READY: "尚未准备",
      STALE: "来源已变化",
      FAILED: "准备失败",
    }[value] || "状态未知"
  );
}

function sourcePage(node: ReadingSelectableNodeV1 | undefined): number {
  const page = Number(node?.page_number || 0);
  return Number.isInteger(page) && page > 0 ? page : 1;
}

function safeSourceLength(node: ReadingSelectableNodeV1): number {
  const length = Number(node.text_length || 0);
  return Number.isInteger(length) && length > 0 ? length : 0;
}

function unicodeLength(value: string): number {
  return Array.from(value).length;
}

function compactComparableText(value: string): string {
  return Array.from(value)
    .filter((character) => !/\s/u.test(character) && character !== "\u00ad")
    .join("");
}

function compactSourceText(value: string): {
  text: string;
  sourceOffsets: number[];
} {
  const text: string[] = [];
  const sourceOffsets: number[] = [];
  Array.from(value).forEach((character, offset) => {
    if (/\s/u.test(character) || character === "\u00ad") return;
    text.push(character);
    sourceOffsets.push(offset);
  });
  return { text: text.join(""), sourceOffsets };
}

export function resolvePdfSourceSelections(
  selectedText: string,
  nodes: ReadingSourcePageNodeV1[],
  maxSourceRefs = 16,
): ExactSelection[] | null {
  const needle = compactComparableText(selectedText.trim());
  if (!needle) return null;
  const orderedNodes = [...nodes].sort(
    (left, right) => left.source_order - right.source_order,
  );
  if (
    orderedNodes.some(
      (node, index) =>
        index > 0 && node.source_order <= orderedNodes[index - 1]!.source_order,
    )
  ) {
    return null;
  }
  const haystack: string[] = [];
  const coordinates: Array<{ nodeIndex: number; sourceOffset: number }> = [];
  orderedNodes.forEach((node, nodeIndex) => {
    const compact = compactSourceText(node.text);
    Array.from(compact.text).forEach((character, compactOffset) => {
      const sourceOffset = compact.sourceOffsets[compactOffset];
      if (sourceOffset === undefined) return;
      haystack.push(character);
      coordinates.push({ nodeIndex, sourceOffset });
    });
  });
  const joined = haystack.join("");
  const firstMatch = joined.indexOf(needle);
  if (firstMatch < 0 || joined.indexOf(needle, firstMatch + 1) >= 0) return null;
  const start = coordinates[firstMatch];
  const end = coordinates[firstMatch + needle.length - 1];
  if (!start || !end || start.nodeIndex > end.nodeIndex) return null;
  const selections: ExactSelection[] = [];
  for (let nodeIndex = start.nodeIndex; nodeIndex <= end.nodeIndex; nodeIndex += 1) {
    const node = orderedNodes[nodeIndex];
    if (!node) return null;
    const startOffset = nodeIndex === start.nodeIndex ? start.sourceOffset : 0;
    const endOffset =
      nodeIndex === end.nodeIndex ? end.sourceOffset + 1 : safeSourceLength(node);
    const text = Array.from(node.text).slice(startOffset, endOffset).join("");
    if (text.trim()) {
      selections.push({
        sourceNodeId: node.source_node_id,
        startOffset,
        endOffset,
        text,
      });
    }
  }
  if (!selections.length || selections.length > maxSourceRefs) return null;
  const reconstructed = compactComparableText(
    selections.map((selection) => selection.text).join(""),
  );
  return reconstructed === needle ? selections : null;
}

function apiErrorReasonCode(error: ApiError): string {
  const payload = asRecord(error.payload);
  const detail = asRecord(payload.detail);
  return String(
    detail.failure_code ||
      payload.failure_code ||
      detail.reason_code ||
      payload.reason_code ||
      payload.code ||
      "",
  );
}

function shouldPrepareReadingContext(cause: unknown): boolean {
  if (!(cause instanceof ApiError)) return false;
  const reasonCode = apiErrorReasonCode(cause);
  return (
    (cause.status === 404 && reasonCode === "reading_context_not_prepared") ||
    (cause.status === 409 && reasonCode === "reading_context_bundle_changed")
  );
}

function isProviderNotConfigured(cause: unknown): boolean {
  if (!(cause instanceof ApiError)) return false;
  const code = apiErrorReasonCode(cause).toLowerCase();
  return (
    code.includes("provider_not_configured") ||
    code.includes("reading_ai_not_configured") ||
    code.includes("reading_codex_auth_required") ||
    code.includes("reading_codex_not_configured") ||
    code.includes("reading_codex_model_unavailable") ||
    code.includes("reading_codex_unavailable") ||
    code.includes("reading_codex_usage_limited") ||
    code === "reading_provider_unavailable"
  );
}

function visualCaptureFailureMessage(cause: unknown, pageNumber: number): string {
  const reasonCode =
    cause instanceof ApiError ? apiErrorReasonCode(cause).toLowerCase() : "";
  if (reasonCode === "visual_image_semantics_not_supported") {
    return `第 ${pageNumber} 页当前仅支持文字层；该页为无文字图像/图纸，暂不能完整阅读。`;
  }
  if (reasonCode.includes("provider_not_configured")) {
    return "视觉模型尚未配置，请先在设置中完成阅读视觉模型配置。";
  }
  if (reasonCode.includes("timeout") || reasonCode.includes("timed_out")) {
    return `第 ${pageNumber} 页视觉识别超时，可稍后重试。`;
  }
  return cause instanceof Error
    ? cause.message
    : `第 ${pageNumber} 页识别失败，可稍后重试。`;
}

function readingConnectionMessage(reasonCode?: string | null): string {
  const code = String(reasonCode || "").toLowerCase();
  if (code.includes("auth_required")) return "Codex 登录尚未完成或已过期，请在 Agent 设置中检查登录状态。";
  if (code.includes("model_unavailable")) return "当前模型不可用，请在 Agent 设置中选择可用模型。";
  if (code.includes("usage_limited")) return "Codex 当前用量受限，请稍后刷新连接。";
  if (code.includes("not_configured")) return "阅读 Agent 尚未配置，请先完成 Agent 设置。";
  if (code.includes("codex_unavailable")) return "无法连接 Codex Agent。服务恢复后可刷新连接，或前往 Agent 设置检查配置。";
  return "暂时无法连接阅读 Agent。请刷新连接；若仍不可用，请检查 Agent 配置。";
}

function readingChatFailureMessage(cause: unknown): string {
  if (cause instanceof ApiError && isProviderNotConfigured(cause)) return readingConnectionMessage(apiErrorReasonCode(cause));
  if (cause instanceof ApiError) {
    const reasonCode = apiErrorReasonCode(cause).toUpperCase();
    if (
      reasonCode === "FULL_DOCUMENT_TOO_LARGE" ||
      reasonCode === "READING_FULL_DOCUMENT_TOO_LARGE"
    ) {
      return "当前文档超出模型安全全文窗口，系统未生成抽样答案；请换用更大上下文模型或拆分文档。";
    }
  }
  return cause instanceof Error ? cause.message : "AI 暂时无法回答。";
}

function isAbortError(cause: unknown): boolean {
  return cause instanceof DOMException && cause.name === "AbortError";
}

function contextMatchesDocument(
  context: ReadingContextV1,
  projectId: string,
  document: ReadingDocument,
): boolean {
  if (
    context.project_id !== projectId ||
    context.sor_document_id !== document.id ||
    !context.effective_bundle_id
  ) {
    return false;
  }
  const expectedHash = String(document.source_sha256 || "").toLowerCase();
  return !expectedHash || context.source_sha256 === expectedHash;
}

function captureManifestMatchesSource(
  manifest: CaptureQualityManifestV1,
  context: ReadingContextV1,
  projectId: string,
  documentId: string,
): boolean {
  return (
    manifest.project_id === projectId &&
    manifest.sor_document_id === documentId &&
    manifest.source_sha256 === context.source_sha256 &&
    manifest.base_source_ir_id === context.source_ir_id
  );
}

function sourcePageNodesMatchContext(
  nodes: ReadingSourcePageNodeV1[],
  context: ReadingContextV1,
  pageNumber: number,
): boolean {
  const identities = new Map(
    context.selectable_nodes.map((node) => [node.source_node_id, node]),
  );
  const seen = new Set<string>();
  return nodes.every((node) => {
    const identity = identities.get(node.source_node_id);
    if (!identity || seen.has(node.source_node_id)) return false;
    seen.add(node.source_node_id);
    return (
      node.page_number === pageNumber &&
      identity.page_number === pageNumber &&
      node.source_order === identity.source_order &&
      node.kind === identity.kind &&
      safeSourceLength(node) === safeSourceLength(identity) &&
      unicodeLength(node.text) === safeSourceLength(identity)
    );
  });
}

function visualSourceRefIsStructurallyValid(sourceRef: ReadingSourceRef): boolean {
  return Boolean(
    sourceRef.visual_snapshot_id &&
      sourceRef.observation_id &&
      Number.isInteger(sourceRef.start_offset) &&
      Number.isInteger(sourceRef.end_offset) &&
      sourceRef.start_offset >= 0 &&
      sourceRef.end_offset > sourceRef.start_offset &&
      (!sourceRef.page_number ||
        (Number.isInteger(sourceRef.page_number) && sourceRef.page_number > 0)),
  );
}

function sourceRefMatchesContext(
  sourceRef: ReadingSourceRef,
  context: ReadingContextV1,
): boolean {
  if (String(sourceRef.kind || "").toUpperCase() === "VISUAL_TEXT") {
    return visualSourceRefIsStructurallyValid(sourceRef);
  }
  if (!sourceRef.source_node_id) return false;
  const node = context.selectable_nodes.find(
    (item) => item.source_node_id === sourceRef.source_node_id,
  );
  if (!node) return false;
  return (
    Number.isInteger(sourceRef.start_offset) &&
    Number.isInteger(sourceRef.end_offset) &&
    sourceRef.start_offset >= 0 &&
    sourceRef.end_offset > sourceRef.start_offset &&
    sourceRef.end_offset <= safeSourceLength(node)
  );
}

function sameNumericTuple(left: number[] | undefined, right: number[]): boolean {
  return Boolean(
    left?.length === right.length &&
      left.every((value, index) => value === right[index]),
  );
}

function visualSourceRefMatchesCapture(
  sourceRef: ReadingSourceRef,
  capture: VisualPageCaptureV1 | null,
): boolean {
  if (!capture || !visualSourceRefIsStructurallyValid(sourceRef)) {
    return false;
  }
  const run = capture.runs.find(
    (candidate) => candidate.observation_id === sourceRef.observation_id,
  );
  if (!run) return false;
  const runStart = run.source_ref.start_offset;
  const runEnd = run.source_ref.end_offset;
  return Boolean(
    sourceRef.visual_snapshot_id === capture.visual_snapshot_id &&
      sourceRef.page_number === capture.page_number &&
      sourceRef.page_image_sha256 === capture.page_image_sha256 &&
      sourceRef.provider_id === capture.provider_id &&
      sourceRef.provider_version === capture.provider_version &&
      sourceRef.start_offset >= runStart &&
      sourceRef.end_offset <= runEnd &&
      sameNumericTuple(sourceRef.bbox, run.bbox) &&
      sameNumericTuple(sourceRef.page_bbox, capture.page_bbox),
  );
}

function visualCaptureReceiptMatchesManifest(
  capture: VisualPageCaptureV1,
  manifest: CaptureQualityManifestV1,
): boolean {
  if (capture.capture_status !== "READY") return true;
  const page = manifest.pages.find(
    (candidate) => candidate.page_number === capture.page_number,
  );
  return Boolean(
    manifest.source_sha256 === capture.source_sha256 &&
      manifest.base_capture_id === capture.base_capture_id &&
      manifest.active_visual_generation_id === capture.generation_id &&
      page?.capture_status === "READY" &&
      page.readable_mode === "VISUAL" &&
      page.visual_snapshot_id === capture.visual_snapshot_id &&
      page.page_image_sha256 === capture.page_image_sha256,
  );
}

function activeNoteMatchesContext(
  note: ReadingNoteV1,
  context: ReadingContextV1,
): boolean {
  return (
    String(note.status || "").toUpperCase() === "ACTIVE" &&
    note.reading_session_id === context.reading_session_id &&
    note.project_id === context.project_id &&
    note.sor_document_id === context.sor_document_id &&
    note.source_sha256 === context.source_sha256 &&
    sourceRefMatchesContext(note.source_ref, context)
  );
}

function noteBelongsToContext(
  note: ReadingNoteV1,
  context: ReadingContextV1,
): boolean {
  const status = String(note.status || "").toUpperCase();
  return Boolean(
    ["ACTIVE", "STALE"].includes(status) &&
      note.reading_session_id === context.reading_session_id &&
      note.project_id === context.project_id &&
      note.sor_document_id === context.sor_document_id &&
      note.source_sha256 === context.source_sha256 &&
      (status === "STALE" || sourceRefMatchesContext(note.source_ref, context)),
  );
}

function conversationMatchesContext(
  conversation: ReadingConversationSummaryV1,
  context: ReadingContextV1,
): boolean {
  const stale = String(conversation.status || "").toUpperCase() === "STALE";
  return (
    (stale ||
      (conversation.reading_session_id === context.reading_session_id &&
        conversation.effective_bundle_id === context.effective_bundle_id)) &&
    conversation.project_id === context.project_id &&
    conversation.sor_document_id === context.sor_document_id &&
    conversation.source_sha256 === context.source_sha256 &&
    conversation.status !== "DELETED"
  );
}

function messageMatchesConversation(
  message: ReadingChatMessageV1,
  conversationId: string,
  context: ReadingContextV1,
): boolean {
  const stale = String(message.status || "").toUpperCase() === "STALE";
  if (
    message.conversation_id !== conversationId ||
    (!stale &&
      (message.reading_session_id !== context.reading_session_id ||
        message.effective_bundle_id !== context.effective_bundle_id)) ||
    message.project_id !== context.project_id ||
    message.sor_document_id !== context.sor_document_id ||
    message.source_sha256 !== context.source_sha256 ||
    !["USER", "ASSISTANT"].includes(String(message.role))
  ) {
    return false;
  }
  if (stale) return true;
  if (
    message.retrieval?.effective_bundle_id &&
    message.retrieval.effective_bundle_id !== context.effective_bundle_id
  ) {
    return false;
  }
  const sourceRefs = message.source_refs?.length
    ? message.source_refs
    : message.source_ref
      ? [message.source_ref]
      : [];
  return (
    sourceRefs.length <= MAX_SELECTION_REFS &&
    sourceRefs.every((sourceRef) => sourceRefMatchesContext(sourceRef, context)) &&
    (message.citations || []).every((citation) =>
      citationMatchesContext(citation, context),
    )
  );
}

function citationMatchesContext(
  citation: ReadingCitationV1,
  context: ReadingContextV1,
): boolean {
  if (
    ["OCR_TEXT", "VISUAL_TEXT", "VISUAL_REGION"].includes(
      String(citation.locator_kind || "").toUpperCase(),
    )
  ) {
    const page = Number(citation.page_number || 0);
    const visualBbox = citation.bbox || [];
    return Boolean(
      citation.visual_snapshot_id &&
        citation.observation_id &&
        Number.isInteger(page) &&
        page > 0 &&
        visualBbox.length === 4 &&
        visualBbox.every(Number.isFinite) &&
        Number.isInteger(citation.start_offset) &&
        Number.isInteger(citation.end_offset) &&
        citation.start_offset >= 0 &&
        citation.end_offset > citation.start_offset,
    );
  }
  if (citation.locator_kind !== "TABLE_CELL") {
    return sourceRefMatchesContext(citationSourceRef(citation), context);
  }
  const node = context.selectable_nodes.find(
    (item) => item.source_node_id === citation.source_node_id,
  );
  const page = Number(citation.page_number || 0);
  return Boolean(
    node &&
      String(node.kind).toUpperCase() === "TABLE" &&
      String(citation.cell_id || "").trim() &&
      Number.isInteger(page) &&
      page > 0 &&
      sourcePage(node) === page &&
      Number.isInteger(citation.start_offset) &&
      Number.isInteger(citation.end_offset) &&
      citation.start_offset >= 0 &&
      citation.end_offset > citation.start_offset,
  );
}

function citationSourceRef(citation: ReadingCitationV1): ReadingSourceRef {
  if (
    ["OCR_TEXT", "VISUAL_TEXT", "VISUAL_REGION"].includes(
      String(citation.locator_kind || "").toUpperCase(),
    )
  ) {
    return {
      kind: "VISUAL_TEXT",
      visual_snapshot_id: citation.visual_snapshot_id,
      observation_id: citation.observation_id,
      start_offset: citation.start_offset,
      end_offset: citation.end_offset,
      page_number: citation.page_number,
      bbox: citation.bbox,
      page_bbox: citation.page_bbox,
      page_image_sha256: citation.page_image_sha256,
      provider_id: citation.provider_id,
      provider_version: citation.provider_version,
    };
  }
  return {
    source_node_id: citation.source_node_id,
    start_offset: citation.start_offset,
    end_offset: citation.end_offset,
  };
}

function upsertConversationSummary(
  items: ReadingConversationSummaryV1[],
  next: ReadingConversationSummaryV1,
): ReadingConversationSummaryV1[] {
  return [next, ...items.filter((item) => item.conversation_id !== next.conversation_id)].sort(
    (left, right) =>
      String(right.updated_at_utc).localeCompare(String(left.updated_at_utc)),
  );
}

function formatConversationTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function questionTitle(question: string): string {
  const compact = question.replace(/\s+/g, " ").trim();
  return compact.length > 34 ? `${compact.slice(0, 34)}…` : compact;
}

function normalizedTaskType(value: unknown): ReadingTaskType {
  return [
    "QUICK_READ",
    "TECHNICAL_CONDITIONS",
    "SECTION_GUIDE",
    "MISSED_CONDITIONS",
  ].includes(String(value))
    ? (String(value) as ReadingTaskType)
    : "FREE_QUESTION";
}

function initialReaderTextSize(): ReaderTextSize {
  try {
    const stored = window.localStorage.getItem("reqpilotx.reader.text-size");
    if (stored === "compact" || stored === "large") return stored;
  } catch {
    // Reading remains usable when browser storage is unavailable.
  }
  return "standard";
}

export function ReadingWorkspacePage({ embedded = false }: { embedded?: boolean } = {}) {
  const navigate = useNavigate();
  const applicationHost = useApplicationHost();
  const location = useBrowserLocation();
  const route = useMemo(
    () => parseWorkspaceRoute(location.search),
    [location.search],
  );
  const [projects, setProjects] = useState<ReadingProject[]>([]);
  const [documents, setDocuments] = useState<ReadingDocument[]>([]);
  const [context, setContext] = useState<ReadingContextV1 | null>(null);
  const [aiReady, setAiReady] = useState<boolean | null>(null);
  const [aiConnectionMessage, setAiConnectionMessage] = useState("");
  const [aiReadinessRetry, setAiReadinessRetry] = useState(0);
  const [notes, setNotes] = useState<ReadingNoteV1[]>([]);
  const [conversations, setConversations] = useState<
    ReadingConversationSummaryV1[]
  >([]);
  const [activeConversationId, setActiveConversationId] = useState("");
  const [messages, setMessages] = useState<ReadingChatMessageV1[]>([]);
  const [draft, setDraft] = useState("");
  const [optimisticQuestion, setOptimisticQuestion] =
    useState<OptimisticQuestion | null>(null);
  const [chatFailure, setChatFailure] = useState<ChatFailure | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [auxiliaryView, setAuxiliaryView] = useState<AuxiliaryView | null>(null);
  const [mobilePane, setMobilePane] = useState<MobilePane>("chat");
  const [readerTextSize, setReaderTextSize] = useState<ReaderTextSize>(
    initialReaderTextSize,
  );
  const [exactSelections, setExactSelections] = useState<ExactSelection[]>([]);
  const [visualSelection, setVisualSelection] =
    useState<VisualSelection | null>(null);
  const [pdfSelectionRequest, setPdfSelectionRequest] =
    useState<PdfSelectionRequest | null>(null);
  const [selectionNotice, setSelectionNotice] = useState("");
  const [pdfPage, setPdfPage] = useState(1);
  const [sourcePageNodes, setSourcePageNodes] = useState<
    ReadingSourcePageNodeV1[]
  >([]);
  const [sourcePageLoading, setSourcePageLoading] = useState(false);
  const [sourcePageError, setSourcePageError] = useState("");
  const [captureManifest, setCaptureManifest] =
    useState<CaptureQualityManifestV1 | null>(null);
  const [captureManifestLoading, setCaptureManifestLoading] = useState(false);
  const [captureManifestError, setCaptureManifestError] = useState("");
  const [visualPageCapture, setVisualPageCapture] =
    useState<VisualPageCaptureV1 | null>(null);
  const [visualPageLoading, setVisualPageLoading] = useState(false);
  const [visualPageError, setVisualPageError] = useState("");
  const [captureBusyPage, setCaptureBusyPage] = useState<number | null>(null);
  const [captureBulkRunning, setCaptureBulkRunning] = useState(false);
  const [captureBulkStopping, setCaptureBulkStopping] = useState(false);
  const [captureSourceUpdating, setCaptureSourceUpdating] = useState(false);
  const [captureBulkProgress, setCaptureBulkProgress] = useState<{
    completed: number;
    total: number;
    failed: number;
  } | null>(null);
  const [captureSessionNotice, setCaptureSessionNotice] = useState("");
  const [focusedEvidenceOverlay, setFocusedEvidenceOverlay] = useState<
    Record<string, unknown> | null
  >(null);
  const [loadingProjects, setLoadingProjects] = useState(true);
  const [loadingDocuments, setLoadingDocuments] = useState(false);
  const [loadingContext, setLoadingContext] = useState(false);
  const [loadingConversations, setLoadingConversations] = useState(false);
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [sending, setSending] = useState(false);
  const [busyNote, setBusyNote] = useState("");
  const [error, setError] = useState<Error | null>(null);
  const [noteDraft, setNoteDraft] = useState("");
  const [editingNoteId, setEditingNoteId] = useState("");
  const [editingNoteText, setEditingNoteText] = useState("");
  const [reloadGeneration, setReloadGeneration] = useState(0);
  const [catalogRetry, setCatalogRetry] = useState(0);
  const pdfFrameRef = useRef<HTMLIFrameElement>(null);
  const pdfPageRef = useRef(1);
  const currentScopeRef = useRef("");
  const currentPageScopeRef = useRef("");
  const activeConversationRef = useRef("");
  const chatRequestRef = useRef<AbortController | null>(null);
  const conversationLoadRef = useRef<AbortController | null>(null);
  const forcePrepareRef = useRef(false);
  const timelineEndRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const consumedPdfActionsRef = useRef(new Set<string>());
  const captureBulkAbortRef = useRef<AbortController | null>(null);
  const captureBulkStopRef = useRef(false);
  const bundleReprepareRef = useRef("");

  const selectedProject = useMemo(
    () => projects.find((item) => item.id === route.projectId) || null,
    [projects, route.projectId],
  );
  const selectedDocument = useMemo(
    () => documents.find((item) => item.id === route.documentId) || null,
    [documents, route.documentId],
  );
  const readingHandoff = useReadingHandoff(context && selectedProject && selectedDocument && contextMatchesDocument(context, selectedProject.id, selectedDocument) ? context : null);
  const selectableNodes = context?.selectable_nodes || [];
  const exactSelection = visualSelection
    ? {
        sourceNodeId: "",
        startOffset: 0,
        endOffset: unicodeLength(visualSelection.text),
        text: visualSelection.text,
      }
    : exactSelections[0] || null;
  const exactSelectionText = useMemo(
    () =>
      visualSelection?.text ||
      exactSelections.map((selection) => selection.text).join("\n"),
    [exactSelections, visualSelection],
  );
  const selectedSourceRefs = useMemo(
    () =>
      visualSelection?.sourceRefs ||
      exactSelections.map((selection) => ({
        source_node_id: selection.sourceNodeId,
        start_offset: selection.startOffset,
        end_offset: selection.endOffset,
      })),
    [exactSelections, visualSelection],
  );
  const selectedSourceRef = selectedSourceRefs[0];
  const readingIsWritable = Boolean(
    context && ["READY", "PARTIAL"].includes(String(context.status)),
  );
  const activeConversation = conversations.find(
    (item) => item.conversation_id === activeConversationId,
  );
  const activeConversationIsStale =
    String(activeConversation?.status || "").toUpperCase() === "STALE";
  const sourceMutationInProgress =
    captureSourceUpdating || captureBulkRunning || captureBusyPage !== null;
  const readingInputIsWritable =
    readingIsWritable && !activeConversationIsStale && !sourceMutationInProgress;
  const readingChatIsWritable = readingInputIsWritable && aiReady === true;
  const chatStatus = !readingIsWritable ? context?.status
    : aiReady === null ? "CHECKING_AI"
    : !aiReady ? "AI_UNAVAILABLE"
    : activeConversationIsStale ? "STALE"
    : sourceMutationInProgress ? "PENDING" : context?.status;
  const latestSuggestions = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message?.role === "ASSISTANT" && message.suggested_questions?.length) {
        return message.suggested_questions.slice(0, 3);
      }
    }
    return [];
  }, [messages]);
  const currentCapturePage = useMemo(
    () =>
      captureManifest?.pages.find((item) => item.page_number === pdfPage) ||
      null,
    [captureManifest, pdfPage],
  );
  const unreadablePageCount = useMemo(() => {
    if (!captureManifest) return 0;
    return captureManifest.pages.filter(
      (page) => page.readable_mode === "NONE",
    ).length;
  }, [captureManifest]);
  const fullDocumentQuestionGate = sourceMutationInProgress
    ? "阅读来源更新中，识别完成后可继续提问"
    : captureManifestLoading
    ? "正在检查可读性，暂不可全文提问"
    : !captureManifest
      ? "暂时无法确认全文可读性，可先选中已读内容提问"
      : captureManifest.status !== "READY"
        ? `还有 ${unreadablePageCount} 页未完成视觉识别，可先选中已读内容提问`
        : "";
  const canAskFullDocument = !fullDocumentQuestionGate;
  const canAskCurrentScope =
    !sourceMutationInProgress &&
    (selectedSourceRefs.length > 0 || canAskFullDocument);
  const pdfSelectableLayer = useMemo(
    () =>
      currentCapturePage && context
        ? pdfSelectableLayerFromCapturePage(
            currentCapturePage,
            context.source_sha256,
            visualPageCapture?.page_number === pdfPage
              ? visualPageCapture
              : null,
          )
        : null,
    [context, currentCapturePage, pdfPage, visualPageCapture],
  );
  const pdfEvidenceOverlays = useMemo(
    () => (focusedEvidenceOverlay ? [focusedEvidenceOverlay] : []),
    [focusedEvidenceOverlay],
  );

  currentScopeRef.current = `${route.projectId}::${route.documentId}::${String(
    selectedDocument?.source_sha256 || context?.source_sha256 || "",
  )}::${reloadGeneration}`;
  currentPageScopeRef.current = `${currentScopeRef.current}::page:${pdfPage}`;
  pdfPageRef.current = pdfPage;
  activeConversationRef.current = activeConversationId;

  useEffect(() => {
    if (!applicationHost) document.title = "ReqPilotX · AI 阅读";
    return () => {
      currentScopeRef.current = "unmounted";
      currentPageScopeRef.current = "unmounted";
      chatRequestRef.current?.abort();
      conversationLoadRef.current?.abort();
      captureBulkAbortRef.current?.abort();
      captureBulkStopRef.current = true;
    };
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem(
        "reqpilotx.reader.text-size",
        readerTextSize,
      );
    } catch {
      // The chosen size still applies for this page session.
    }
  }, [readerTextSize]);

  useEffect(() => {
    const canonical = serializeWorkspaceRoute({
      ...route,
      task: "reading",
      requirementSetId: "",
    });
    if (`${location.pathname}${location.search}` !== canonical) {
      navigate(canonical, { replace: true });
    }
  }, [location.pathname, location.search, route]);

  useEffect(() => {
    const controller = new AbortController();
    setLoadingProjects(true);
    setError(null);
    void readingApi
      .projects()
      .then((items) => {
        if (controller.signal.aborted) return;
        setProjects(items);
        const nextProject = route.projectId
          ? items.find((item) => item.id === route.projectId) : items[0];
        if (nextProject && nextProject.id !== route.projectId) {
          navigate(
            serializeWorkspaceRoute({
              ...route,
              task: "reading",
              projectId: nextProject.id,
              documentId: "",
              requirementSetId: "",
            }),
            { replace: true },
          );
        }
      })
      .catch((cause) => {
        if (!controller.signal.aborted) {
          setError(cause instanceof Error ? cause : new Error(String(cause)));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingProjects(false);
      });
    return () => controller.abort();
  }, [catalogRetry]);

  useEffect(() => {
    if (!route.projectId) {
      setDocuments([]);
      return;
    }
    const controller = new AbortController();
    const projectId = route.projectId;
    setLoadingDocuments(true);
    setError(null);
    setDocuments([]);
    void readingApi
      .documents(projectId)
      .then((items) => {
        if (controller.signal.aborted || projectId !== route.projectId) return;
        setDocuments(items);
        const nextDocument = route.documentId
          ? items.find((item) => item.id === route.documentId) : items[0];
        if (nextDocument && nextDocument.id !== route.documentId) {
          navigate(
            serializeWorkspaceRoute({
              ...route,
              task: "reading",
              documentId: nextDocument.id,
              requirementSetId: "",
            }),
            { replace: true },
          );
        }
      })
      .catch((cause) => {
        if (!controller.signal.aborted) {
          setError(cause instanceof Error ? cause : new Error(String(cause)));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingDocuments(false);
      });
    return () => controller.abort();
  }, [route.projectId, catalogRetry]);

  useEffect(() => {
    chatRequestRef.current?.abort();
    conversationLoadRef.current?.abort();
    if (!selectedProject || !selectedDocument) {
      setContext(null);
      setNotes([]);
      setConversations([]);
      setMessages([]);
      setCaptureSessionNotice("");
      bundleReprepareRef.current = "";
      setCaptureSourceUpdating(false);
      return;
    }
    const controller = new AbortController();
    const projectId = selectedProject.id;
    const document = selectedDocument;
    const preparePayload = {
      schema_version: "reading-prepare-request/v1" as const,
      ...(SHA256_PATTERN.test(String(document.source_sha256 || ""))
        ? { expected_source_sha256: String(document.source_sha256) }
        : {}),
    };
    const shouldPrepare = forcePrepareRef.current;
    const requestedBundle = shouldPrepare ? bundleReprepareRef.current : "";
    forcePrepareRef.current = false;
    if (!shouldPrepare) {
      setCaptureSessionNotice("");
      bundleReprepareRef.current = "";
      setCaptureSourceUpdating(false);
    }
    setContext(null);
    setAiReady(null);
    setNotes([]);
    setConversations([]);
    setActiveConversationId("");
    activeConversationRef.current = "";
    setMessages([]);
    setOptimisticQuestion(null);
    setSending(false);
    setChatFailure(null);
    setDraft("");
    setExactSelections([]);
    setVisualSelection(null);
    setPdfSelectionRequest(null);
    setSelectionNotice("");
    setPdfPage(1);
    setCaptureManifest(null);
    setCaptureManifestError("");
    setCaptureManifestLoading(false);
    setVisualPageCapture(null);
    setVisualPageLoading(false);
    setVisualPageError("");
    setCaptureBusyPage(null);
    setCaptureBulkRunning(false);
    setCaptureBulkStopping(false);
    setCaptureBulkProgress(null);
    captureBulkAbortRef.current?.abort();
    captureBulkAbortRef.current = null;
    setFocusedEvidenceOverlay(null);
    setHistoryOpen(false);
    setAuxiliaryView(null);
    setLoadingContext(true);
    setError(null);
    const contextRequest = shouldPrepare
      ? readingApi.prepare(projectId, document.id, preparePayload, controller.signal)
      : readingApi
          .context(projectId, document.id, controller.signal)
          .catch((cause) => {
            if (shouldPrepareReadingContext(cause)) {
              return readingApi.prepare(
                projectId,
                document.id,
                preparePayload,
                controller.signal,
              );
            }
            throw cause;
          });
    void contextRequest
      .then((next) => {
        if (controller.signal.aborted) return;
        if (!contextMatchesDocument(next, projectId, document)) {
          throw new Error("阅读上下文与当前文档身份不一致，已阻止显示。");
        }
        if (
          requestedBundle &&
          next.effective_bundle_id !== requestedBundle
        ) {
          throw new Error("阅读上下文未切换到最新捕获版本，已阻止继续对话。");
        }
        if (!(next.notes || []).every((note) => noteBelongsToContext(note, next))) {
          throw new Error("阅读上下文包含不属于当前来源的笔记，已阻止显示。");
        }
        setContext(next);
        setNotes(next.notes || []);
        if (requestedBundle) {
          bundleReprepareRef.current = "";
          setCaptureSourceUpdating(false);
          setCaptureSessionNotice(
            "页面识别已更新当前阅读来源；旧捕获会话仅供参考，新问题将使用最新内容。",
          );
        }
      })
      .catch((cause) => {
        if (!controller.signal.aborted) {
          if (requestedBundle) setCaptureSourceUpdating(false);
          setError(cause instanceof Error ? cause : new Error(String(cause)));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingContext(false);
      });
    return () => controller.abort();
  }, [reloadGeneration, selectedDocument, selectedProject]);

  // Refreshing the model connection must not re-prepare the document or discard
  // the selected conversation, history, source selection or unsent question.
  useEffect(() => {
    setAiReady(null);
    setAiConnectionMessage("");
    if (!context) return;
    const controller = new AbortController();
    void readingApi.aiReadiness(context.project_id, context.sor_document_id, controller.signal)
      .then(readiness => {
        if (controller.signal.aborted) return;
        const ready = readiness.ready === true;
        setAiReady(ready);
        setAiConnectionMessage(ready ? "" : readingConnectionMessage(readiness.reason_code));
      })
      .catch(cause => {
        if (controller.signal.aborted) return;
        setAiReady(false);
        setAiConnectionMessage(readingConnectionMessage(cause instanceof ApiError ? apiErrorReasonCode(cause) : null));
      });
    return () => controller.abort();
  }, [context, aiReadinessRetry]);

  const refreshAiConnection = () => {
    setAiReady(null);
    setAiReadinessRetry(value => value + 1);
  };

  const openConversation = useCallback(
    async (summary: ReadingConversationSummaryV1) => {
      if (!context || !selectedProject || !selectedDocument || sending) return;
      conversationLoadRef.current?.abort();
      const controller = new AbortController();
      conversationLoadRef.current = controller;
      const scope = currentScopeRef.current;
      activeConversationRef.current = summary.conversation_id;
      setActiveConversationId(summary.conversation_id);
      setMessages([]);
      setChatFailure(null);
      setLoadingConversation(true);
      setHistoryOpen(false);
      try {
        const detail = await readingApi.conversationMessages(
          selectedProject.id,
          selectedDocument.id,
          summary.conversation_id,
          controller.signal,
        );
        if (
          controller.signal.aborted ||
          currentScopeRef.current !== scope ||
          activeConversationRef.current !== summary.conversation_id
        ) {
          return;
        }
        if (
          detail.conversation_id !== summary.conversation_id ||
          !(detail.items || []).every((message) =>
            messageMatchesConversation(
              message,
              summary.conversation_id,
              context,
            ),
          )
        ) {
          throw new Error("会话包含不属于当前文档的内容，已阻止显示。");
        }
        setMessages(detail.items || []);
      } catch (cause) {
        if (!controller.signal.aborted && currentScopeRef.current === scope) {
          setChatFailure({
            message:
              cause instanceof Error ? cause.message : "无法载入这段会话。",
            question: "",
            sourceRefs: [],
            providerNotConfigured: false,
            taskType: "FREE_QUESTION",
          });
        }
      } finally {
        if (!controller.signal.aborted && currentScopeRef.current === scope) {
          setLoadingConversation(false);
        }
      }
    },
    [context, selectedDocument, selectedProject, sending],
  );

  useEffect(() => {
    if (!context || !selectedProject || !selectedDocument) return;
    const controller = new AbortController();
    conversationLoadRef.current = controller;
    const scope = currentScopeRef.current;
    setLoadingConversations(true);
    setChatFailure(null);
    void readingApi
      .conversations(
        selectedProject.id,
        selectedDocument.id,
        controller.signal,
      )
      .then(async (payload) => {
        if (controller.signal.aborted || currentScopeRef.current !== scope) return;
        const items = payload.items || [];
        if (!items.every((item) => conversationMatchesContext(item, context))) {
          throw new Error("会话列表混入了其他文档，已阻止显示。");
        }
        setConversations(items);
        const first = items.find(
          (item) =>
            String(item.status || "").toUpperCase() === "ACTIVE" &&
            item.effective_bundle_id === context.effective_bundle_id,
        );
        if (!first) return;
        activeConversationRef.current = first.conversation_id;
        setActiveConversationId(first.conversation_id);
        setLoadingConversation(true);
        const detail = await readingApi.conversationMessages(
          selectedProject.id,
          selectedDocument.id,
          first.conversation_id,
          controller.signal,
        );
        if (
          controller.signal.aborted ||
          currentScopeRef.current !== scope ||
          activeConversationRef.current !== first.conversation_id
        ) {
          return;
        }
        if (
          detail.conversation_id !== first.conversation_id ||
          !(detail.items || []).every((message) =>
            messageMatchesConversation(
              message,
              first.conversation_id,
              context,
            ),
          )
        ) {
          throw new Error("会话包含不属于当前文档的内容，已阻止显示。");
        }
        setMessages(detail.items || []);
      })
      .catch((cause) => {
        if (!controller.signal.aborted && currentScopeRef.current === scope) {
          setChatFailure({
            message:
              cause instanceof Error ? cause.message : "无法载入阅读会话。",
            question: "",
            sourceRefs: [],
            providerNotConfigured: false,
            taskType: "FREE_QUESTION",
          });
        }
      })
      .finally(() => {
        if (!controller.signal.aborted && currentScopeRef.current === scope) {
          setLoadingConversations(false);
          setLoadingConversation(false);
        }
      });
    return () => controller.abort();
  }, [context, selectedDocument, selectedProject]);

  useEffect(() => {
    if (!context || !selectedProject || !selectedDocument) {
      setCaptureManifest(null);
      setCaptureManifestLoading(false);
      setCaptureManifestError("");
      return;
    }
    const controller = new AbortController();
    const scope = currentScopeRef.current;
    setCaptureManifest(null);
    setCaptureManifestLoading(true);
    setCaptureManifestError("");
    void captureApi
      .quality(selectedProject.id, selectedDocument.id, controller.signal)
      .then((payload) => {
        if (controller.signal.aborted || currentScopeRef.current !== scope) return;
        const manifest = normalizeCaptureQualityManifest(payload);
        if (
          !manifest ||
          !captureManifestMatchesSource(
            manifest,
            context,
            selectedProject.id,
            selectedDocument.id,
          )
        ) {
          throw new Error("文档可读性清单与当前来源不一致，已阻止显示。");
        }
        setCaptureManifest(manifest);
        if (manifest.effective_bundle_id !== context.effective_bundle_id) {
          if (bundleReprepareRef.current !== manifest.effective_bundle_id) {
            bundleReprepareRef.current = manifest.effective_bundle_id;
            forcePrepareRef.current = true;
            setReloadGeneration((value) => value + 1);
          }
          return;
        }
        bundleReprepareRef.current = "";
      })
      .catch((cause) => {
        if (controller.signal.aborted || currentScopeRef.current !== scope) return;
        setCaptureManifestError(
          cause instanceof ApiError && cause.status === 404
            ? "尚未生成逐页可读性清单；当前不能确认全文覆盖。"
            : cause instanceof Error
              ? cause.message
              : "暂时无法确认逐页可读性。",
        );
      })
      .finally(() => {
        if (!controller.signal.aborted && currentScopeRef.current === scope) {
          setCaptureManifestLoading(false);
        }
      });
    return () => controller.abort();
  }, [context, selectedDocument, selectedProject]);

  useEffect(() => {
    const capturePage = captureManifest?.pages.find(
      (item) => item.page_number === pdfPage,
    );
    if (
      !context ||
      !selectedProject ||
      !selectedDocument ||
      !capturePage ||
      !["VISUAL", "HYBRID"].includes(capturePage.readable_mode) ||
      capturePage.capture_status !== "READY"
    ) {
      setVisualPageCapture(null);
      setVisualPageLoading(false);
      setVisualPageError("");
      return;
    }
    const controller = new AbortController();
    const requestScope = currentPageScopeRef.current;
    const baseCaptureId = captureManifest?.base_capture_id || "";
    setVisualPageCapture(null);
    setVisualPageLoading(true);
    setVisualPageError("");
    void captureApi
      .visualPage(
        selectedProject.id,
        selectedDocument.id,
        pdfPage,
        controller.signal,
      )
      .then((payload) => {
        if (
          controller.signal.aborted ||
          currentPageScopeRef.current !== requestScope
        ) {
          return;
        }
        const pageCapture = normalizeVisualPageCapture(payload);
        if (
          !pageCapture ||
          pageCapture.source_sha256 !== context.source_sha256 ||
          pageCapture.page_number !== pdfPage ||
          pageCapture.base_capture_id !== baseCaptureId ||
          (capturePage.visual_snapshot_id &&
            pageCapture.visual_snapshot_id !== capturePage.visual_snapshot_id) ||
          (capturePage.page_image_sha256 &&
            pageCapture.page_image_sha256 !== capturePage.page_image_sha256)
        ) {
          throw new Error("本页视觉文字层与当前来源不一致，已阻止显示。");
        }
        setVisualPageCapture(pageCapture);
      })
      .catch((cause) => {
        if (
          !controller.signal.aborted &&
          currentPageScopeRef.current === requestScope
        ) {
          setVisualPageError(
            cause instanceof Error ? cause.message : "本页视觉文字层无法载入。",
          );
        }
      })
      .finally(() => {
        if (
          !controller.signal.aborted &&
          currentPageScopeRef.current === requestScope
        ) {
          setVisualPageLoading(false);
        }
      });
    return () => controller.abort();
  }, [captureManifest, context, pdfPage, selectedDocument, selectedProject]);

  const syncPdf = useCallback(() => {
    pdfFrameRef.current?.contentWindow?.postMessage(
      {
        type: "REQPILOTX_PDF_NAVIGATE",
        page: pdfPage,
        overlays: pdfEvidenceOverlays,
        selectableLayer: pdfSelectableLayer,
      },
      window.location.origin,
    );
  }, [pdfEvidenceOverlays, pdfPage, pdfSelectableLayer]);

  useEffect(() => {
    const listener = (event: MessageEvent) => {
      if (
        event.origin !== window.location.origin ||
        event.source !== pdfFrameRef.current?.contentWindow
      ) {
        return;
      }
      const message = asRecord(event.data);
      if (message.type === "pdf-viewer-ready") {
        syncPdf();
        return;
      }
      if (message.type === "pdf-page-changed") {
        const nextPage = Number(message.page || 0);
        if (Number.isInteger(nextPage) && nextPage > 0) {
          setPdfPage(nextPage);
          setExactSelections([]);
          setVisualSelection(null);
          setPdfSelectionRequest(null);
          setSelectionNotice("");
          setFocusedEvidenceOverlay(null);
        }
        return;
      }
      if (
        message.type === "pdf-text-selected" ||
        message.type === "pdf-selection-action"
      ) {
        const selectedPage = Number(message.page || 0);
        const selectedText = String(message.text || "").trim();
        const selectionId = String(message.selectionId || "");
        const action = String(message.action || "") as PdfSelectionAction;
        const rawVisualSourceRefs = (Array.isArray(message.sourceRefs)
          ? message.sourceRefs
          : Array.isArray(message.source_refs)
            ? message.source_refs
            : []);
        const visualMessage =
          rawVisualSourceRefs.length > 0 ||
          String(message.provenanceChannel || "").toUpperCase() ===
            "REMOTE_VISUAL";
        if (
          visualMessage &&
          (!context ||
            rawVisualSourceRefs.length < 1 ||
            rawVisualSourceRefs.length > MAX_SELECTION_REFS ||
            String(message.sourceSha256 || "").toLowerCase() !==
              context.source_sha256 ||
            String(message.pageImageSha256 || "").toLowerCase() !==
              String(currentCapturePage?.page_image_sha256 || "").toLowerCase() ||
            String(message.visualSnapshotId || "") !==
              String(currentCapturePage?.visual_snapshot_id || ""))
        ) {
          return;
        }
        if (
          sourceMutationInProgress &&
          message.type === "pdf-selection-action"
        ) {
          setSelectionNotice("阅读来源更新中，识别完成后再使用 AI 操作。");
          return;
        }
        const visualSourceRefs = rawVisualSourceRefs
          .map(asRecord)
          .map<ReadingSourceRef>((sourceRef) => ({
            ...sourceRef,
            kind: String(sourceRef.kind || "").toUpperCase(),
            visual_snapshot_id: String(sourceRef.visual_snapshot_id || ""),
            observation_id: String(sourceRef.observation_id || ""),
            start_offset: Number(sourceRef.start_offset),
            end_offset: Number(sourceRef.end_offset),
            page_number: Number(sourceRef.page_number || selectedPage),
            bbox: Array.isArray(sourceRef.bbox)
              ? sourceRef.bbox.map(Number)
              : undefined,
            page_bbox: Array.isArray(sourceRef.page_bbox)
              ? sourceRef.page_bbox.map(Number)
              : undefined,
            page_image_sha256: String(sourceRef.page_image_sha256 || ""),
            provider_id: String(sourceRef.provider_id || ""),
            provider_version: String(sourceRef.provider_version || ""),
          }))
          .filter(
            (sourceRef) =>
              sourceRef.kind === "VISUAL_TEXT" &&
              sourceRef.page_number === selectedPage &&
              sourceRefMatchesContext(sourceRef, context as ReadingContextV1) &&
              visualSourceRefMatchesCapture(sourceRef, visualPageCapture),
          );
        if (visualMessage && visualSourceRefs.length !== rawVisualSourceRefs.length) {
          return;
        }
        if (
          selectedPage !== pdfPageRef.current ||
          !selectionId ||
          !selectedText ||
          selectedText.length > MAX_QUESTION_LENGTH ||
          (message.type === "pdf-selection-action" &&
            !["ASK", "EXPLAIN", "TRANSLATE", "SUMMARIZE"].includes(action))
        ) {
          return;
        }
        setPdfSelectionRequest({
          selectionId,
          page: selectedPage,
          text: selectedText,
          sourceRefs: visualSourceRefs,
          ...(message.type === "pdf-selection-action" ? { action } : {}),
        });
      }
    };
    window.addEventListener("message", listener);
    return () => window.removeEventListener("message", listener);
  }, [
    context,
    currentCapturePage,
    sourceMutationInProgress,
    syncPdf,
    visualPageCapture,
  ]);

  useEffect(() => {
    syncPdf();
  }, [selectedDocument?.id, syncPdf]);

  useEffect(() => {
    if (!context || !selectedProject || !selectedDocument || pdfPage < 1) {
      setSourcePageNodes([]);
      setSourcePageError("");
      setSourcePageLoading(false);
      return;
    }
    if (
      currentCapturePage &&
      currentCapturePage.readable_mode !== "NATIVE"
    ) {
      setSourcePageNodes([]);
      setSourcePageError("");
      setSourcePageLoading(false);
      return;
    }
    if (context.status === "STALE") {
      setSourcePageNodes([]);
      setSourcePageError("来源版本已变化；旧内容不能继续用于 AI 对话。");
      setSourcePageLoading(false);
      return;
    }
    const controller = new AbortController();
    const requestScope = currentPageScopeRef.current;
    setSourcePageNodes([]);
    setSourcePageError("");
    setSourcePageLoading(true);
    void readingApi
      .sourcePage(
        selectedProject.id,
        selectedDocument.id,
        pdfPage,
        controller.signal,
      )
      .then((page) => {
        if (
          controller.signal.aborted ||
          currentPageScopeRef.current !== requestScope
        ) {
          return;
        }
        if (
          page.project_id !== selectedProject.id ||
          page.sor_document_id !== selectedDocument.id ||
          page.reading_session_id !== context.reading_session_id ||
          page.source_ir_id !== context.source_ir_id ||
          page.source_sha256 !== context.source_sha256 ||
          page.effective_bundle_id !== context.effective_bundle_id ||
          page.visual_generation_id !== context.visual_generation_id ||
          page.readable_mode !== "NATIVE" ||
          page.page_number !== pdfPage ||
          page.page_count !== context.overview.page_count ||
          !sourcePageNodesMatchContext(page.nodes || [], context, pdfPage)
        ) {
          throw new Error("当前页来源事实与阅读上下文不一致，已阻止显示。");
        }
        setSourcePageNodes(page.nodes || []);
      })
      .catch((cause) => {
        if (
          !controller.signal.aborted &&
          currentPageScopeRef.current === requestScope
        ) {
          setSourcePageError(
            cause instanceof Error ? cause.message : "当前页文字无法读取。",
          );
        }
      })
      .finally(() => {
        if (
          !controller.signal.aborted &&
          currentPageScopeRef.current === requestScope
        ) {
          setSourcePageLoading(false);
        }
      });
    return () => controller.abort();
  }, [context, currentCapturePage, pdfPage, selectedDocument, selectedProject]);

  useEffect(() => {
    timelineEndRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages, optimisticQuestion, sending]);

  const jumpToSource = useCallback(
    (sourceRef: ReadingSourceRef) => {
      if (String(sourceRef.kind || "").toUpperCase() === "VISUAL_TEXT") {
        const page = Number(sourceRef.page_number || 0);
        const visualBbox = sourceRef.bbox || [];
        if (
          !Number.isInteger(page) ||
          page < 1 ||
          visualBbox.length !== 4 ||
          !visualBbox.every(Number.isFinite)
        ) {
          setSelectionNotice("这条视觉依据没有可靠页码或位置，已拒绝跳转。");
          return;
        }
        setExactSelections([]);
        setVisualSelection(null);
        setPdfSelectionRequest(null);
        setPdfPage(page);
        setFocusedEvidenceOverlay({
          id: `visual-evidence-${sourceRef.visual_snapshot_id}-${sourceRef.observation_id}`,
          type: "TEXT",
          label: "视觉依据",
          bbox: visualBbox,
          sourceBounds: sourceRef.page_bbox || [],
          selected: true,
          focus: true,
          precision: "VISUAL_OBSERVATION",
        });
        setSelectionNotice(`已定位到第 ${page} 页的视觉识别依据。`);
        setMobilePane("source");
        return;
      }
      const node = selectableNodes.find(
        (item) => item.source_node_id === sourceRef.source_node_id,
      );
      if (!node) {
        setSelectionNotice("这条引用不属于当前文档，已拒绝跳转。");
        return;
      }
      setExactSelections([]);
      setVisualSelection(null);
      setPdfSelectionRequest(null);
      setFocusedEvidenceOverlay(null);
      setPdfPage(sourcePage(node));
      setSelectionNotice(`已定位到第 ${sourcePage(node)} 页的原文依据。`);
      setMobilePane("source");
    },
    [selectableNodes],
  );

  const jumpToCitation = useCallback(
    (citation: ReadingCitationV1) => {
      jumpToSource(citationSourceRef(citation));
      if (citation.locator_kind === "TABLE_CELL") {
        setSelectionNotice(
          `已定位到第 ${citation.page_number || 1} 页的表格单元格依据。`,
        );
      }
    },
    [jumpToSource],
  );

  const startNewConversation = useCallback(() => {
    if (sending) return;
    conversationLoadRef.current?.abort();
    activeConversationRef.current = "";
    setActiveConversationId("");
    setMessages([]);
    setOptimisticQuestion(null);
    setChatFailure(null);
    setDraft("");
    setHistoryOpen(false);
    setAuxiliaryView(null);
    requestAnimationFrame(() => composerRef.current?.focus());
  }, [sending]);

  const submitQuestion = useCallback(
    async (
      questionOverride?: string,
      refsOverride?: ReadingSourceRef[],
      taskType: ReadingTaskType = "FREE_QUESTION",
    ) => {
      const question = (questionOverride ?? draft).trim();
      const sourceRefs = refsOverride ?? selectedSourceRefs;
      if (
        !question ||
        (!sourceRefs.length && !canAskFullDocument) ||
        question.length > MAX_QUESTION_LENGTH ||
        sourceRefs.length > MAX_SELECTION_REFS ||
        sourceRefs.reduce(
          (total, ref) => total + ref.end_offset - ref.start_offset,
          0,
        ) > MAX_SELECTION_LENGTH ||
        !context ||
        !selectedProject ||
        !selectedDocument ||
        !readingChatIsWritable ||
        sending
      ) {
        return;
      }
      chatRequestRef.current?.abort();
      const controller = new AbortController();
      chatRequestRef.current = controller;
      const scope = currentScopeRef.current;
      let conversationId = activeConversationRef.current;
      const visibleQuestion = question;
      const optimistic: OptimisticQuestion = {
        id: `pending-${Date.now()}`,
        content: visibleQuestion,
        sourceRefs,
        taskType,
      };
      setOptimisticQuestion(optimistic);
      setChatFailure(null);
      setSending(true);
      setDraft("");
      setHistoryOpen(false);
      setAuxiliaryView(null);
      setMobilePane("chat");
      try {
        if (!conversationId) {
          const created = await readingApi.createConversation(
            selectedProject.id,
            selectedDocument.id,
            {
              schema_version: "reading-conversation-create-request/v1",
              title: questionTitle(visibleQuestion),
            },
            controller.signal,
          );
          if (
            controller.signal.aborted ||
            currentScopeRef.current !== scope ||
            !conversationMatchesContext(created, context)
          ) {
            if (!controller.signal.aborted && currentScopeRef.current === scope) {
              throw new Error("新会话与当前文档身份不一致，已阻止继续。");
            }
            return;
          }
          conversationId = created.conversation_id;
          activeConversationRef.current = conversationId;
          setActiveConversationId(conversationId);
          setMessages([]);
          setConversations((current) =>
            upsertConversationSummary(current, created),
          );
        }

        const turn = await readingApi.sendMessage(
          selectedProject.id,
          selectedDocument.id,
          conversationId,
          {
            schema_version: "reading-message-create-request/v1",
            source_sha256: context.source_sha256,
            content: question,
            task_type: taskType,
            ...(sourceRefs.length ? { source_refs: sourceRefs } : {}),
          },
          controller.signal,
        );
        if (
          controller.signal.aborted ||
          currentScopeRef.current !== scope ||
          activeConversationRef.current !== conversationId
        ) {
          return;
        }
        if (
          !conversationMatchesContext(turn.conversation, context) ||
          turn.conversation.conversation_id !== conversationId ||
          turn.user_message.role !== "USER" ||
          turn.assistant_message.role !== "ASSISTANT" ||
          turn.user_message.content !== question ||
          !messageMatchesConversation(turn.user_message, conversationId, context) ||
          !messageMatchesConversation(
            turn.assistant_message,
            conversationId,
            context,
          )
        ) {
          throw new Error("AI 回答与当前文档或会话不一致，已阻止显示。");
        }
        setMessages((current) => {
          const ids = new Set(current.map((message) => message.message_id));
          const next = [...current];
          if (!ids.has(turn.user_message.message_id)) {
            next.push({ ...turn.user_message, content: visibleQuestion });
          }
          if (!ids.has(turn.assistant_message.message_id)) {
            next.push({
              ...turn.assistant_message,
              retrieval: turn.assistant_message.retrieval || turn.retrieval,
            });
          }
          return next;
        });
        setConversations((current) =>
          upsertConversationSummary(current, turn.conversation),
        );
        setOptimisticQuestion(null);
        setExactSelections([]);
        setVisualSelection(null);
        setSelectionNotice("");
      } catch (cause) {
        if (
          !controller.signal.aborted &&
          !isAbortError(cause) &&
          currentScopeRef.current === scope &&
          activeConversationRef.current === conversationId
        ) {
          setOptimisticQuestion(null);
          setDraft(question);
          if (isProviderNotConfigured(cause)) {
            setAiReady(false);
            setAiConnectionMessage(readingChatFailureMessage(cause));
            setChatFailure(null);
          } else {
            setChatFailure({
              message: readingChatFailureMessage(cause),
              question,
              sourceRefs,
              providerNotConfigured: false,
              taskType,
            });
          }
        }
      } finally {
        if (!controller.signal.aborted && currentScopeRef.current === scope) {
          setSending(false);
        }
      }
    },
    [
      context,
      canAskFullDocument,
      draft,
      readingChatIsWritable,
      selectedDocument,
      selectedProject,
      selectedSourceRefs,
      sending,
    ],
  );

  useEffect(() => {
    if (!pdfSelectionRequest || pdfSelectionRequest.page !== pdfPage) return;
    let sourceRefs: ReadingSourceRef[] = [];
    if (pdfSelectionRequest.sourceRefs.length) {
      const currentSnapshotId =
        visualPageCapture?.page_number === pdfPage
          ? visualPageCapture.visual_snapshot_id
          : currentCapturePage?.visual_snapshot_id;
      const validVisualRefs = pdfSelectionRequest.sourceRefs.every(
        (sourceRef) =>
          sourceRef.kind === "VISUAL_TEXT" &&
          sourceRef.page_number === pdfPage &&
          sourceRef.visual_snapshot_id === currentSnapshotId &&
          sourceRef.page_image_sha256 === currentCapturePage?.page_image_sha256,
      );
      if (!validVisualRefs) {
        setVisualSelection(null);
        setSelectionNotice("这段视觉文字与当前页面版本不一致，已阻止引用。");
        return;
      }
      sourceRefs = pdfSelectionRequest.sourceRefs;
      setExactSelections([]);
      setVisualSelection({
        text: pdfSelectionRequest.text,
        sourceRefs,
      });
    } else {
      if (sourcePageLoading) {
        setSelectionNotice("正在把选区与本页原文精确对应…");
        return;
      }
      const resolved = resolvePdfSourceSelections(
        pdfSelectionRequest.text,
        sourcePageNodes,
        MAX_SELECTION_REFS,
      );
      if (!resolved) {
        setExactSelections([]);
        setVisualSelection(null);
        setSelectionNotice(
          sourcePageError ||
            "这段选区无法唯一对应到原文，或跨越了过多段落。请缩短选区，或多选几个字避免重复文本。",
        );
        return;
      }
      setVisualSelection(null);
      setExactSelections((current) => {
        const currentKey = current
          .map((item) => `${item.sourceNodeId}:${item.startOffset}:${item.endOffset}`)
          .join("|");
        const resolvedKey = resolved
          .map((item) => `${item.sourceNodeId}:${item.startOffset}:${item.endOffset}`)
          .join("|");
        return currentKey === resolvedKey ? current : resolved;
      });
      sourceRefs = resolved.map((selection) => ({
        source_node_id: selection.sourceNodeId,
        start_offset: selection.startOffset,
        end_offset: selection.endOffset,
      }));
    }
    setSelectionNotice(
      `已选取第 ${pdfPage} 页${pdfSelectionRequest.sourceRefs.length ? "视觉识别文字" : "原文"}，可直接提问、解释、翻译或总结。`,
    );
    if (!pdfSelectionRequest.action) return;
    const actionKey = `${pdfSelectionRequest.selectionId}:${pdfSelectionRequest.action}`;
    if (consumedPdfActionsRef.current.has(actionKey)) return;
    if (pdfSelectionRequest.action === "ASK") {
      consumedPdfActionsRef.current.add(actionKey);
      setMobilePane("chat");
      requestAnimationFrame(() => composerRef.current?.focus());
      return;
    }
    if (sending) {
      setSelectionNotice("请先等待当前回答完成，再对这段原文执行操作。");
      return;
    }
    consumedPdfActionsRef.current.add(actionKey);
    const prompt = {
      EXPLAIN:
        "请解释我选中的这段原文。先用通俗语言复述，再说明它在当前章节中的作用、成立条件和对工程执行的影响；不要脱离原文补写结论。",
      TRANSLATE:
        "请翻译我选中的这段原文。如果主要是中文，译成英文；如果主要是外文，译成中文。先给准确译文，再列出专业术语对照；保留编号、数值、单位和限定条件。",
      SUMMARIZE:
        "请总结我选中的这段原文。用一句话说明核心意思，再列出必须保留的条件、参数、例外和待澄清点，并说明它与当前文档上下文的关系。",
    }[pdfSelectionRequest.action];
    if (prompt) void submitQuestion(prompt, sourceRefs);
  }, [
    pdfPage,
    pdfSelectionRequest,
    sending,
    currentCapturePage,
    sourcePageError,
    sourcePageLoading,
    sourcePageNodes,
    submitQuestion,
    visualPageCapture,
  ]);

  const deleteConversation = useCallback(
    async (summary: ReadingConversationSummaryV1) => {
      if (!selectedProject || !selectedDocument || sending) return;
      const confirmed = window.confirm(`删除会话“${summary.title || "未命名会话"}”？`);
      if (!confirmed) return;
      const scope = currentScopeRef.current;
      try {
        const result = await readingApi.deleteConversation(
          selectedProject.id,
          selectedDocument.id,
          summary.conversation_id,
        );
        if (currentScopeRef.current !== scope) return;
        if (
          result.deleted !== true ||
          result.conversation_id !== summary.conversation_id
        ) {
          throw new Error("删除会话的回执不匹配，当前会话已保留。");
        }
        const remaining = conversations.filter(
          (item) => item.conversation_id !== summary.conversation_id,
        );
        setConversations(remaining);
        if (activeConversationRef.current === summary.conversation_id) {
          activeConversationRef.current = "";
          setActiveConversationId("");
          setMessages([]);
          setChatFailure(null);
          if (remaining[0]) void openConversation(remaining[0]);
        }
      } catch (cause) {
        if (currentScopeRef.current === scope) {
          setChatFailure({
            message: cause instanceof Error ? cause.message : "无法删除会话。",
            question: "",
            sourceRefs: [],
            providerNotConfigured: false,
            taskType: "FREE_QUESTION",
          });
        }
      }
    },
    [conversations, openConversation, selectedDocument, selectedProject, sending],
  );

  const clearActiveConversation = useCallback(async () => {
    if (
      !activeConversationId ||
      !selectedProject ||
      !selectedDocument ||
      sending ||
      !window.confirm("清空当前会话中的全部消息？会话本身会保留。")
    ) {
      return;
    }
    const scope = currentScopeRef.current;
    const conversationId = activeConversationId;
    try {
      const result = await readingApi.clearConversation(
        selectedProject.id,
        selectedDocument.id,
        conversationId,
      );
      if (
        currentScopeRef.current !== scope ||
        activeConversationRef.current !== conversationId
      ) {
        return;
      }
      if (result.cleared !== true || result.conversation_id !== conversationId) {
        throw new Error("清空会话的回执不匹配，当前消息已保留。");
      }
      setMessages([]);
      setChatFailure(null);
    } catch (cause) {
      if (currentScopeRef.current === scope) {
        setChatFailure({
          message: cause instanceof Error ? cause.message : "无法清空会话。",
          question: "",
          sourceRefs: [],
          providerNotConfigured: false,
          taskType: "FREE_QUESTION",
        });
      }
    }
  }, [activeConversationId, selectedDocument, selectedProject, sending]);

  const stopWaiting = useCallback(() => {
    const pending = optimisticQuestion;
    chatRequestRef.current?.abort();
    setSending(false);
    setOptimisticQuestion(null);
    if (pending) setDraft(pending.content);
    setChatFailure({
      message:
        "已停止在本页等待完整回答。服务端请求可能仍在结束处理中，迟到结果不会自动写入当前界面。",
      question: pending?.content || "",
      sourceRefs: pending?.sourceRefs || [],
      providerNotConfigured: false,
      stopped: true,
      taskType: pending?.taskType || "FREE_QUESTION",
    });
  }, [optimisticQuestion]);

  const reloadCaptureQuality = useCallback(
    async (
      expectedCaptures: VisualPageCaptureV1[] = [],
      signal?: AbortSignal,
    ) => {
      if (!context || !selectedProject || !selectedDocument) return null;
      const payload = await captureApi.quality(
        selectedProject.id,
        selectedDocument.id,
        signal,
      );
      const manifest = normalizeCaptureQualityManifest(payload);
      if (
        !manifest ||
        !captureManifestMatchesSource(
          manifest,
          context,
          selectedProject.id,
          selectedDocument.id,
        )
      ) {
        throw new Error("更新后的文档可读性清单与当前来源不一致。");
      }
      if (
        expectedCaptures.some(
          (capture) => !visualCaptureReceiptMatchesManifest(capture, manifest),
        )
      ) {
        throw new Error("视觉识别回执未进入当前有效捕获版本，已阻止采用。");
      }
      setCaptureManifest(manifest);
      setCaptureManifestError("");
      const bundleChanged =
        manifest.effective_bundle_id !== context.effective_bundle_id;
      if (bundleChanged) {
        bundleReprepareRef.current = manifest.effective_bundle_id;
        forcePrepareRef.current = true;
        setReloadGeneration((value) => value + 1);
      }
      return { manifest, bundleChanged };
    },
    [context, selectedDocument, selectedProject],
  );

  const captureOneVisualPage = useCallback(
    async (pageNumber: number, signal?: AbortSignal) => {
      if (!context || !captureManifest || !selectedProject || !selectedDocument) {
        throw new Error("当前文档尚未准备好视觉识别。");
      }
      const payload = await captureApi.resumeVisualPage(
        selectedProject.id,
        selectedDocument.id,
        pageNumber,
        signal,
      );
      const pageCapture = normalizeVisualPageCapture(payload);
      if (
        !pageCapture ||
        pageCapture.source_sha256 !== context.source_sha256 ||
        pageCapture.base_capture_id !== captureManifest.base_capture_id ||
        pageCapture.page_number !== pageNumber
      ) {
        throw new Error(`第 ${pageNumber} 页识别回执与当前来源不一致。`);
      }
      if (
        pageCapture.capture_status === "READY" &&
        captureManifest.active_visual_generation_id &&
        pageCapture.generation_id !==
          captureManifest.active_visual_generation_id
      ) {
        throw new Error(`第 ${pageNumber} 页识别回执不属于当前视觉版本。`);
      }
      if (
        pageCapture.capture_status === "READY" &&
        pageNumber === pdfPage
      ) {
        setVisualPageCapture(pageCapture);
        setVisualPageError("");
      }
      return pageCapture;
    },
    [captureManifest, context, pdfPage, selectedDocument, selectedProject],
  );

  const resumeCapturePage = useCallback(
    async (pageNumber: number) => {
      if (captureBusyPage || captureBulkRunning) return;
      setCaptureSourceUpdating(true);
      setCaptureBusyPage(pageNumber);
      setSelectionNotice(`正在识别第 ${pageNumber} 页；不会启动需求提取。`);
      let awaitingBundlePrepare = false;
      try {
        const pageCapture = await captureOneVisualPage(pageNumber);
        const refreshed = await reloadCaptureQuality([pageCapture]);
        if (refreshed?.bundleChanged) {
          awaitingBundlePrepare = true;
          setSelectionNotice("页面识别已更新，正在切换到新的阅读来源…");
          return;
        }
        if (pageCapture.capture_status === "READY") {
          setSelectionNotice(
            pageCapture.content_state === "VERIFIED_EMPTY"
              ? `第 ${pageNumber} 页已验证为无可选文字；无文字图片/图纸语义未解析。`
              : `第 ${pageNumber} 页视觉文字层已捕获。`,
          );
        } else {
          setSelectionNotice(
            `第 ${pageNumber} 页仍未识别成功${pageCapture.failure_code ? `：${pageCapture.failure_code}` : ""}。可稍后重试。`,
          );
        }
      } catch (cause) {
        if (cause instanceof ApiError) {
          try {
            await reloadCaptureQuality();
          } catch {
            // Keep the original application failure as the primary message.
          }
        }
        setSelectionNotice(
          visualCaptureFailureMessage(cause, pageNumber),
        );
      } finally {
        setCaptureBusyPage(null);
        if (!awaitingBundlePrepare) setCaptureSourceUpdating(false);
      }
    }, [
      captureBulkRunning,
      captureBusyPage,
      captureOneVisualPage,
      reloadCaptureQuality,
    ],
  );

  const resumeAllCapturePages = useCallback(
    async (pageNumbers: number[]) => {
      if (captureBulkRunning || captureBusyPage || !captureManifest) return;
      const pending = [...new Set(pageNumbers)]
        .filter((pageNumber) => {
          const page = captureManifest.pages.find(
            (candidate) => candidate.page_number === pageNumber,
          );
          return Boolean(
            page &&
              page.required_capability !== "NONE" &&
              page.capture_status !== "READY" &&
              page.capture_status !== "NOT_REQUIRED",
          );
        })
        .sort((left, right) => left - right);
      if (!pending.length) return;
      if (
        !window.confirm(
          `识别全部 ${pending.length} 个不可选页面？页面图像会发送给已配置的视觉模型；该操作不会启动需求提取。`,
        )
      ) {
        return;
      }
      const controller = new AbortController();
      captureBulkAbortRef.current = controller;
      captureBulkStopRef.current = false;
      setCaptureSourceUpdating(true);
      setCaptureBulkRunning(true);
      setCaptureBulkStopping(false);
      setCaptureBulkProgress({ completed: 0, total: pending.length, failed: 0 });
      let completed = 0;
      let failed = 0;
      let lastFailureMessage = "";
      const captureReceipts: VisualPageCaptureV1[] = [];
      for (const pageNumber of pending) {
        if (controller.signal.aborted || captureBulkStopRef.current) break;
        setCaptureBusyPage(pageNumber);
        try {
          const result = await captureOneVisualPage(
            pageNumber,
            controller.signal,
          );
          captureReceipts.push(result);
          if (result.capture_status !== "READY") failed += 1;
        } catch (cause) {
          if (isAbortError(cause) || controller.signal.aborted) break;
          failed += 1;
          lastFailureMessage = visualCaptureFailureMessage(cause, pageNumber);
        }
        completed += 1;
        setCaptureBulkProgress({
          completed,
          total: pending.length,
          failed,
        });
      }
      const stopped = captureBulkStopRef.current;
      captureBulkAbortRef.current = null;
      setCaptureBusyPage(null);
      let bundleChanged = false;
      try {
        const refreshed = await reloadCaptureQuality(captureReceipts);
        bundleChanged = Boolean(refreshed?.bundleChanged);
      } catch (cause) {
        setCaptureManifestError(
          cause instanceof Error ? cause.message : "识别后无法刷新可读性清单。",
        );
      } finally {
        setCaptureBulkRunning(false);
        setCaptureBulkStopping(false);
        captureBulkStopRef.current = false;
        if (!bundleChanged) setCaptureSourceUpdating(false);
      }
      setSelectionNotice(
        bundleChanged
          ? "页面识别已更新，正在切换到新的阅读来源…"
          : stopped
          ? `已停止批量识别；已处理 ${completed}/${pending.length} 页，可稍后继续。`
          : failed
            ? `批量识别已完成，${failed} 页仍失败；${lastFailureMessage || "可在可读性详情中查看失败原因。"}`
            : `已捕获 ${completed} 页视觉文字层。`,
      );
    }, [
      captureBulkRunning,
      captureBusyPage,
      captureManifest,
      captureOneVisualPage,
      reloadCaptureQuality,
    ],
  );

  const stopCaptureBatch = useCallback(() => {
    if (!captureBulkAbortRef.current) return;
    captureBulkStopRef.current = true;
    setCaptureBulkStopping(true);
    setSelectionNotice("将在当前页识别完成后停止。");
  }, []);

  const saveNote = async () => {
    const content = noteDraft.trim();
    if (
      !context ||
      !selectedProject ||
      !selectedDocument ||
      !selectedSourceRef ||
      !readingIsWritable ||
      sourceMutationInProgress ||
      !content
    ) {
      return;
    }
    setBusyNote("create");
    const scope = currentScopeRef.current;
    try {
      const note = await readingApi.createNote(
        selectedProject.id,
        selectedDocument.id,
        {
          schema_version: "reading-note-create-request/v1",
          source_sha256: context.source_sha256,
          source_ref: selectedSourceRef,
          content,
        },
      );
      if (currentScopeRef.current !== scope) return;
      if (!activeNoteMatchesContext(note, context) || note.content !== content) {
        throw new Error("阅读笔记与当前原文不一致，已阻止保存。");
      }
      setNotes((current) => [note, ...current]);
      setNoteDraft("");
    } catch (cause) {
      if (currentScopeRef.current === scope) {
        setError(cause instanceof Error ? cause : new Error(String(cause)));
      }
    } finally {
      if (currentScopeRef.current === scope) setBusyNote("");
    }
  };

  const saveEditedNote = async (note: ReadingNoteV1) => {
    const content = editingNoteText.trim();
    if (
      !selectedProject ||
      !selectedDocument ||
      !context ||
      sourceMutationInProgress ||
      String(note.status || "").toUpperCase() !== "ACTIVE" ||
      !content
    ) return;
    setBusyNote(`edit:${note.note_id}`);
    const scope = currentScopeRef.current;
    try {
      const updated = await readingApi.updateNote(
        selectedProject.id,
        selectedDocument.id,
        note.note_id,
        {
          schema_version: "reading-note-update-request/v1",
          expected_revision: note.revision,
          content,
        },
      );
      if (currentScopeRef.current !== scope) return;
      if (
        !activeNoteMatchesContext(updated, context) ||
        updated.note_id !== note.note_id ||
        updated.revision !== note.revision + 1
      ) {
        throw new Error("笔记修订与当前来源不一致，已阻止显示。");
      }
      setNotes((current) =>
        current.map((item) => (item.note_id === updated.note_id ? updated : item)),
      );
      setEditingNoteId("");
      setEditingNoteText("");
    } catch (cause) {
      if (currentScopeRef.current === scope) {
        setError(cause instanceof Error ? cause : new Error(String(cause)));
      }
    } finally {
      if (currentScopeRef.current === scope) setBusyNote("");
    }
  };

  const deleteNote = async (note: ReadingNoteV1) => {
    if (
      !selectedProject ||
      !selectedDocument ||
      sourceMutationInProgress ||
      String(note.status || "").toUpperCase() !== "ACTIVE"
    ) return;
    setBusyNote(`delete:${note.note_id}`);
    const scope = currentScopeRef.current;
    try {
      const result = await readingApi.deleteNote(
        selectedProject.id,
        selectedDocument.id,
        note.note_id,
        note.revision,
      );
      if (currentScopeRef.current !== scope) return;
      if (
        result.deleted !== true ||
        result.note_id !== note.note_id ||
        result.revision !== note.revision + 1
      ) {
        throw new Error("删除笔记的回执不匹配，当前笔记已保留。");
      }
      setNotes((current) =>
        current.filter((item) => item.note_id !== note.note_id),
      );
    } catch (cause) {
      if (currentScopeRef.current === scope) {
        setError(cause instanceof Error ? cause : new Error(String(cause)));
      }
    } finally {
      if (currentScopeRef.current === scope) setBusyNote("");
    }
  };

  const handleComposerKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      void submitQuestion();
    }
  };

  const fileUrl =
    selectedProject && selectedDocument
      ? readingApi.fileUrl(selectedProject.id, selectedDocument.id)
      : "";
  const pdfUrl =
    fileUrl && fileIsPdf(selectedDocument)
      ? `/pdf-viewer?embedded=1&keyboard=parent&fit=width&selection=reading&pdf=${encodeURIComponent(fileUrl)}&page=1`
      : "";

  const ReadingContent = embedded ? "section" : "main";
  return (
    <div className={`reading-page${embedded ? " is-embedded" : ""}`} data-text-size={readerTextSize}>
      <header className={embedded ? "reading-work-context" : "reading-header"}>
        {!embedded && <Brand compact />}
        <div className="reading-document-context">
          {!embedded && <><label>
            <span>项目</span>
            <select
              aria-label="阅读项目"
              value={route.projectId}
              disabled={loadingProjects || projects.length === 0}
              onChange={(event) => {
                currentScopeRef.current = "navigating:project";
                chatRequestRef.current?.abort();
                navigate(
                  serializeWorkspaceRoute({
                    ...route,
                    task: "reading",
                    projectId: event.target.value,
                    documentId: "",
                    requirementSetId: "",
                  }),
                );
              }}
            >
              {!selectedProject && <option value={route.projectId}>{route.projectId ? "所选项目不可用" : "选择项目"}</option>}
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>
          <span aria-hidden="true">/</span></>}
          <label>
            <span>当前文档</span>
            <select
              aria-label="当前阅读文档"
              value={route.documentId}
              disabled={loadingDocuments || documents.length === 0}
              onChange={(event) => {
                currentScopeRef.current = "navigating:document";
                chatRequestRef.current?.abort();
                navigate(
                  serializeWorkspaceRoute({
                    ...route,
                    task: "reading",
                    documentId: event.target.value,
                    requirementSetId: "",
                  }),
                );
              }}
            >
              {!selectedDocument && <option value={route.documentId}>{route.documentId ? "所选文档不可用" : "选择文档"}</option>}
              {documents.map((document) => (
                <option key={document.id} value={document.id}>
                  {document.file_name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="reading-header-actions">
          {readingHandoff.target && <button type="button" className="button secondary" onClick={() => navigate(readingHandoff.target!.url)}>返回当前工作</button>}
          {context ? (
            <StatusBadge tone={statusTone(chatStatus)}>
              {statusLabel(chatStatus)}
            </StatusBadge>
          ) : null}
          <button
            className="button ghost compact"
            type="button"
            onClick={() =>
              navigate(
                serializeWorkspaceRoute({
                  ...route,
                  task: "projects",
                  requirementSetId: "",
                }),
              )
            }
          >
            返回项目
          </button>
        </div>
      </header>

      <ReadingContent className="reading-main" aria-label={embedded ? "文档阅读工作区" : undefined}>
        {error ? (
          <ErrorBlock
            error={error}
            onRetry={() => {
              if (!projects.length || !documents.length) setCatalogRetry(value => value + 1);
              setReloadGeneration(value => value + 1);
            }}
          />
        ) : null}
        {loadingProjects || loadingDocuments ? (
          <LoadingBlock label="正在载入当前文档" />
        ) : !selectedProject || !selectedDocument ? (
          <EmptyState
            icon="document"
            title={route.projectId && !selectedProject ? "所选项目不可用" : route.documentId ? "所选文档不可用" : "选择一份文档开始阅读"}
            description={route.projectId && !selectedProject || route.documentId ? "未切换到其他来源。请重新选择项目或文档，也可返回项目页核对。" : "AI 阅读一次只围绕当前选中的文档展开。"}
          />
        ) : (
          <div className="reading-workbench" data-mobile-pane={mobilePane}>
            <nav className="reading-mobile-switch" aria-label="移动端阅读视图">
              <button
                type="button"
                className={cx(mobilePane === "source" && "is-active")}
                onClick={() => setMobilePane("source")}
              >
                原文
              </button>
              <button
                type="button"
                className={cx(mobilePane === "chat" && "is-active")}
                onClick={() => setMobilePane("chat")}
              >
                AI 对话
              </button>
            </nav>

            <section className="reading-source-pane" aria-label="当前文档原文">
              <header>
                <div>
                  <p className="section-label">CURRENT DOCUMENT</p>
                  <h1>{selectedDocument.file_name}</h1>
                </div>
                <div className="reading-source-actions">
                  {fileIsPdf(selectedDocument) ? <CaptureStatus
                    manifest={captureManifest}
                    loading={captureManifestLoading}
                    error={captureManifestError}
                    currentPage={pdfPage}
                    busyPage={captureBusyPage}
                    bulkRunning={captureBulkRunning}
                    bulkStopping={captureBulkStopping}
                    bulkProgress={captureBulkProgress}
                    onPage={(pageNumber) => {
                      setPdfPage(pageNumber);
                      setFocusedEvidenceOverlay(null);
                      setExactSelections([]);
                      setVisualSelection(null);
                      setPdfSelectionRequest(null);
                    }}
                    onResumePage={(pageNumber) =>
                      void resumeCapturePage(pageNumber)
                    }
                    onResumeAll={(pageNumbers) =>
                      void resumeAllCapturePages(pageNumbers)
                    }
                    onStopAll={stopCaptureBatch}
                  /> : null}
                  {fileIsPdf(selectedDocument) ? <span>第 {pdfPage} 页</span> : null}
                  <a
                    className="button ghost compact"
                    href={fileUrl}
                    target="_blank"
                    rel="noreferrer"
                  >
                    <Icon name="external" size={15} />
                    原文件
                  </a>
                </div>
              </header>
              <div className="reading-source-canvas">
                {pdfUrl ? (
                  <iframe
                    key={selectedDocument.id}
                    ref={pdfFrameRef}
                    title={`当前文档原文 · ${selectedDocument.file_name}`}
                    src={pdfUrl}
                    allow="clipboard-write"
                  />
                ) : (
                  <EmptyState
                    icon="document"
                    title="当前格式暂不提供页内阅读"
                    description="可以打开原文件，并继续在右侧围绕当前文档提问。"
                    action={
                      <a
                        className="button secondary"
                        href={fileUrl}
                        target="_blank"
                        rel="noreferrer"
                      >
                        打开原文件
                      </a>
                    }
                  />
                )}
              </div>
              {context && fileIsPdf(selectedDocument) ? (
                <div className="reading-source-guidance" role="status">
                  <span className="reading-source-line" aria-hidden="true" />
                  <p>
                    {selectionNotice ||
                    (currentCapturePage?.readable_mode === "VISUAL"
                      ? visualPageLoading
                        ? "正在载入本页视觉文字层…"
                        : visualPageError
                          ? "本页视觉文字层暂时无法载入，当前不可选择。"
                          : visualPageCapture?.content_state === "VERIFIED_EMPTY"
                            ? "本页已验证为无可选文字；无文字图片/图纸语义未解析。"
                          : "直接在视觉识别文字层上拖选，即可解释、翻译、总结或问 AI。"
                      : currentCapturePage?.readable_mode === "NONE"
                        ? "本页当前不可选择；可从“文档可读性”中继续识别。"
                        : sourcePageLoading
                          ? "正在准备本页可引用文字…"
                          : sourcePageError
                            ? "本页文字暂时无法与来源精确对应。"
                            : "直接在 PDF 纸面上拖选文字，即可解释、翻译、总结或问 AI。")}
                  </p>
                </div>
              ) : null}
              <footer>
                <p>当前会话只读取这份文档；切换文档会切换到独立会话空间。</p>
              </footer>
            </section>

            <aside className="reading-chat-pane" aria-label="AI 阅读对话">
              <header className="reading-chat-heading">
                <div className="reading-chat-identity">
                  <span className="reading-chat-mark" aria-hidden="true">
                    <Sparkles size={17} />
                  </span>
                  <div>
                    <p>AI READER</p>
                    <h2>{activeConversation?.title || "和文档聊一聊"}</h2>
                  </div>
                </div>
                <div className="reading-chat-heading-actions">
                  <div className="reading-text-size-control" role="group" aria-label="阅读字号">
                    {(
                      [
                        ["compact", "A−", "较小字号"],
                        ["standard", "A", "标准字号"],
                        ["large", "A+", "大字号"],
                      ] as const
                    ).map(([value, label, ariaLabel]) => (
                      <button
                        key={value}
                        type="button"
                        aria-label={ariaLabel}
                        aria-pressed={readerTextSize === value}
                        onClick={() => setReaderTextSize(value)}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  <button
                    type="button"
                    aria-label="会话历史"
                    title="会话历史"
                    aria-pressed={historyOpen}
                    disabled={sending}
                    onClick={() => {
                      setHistoryOpen((value) => !value);
                      setAuxiliaryView(null);
                    }}
                  >
                    <Clock3 size={17} />
                    {conversations.length ? <span>{conversations.length}</span> : null}
                  </button>
                  <button
                    type="button"
                    aria-label="新建会话"
                    title="新建会话"
                    disabled={sending}
                    onClick={startNewConversation}
                  >
                    <MessageSquarePlus size={17} />
                  </button>
                  <button
                    type="button"
                    aria-label="清空当前会话"
                    title="清空当前会话"
                    disabled={!activeConversationId || !messages.length || sending}
                    onClick={() => void clearActiveConversation()}
                  >
                    <Trash2 size={16} />
                  </button>
                  <button
                    type="button"
                    aria-label="文档辅助信息"
                    title="概览、结构地图和笔记"
                    aria-pressed={Boolean(auxiliaryView)}
                    onClick={() => {
                      setAuxiliaryView((value) => value || "overview");
                      setHistoryOpen(false);
                    }}
                  >
                    <PanelRightOpen size={17} />
                  </button>
                </div>
              </header>

              <div className="reading-chat-timeline" aria-live="polite">
                <p className="reading-drawer-empty">{readingHandoff.reason || `可将阅读发现准备到核对版 v${readingHandoff.number} 的工作台草稿，提交前仍需核对。`}{readingHandoff.canRetry && <button type="button" onClick={readingHandoff.refresh}>重新核对工作版本</button>}</p>
                {captureSessionNotice ? (
                  <div className="reading-capture-session-notice" role="status">
                    <BookOpenText size={15} aria-hidden="true" />
                    <span>{captureSessionNotice}</span>
                  </div>
                ) : null}
                {activeConversationIsStale ? (
                  <div className="reading-stale-conversation-banner" role="status">
                    <div>
                      <strong>旧捕获会话，仅供参考</strong>
                      <span>该会话不会自动混入最新阅读来源，也不能继续追问。</span>
                    </div>
                    <button type="button" onClick={startNewConversation}>
                      新建最新会话
                    </button>
                  </div>
                ) : null}
                {aiReady !== true && context ? (
                  <div className="reading-ai-availability" role="status">
                    <div><strong>{aiReady === null ? "正在检查 Agent 连接" : "阅读 Agent 尚未就绪"}</strong>
                      <p>{aiReady === null ? "原件和历史记录可继续查看，也可先写好问题。" : aiConnectionMessage}</p>
                    </div>
                    {aiReady === false && <div className="reading-ai-availability-actions">
                      <button type="button" className="button secondary compact" disabled={sending} onClick={refreshAiConnection}>刷新连接</button>
                      <AppLink to={`${serializeWorkspaceRoute({ ...route, task: "reading" })}&settings=agent`}>配置 Agent</AppLink>
                    </div>}
                  </div>
                ) : null}
                {loadingContext || loadingConversations || loadingConversation ? (
                  <div className="reading-chat-centered">
                    <LoadingBlock label="正在打开文档会话" />
                  </div>
                ) : !context ? (
                  <div className="reading-chat-centered">
                    <EmptyState
                      icon="eye"
                      title="文档还未准备好"
                      description="原文仍可浏览；重新准备不会启动需求提取。"
                    />
                  </div>
                ) : messages.length || optimisticQuestion ? (
                  <div className="reading-message-stream">
                    {messages.map((message, index) => {
                      let previousUserMessage: ReadingChatMessageV1 | null = null;
                      for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
                        if (messages[cursor]?.role === "USER") {
                          previousUserMessage = messages[cursor] || null;
                          break;
                        }
                      }
                      const previousSourceRefs = previousUserMessage
                        ? previousUserMessage.source_refs?.length
                          ? previousUserMessage.source_refs
                          : previousUserMessage.source_ref
                            ? [previousUserMessage.source_ref]
                            : []
                        : [];
                      const previousTaskType = normalizedTaskType(
                        previousUserMessage?.task_type,
                      );
                      return (
                      <ChatMessage
                        key={message.message_id}
                        message={message}
                        readingHandoff={sourceMutationInProgress ? { ...readingHandoff, reason: "阅读来源正在更新，完成后再准备工作台草稿。" } : readingHandoff}
                        captureManifest={captureManifest}
                        onCitation={jumpToCitation}
                        onSuggested={(question) => void submitQuestion(question, [])}
                        disableSuggestions={
                          !readingChatIsWritable || !canAskFullDocument || sending
                        }
                        onRegenerate={
                          message.role === "ASSISTANT" &&
                          previousUserMessage &&
                          readingChatIsWritable && !sending &&
                          (previousSourceRefs.length > 0 || canAskFullDocument)
                            ? () =>
                                void submitQuestion(
                                  previousUserMessage!.content,
                                  previousSourceRefs,
                                  previousTaskType,
                                )
                            : undefined
                        }
                      />
                      );
                    })}
                    {optimisticQuestion ? (
                      <OptimisticUserMessage question={optimisticQuestion} />
                    ) : null}
                    {sending ? <AssistantThinking onStop={stopWaiting} /> : null}
                    <div ref={timelineEndRef} />
                  </div>
                ) : (
                  <ChatWelcome
                    documentName={selectedDocument.file_name}
                    context={context}
                    disabled={!readingChatIsWritable || sending || !canAskFullDocument}
                    gateMessage={fullDocumentQuestionGate}
                    agentReady={aiReady === true}
                    onPrompt={(prompt, taskType) =>
                      void submitQuestion(prompt, [], taskType)
                    }
                  />
                )}

                {chatFailure ? (
                  <ChatFailureNotice
                    failure={chatFailure}
                    onRetry={
                      chatFailure.question && readingChatIsWritable && !sending
                        ? () =>
                            void submitQuestion(
                              chatFailure.question,
                              chatFailure.sourceRefs,
                              chatFailure.taskType,
                            )
                        : undefined
                    }
                    onDismiss={() => setChatFailure(null)}
                  />
                ) : null}
              </div>

              <footer className="reading-composer-shell">
                {latestSuggestions.length && !sending ? (
                  <div className="reading-suggested-row" aria-label="建议追问">
                    {latestSuggestions.map((question) => (
                      <button
                        key={question}
                        type="button"
                        disabled={
                          !readingChatIsWritable || !canAskFullDocument || sending
                        }
                        onClick={() => void submitQuestion(question, [])}
                      >
                        {question}
                      </button>
                    ))}
                  </div>
                ) : null}
                {exactSelection ? (
                  <div className="reading-composer-reference">
                    <span>
                      {visualSelection
                        ? `已引用视觉识别文字${selectedSourceRefs.length > 1 ? ` · ${selectedSourceRefs.length} 段` : ""}`
                        : exactSelections.length > 1
                          ? `已引用 ${exactSelections.length} 段原文`
                          : "已引用原文"}
                    </span>
                    <p>{exactSelectionText.replace(/\s+/g, " ")}</p>
                    <button
                      type="button"
                      aria-label="移除原文引用"
                      onClick={() => {
                        setExactSelections([]);
                        setVisualSelection(null);
                        setPdfSelectionRequest(null);
                      }}
                    >
                      <X size={13} />
                    </button>
                  </div>
                ) : null}
                <div className="reading-composer">
                  <textarea
                    ref={composerRef}
                    aria-label="向当前文档提问"
                    value={draft}
                    rows={1}
                    maxLength={MAX_QUESTION_LENGTH}
                    disabled={!readingInputIsWritable}
                    placeholder={
                      readingInputIsWritable
                        ? aiReady !== true ? "可先写下问题，连接就绪后再发送…" : exactSelection
                          ? "围绕选中的原文提问…"
                          : fullDocumentQuestionGate
                            ? "先补齐不可读页面，或选中已读内容提问…"
                            : "问这份文档中的任何问题…"
                        : "当前文档暂时不可对话"
                    }
                    onChange={(event) => setDraft(event.target.value)}
                    onKeyDown={handleComposerKeyDown}
                  />
                  <button
                    type="button"
                    aria-label={sending ? "正在回答" : "发送问题"}
                    disabled={
                      !draft.trim() ||
                      !readingChatIsWritable ||
                      !canAskCurrentScope ||
                      sending
                    }
                    onClick={() => void submitQuestion()}
                  >
                    {sending ? (
                      <span className="reading-send-spinner" aria-hidden="true" />
                    ) : (
                      <SendHorizontal size={18} aria-hidden="true" />
                    )}
                  </button>
                </div>
                <p
                  className={cx(
                    fullDocumentQuestionGate &&
                      !selectedSourceRefs.length &&
                      "is-capture-gated",
                  )}
                >
                  {aiReady !== true ? "可先写下问题 · Agent 连接就绪后可发送" : selectedSourceRefs.length
                    ? "Enter 发送 · 仅围绕已选内容回答 · 请结合引用核对"
                    : fullDocumentQuestionGate ||
                      "Enter 发送 · 当前为全文可读文字回答模式，可停止等待 · 请结合引用核对"}
                </p>
              </footer>

              {historyOpen ? (
                <ConversationDrawer
                  conversations={conversations}
                  activeConversationId={activeConversationId}
                  loading={loadingConversations}
                  onClose={() => setHistoryOpen(false)}
                  onNew={startNewConversation}
                  onOpen={(summary) => void openConversation(summary)}
                  onDelete={(summary) => void deleteConversation(summary)}
                />
              ) : null}

              {auxiliaryView && context ? (
                <AuxiliaryDrawer
                  view={auxiliaryView}
                  context={context}
                  notes={notes}
                  readingHandoff={sourceMutationInProgress ? { ...readingHandoff, reason: "阅读来源正在更新，完成后再准备工作台草稿。" } : readingHandoff}
                  noteDraft={noteDraft}
                  exactSelection={
                    exactSelection
                      ? { ...exactSelection, text: exactSelectionText }
                      : null
                  }
                  editingNoteId={editingNoteId}
                  editingNoteText={editingNoteText}
                  busyNote={busyNote}
                  canWrite={readingIsWritable && !sourceMutationInProgress}
                  canAskFullDocument={canAskFullDocument && readingChatIsWritable && !sending}
                  onView={setAuxiliaryView}
                  onClose={() => setAuxiliaryView(null)}
                  onMapNode={(node) => {
                    const selectable = context.selectable_nodes.find(
                      (item) => item.source_node_id === node.source_node_id,
                    );
                    setPdfPage(
                      Number(node.page_start || selectable?.page_number || 1),
                    );
                    setMobilePane("source");
                    setAuxiliaryView(null);
                  }}
                  onAsk={(prompt) => void submitQuestion(prompt, [])}
                  onNoteDraft={setNoteDraft}
                  onSaveNote={() => void saveNote()}
                  onStartEdit={(note) => {
                    if (String(note.status || "").toUpperCase() !== "ACTIVE") {
                      return;
                    }
                    setEditingNoteId(note.note_id);
                    setEditingNoteText(note.content);
                  }}
                  onCancelEdit={() => {
                    setEditingNoteId("");
                    setEditingNoteText("");
                  }}
                  onEditingText={setEditingNoteText}
                  onSaveEdit={(note) => void saveEditedNote(note)}
                  onDeleteNote={(note) => void deleteNote(note)}
                  onJumpToSource={jumpToSource}
                />
              ) : null}
            </aside>
          </div>
        )}
      </ReadingContent>
    </div>
  );
}

function ChatWelcome({
  documentName,
  context,
  disabled,
  gateMessage,
  agentReady,
  onPrompt,
}: {
  documentName: string;
  context: ReadingContextV1;
  disabled: boolean;
  gateMessage: string;
  agentReady: boolean;
  onPrompt: (prompt: string, taskType: ReadingTaskType) => void;
}) {
  return (
    <div className="reading-chat-welcome">
      <div className="reading-welcome-orbit" aria-hidden="true">
        <span />
        <Sparkles size={24} />
      </div>
      <p className="section-label">ASK THE DOCUMENT</p>
      <h2>你想从这份文档了解什么？</h2>
      {!agentReady ? <p>先浏览左侧原件，或在下方写下问题。Agent 连接就绪后即可提问。</p> : gateMessage ? (
        <p className="reading-welcome-gate" role="status">
          {gateMessage}。补齐后可使用整文导读；现在仍可在左侧选中已读文字提问。
        </p>
      ) : (
        <p>
          直接提问即可，不需要先选择文字。AI 会围绕
          <strong> {documentName} </strong>
          回答，并把依据放在回答旁的可点击证据中。
        </p>
      )}
      <div className="reading-quick-prompts">
        {quickPrompts.map((item) => {
          const PromptIcon = item.icon;
          return (
            <button
              key={item.label}
              type="button"
              disabled={disabled}
              onClick={() => onPrompt(item.prompt, item.taskType)}
            >
              <PromptIcon size={17} aria-hidden="true" />
              <span>
                <strong>{item.label}</strong>
                <small>{item.description}</small>
              </span>
            </button>
          );
        })}
      </div>
      <dl className="reading-welcome-facts">
        <div>
          <dt>页数</dt>
          <dd>{context.overview.page_count || "—"}</dd>
        </div>
        <div>
          <dt>章节线索</dt>
          <dd>{context.overview.heading_count || 0}</dd>
        </div>
        <div>
          <dt>可引用片段</dt>
          <dd>{context.selectable_nodes.length}</dd>
        </div>
      </dl>
    </div>
  );
}

function ChatMessage({
  message,
  readingHandoff,
  captureManifest,
  onCitation,
  onSuggested,
  disableSuggestions,
  onRegenerate,
}: {
  message: ReadingChatMessageV1;
  readingHandoff: ReadingHandoff;
  captureManifest: CaptureQualityManifestV1 | null;
  onCitation: (citation: ReadingCitationV1) => void;
  onSuggested: (question: string) => void;
  disableSuggestions: boolean;
  onRegenerate?: () => void;
}) {
  if (message.role === "USER") {
    const sourceRefs = message.source_refs?.length
      ? message.source_refs
      : message.source_ref
        ? [message.source_ref]
        : [];
    return (
      <article className="reading-message is-user">
        <div className="reading-user-bubble">
          <p>{message.content}</p>
          {sourceRefs.length ? (
            <button
              className="reading-user-source-chip"
              type="button"
              onClick={() => {
                const sourceRef = sourceRefs[0]!;
                const visual =
                  String(sourceRef.kind || "").toUpperCase() === "VISUAL_TEXT";
                onCitation({
                  citation_id: `${message.message_id}-source-ref`,
                  locator_kind: visual ? "VISUAL_TEXT" : "SOURCE_TEXT",
                  source_node_id: sourceRef.source_node_id,
                  visual_snapshot_id: sourceRef.visual_snapshot_id,
                  observation_id: sourceRef.observation_id,
                  start_offset: sourceRef.start_offset,
                  end_offset: sourceRef.end_offset,
                  page_number: sourceRef.page_number,
                  bbox: sourceRef.bbox,
                  page_bbox: sourceRef.page_bbox,
                  page_image_sha256: sourceRef.page_image_sha256,
                  provider_id: sourceRef.provider_id,
                  provider_version: sourceRef.provider_version,
                  label: visual ? "提问引用的视觉识别文字" : "提问引用的原文",
                });
              }}
            >
              引用了 {sourceRefs.length} 段
              {String(sourceRefs[0]?.kind || "").toUpperCase() === "VISUAL_TEXT"
                ? "视觉识别文字"
                : "原文"}
              {" · 查看"}
            </button>
          ) : null}
        </div>
      </article>
    );
  }
  const citations = message.citations || [];
  const suggestions = message.suggested_questions?.length
    ? message.suggested_questions.slice(0, 3)
    : defaultFollowUps;
  return (
    <article
      className={cx(
        "reading-message is-assistant",
        String(message.status || "").toUpperCase() === "STALE" && "is-stale",
      )}
    >
      <div className="reading-assistant-content">
        <header>
          <span><Sparkles size={13} /> AI 阅读</span>
          <time>{formatConversationTime(message.created_at_utc)}</time>
        </header>
        {String(message.status || "").toUpperCase() === "STALE" ? (
          <p className="reading-stale-capture-note">旧捕获，仅供参考</p>
        ) : null}
        <ChatText
          content={message.content}
          citations={citations}
          onCitation={onCitation}
        />
        {citations.length ? (
          <EvidenceList citations={citations} onCitation={onCitation} />
        ) : null}
        {message.retrieval ? (
          <RetrievalCoverage
            retrieval={message.retrieval}
            captureManifest={captureManifest}
          />
        ) : null}
        {!citations.length ? (
          <p className="reading-no-citation">本回答未返回原文引用，请谨慎核对。</p>
        ) : null}
        <footer className="reading-answer-actions">
          <ReadingHandoffAction finding={{ kind: "message", value: message }} handoff={readingHandoff} />
          {onRegenerate ? (
            <button type="button" onClick={onRegenerate}>
              <Icon name="refresh" size={13} />
              重新回答
            </button>
          ) : null}
        </footer>
        <div className="reading-inline-suggestions">
            {suggestions.map((question) => (
              <button
                key={question}
                type="button"
                disabled={disableSuggestions}
                onClick={() => onSuggested(question)}
              >
                {question}
                <Icon name="arrow" size={13} />
              </button>
            ))}
        </div>
      </div>
    </article>
  );
}

function RetrievalCoverage({
  retrieval,
  captureManifest,
}: {
  retrieval: NonNullable<ReadingChatMessageV1["retrieval"]>;
  captureManifest: CaptureQualityManifestV1 | null;
}) {
  const pages = retrieval.covered_page_numbers || [];
  const isDirectFullDocument =
    [
      "source-ir-full-document/v2",
      "effective-source-full-document/v1",
    ].includes(retrieval.strategy) &&
    retrieval.coverage_mode === "FULL_DOCUMENT";
  const isHierarchicalFullDocument =
    retrieval.strategy === "source-ir-full-document/v2" &&
    retrieval.coverage_mode === "HIERARCHICAL_FULL_DOCUMENT";
  const isFullEffectiveText =
    retrieval.strategy === "effective-source-full-effective-text/v1" &&
    retrieval.coverage_mode === "FULL_EFFECTIVE_TEXT";
  const isSelectedContext =
    retrieval.strategy === "effective-source-selected-context/v1" &&
    retrieval.coverage_mode === "SELECTED_CONTEXT";
  const pageLabel = pages.length ? ` · ${pages.length} 页` : "";
  const readablePages = captureManifest?.pages.filter(
    (page) => page.readable_mode !== "NONE",
  ).length;
  const completeCapture = Boolean(
    captureManifest &&
      captureManifest.status === "READY" &&
      readablePages === captureManifest.page_count,
  );
  const selectedUnreadableCount =
    retrieval.unreadable_page_numbers?.length ??
    (captureManifest
      ? captureManifest.page_count - Number(readablePages || 0)
      : 0);
  const coverageLabel = isSelectedContext
    ? `仅基于已选内容回答${pageLabel}${selectedUnreadableCount ? ` · 全文仍有 ${selectedUnreadableCount} 页待识别` : ""}`
    : !captureManifest
    ? "已检索当前可读内容 · 逐页完整性待确认"
    : !completeCapture
      ? `已检索当前可读内容 · 可读 ${readablePages}/${captureManifest.page_count} 页 · ${captureManifest.page_count - Number(readablePages || 0)} 页待补齐`
      : isDirectFullDocument
        ? `已读取当前文档全部原生文字${pageLabel}`
        : isFullEffectiveText
          ? `基于全部可读文字层（含视觉识别），未解析无文字图像语义${pageLabel}`
        : isHierarchicalFullDocument
          ? `已分层读取当前文档文字${pageLabel}`
          : "历史回答未使用当前整文阅读方式，建议重新回答";
  return (
    <div
      className={cx(
        "reading-retrieval-coverage",
        isSelectedContext
          ? "is-selected-context"
          : !isDirectFullDocument &&
            !isFullEffectiveText &&
            !isHierarchicalFullDocument &&
            "is-legacy",
      )}
      role="status"
    >
      <BookOpenText size={15} aria-hidden="true" />
      <span>
        {coverageLabel}
      </span>
    </div>
  );
}

function citationIndexById(
  citations: ReadingCitationV1[],
): Map<string, { citation: ReadingCitationV1; index: number }> {
  return new Map(
    citations.map((citation, index) => [
      citation.citation_id,
      { citation, index: index + 1 },
    ]),
  );
}

function isVisualCitation(citation: ReadingCitationV1): boolean {
  return (
    ["OCR_TEXT", "VISUAL_TEXT", "VISUAL_REGION"].includes(
      String(citation.locator_kind || "").toUpperCase(),
    ) || String(citation.provenance_channel || "").toUpperCase() === "REMOTE_VISUAL"
  );
}

function citationVisibleLabel(
  citation: ReadingCitationV1,
  index: number,
): string {
  const page = citation.page_number ? ` · 第 ${citation.page_number} 页` : "";
  return `证据 ${index}${page}${isVisualCitation(citation) ? " · 视觉识别" : ""}`;
}

function inlineCitationAriaLabel(
  citation: ReadingCitationV1,
  index: number,
): string {
  if (isVisualCitation(citation)) {
    return `查看文中${citationVisibleLabel(citation, index)}`;
  }
  const page = citation.page_number ? ` · 第 ${citation.page_number} 页` : "";
  return `查看证据 ${index}${page}`;
}

function evidenceListCitationAriaLabel(
  citation: ReadingCitationV1,
  index: number,
): string {
  if (isVisualCitation(citation)) {
    return `查看${citationVisibleLabel(citation, index)}`;
  }
  const page = citation.page_number ? `，第 ${citation.page_number} 页` : "";
  return `查看证据 ${index}${page}`;
}

function ChatText({
  content,
  citations,
  onCitation,
}: {
  content: string;
  citations: ReadingCitationV1[];
  onCitation: (citation: ReadingCitationV1) => void;
}) {
  const citationLookup = citationIndexById(citations);
  const blocks = splitChatMarkdownBlocks(content);
  return (
    <div className="reading-chat-copy">
      {blocks.map((block, index) => {
        const lines = block.split("\n");
        const fencedCode = block.match(/^```[^\n]*\n([\s\S]*?)\n?```$/);
        if (fencedCode) {
          return (
            <pre key={`code-${index}`}>
              <code>{fencedCode[1]}</code>
            </pre>
          );
        }
        if (
          lines.length >= 2 &&
          isMarkdownTableDivider(lines[1] || "")
        ) {
          const header = markdownTableCells(lines[0] || "");
          const rows = lines.slice(2).map(markdownTableCells);
          return (
            <div className="reading-chat-table-scroll" key={`table-${index}`}>
              <table>
                <thead>
                  <tr>
                    {header.map((cell, cellIndex) => (
                      <th key={`${cell}-${cellIndex}`}>
                        {renderInlineMarkdown(cell, citationLookup, onCitation)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, rowIndex) => (
                    <tr key={`${row.join("|")}-${rowIndex}`}>
                      {header.map((_, cellIndex) => (
                        <td key={`${cellIndex}-${rowIndex}`}>
                          {renderInlineMarkdown(
                            row[cellIndex] || "",
                            citationLookup,
                            onCitation,
                          )}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }
        if (lines.every((line) => /^\s*>\s?/.test(line))) {
          return (
            <blockquote key={`quote-${index}`}>
              {renderInlineMarkdown(
                lines.map((line) => line.replace(/^\s*>\s?/, "")).join("\n"),
                citationLookup,
                onCitation,
              )}
            </blockquote>
          );
        }
        const isList = lines.every((line) => /^\s*(?:[-*•]|\d+[.)])\s+/.test(line));
        if (isList) {
          return (
            <ul key={`${block.slice(0, 20)}-${index}`}>
              {lines.map((line) => (
                <li key={line}>
                  {renderInlineMarkdown(
                    line.replace(/^\s*(?:[-*•]|\d+[.)])\s+/, ""),
                    citationLookup,
                    onCitation,
                  )}
                </li>
              ))}
            </ul>
          );
        }
        if (/^#{1,3}\s+/.test(block)) {
          return (
            <h3 key={`${block}-${index}`}>
              {renderInlineMarkdown(
                block.replace(/^#{1,3}\s+/, ""),
                citationLookup,
                onCitation,
              )}
            </h3>
          );
        }
        return (
          <p key={`${block.slice(0, 20)}-${index}`}>
            {renderInlineMarkdown(block, citationLookup, onCitation)}
          </p>
        );
      })}
    </div>
  );
}

type ChatMarkdownBlockKind = "paragraph" | "quote" | "list" | "table";

function splitChatMarkdownBlocks(content: string): string[] {
  const lines = content.replace(/\r\n?/g, "\n").split("\n");
  const blocks: string[] = [];
  let pendingLines: string[] = [];
  let pendingKind: ChatMarkdownBlockKind | null = null;

  const flushPending = () => {
    if (pendingLines.length) {
      blocks.push(pendingLines.join("\n"));
    }
    pendingLines = [];
    pendingKind = null;
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index] || "";
    const trimmed = line.trim();

    if (!trimmed) {
      flushPending();
      continue;
    }

    if (/^```/.test(trimmed)) {
      flushPending();
      const fencedLines = [line];
      while (index + 1 < lines.length) {
        index += 1;
        const fencedLine = lines[index] || "";
        fencedLines.push(fencedLine);
        if (/^```\s*$/.test(fencedLine.trim())) {
          break;
        }
      }
      blocks.push(fencedLines.join("\n"));
      continue;
    }

    if (/^#{1,3}\s+/.test(trimmed)) {
      flushPending();
      blocks.push(trimmed);
      continue;
    }

    const nextLine = lines[index + 1] || "";
    const kind: ChatMarkdownBlockKind = /^\s*>\s?/.test(line)
      ? "quote"
      : /^\s*(?:[-*•]|\d+[.)])\s+/.test(line)
        ? "list"
        : (pendingKind === "table" && line.includes("|")) ||
            (line.includes("|") && isMarkdownTableDivider(nextLine)) ||
            isMarkdownTableDivider(line)
          ? "table"
          : "paragraph";

    if (pendingKind && pendingKind !== kind) {
      flushPending();
    }
    pendingKind = kind;
    pendingLines.push(line);
  }

  flushPending();
  return blocks;
}

function markdownTableCells(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function isMarkdownTableDivider(line: string): boolean {
  const cells = markdownTableCells(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function renderInlineMarkdown(
  value: string,
  citations: Map<string, { citation: ReadingCitationV1; index: number }>,
  onCitation: (citation: ReadingCitationV1) => void,
) {
  const parts = value.split(
    /(\*\*[^*\n]+\*\*|`[^`\n]+`|【[^】\n]+】|\[R\d+\])/g,
  );
  return parts.map((part, index) => {
    if (/^\*\*[^*\n]+\*\*$/.test(part)) {
      return <strong key={`${part}-${index}`}>{part.slice(2, -2)}</strong>;
    }
    if (/^`[^`\n]+`$/.test(part)) {
      return <code key={`${part}-${index}`}>{part.slice(1, -1)}</code>;
    }
    const citationId =
      part.match(/^【([^】\n]+)】$/)?.[1] || part.match(/^\[(R\d+)\]$/)?.[1];
    const evidence = citationId ? citations.get(citationId) : undefined;
    if (evidence) {
      return (
        <button
          className="reading-inline-evidence"
          key={`${part}-${index}`}
          type="button"
          title={evidence.citation.quote || evidence.citation.label || "查看原文依据"}
          aria-label={inlineCitationAriaLabel(
            evidence.citation,
            evidence.index,
          )}
          onClick={() => onCitation(evidence.citation)}
        >
          {citationVisibleLabel(evidence.citation, evidence.index)}
        </button>
      );
    }
    if (citationId) return null;
    return part;
  });
}

function EvidenceList({
  citations,
  onCitation,
}: {
  citations: ReadingCitationV1[];
  onCitation: (citation: ReadingCitationV1) => void;
}) {
  return (
    <details className="reading-evidence-list">
      <summary>查看 {citations.length} 条原文依据</summary>
      <div>
        {citations.map((citation, index) => (
          <button
            key={citation.citation_id}
            type="button"
            aria-label={evidenceListCitationAriaLabel(citation, index + 1)}
            onClick={() => onCitation(citation)}
          >
            <strong>{citationVisibleLabel(citation, index + 1)}</strong>
            <span>
              {citation.locator_kind === "TABLE_CELL" ? "表格单元格 · " : ""}
              {isVisualCitation(citation) ? "视觉识别 · " : ""}
              {citation.page_number ? `第 ${citation.page_number} 页` : "页码未定位"}
            </span>
            <q>{citation.quote || citation.label || "查看对应原文"}</q>
          </button>
        ))}
      </div>
    </details>
  );
}

function OptimisticUserMessage({ question }: { question: OptimisticQuestion }) {
  return (
    <article className="reading-message is-user is-pending">
      <div className="reading-user-bubble">
        <p>{question.content}</p>
        {question.sourceRefs.length ? (
          <span>引用了 {question.sourceRefs.length} 处原文</span>
        ) : null}
      </div>
    </article>
  );
}

function AssistantThinking({ onStop }: { onStop: () => void }) {
  return (
    <article className="reading-message is-assistant is-thinking" role="status">
      <div>
        <span><i /><i /><i /></span>
        <p>正在阅读当前文档并等待完整回答…</p>
        <button type="button" onClick={onStop}>停止等待</button>
      </div>
    </article>
  );
}

function ChatFailureNotice({
  failure,
  onRetry,
  onDismiss,
}: {
  failure: ChatFailure;
  onRetry?: () => void;
  onDismiss: () => void;
}) {
  return (
    <div
      className={cx(
        "reading-chat-failure",
        failure.providerNotConfigured && "is-provider",
      )}
      role="alert"
    >
      <div>
        <strong>
          {failure.providerNotConfigured
            ? "阅读 Agent 尚未就绪"
            : failure.stopped
              ? "已停止等待"
              : "这次没有回答成功"}
        </strong>
        <p>
          {failure.providerNotConfigured
            ? failure.message
            : failure.message}
        </p>
      </div>
      <div>
        {onRetry && !failure.providerNotConfigured ? (
          <button type="button" onClick={onRetry}>重试</button>
        ) : null}
        <button type="button" aria-label="关闭错误提示" onClick={onDismiss}>
          <X size={14} />
        </button>
      </div>
    </div>
  );
}

function ConversationDrawer({
  conversations,
  activeConversationId,
  loading,
  onClose,
  onNew,
  onOpen,
  onDelete,
}: {
  conversations: ReadingConversationSummaryV1[];
  activeConversationId: string;
  loading: boolean;
  onClose: () => void;
  onNew: () => void;
  onOpen: (conversation: ReadingConversationSummaryV1) => void;
  onDelete: (conversation: ReadingConversationSummaryV1) => void;
}) {
  return (
    <Dialog open title="会话历史" onClose={onClose} className="reading-drawer-dialog">
      <section className="reading-drawer reading-history-drawer" aria-label="会话历史">
        <header>
          <div><small>THIS DOCUMENT</small><h3>会话历史</h3></div>
          <button type="button" aria-label="关闭会话历史" onClick={onClose}><X size={17} /></button>
        </header>
        <button className="reading-new-chat" type="button" onClick={onNew}>
          <MessageSquarePlus size={16} />
          新建会话
        </button>
        <div className="reading-history-list">
          {loading ? (
            <LoadingBlock label="正在载入会话" />
          ) : conversations.length ? (
            conversations.map((conversation) => (
              <div
                key={conversation.conversation_id}
                className={cx(
                  "reading-history-item",
                  conversation.conversation_id === activeConversationId && "is-active",
                )}
              >
                <button type="button" onClick={() => onOpen(conversation)}>
                  <strong>{conversation.title || "未命名会话"}</strong>
                  <span>
                    {String(conversation.status || "").toUpperCase() === "STALE"
                      ? "旧捕获，仅供参考 · "
                      : ""}
                    {formatConversationTime(conversation.last_message_at_utc || conversation.updated_at_utc)}
                  </span>
                </button>
                <button
                  type="button"
                  aria-label={`删除会话 ${conversation.title || "未命名会话"}`}
                  onClick={() => onDelete(conversation)}
                >
                  <Trash2 size={14} />
                </button>
              </div>
            ))
          ) : (
            <p className="reading-drawer-empty">还没有历史会话。提出第一个问题后，会话会保存在这里。</p>
          )}
        </div>
      </section>
    </Dialog>
  );
}

function AuxiliaryDrawer({
  view,
  context,
  notes,
  readingHandoff,
  noteDraft,
  exactSelection,
  editingNoteId,
  editingNoteText,
  busyNote,
  canWrite,
  canAskFullDocument,
  onView,
  onClose,
  onMapNode,
  onAsk,
  onNoteDraft,
  onSaveNote,
  onStartEdit,
  onCancelEdit,
  onEditingText,
  onSaveEdit,
  onDeleteNote,
  onJumpToSource,
}: {
  view: AuxiliaryView;
  context: ReadingContextV1;
  notes: ReadingNoteV1[];
  readingHandoff: ReadingHandoff;
  noteDraft: string;
  exactSelection: ExactSelection | null;
  editingNoteId: string;
  editingNoteText: string;
  busyNote: string;
  canWrite: boolean;
  canAskFullDocument: boolean;
  onView: (view: AuxiliaryView) => void;
  onClose: () => void;
  onMapNode: (node: ReadingStructureMapNodeV1) => void;
  onAsk: (prompt: string) => void;
  onNoteDraft: (value: string) => void;
  onSaveNote: () => void;
  onStartEdit: (note: ReadingNoteV1) => void;
  onCancelEdit: () => void;
  onEditingText: (value: string) => void;
  onSaveEdit: (note: ReadingNoteV1) => void;
  onDeleteNote: (note: ReadingNoteV1) => void;
  onJumpToSource: (sourceRef: ReadingSourceRef) => void;
}) {
  return (
    <Dialog open title="文档辅助信息" onClose={onClose} className="reading-drawer-dialog">
      <section className="reading-drawer reading-auxiliary-drawer" aria-label="文档辅助信息">
        <header>
          <div><small>DOCUMENT COMPANION</small><h3>文档辅助信息</h3></div>
          <button type="button" aria-label="关闭文档辅助信息" onClick={onClose}><X size={17} /></button>
        </header>
        <nav aria-label="文档辅助视图">
          <button type="button" className={cx(view === "overview" && "is-active")} onClick={() => onView("overview")}>概览</button>
          <button type="button" className={cx(view === "map" && "is-active")} onClick={() => onView("map")}>结构地图</button>
          <button type="button" className={cx(view === "notes" && "is-active")} onClick={() => onView("notes")}>笔记 {notes.length}</button>
        </nav>
        <div className="reading-drawer-body">
          {view === "overview" ? (
            <OverviewCompanion
              context={context}
              canAskFullDocument={canAskFullDocument}
              onAsk={onAsk}
            />
          ) : view === "map" ? (
            <MapCompanion nodes={context.structure_map.nodes || []} onChoose={onMapNode} />
          ) : (
            <NotesCompanion
              notes={notes}
              readingHandoff={readingHandoff}
              draft={noteDraft}
              selection={exactSelection}
              editingNoteId={editingNoteId}
              editingNoteText={editingNoteText}
              busyNote={busyNote}
              canWrite={canWrite}
              onDraft={onNoteDraft}
              onSave={onSaveNote}
              onStartEdit={onStartEdit}
              onCancelEdit={onCancelEdit}
              onEditingText={onEditingText}
              onSaveEdit={onSaveEdit}
              onDelete={onDeleteNote}
              onJump={onJumpToSource}
            />
          )}
        </div>
      </section>
    </Dialog>
  );
}

function OverviewCompanion({
  context,
  canAskFullDocument,
  onAsk,
}: {
  context: ReadingContextV1;
  canAskFullDocument: boolean;
  onAsk: (prompt: string) => void;
}) {
  const overview = context.overview;
  const type = String(
    overview.document_type_candidate?.title ||
      overview.document_type_candidate?.label ||
      "尚未判断",
  );
  return (
    <div className="reading-companion-stack">
      <dl className="reading-companion-metrics">
        <div><dt>类型</dt><dd>{type}</dd></div>
        <div><dt>页数</dt><dd>{overview.page_count || "—"}</dd></div>
        <div><dt>章节</dt><dd>{overview.heading_count || 0}</dd></div>
        <div><dt>表格</dt><dd>{overview.table_count || 0}</dd></div>
        <div><dt>图片</dt><dd>{overview.image_count || 0}</dd></div>
        <div><dt>捕获覆盖</dt><dd>{({ COMPLETE: "完整", PARTIAL: "部分", UNKNOWN: "未评估" } as Record<string, string>)[String(overview.coverage.status)] || "待核对"}</dd></div>
      </dl>
      <section className="reading-companion-card">
        <h4>结构提示</h4>
        <ul>
          {(overview.section_candidates || []).slice(0, 8).map((item, index) => (
            <li key={`${String(item.title)}-${index}`}>
              <span>{String(item.title || item.label || "未命名章节")}</span>
              <small>{item.page_start ? `第 ${item.page_start} 页` : "页码未定位"}</small>
            </li>
          ))}
        </ul>
        {!overview.section_candidates?.length ? <p>还没有章节提示。</p> : null}
      </section>
      <section className="reading-companion-card">
        <h4>需要留意</h4>
        <p>{overview.risk_regions.length ? `发现 ${overview.risk_regions.length} 处可能需要关注的区域。` : "当前已评估范围内没有特殊风险提示。"}</p>
        <button
          type="button"
          disabled={!canAskFullDocument}
          onClick={() =>
            onAsk("请结合全文解释这份文档的结构，并指出建议优先阅读的章节和原因。")
          }
        >
          让 AI 导读文档结构
        </button>
      </section>
    </div>
  );
}

function MapCompanion({
  nodes,
  onChoose,
}: {
  nodes: ReadingStructureMapNodeV1[];
  onChoose: (node: ReadingStructureMapNodeV1) => void;
}) {
  return nodes.length ? (
    <div>
    <p className="reading-drawer-empty">以下是来源捕获的定位线索；已保存的文档结构请在需求提取中核对。</p>
    <div className="reading-companion-map" role="tree" aria-label="阅读结构地图">
      {nodes.map((node) => (
        <button key={node.map_node_id} type="button" role="treeitem" onClick={() => onChoose(node)}>
          <span>{({ SECTION: "结构线索", TABLE: "表格", IMAGE: "图片", FIGURE: "图片", PAGE: "页面" } as Record<string, string>)[node.kind] || "原文线索"}</span>
          <strong>{node.title || "未命名结构线索"}</strong>
          <small>第 {node.page_start}{node.page_end !== node.page_start ? `–${node.page_end}` : ""} 页</small>
        </button>
      ))}
    </div>
    </div>
  ) : (
    <p className="reading-drawer-empty">当前文档还没有可定位的结构线索。你仍然可以直接向 AI 提问。</p>
  );
}

function NotesCompanion({
  notes,
  readingHandoff,
  draft,
  selection,
  editingNoteId,
  editingNoteText,
  busyNote,
  canWrite,
  onDraft,
  onSave,
  onStartEdit,
  onCancelEdit,
  onEditingText,
  onSaveEdit,
  onDelete,
  onJump,
}: {
  notes: ReadingNoteV1[];
  readingHandoff: ReadingHandoff;
  draft: string;
  selection: ExactSelection | null;
  editingNoteId: string;
  editingNoteText: string;
  busyNote: string;
  canWrite: boolean;
  onDraft: (value: string) => void;
  onSave: () => void;
  onStartEdit: (note: ReadingNoteV1) => void;
  onCancelEdit: () => void;
  onEditingText: (value: string) => void;
  onSaveEdit: (note: ReadingNoteV1) => void;
  onDelete: (note: ReadingNoteV1) => void;
  onJump: (sourceRef: ReadingSourceRef) => void;
}) {
  return (
    <div className="reading-companion-stack">
      <section className="reading-note-compose">
        <label htmlFor="reading-note-draft">给选中的原文记笔记</label>
        <textarea
          id="reading-note-draft"
          value={draft}
          disabled={!canWrite || !selection}
          placeholder={selection ? "记录理解、疑问或待核对事项" : "先在左侧选择一段原文"}
          onChange={(event) => onDraft(event.target.value)}
        />
        {selection ? <p>“{selection.text.replace(/\s+/g, " ").slice(0, 90)}”</p> : null}
        <button type="button" disabled={!canWrite || !selection || !draft.trim() || Boolean(busyNote)} onClick={onSave}>
          {busyNote === "create" ? "保存中…" : "保存笔记"}
        </button>
      </section>
      <div className="reading-companion-notes">
        {notes.length ? notes.map((note) => {
          const stale = String(note.status || "").toUpperCase() === "STALE";
          return (
          <article key={note.note_id} className={cx(stale && "is-stale")}>
            {stale ? (
              <small className="reading-note-stale-label">旧捕获，仅供参考</small>
            ) : null}
            {!stale && editingNoteId === note.note_id ? (
              <textarea aria-label="编辑阅读笔记" value={editingNoteText} onChange={(event) => onEditingText(event.target.value)} />
            ) : <p>{note.content}</p>}
            <footer>
              {stale ? (
                <span>不可编辑、删除或回到旧版原文</span>
              ) : (
              <><button type="button" onClick={() => onJump(note.source_ref)}>回到原文</button>
              {editingNoteId === note.note_id ? (
                <><button type="button" onClick={onCancelEdit}>取消</button><button type="button" disabled={!editingNoteText.trim() || Boolean(busyNote)} onClick={() => onSaveEdit(note)}>保存</button></>
              ) : (
                <><button type="button" onClick={() => onStartEdit(note)}>编辑</button><button type="button" disabled={Boolean(busyNote)} onClick={() => onDelete(note)}>删除</button></>
              )}</>
              )}
            </footer>
            {editingNoteId !== note.note_id && <ReadingHandoffAction finding={{ kind: "note", value: note }} handoff={readingHandoff} />}
          </article>
          );
        }) : <p className="reading-drawer-empty">还没有阅读笔记。</p>}
      </div>
    </div>
  );
}
