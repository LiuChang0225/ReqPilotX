import {
  act,
  cleanup,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../shared/api/client";
import type {
  ReadingChatMessageV1,
  ReadingChatTurnV1,
  ReadingContextV1,
  ReadingConversationSummaryV1,
  ReadingDocument,
  ReadingNoteV1,
  ReadingSourcePageNodeV1,
  ReadingSourcePageV1,
} from "./contracts";
import type {
  CaptureQualityManifestV1,
  VisualPageCaptureV1,
} from "../document-capture/contracts";
import {
  ReadingWorkspacePage,
  resolvePdfSourceSelections,
} from "./ReadingWorkspacePage";

const apiMocks = vi.hoisted(() => ({
  projects: vi.fn(),
  documents: vi.fn(),
  fileUrl: vi.fn(),
  context: vi.fn(),
  sourcePage: vi.fn(),
  prepare: vi.fn(),
  conversations: vi.fn(),
  createConversation: vi.fn(),
  conversationMessages: vi.fn(),
  sendMessage: vi.fn(),
  deleteConversation: vi.fn(),
  clearConversation: vi.fn(),
  aiReadiness: vi.fn(),
  createNote: vi.fn(),
  updateNote: vi.fn(),
  deleteNote: vi.fn(),
}));

const captureApiMocks = vi.hoisted(() => ({
  quality: vi.fn(),
  visualPage: vi.fn(),
  resumeVisualPage: vi.fn(),
}));

vi.mock("./readingApi", () => ({ readingApi: apiMocks }));
vi.mock("../document-capture/captureApi", () => ({
  captureApi: captureApiMocks,
}));

const PROJECT_ID = "project-1";
const SOURCE_TEXT = "要求😀盐雾试验";
const memoryStorageValues = new Map<string, string>();

const memoryLocalStorage: Storage = {
  clear: () => memoryStorageValues.clear(),
  getItem: (key) => memoryStorageValues.get(key) ?? null,
  key: (index) => Array.from(memoryStorageValues.keys())[index] ?? null,
  get length() {
    return memoryStorageValues.size;
  },
  removeItem: (key) => {
    memoryStorageValues.delete(key);
  },
  setItem: (key, value) => {
    memoryStorageValues.set(key, String(value));
  },
};

function documentFixture(id: string, sourceSha256: string): ReadingDocument {
  return {
    id,
    project_id: PROJECT_ID,
    file_name: `${id}.pdf`,
    file_type: "pdf",
    source_sha256: sourceSha256,
  };
}

function contextFixture(
  documentId: string,
  sourceSha256: string,
  pageNumber = 1,
): ReadingContextV1 {
  const sourceNodeId = `${documentId}-node-1`;
  return {
    schema_version: "reading-context/v1",
    reading_session_id: `${documentId}-session`,
    project_id: PROJECT_ID,
    sor_document_id: documentId,
    source_sha256: sourceSha256,
    source_ir_id: `${documentId}-ir`,
    source_ir_document_id: `${documentId}-source`,
    effective_bundle_id: `${documentId}-bundle`,
    visual_generation_id: null,
    status: "READY",
    overview: {
      schema_version: "document-reading-overview/v1",
      document_type_candidate: { title: "工程规范", confidence: 0.9 },
      document_format: "PDF",
      page_count: Math.max(1, pageNumber),
      node_count: 1,
      table_count: 0,
      image_count: 0,
      heading_count: 1,
      toc_candidates: [],
      section_candidates: [
        {
          title: "试验要求",
          page_start: pageNumber,
          page_end: pageNumber,
          confidence: 0.9,
        },
      ],
      body_candidate_ranges: [
        { page_start: 1, page_end: Math.max(1, pageNumber) },
      ],
      appendix_candidate_ranges: [],
      attachment_suspect_ranges: [],
      risk_regions: [],
      coverage: {
        status: "COMPLETE",
        evaluated_ranges: [
          { page_start: 1, page_end: Math.max(1, pageNumber) },
        ],
        unevaluated_ranges: [],
        blocked_ranges: [],
        reason_codes: [],
      },
    },
    structure_map: {
      schema_version: "reading-structure-map/v1",
      map_id: `${documentId}-map`,
      status: "READY",
      generation_status: "READY",
      coverage_status: "COMPLETE",
      page_count: Math.max(1, pageNumber),
      reason_codes: [],
      nodes: [
        {
          map_node_id: `${documentId}-map-1`,
          source_node_id: sourceNodeId,
          kind: "SECTION",
          title: "试验要求",
          level_hint: 1,
          source_order: 1,
          page_start: pageNumber,
          page_end: pageNumber,
          confidence: 0.9,
          reason_codes: [],
          evidence_status: "SOURCE_BOUND",
        },
      ],
    },
    selectable_nodes: [
      {
        source_node_id: sourceNodeId,
        kind: "PARAGRAPH",
        source_order: 1,
        page_number: pageNumber,
        text_length: Array.from(SOURCE_TEXT).length,
        preview: SOURCE_TEXT,
      },
    ],
    artifacts: [],
    notes: [],
    created_at_utc: "2026-08-13T00:00:00Z",
    updated_at_utc: "2026-08-13T00:00:00Z",
  };
}

function sourcePageFixture(
  context: ReadingContextV1,
  pageNumber: number,
): ReadingSourcePageV1 {
  const node = context.selectable_nodes.find(
    (item) => item.page_number === pageNumber,
  );
  return {
    schema_version: "reading-source-page/v1",
    project_id: context.project_id,
    sor_document_id: context.sor_document_id,
    reading_session_id: context.reading_session_id,
    source_ir_id: context.source_ir_id,
    source_sha256: context.source_sha256,
    effective_bundle_id: context.effective_bundle_id,
    visual_generation_id: context.visual_generation_id,
    page_number: pageNumber,
    page_count: context.overview.page_count,
    readable_mode: "NATIVE",
    nodes: node ? [{ ...node, text: SOURCE_TEXT }] : [],
    visual_runs: [],
  };
}

function conversationFixture(
  context: ReadingContextV1,
  id = "conversation-1",
  title = "盐雾试验是什么",
): ReadingConversationSummaryV1 {
  return {
    schema_version: "reading-conversation/v1",
    conversation_id: id,
    reading_session_id: context.reading_session_id,
    project_id: context.project_id,
    sor_document_id: context.sor_document_id,
    source_sha256: context.source_sha256,
    effective_bundle_id: context.effective_bundle_id,
    title,
    revision: 1,
    status: "ACTIVE",
    created_at_utc: "2026-08-13T00:00:00Z",
    updated_at_utc: "2026-08-13T00:00:01Z",
    last_message_at_utc: "2026-08-13T00:00:01Z",
  };
}

function noteFixture(
  context: ReadingContextV1,
  status: "ACTIVE" | "STALE" = "ACTIVE",
): ReadingNoteV1 {
  return {
    schema_version: "reading-note/v1",
    note_id: `${context.sor_document_id}-note-1`,
    reading_session_id: context.reading_session_id,
    project_id: context.project_id,
    sor_document_id: context.sor_document_id,
    source_sha256: context.source_sha256,
    source_ref: {
      source_node_id: `${context.sor_document_id}-node-1`,
      start_offset: 0,
      end_offset: 2,
    },
    content: "旧版捕获下保存的笔记",
    revision: 1,
    status,
    created_at_utc: "2026-08-13T00:00:00Z",
    updated_at_utc: "2026-08-13T00:00:01Z",
  };
}

function captureManifestFixture(
  context: ReadingContextV1,
): CaptureQualityManifestV1 {
  const pageCount = Math.max(1, context.overview.page_count);
  return {
    schema_version: "capture-quality-manifest/v1",
    project_id: context.project_id,
    sor_document_id: context.sor_document_id,
    source_sha256: context.source_sha256,
    base_capture_id: `${context.sor_document_id}-capture`,
    base_source_ir_id: context.source_ir_id,
    effective_bundle_id: `${context.sor_document_id}-bundle`,
    status: "READY",
    page_count: pageCount,
    native_ready_page_count: pageCount,
    visual_required_page_count: 0,
    visual_ready_page_count: 0,
    failed_page_count: 0,
    active_visual_generation_id: null,
    pages: Array.from({ length: pageCount }, (_, index) => ({
      page_number: index + 1,
      page_bbox: [0, 0, 595, 842],
      readable_mode: "NATIVE",
      capture_status: "NOT_REQUIRED",
      native_character_count: 20,
      native_cjk_character_count: 10,
      native_selectable: true,
      risk_codes: [],
      required_capability: "NONE",
      visual_snapshot_id: null,
      page_image_sha256: null,
      provider_id: null,
      provider_version: null,
      failure_code: null,
    })),
  };
}

function visualManifestFixture(
  context: ReadingContextV1,
): CaptureQualityManifestV1 {
  const manifest = captureManifestFixture(context);
  return {
    ...manifest,
    active_visual_generation_id: `${context.sor_document_id}-visual-generation`,
    native_ready_page_count: manifest.page_count - 1,
    visual_required_page_count: 1,
    visual_ready_page_count: 1,
    pages: manifest.pages.map((page) =>
      page.page_number === 1
        ? {
            ...page,
            readable_mode: "VISUAL",
            capture_status: "READY",
            native_character_count: 0,
            native_cjk_character_count: 0,
            native_selectable: false,
            risk_codes: ["VISUAL_TEXT_REQUIRED"],
            required_capability: "LAYOUT",
            visual_snapshot_id: `${context.sor_document_id}-visual-1`,
            page_image_sha256: "b".repeat(64),
            provider_id: "bailian",
            provider_version: "qwen-ocr-v1",
          }
        : page,
    ),
  };
}

function pendingVisualManifestFixture(
  context: ReadingContextV1,
): CaptureQualityManifestV1 {
  const manifest = captureManifestFixture(context);
  return {
    ...manifest,
    status: "PARTIAL",
    native_ready_page_count: Math.max(0, manifest.page_count - 1),
    visual_required_page_count: 1,
    visual_ready_page_count: 0,
    pages: manifest.pages.map((page) =>
      page.page_number === 1
        ? {
            ...page,
            readable_mode: "NONE",
            capture_status: "PENDING",
            native_character_count: 0,
            native_cjk_character_count: 0,
            native_selectable: false,
            risk_codes: ["VISUAL_TEXT_REQUIRED"],
            required_capability: "LAYOUT",
          }
        : page,
    ),
  };
}

function pendingTwoVisualPagesFixture(
  context: ReadingContextV1,
): CaptureQualityManifestV1 {
  const manifest = captureManifestFixture(context);
  return {
    ...manifest,
    status: "PARTIAL",
    native_ready_page_count: 0,
    visual_required_page_count: 2,
    visual_ready_page_count: 0,
    pages: manifest.pages.map((page) => ({
      ...page,
      readable_mode: "NONE",
      capture_status: "PENDING",
      native_character_count: 0,
      native_cjk_character_count: 0,
      native_selectable: false,
      risk_codes: ["VISUAL_TEXT_REQUIRED"],
      required_capability: "LAYOUT",
    })),
  };
}

function partialVisualManifestFixture(
  context: ReadingContextV1,
): CaptureQualityManifestV1 {
  const visual = visualManifestFixture(context);
  const secondPage = captureManifestFixture(context).pages[1]!;
  return {
    ...visual,
    status: "PARTIAL",
    native_ready_page_count: 0,
    visual_required_page_count: 2,
    visual_ready_page_count: 1,
    pages: [
      visual.pages[0]!,
      {
        ...secondPage,
        readable_mode: "NONE",
        capture_status: "PENDING",
        native_character_count: 0,
        native_cjk_character_count: 0,
        native_selectable: false,
        risk_codes: ["VISUAL_TEXT_REQUIRED"],
        required_capability: "LAYOUT",
      },
    ],
  };
}

function partialNativeManifestFixture(
  context: ReadingContextV1,
): CaptureQualityManifestV1 {
  const manifest = captureManifestFixture(context);
  const secondPage = manifest.pages[1]!;
  return {
    ...manifest,
    status: "PARTIAL",
    native_ready_page_count: 1,
    visual_required_page_count: 1,
    visual_ready_page_count: 0,
    pages: [
      manifest.pages[0]!,
      {
        ...secondPage,
        readable_mode: "NONE",
        capture_status: "PENDING",
        native_character_count: 0,
        native_cjk_character_count: 0,
        native_selectable: false,
        risk_codes: ["VISUAL_TEXT_REQUIRED"],
        required_capability: "LAYOUT",
      },
    ],
  };
}

function visualPageFixture(context: ReadingContextV1): VisualPageCaptureV1 {
  return {
    schema_version: "visual-page-capture/v1",
    generation_id: `${context.sor_document_id}-visual-generation`,
    visual_snapshot_id: `${context.sor_document_id}-visual-1`,
    source_sha256: context.source_sha256,
    base_capture_id: `${context.sor_document_id}-capture`,
    page_number: 1,
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
    readable_mode: "VISUAL",
    runs: [
      {
        run_id: "visual-run-1",
        observation_id: "visual-observation-1",
        text: "电涡流传感器",
        bbox: [40, 80, 240, 110],
        content_type: "TEXT",
        reading_order: 1,
        confidence: 0.92,
        source_ref: {
          kind: "VISUAL_TEXT",
          visual_snapshot_id: `${context.sor_document_id}-visual-1`,
          observation_id: "visual-observation-1",
          start_offset: 0,
          end_offset: 6,
          page_number: 1,
          bbox: [40, 80, 240, 110],
          page_bbox: [0, 0, 595, 842],
          page_image_sha256: "b".repeat(64),
          provider_id: "bailian",
          provider_version: "qwen-ocr-v1",
        },
      },
    ],
  };
}

function visualSelectionRef(context: ReadingContextV1) {
  return {
    kind: "VISUAL_TEXT",
    visual_snapshot_id: `${context.sor_document_id}-visual-1`,
    observation_id: "visual-observation-1",
    start_offset: 0,
    end_offset: 3,
    page_number: 1,
    bbox: [40, 80, 240, 110],
    page_bbox: [0, 0, 595, 842],
    page_image_sha256: "b".repeat(64),
    provider_id: "bailian",
    provider_version: "qwen-ocr-v1",
  } as const;
}

function dispatchPdfMessage(
  frame: HTMLIFrameElement,
  data: Record<string, unknown>,
  source: MessageEventSource | null = frame.contentWindow,
) {
  window.dispatchEvent(
    new MessageEvent("message", {
      origin: window.location.origin,
      source,
      data,
    }),
  );
}

function chatMessage(
  conversation: ReadingConversationSummaryV1,
  role: "USER" | "ASSISTANT",
  content: string,
  id: string,
  sourceNodeId?: string,
): ReadingChatMessageV1 {
  return {
    schema_version: "reading-chat-message/v1",
    message_id: id,
    conversation_id: conversation.conversation_id,
    reading_session_id: conversation.reading_session_id,
    project_id: conversation.project_id,
    sor_document_id: conversation.sor_document_id,
    source_sha256: conversation.source_sha256,
    effective_bundle_id: conversation.effective_bundle_id,
    sequence_number: role === "USER" ? 1 : 2,
    role,
    content,
    citations:
      role === "ASSISTANT" && sourceNodeId
        ? [
            {
              citation_id: `${id}-citation`,
              source_node_id: sourceNodeId,
              page_number: 1,
              start_offset: 0,
              end_offset: 2,
              quote: "要求",
              label: "试验要求",
            },
          ]
        : [],
    status: "ACTIVE",
    created_at_utc: "2026-08-13T00:00:01Z",
  };
}

function turnFixture(
  conversation: ReadingConversationSummaryV1,
  question: string,
  answer: string,
  turnNumber = 1,
): ReadingChatTurnV1 {
  return {
    schema_version: "reading-chat-turn/v1",
    conversation: {
      ...conversation,
      title: question,
      revision: conversation.revision + turnNumber,
      updated_at_utc: `2026-08-13T00:00:0${turnNumber + 1}Z`,
    },
    user_message: chatMessage(
      conversation,
      "USER",
      question,
      `user-${turnNumber}`,
    ),
    assistant_message: chatMessage(
      conversation,
      "ASSISTANT",
      answer,
      `assistant-${turnNumber}`,
      `${conversation.sor_document_id}-node-1`,
    ),
    retrieval: {
      schema_version: "reading-retrieval/v1",
      strategy: "source-ir-full-document/v2",
      selected_node_count: 1,
      selected_character_count: Array.from(SOURCE_TEXT).length,
      document_node_count: 1,
      covered_page_numbers: [1],
      coverage_mode: "FULL_DOCUMENT",
    },
  };
}

function configureDocuments(documents: ReadingDocument[]) {
  const contexts = new Map(
    documents.map((item) => [
      item.id,
      contextFixture(item.id, String(item.source_sha256)),
    ]),
  );
  apiMocks.projects.mockResolvedValue([
    { id: PROJECT_ID, name: "工程项目", customer_name: "客户 A" },
  ]);
  apiMocks.documents.mockResolvedValue(documents);
  apiMocks.fileUrl.mockImplementation(
    (projectId: string, documentId: string) =>
      `/api/projects/${projectId}/sor-documents/${documentId}/file`,
  );
  apiMocks.context.mockImplementation(
    async (_projectId: string, documentId: string) => contexts.get(documentId),
  );
  apiMocks.sourcePage.mockImplementation(
    async (_projectId: string, documentId: string, pageNumber: number) => {
      const context = contexts.get(documentId);
      if (!context) throw new Error("unknown document");
      return sourcePageFixture(context, pageNumber);
    },
  );
  apiMocks.conversations.mockResolvedValue({
    schema_version: "reading-conversation-list/v1",
    items: [],
  });
  apiMocks.aiReadiness.mockResolvedValue({
    schema_version: "reading-ai-readiness/v1",
    ready: true,
    provider_id: "test-provider",
    model_name: "test-model",
    configuration_source: "READING_OVERRIDE",
    reason_code: null,
  });
  captureApiMocks.quality.mockImplementation(
    async (_projectId: string, documentId: string) => {
      const context = contexts.get(documentId);
      if (!context) throw new Error("unknown document");
      return captureManifestFixture(context);
    },
  );
  return contexts;
}

async function enabledComposer() {
  const composer = await screen.findByRole("textbox", {
    name: "向当前文档提问",
  });
  await waitFor(() => expect(composer).toBeEnabled());
  return composer;
}

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function(this: HTMLDialogElement) { this.setAttribute("open", ""); } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function(this: HTMLDialogElement) { this.removeAttribute("open"); this.dispatchEvent(new Event("close")); } });

  Object.values(apiMocks).forEach((mock) => mock.mockReset());
  Object.values(captureApiMocks).forEach((mock) => mock.mockReset());
  memoryStorageValues.clear();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: memoryLocalStorage,
  });
  window.history.replaceState(
    null,
    "",
    `/workspace?task=reading&projectId=${PROJECT_ID}&documentId=document-1`,
  );
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(() => {
  window.getSelection()?.removeAllRanges();
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

describe("ReadingWorkspacePage conversation-first reader", () => {
  it("keeps source selection and reading tools when embedded without duplicating app navigation", async () => {
    configureDocuments([documentFixture("document-1", "a".repeat(64))]);
    render(<ReadingWorkspacePage embedded />);
    await enabledComposer();
    expect(screen.getByRole("region", { name: "文档阅读工作区" })).toBeVisible();
    expect(screen.getByRole("combobox", { name: "当前阅读文档" })).toHaveValue("document-1");
    expect(screen.queryByRole("combobox", { name: "阅读项目" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Schaeffler ReqPilotX" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "会话历史" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "文档辅助信息" })).toBeEnabled();
    expect(apiMocks.sendMessage).not.toHaveBeenCalled();
  });
  it("persists a right-pane reading size without changing the document toolbar", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    configureDocuments([document]);
    const user = userEvent.setup();

    const { container } = render(<ReadingWorkspacePage />);
    await enabledComposer();
    const pdfFrame = screen.getByTitle("当前文档原文 · document-1.pdf");
    expect(pdfFrame.getAttribute("src")).toContain("selection=reading");
    expect(pdfFrame).toHaveAttribute("allow", "clipboard-write");
    await user.click(screen.getByRole("button", { name: "大字号" }));

    expect(container.querySelector(".reading-page")).toHaveAttribute(
      "data-text-size",
      "large",
    );
    expect(window.localStorage.getItem("reqpilotx.reader.text-size")).toBe(
      "large",
    );
    expect(screen.getByRole("button", { name: "大字号" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("prepares a missing reading context without creating a chat", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    apiMocks.context.mockRejectedValue(
      new ApiError("Not prepared", 404, {
        detail: { reason_code: "reading_context_not_prepared" },
      }),
    );
    apiMocks.prepare.mockResolvedValue(context);

    render(<ReadingWorkspacePage />);

    expect(
      await screen.findByRole("heading", { name: "你想从这份文档了解什么？" }),
    ).toBeInTheDocument();
    expect(apiMocks.prepare).toHaveBeenCalledWith(
      PROJECT_ID,
      document.id,
      {
        schema_version: "reading-prepare-request/v1",
        expected_source_sha256: "a".repeat(64),
      },
      expect.any(AbortSignal),
    );
    expect(apiMocks.createConversation).not.toHaveBeenCalled();
    expect(apiMocks.sendMessage).not.toHaveBeenCalled();
  });

  it("prepares a legacy empty-bundle context only for its explicit 409 reason", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    apiMocks.context.mockRejectedValue(
      new ApiError("Legacy session bundle changed", 409, {
        detail: {
          reason_code: "reading_context_bundle_changed",
          stored_effective_bundle_id: "",
          current_effective_bundle_id: context.effective_bundle_id,
        },
      }),
    );
    apiMocks.prepare.mockResolvedValue(context);

    render(<ReadingWorkspacePage />);

    expect(
      await screen.findByRole("heading", { name: "你想从这份文档了解什么？" }),
    ).toBeInTheDocument();
    expect(apiMocks.context).toHaveBeenCalledOnce();
    expect(apiMocks.prepare).toHaveBeenCalledWith(
      PROJECT_ID,
      document.id,
      {
        schema_version: "reading-prepare-request/v1",
        expected_source_sha256: "a".repeat(64),
      },
      expect.any(AbortSignal),
    );
    expect(apiMocks.createConversation).not.toHaveBeenCalled();
    expect(apiMocks.sendMessage).not.toHaveBeenCalled();
  });

  it("does not auto-prepare unrelated reading context conflicts", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    configureDocuments([document]);
    apiMocks.context.mockRejectedValue(
      new ApiError("Conflict", 409, {
        detail: { reason_code: "reading_context_source_changed" },
      }),
    );

    render(<ReadingWorkspacePage />);

    expect(await screen.findByText("Conflict")).toBeInTheDocument();
    expect(apiMocks.prepare).not.toHaveBeenCalled();
  });

  it("sends an arbitrary whole-document question without a selection", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    const conversation = conversationFixture(context);
    apiMocks.createConversation.mockResolvedValue(conversation);
    apiMocks.sendMessage.mockResolvedValue(
      turnFixture(conversation, "这份文档主要讲什么？", "文档规定了试验要求。"),
    );
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);

    const composer = await enabledComposer();
    await user.type(composer, "这份文档主要讲什么？");
    expect(composer).toHaveValue("这份文档主要讲什么？");
    const sendButton = screen.getByRole("button", { name: "发送问题" });
    expect(sendButton).toBeEnabled();
    await user.click(sendButton);

    await waitFor(() => expect(apiMocks.createConversation).toHaveBeenCalledOnce());

    await waitFor(() => expect(apiMocks.sendMessage).toHaveBeenCalledOnce());
    expect(apiMocks.createConversation).toHaveBeenCalledWith(
      PROJECT_ID,
      document.id,
      {
        schema_version: "reading-conversation-create-request/v1",
        title: "这份文档主要讲什么？",
      },
      expect.any(AbortSignal),
    );
    expect(apiMocks.sendMessage).toHaveBeenCalledWith(
      PROJECT_ID,
      document.id,
      conversation.conversation_id,
      {
        schema_version: "reading-message-create-request/v1",
        source_sha256: context.source_sha256,
        content: "这份文档主要讲什么？",
        task_type: "FREE_QUESTION",
      },
      expect.any(AbortSignal),
    );
    expect(await screen.findByText("文档规定了试验要求。")).toBeInTheDocument();
    expect(
      screen.getByText("已读取当前文档全部原生文字 · 1 页"),
    ).toBeInTheDocument();
  });

  it("labels complete visual text coverage without claiming image understanding", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    context.visual_generation_id = `${context.sor_document_id}-visual-generation`;
    captureApiMocks.quality.mockResolvedValue(visualManifestFixture(context));
    captureApiMocks.visualPage.mockResolvedValue(visualPageFixture(context));
    const conversation = conversationFixture(context);
    apiMocks.createConversation.mockResolvedValue(conversation);
    const turn = turnFixture(conversation, "总结可读文字", "这是视觉文字总结。");
    turn.retrieval = {
      ...turn.retrieval,
      strategy: "effective-source-full-effective-text/v1",
      coverage_mode: "FULL_EFFECTIVE_TEXT",
      effective_bundle_id: context.effective_bundle_id,
      visual_generation_id: context.visual_generation_id,
      capture_quality_status: "READY",
      unreadable_page_numbers: [],
    };
    turn.assistant_message.retrieval = turn.retrieval;
    apiMocks.sendMessage.mockResolvedValue(turn);
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    const composer = await enabledComposer();
    await user.type(composer, "总结可读文字");
    await user.keyboard("{Enter}");

    expect(await screen.findByText("这是视觉文字总结。")).toBeInTheDocument();
    expect(
      screen.getByText(
        "基于全部可读文字层（含视觉识别），未解析无文字图像语义 · 1 页",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/已完整阅读/)).not.toBeInTheDocument();
  });

  it.each(["docx", "xlsx"])(
    "keeps %s whole-document chat available with a strict zero-page manifest",
    async (extension) => {
      const document = {
        ...documentFixture("document-1", "a".repeat(64)),
        file_name: `document-1.${extension}`,
        file_type: extension,
      };
      const context = configureDocuments([document]).get(document.id)!;
      captureApiMocks.quality.mockResolvedValue({
        ...captureManifestFixture(context),
        status: "READY",
        page_count: 0,
        native_ready_page_count: 0,
        visual_required_page_count: 0,
        visual_ready_page_count: 0,
        failed_page_count: 0,
        pages: [],
      });
      const conversation = conversationFixture(context);
      apiMocks.createConversation.mockResolvedValue(conversation);
      apiMocks.sendMessage.mockResolvedValue(
        turnFixture(conversation, "总结当前文档", "非 PDF 文档回答。"),
      );
      const user = userEvent.setup();

      render(<ReadingWorkspacePage />);
      const composer = await enabledComposer();
      expect(
        screen.queryByRole("button", { name: /文档可读性/ }),
      ).not.toBeInTheDocument();
      await user.type(composer, "总结当前文档");
      await user.keyboard("{Enter}");

      expect(await screen.findByText("非 PDF 文档回答。")).toBeInTheDocument();
      expect(captureApiMocks.resumeVisualPage).not.toHaveBeenCalled();
    },
  );

  it("fails closed for whole-document questions while one page is unreadable", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    context.overview.page_count = 2;
    context.structure_map.page_count = 2;
    captureApiMocks.quality.mockResolvedValue(
      partialNativeManifestFixture(context),
    );
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    const composer = await enabledComposer();
    expect(
      await screen.findAllByText(
        /还有 1 页未完成视觉识别，可先选中已读内容提问/,
      ),
    ).not.toHaveLength(0);
    expect(screen.getByRole("button", { name: /快速读懂/ })).toBeDisabled();
    await user.type(composer, "请总结全文");
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
    await user.keyboard("{Enter}");
    expect(apiMocks.createConversation).not.toHaveBeenCalled();
    expect(apiMocks.sendMessage).not.toHaveBeenCalled();
  });

  it("distinguishes capture inspection from an incomplete manifest gate", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    configureDocuments([document]);
    captureApiMocks.quality.mockReturnValue(new Promise(() => undefined));

    render(<ReadingWorkspacePage />);
    await enabledComposer();
    expect(
      await screen.findAllByText("正在检查可读性，暂不可全文提问"),
    ).not.toHaveLength(0);
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
  });

  it("allows a visual selection as SELECTED_CONTEXT while whole-document capture is partial", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    context.overview.page_count = 2;
    context.structure_map.page_count = 2;
    context.visual_generation_id = `${context.sor_document_id}-visual-generation`;
    const partialManifest = partialVisualManifestFixture(context);
    captureApiMocks.quality.mockResolvedValue(partialManifest);
    captureApiMocks.visualPage.mockResolvedValue(visualPageFixture(context));
    const conversation = conversationFixture(context);
    apiMocks.createConversation.mockResolvedValue(conversation);
    const turn = turnFixture(
      conversation,
      "请解释我选中的这段原文。先用通俗语言复述，再说明它在当前章节中的作用、成立条件和对工程执行的影响；不要脱离原文补写结论。",
      "这是选中内容的解释。",
    );
    turn.retrieval = {
      ...turn.retrieval,
      strategy: "effective-source-selected-context/v1",
      coverage_mode: "SELECTED_CONTEXT",
      effective_bundle_id: context.effective_bundle_id,
      visual_generation_id: context.visual_generation_id,
      capture_quality_status: "PARTIAL",
      unreadable_page_numbers: [2],
    };
    turn.assistant_message.retrieval = turn.retrieval;
    apiMocks.sendMessage.mockResolvedValue(turn);

    render(<ReadingWorkspacePage />);
    await enabledComposer();
    await waitFor(() => expect(captureApiMocks.visualPage).toHaveBeenCalledOnce());
    const frame = screen.getByTitle(
      "当前文档原文 · document-1.pdf",
    ) as HTMLIFrameElement;
    const sourceRef = visualSelectionRef(context);
    act(() => {
      dispatchPdfMessage(frame, {
        type: "pdf-selection-action",
        selectionId: "partial-visual-selection",
        page: 1,
        text: "电涡流",
        action: "EXPLAIN",
        sourceRefs: [sourceRef],
        provenanceChannel: "REMOTE_VISUAL",
        sourceSha256: context.source_sha256,
        pageImageSha256: sourceRef.page_image_sha256,
        visualSnapshotId: sourceRef.visual_snapshot_id,
      });
    });

    await waitFor(() => expect(apiMocks.sendMessage).toHaveBeenCalledOnce());
    expect(apiMocks.sendMessage.mock.calls[0]?.[3]).toMatchObject({
      source_refs: [sourceRef],
    });
    expect(await screen.findByText("这是选中内容的解释。")).toBeInTheDocument();
    expect(
      await screen.findByText(
        "仅基于已选内容回答 · 1 页 · 全文仍有 1 页待识别",
      ),
    ).toBeInTheDocument();
  });

  it("accepts a source-bound visual selection action without string reverse lookup", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    const conversation = conversationFixture(context);
    captureApiMocks.quality.mockResolvedValue(visualManifestFixture(context));
    captureApiMocks.visualPage.mockResolvedValue(visualPageFixture(context));
    apiMocks.createConversation.mockResolvedValue(conversation);
    apiMocks.sendMessage.mockResolvedValue(
      turnFixture(conversation, "翻译视觉选区", "已翻译视觉选区。"),
    );

    render(<ReadingWorkspacePage />);
    await enabledComposer();
    await waitFor(() => expect(captureApiMocks.visualPage).toHaveBeenCalledOnce());
    const frame = screen.getByTitle(
      "当前文档原文 · document-1.pdf",
    ) as HTMLIFrameElement;
    const sourceRef = visualSelectionRef(context);

    act(() => {
      dispatchPdfMessage(frame, {
        type: "pdf-selection-action",
        selectionId: "visual-selection-1",
        page: 1,
        text: "电涡流",
        action: "TRANSLATE",
        sourceRefs: [sourceRef],
        provenanceChannel: "REMOTE_VISUAL",
        sourceSha256: context.source_sha256,
        pageImageSha256: sourceRef.page_image_sha256,
        visualSnapshotId: sourceRef.visual_snapshot_id,
      });
    });

    await waitFor(() => expect(apiMocks.sendMessage).toHaveBeenCalledOnce());
    expect(apiMocks.sendMessage.mock.calls[0]?.[3]).toMatchObject({
      task_type: "FREE_QUESTION",
      source_sha256: context.source_sha256,
      source_refs: [sourceRef],
    });
    expect(apiMocks.sendMessage.mock.calls[0]?.[3].content).toContain(
      "请翻译我选中的这段原文",
    );
    expect(apiMocks.sourcePage).toHaveBeenCalled();
    expect(apiMocks.sendMessage.mock.calls[0]?.[3].source_refs?.[0]).not.toHaveProperty(
      "source_node_id",
    );
  });

  it("rejects visual selection messages with a mismatched frame, source, or page", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    captureApiMocks.quality.mockResolvedValue(visualManifestFixture(context));
    captureApiMocks.visualPage.mockResolvedValue(visualPageFixture(context));

    render(<ReadingWorkspacePage />);
    await enabledComposer();
    await waitFor(() => expect(captureApiMocks.visualPage).toHaveBeenCalledOnce());
    const frame = screen.getByTitle(
      "当前文档原文 · document-1.pdf",
    ) as HTMLIFrameElement;
    const sourceRef = visualSelectionRef(context);
    const baseMessage = {
      type: "pdf-selection-action",
      selectionId: "visual-selection-rejected",
      page: 1,
      text: "电涡流",
      action: "TRANSLATE",
      sourceRefs: [sourceRef],
      provenanceChannel: "REMOTE_VISUAL",
      sourceSha256: context.source_sha256,
      pageImageSha256: sourceRef.page_image_sha256,
      visualSnapshotId: sourceRef.visual_snapshot_id,
    };

    act(() => {
      dispatchPdfMessage(frame, baseMessage, window);
      dispatchPdfMessage(frame, {
        ...baseMessage,
        selectionId: "wrong-source",
        sourceSha256: "c".repeat(64),
      });
      dispatchPdfMessage(frame, {
        ...baseMessage,
        selectionId: "wrong-page",
        page: 2,
      });
    });

    await act(async () => Promise.resolve());
    expect(apiMocks.createConversation).not.toHaveBeenCalled();
    expect(apiMocks.sendMessage).not.toHaveBeenCalled();
    expect(screen.queryByText(/已选取第 1 页视觉识别文字/)).not.toBeInTheDocument();
  });

  it("keeps consecutive follow-up questions in the same conversation", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    const conversation = conversationFixture(context);
    apiMocks.createConversation.mockResolvedValue(conversation);
    apiMocks.sendMessage
      .mockResolvedValueOnce(turnFixture(conversation, "先总结", "这是第一轮回答。", 1))
      .mockResolvedValueOnce(turnFixture(conversation, "再解释第二章", "这是连续追问。", 2));
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    const composer = await enabledComposer();
    await user.type(composer, "先总结");
    await user.keyboard("{Enter}");
    expect(await screen.findByText("这是第一轮回答。")).toBeInTheDocument();

    await user.type(composer, "再解释第二章");
    await user.keyboard("{Enter}");
    expect(await screen.findByText("这是连续追问。")).toBeInTheDocument();

    expect(apiMocks.createConversation).toHaveBeenCalledOnce();
    expect(apiMocks.sendMessage).toHaveBeenCalledTimes(2);
    expect(apiMocks.sendMessage.mock.calls[0]?.[2]).toBe(conversation.conversation_id);
    expect(apiMocks.sendMessage.mock.calls[1]?.[2]).toBe(conversation.conversation_id);
  });

  it("loads persisted conversation history and messages on refresh", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    const conversation = conversationFixture(context, "saved-chat", "历史问题");
    apiMocks.conversations.mockResolvedValue({
      schema_version: "reading-conversation-list/v1",
      items: [conversation],
    });
    const restoredAssistant = chatMessage(
      conversation,
      "ASSISTANT",
      "这是刷新后恢复的回答。",
      "saved-assistant",
      "document-1-node-1",
    );
    restoredAssistant.retrieval = {
      schema_version: "reading-retrieval/v1",
      strategy: "source-ir-full-document/v2",
      selected_node_count: 1,
      selected_character_count: 8,
      document_node_count: 6,
      covered_page_numbers: [1],
      coverage_mode: "HIERARCHICAL_FULL_DOCUMENT",
    };
    apiMocks.conversationMessages.mockResolvedValue({
      schema_version: "reading-message-list/v1",
      conversation_id: conversation.conversation_id,
      items: [
        {
          ...chatMessage(conversation, "USER", "历史问题", "saved-user"),
          source_ref: {
            source_node_id: "document-1-node-1",
            start_offset: 0,
            end_offset: 2,
          },
        },
        restoredAssistant,
      ],
    });
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);

    expect(await screen.findByText("这是刷新后恢复的回答。")).toBeInTheDocument();
    expect(screen.getByText("已分层读取当前文档文字 · 1 页")).toBeInTheDocument();
    expect(apiMocks.conversationMessages).toHaveBeenCalledWith(
      PROJECT_ID,
      document.id,
      conversation.conversation_id,
      expect.any(AbortSignal),
    );
    expect(
      screen.getByRole("button", { name: "引用了 1 段原文 · 查看" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "会话历史" }));
    expect(screen.getAllByText("历史问题").length).toBeGreaterThanOrEqual(2);
  });

  it("renders common Markdown tables and quotations without HTML injection", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    const conversation = conversationFixture(context);
    const answer = chatMessage(
      conversation,
      "ASSISTANT",
      [
        "## 检查结果",
        "> 已核对整份文档。",
        "| 项目 | **结论** |\n| --- | --- |\n| 盐雾 | `通过` |",
        "```text\n<script>not executed</script>\n```",
      ].join("\n\n"),
      "assistant-markdown",
      "document-1-node-1",
    );
    answer.retrieval = {
      schema_version: "reading-retrieval/v1",
      strategy: "source-ir-full-document/v2",
      selected_node_count: 1,
      selected_character_count: 8,
      document_node_count: 1,
      covered_page_numbers: [1],
      coverage_mode: "FULL_DOCUMENT",
    };
    apiMocks.conversations.mockResolvedValue({
      schema_version: "reading-conversation-list/v1",
      items: [conversation],
    });
    apiMocks.conversationMessages.mockResolvedValue({
      schema_version: "reading-message-list/v1",
      conversation_id: conversation.conversation_id,
      items: [answer],
    });

    const { container } = render(<ReadingWorkspacePage />);

    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "检查结果" })).toBeInTheDocument();
    expect(screen.getByText("已核对整份文档。").closest("blockquote")).not.toBeNull();
    expect(screen.getByText("结论").tagName).toBe("STRONG");
    expect(screen.getByText("通过").tagName).toBe("CODE");
    expect(screen.getByText("<script>not executed</script>")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
  });

  it("keeps headings separate from following Markdown without blank lines", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    const conversation = conversationFixture(context);
    const answer = chatMessage(
      conversation,
      "ASSISTANT",
      [
        "### 关键条件",
        "先读本段。",
        "- 条件一",
        "- 条件二",
        "### 后续章节",
        "| 项目 | 结论 |",
        "| --- | --- |",
        "| 温度 | 通过 |",
        "最后补充。",
      ].join("\n"),
      "assistant-compact-markdown",
      "document-1-node-1",
    );
    apiMocks.conversations.mockResolvedValue({
      schema_version: "reading-conversation-list/v1",
      items: [conversation],
    });
    apiMocks.conversationMessages.mockResolvedValue({
      schema_version: "reading-message-list/v1",
      conversation_id: conversation.conversation_id,
      items: [answer],
    });

    render(<ReadingWorkspacePage />);

    const firstHeading = await screen.findByRole("heading", { name: "关键条件" });
    expect(firstHeading).toHaveTextContent(/^关键条件$/);
    expect(screen.getByRole("heading", { name: "后续章节" })).toHaveTextContent(
      /^后续章节$/,
    );
    expect(screen.getByText("先读本段。").tagName).toBe("P");
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("最后补充。").tagName).toBe("P");
  });

  it("clears messages while keeping the active conversation", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    const conversation = conversationFixture(context);
    apiMocks.conversations.mockResolvedValue({
      schema_version: "reading-conversation-list/v1",
      items: [conversation],
    });
    apiMocks.conversationMessages.mockResolvedValue({
      schema_version: "reading-message-list/v1",
      conversation_id: conversation.conversation_id,
      items: [
        chatMessage(
          conversation,
          "ASSISTANT",
          "即将被清空的回答",
          "assistant-clear",
          "document-1-node-1",
        ),
      ],
    });
    apiMocks.clearConversation.mockResolvedValue({
      schema_version: "reading-conversation-clear-result/v1",
      conversation_id: conversation.conversation_id,
      cleared: true,
      deleted_message_count: 1,
    });
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    expect(await screen.findByText("即将被清空的回答")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "清空当前会话" }));

    await waitFor(() => expect(apiMocks.clearConversation).toHaveBeenCalledOnce());
    expect(screen.queryByText("即将被清空的回答")).not.toBeInTheDocument();
    expect(apiMocks.deleteConversation).not.toHaveBeenCalled();
  });

  it("hides raw citation ids and jumps from numbered evidence to the source page", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = contextFixture(document.id, String(document.source_sha256), 3);
    configureDocuments([document]);
    apiMocks.context.mockResolvedValue(context);
    apiMocks.sourcePage.mockImplementation(
      async (_projectId: string, _documentId: string, page: number) =>
        sourcePageFixture(context, page),
    );
    const conversation = conversationFixture(context);
    apiMocks.conversations.mockResolvedValue({
      schema_version: "reading-conversation-list/v1",
      items: [conversation],
    });
    const answer = chatMessage(
      conversation,
      "ASSISTANT",
      "这条回答引用第三页。",
      "assistant-page-3",
    );
    answer.citations = [
      {
        citation_id: "R2",
        source_node_id: "document-1-node-1",
        page_number: 3,
        start_offset: 0,
        end_offset: 1,
        quote: "要",
      },
      {
        citation_id: "R48",
        source_node_id: "document-1-node-1",
        page_number: 3,
        start_offset: 0,
        end_offset: 2,
        quote: "要求",
      },
    ];
    apiMocks.conversationMessages.mockResolvedValue({
      schema_version: "reading-message-list/v1",
      conversation_id: conversation.conversation_id,
      items: [answer],
    });
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);

    expect(
      await screen.findByRole("button", { name: "查看证据 1，第 3 页" }),
    ).toHaveTextContent("证据 1");
    expect(screen.queryByText("R2")).not.toBeInTheDocument();
    expect(screen.queryByText("R48")).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "查看证据 2，第 3 页" }),
    );
    await waitFor(() =>
      expect(apiMocks.sourcePage).toHaveBeenLastCalledWith(
        PROJECT_ID,
        document.id,
        3,
        expect.any(AbortSignal),
      ),
    );
    expect(screen.getByText("已定位到第 3 页的原文依据。")).toBeInTheDocument();
  });

  it("labels visual evidence and sends its page bbox overlay to the PDF viewer", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    captureApiMocks.quality.mockResolvedValue(visualManifestFixture(context));
    captureApiMocks.visualPage.mockResolvedValue(visualPageFixture(context));
    const conversation = conversationFixture(context);
    apiMocks.conversations.mockResolvedValue({
      schema_version: "reading-conversation-list/v1",
      items: [conversation],
    });
    const answer = chatMessage(
      conversation,
      "ASSISTANT",
      "图纸标注了电涡流传感器。",
      "assistant-visual-evidence",
    );
    answer.citations = [
      {
        citation_id: "visual-citation-1",
        locator_kind: "VISUAL_TEXT",
        visual_snapshot_id: `${context.sor_document_id}-visual-1`,
        observation_id: "visual-observation-1",
        page_number: 1,
        start_offset: 0,
        end_offset: 3,
        bbox: [40, 80, 240, 110],
        page_bbox: [0, 0, 595, 842],
        page_image_sha256: "b".repeat(64),
        provider_id: "bailian",
        provider_version: "qwen-ocr-v1",
        quote: "电涡流",
      },
    ];
    apiMocks.conversationMessages.mockResolvedValue({
      schema_version: "reading-message-list/v1",
      conversation_id: conversation.conversation_id,
      items: [answer],
    });
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    const evidence = await screen.findByRole("button", {
      name: "查看证据 1 · 第 1 页 · 视觉识别",
    });
    expect(evidence).toHaveTextContent("证据 1 · 第 1 页 · 视觉识别");
    const frame = screen.getByTitle(
      "当前文档原文 · document-1.pdf",
    ) as HTMLIFrameElement;
    const postMessage = vi.spyOn(frame.contentWindow!, "postMessage");
    postMessage.mockClear();

    await user.click(evidence);

    await waitFor(() =>
      expect(postMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          type: "REQPILOTX_PDF_NAVIGATE",
          page: 1,
          overlays: [
            expect.objectContaining({
              bbox: [40, 80, 240, 110],
              sourceBounds: [0, 0, 595, 842],
              precision: "VISUAL_OBSERVATION",
              focus: true,
            }),
          ],
        }),
        window.location.origin,
      ),
    );
    expect(
      screen.getByText("已定位到第 1 页的视觉识别依据。"),
    ).toBeInTheDocument();
  });

  it("accepts a table-cell citation without comparing cell offsets to table text", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = contextFixture(document.id, String(document.source_sha256));
    context.selectable_nodes[0] = {
      ...context.selectable_nodes[0]!,
      kind: "TABLE",
      text_length: 1,
    };
    configureDocuments([document]);
    apiMocks.context.mockResolvedValue(context);
    const conversation = conversationFixture(context);
    apiMocks.conversations.mockResolvedValue({
      schema_version: "reading-conversation-list/v1",
      items: [conversation],
    });
    const answer = chatMessage(
      conversation,
      "ASSISTANT",
      "表格中的条件必须同时满足【R7】。",
      "assistant-table",
    );
    answer.citations = [
      {
        citation_id: "R7",
        locator_kind: "TABLE_CELL",
        source_node_id: "document-1-node-1",
        cell_id: "cell-A1",
        page_number: 1,
        start_offset: 0,
        end_offset: 20,
        quote: "表格单元格中的完整条件",
      },
    ];
    apiMocks.conversationMessages.mockResolvedValue({
      schema_version: "reading-message-list/v1",
      conversation_id: conversation.conversation_id,
      items: [answer],
    });

    render(<ReadingWorkspacePage />);

    expect(
      await screen.findByRole("button", { name: "查看证据 1 · 第 1 页" }),
    ).toHaveTextContent("证据 1");
    expect(screen.getByText(/查看 1 条原文依据/)).toBeInTheDocument();
  });

  it("stops waiting and ignores a late synchronous response", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    const conversation = conversationFixture(context);
    apiMocks.createConversation.mockResolvedValue(conversation);
    let resolveTurn!: (turn: ReadingChatTurnV1) => void;
    const pending = new Promise<ReadingChatTurnV1>((resolve) => {
      resolveTurn = resolve;
    });
    apiMocks.sendMessage.mockReturnValue(pending);
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    const composer = await enabledComposer();
    await user.type(composer, "一个很慢的问题");
    await user.keyboard("{Enter}");
    await user.click(await screen.findByRole("button", { name: "停止等待" }));
    expect(await screen.findByText("已停止等待")).toBeInTheDocument();

    await act(async () => {
      resolveTurn(turnFixture(conversation, "一个很慢的问题", "不应显示的迟到回答"));
      await pending;
    });

    expect(screen.queryByText("不应显示的迟到回答")).not.toBeInTheDocument();
    expect(composer).toHaveValue("一个很慢的问题");
  });

  it("clears document A messages immediately when switching to document B", async () => {
    const documentA = documentFixture("document-1", "a".repeat(64));
    const documentB = documentFixture("document-2", "b".repeat(64));
    const contexts = configureDocuments([documentA, documentB]);
    const conversationA = conversationFixture(contexts.get(documentA.id)!, "chat-a", "A 会话");
    apiMocks.conversations.mockImplementation(
      async (_projectId: string, documentId: string) => ({
        schema_version: "reading-conversation-list/v1",
        items: documentId === documentA.id ? [conversationA] : [],
      }),
    );
    apiMocks.conversationMessages.mockResolvedValue({
      schema_version: "reading-message-list/v1",
      conversation_id: conversationA.conversation_id,
      items: [
        chatMessage(
          conversationA,
          "ASSISTANT",
          "只属于 A 文档的回答",
          "assistant-a",
          "document-1-node-1",
        ),
      ],
    });
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    expect(await screen.findByText("只属于 A 文档的回答")).toBeInTheDocument();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "当前阅读文档" }),
      documentB.id,
    );
    await waitFor(() =>
      expect(
        screen.getByRole("combobox", { name: "当前阅读文档" }),
      ).toHaveValue(documentB.id),
    );
    await waitFor(() =>
      expect(screen.queryByText("只属于 A 文档的回答")).not.toBeInTheDocument(),
    );
    expect(
      await screen.findByRole("heading", { name: "你想从这份文档了解什么？" }),
    ).toBeInTheDocument();
  });

  it("reprepares on a new capture bundle and keeps old conversations read-only", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const oldContext = configureDocuments([document]).get(document.id)!;
    const oldManifest = pendingVisualManifestFixture(oldContext);
    const nextContext: ReadingContextV1 = {
      ...oldContext,
      effective_bundle_id: "document-1-bundle-after-visual",
      visual_generation_id: "document-1-visual-generation",
      updated_at_utc: "2026-08-13T00:01:00Z",
      notes: [noteFixture(oldContext, "STALE")],
    };
    const nextManifest = {
      ...visualManifestFixture(nextContext),
      effective_bundle_id: nextContext.effective_bundle_id,
    };
    const staleConversation = {
      ...conversationFixture(oldContext, "old-capture-chat", "旧版问题"),
      status: "STALE" as const,
    };
    const staleAnswer = {
      ...chatMessage(
        staleConversation,
        "ASSISTANT",
        "这是旧捕获生成的回答。",
        "old-capture-answer",
      ),
      status: "STALE" as const,
    };
    captureApiMocks.quality
      .mockResolvedValueOnce(oldManifest)
      .mockResolvedValue(nextManifest);
    captureApiMocks.resumeVisualPage.mockResolvedValue(
      visualPageFixture(nextContext),
    );
    captureApiMocks.visualPage.mockResolvedValue(visualPageFixture(nextContext));
    apiMocks.prepare.mockResolvedValue(nextContext);
    apiMocks.conversations
      .mockResolvedValueOnce({
        schema_version: "reading-conversation-list/v1",
        items: [],
      })
      .mockResolvedValue({
        schema_version: "reading-conversation-list/v1",
        items: [staleConversation],
      });
    apiMocks.conversationMessages.mockResolvedValue({
      schema_version: "reading-message-list/v1",
      conversation_id: staleConversation.conversation_id,
      items: [staleAnswer],
    });
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    await user.click(
      await screen.findByRole("button", {
        name: "文档可读性：可读 0/1",
      }),
    );
    await user.click(screen.getByRole("button", { name: "识别本页" }));

    expect(
      await screen.findByText(
        "页面识别已更新当前阅读来源；旧捕获会话仅供参考，新问题将使用最新内容。",
      ),
    ).toBeInTheDocument();
    expect(apiMocks.prepare).toHaveBeenCalledWith(
      PROJECT_ID,
      document.id,
      {
        schema_version: "reading-prepare-request/v1",
        expected_source_sha256: contextFixture(
          document.id,
          String(document.source_sha256),
        ).source_sha256,
      },
      expect.any(AbortSignal),
    );
    await waitFor(() => expect(apiMocks.conversations).toHaveBeenCalledTimes(2));
    expect(apiMocks.conversationMessages).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "会话历史" }));
    expect(screen.getByText(/旧捕获，仅供参考/)).toBeInTheDocument();
    await user.click(screen.getByText("旧版问题"));
    expect(await screen.findByText("这是旧捕获生成的回答。")).toBeInTheDocument();
    expect(screen.getByText("旧捕获会话，仅供参考")).toBeInTheDocument();
    expect(
      screen.getByText("该会话不会自动混入最新阅读来源，也不能继续追问。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "向当前文档提问" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "文档辅助信息" }));
    await user.click(screen.getByRole("button", { name: "笔记 1" }));
    expect(screen.getByText("旧版捕获下保存的笔记")).toBeInTheDocument();
    expect(screen.getAllByText("旧捕获，仅供参考").length).toBeGreaterThan(0);
    expect(
      screen.getByText("不可编辑、删除或回到旧版原文"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "回到原文" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "删除" })).not.toBeInTheDocument();
  });

  it("stops bulk scheduling after the in-flight page settles and then reprepares", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const oldContext = configureDocuments([document]).get(document.id)!;
    oldContext.overview.page_count = 2;
    oldContext.structure_map.page_count = 2;
    const oldManifest = pendingTwoVisualPagesFixture(oldContext);
    const nextContext: ReadingContextV1 = {
      ...oldContext,
      effective_bundle_id: "document-1-bundle-after-first-page",
      visual_generation_id: "document-1-visual-generation",
      updated_at_utc: "2026-08-13T00:02:00Z",
    };
    const nextManifest = {
      ...partialVisualManifestFixture(nextContext),
      effective_bundle_id: nextContext.effective_bundle_id,
    };
    let resolveCapture!: (capture: VisualPageCaptureV1) => void;
    const inFlightCapture = new Promise<VisualPageCaptureV1>((resolve) => {
      resolveCapture = resolve;
    });
    captureApiMocks.quality
      .mockResolvedValueOnce(oldManifest)
      .mockResolvedValue(nextManifest);
    captureApiMocks.resumeVisualPage.mockReturnValue(inFlightCapture);
    captureApiMocks.visualPage.mockResolvedValue(visualPageFixture(nextContext));
    apiMocks.prepare.mockResolvedValue(nextContext);
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    await user.click(
      await screen.findByRole("button", { name: "文档可读性：可读 0/2" }),
    );
    await user.click(screen.getByRole("button", { name: "识别全部 2 页" }));
    await waitFor(() =>
      expect(captureApiMocks.resumeVisualPage).toHaveBeenCalledTimes(1),
    );
    expect(screen.getByRole("textbox", { name: "向当前文档提问" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /快速读懂/ })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "停止批量识别" }));
    expect(
      screen.getByText("将在当前页识别完成后停止"),
    ).toBeInTheDocument();
    expect(captureApiMocks.resumeVisualPage).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveCapture(visualPageFixture(nextContext));
      await inFlightCapture;
    });

    expect(
      await screen.findByText(
        "页面识别已更新当前阅读来源；旧捕获会话仅供参考，新问题将使用最新内容。",
      ),
    ).toBeInTheDocument();
    expect(captureApiMocks.resumeVisualPage).toHaveBeenCalledTimes(1);
    expect(captureApiMocks.quality).toHaveBeenCalledTimes(3);
    expect(apiMocks.prepare).toHaveBeenCalledOnce();
    await waitFor(() =>
      expect(
        screen.getByRole("textbox", { name: "向当前文档提问" }),
      ).toBeEnabled(),
    );
  });

  it("blocks AI selection actions and note writes while a capture request is in flight", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    context.overview.page_count = 2;
    context.structure_map.page_count = 2;
    context.selectable_nodes[0] = {
      ...context.selectable_nodes[0]!,
      page_number: 2,
    };
    captureApiMocks.quality.mockResolvedValue(
      pendingVisualManifestFixture(context),
    );
    captureApiMocks.resumeVisualPage.mockReturnValue(
      new Promise<VisualPageCaptureV1>(() => undefined),
    );
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    const frame = await screen.findByTitle(
      "当前文档原文 · document-1.pdf",
    ) as HTMLIFrameElement;
    act(() => {
      dispatchPdfMessage(frame, { type: "pdf-page-changed", page: 2 });
    });
    await waitFor(() =>
      expect(apiMocks.sourcePage).toHaveBeenLastCalledWith(
        PROJECT_ID,
        document.id,
        2,
        expect.any(AbortSignal),
      ),
    );
    await user.click(
      screen.getByRole("button", { name: "文档可读性：可读 1/2" }),
    );
    await user.click(screen.getByRole("button", { name: "识别全部 1 页" }));
    await waitFor(() =>
      expect(captureApiMocks.resumeVisualPage).toHaveBeenCalledOnce(),
    );

    act(() => {
      dispatchPdfMessage(frame, {
        type: "pdf-selection-action",
        selectionId: "busy-action",
        page: 2,
        text: "要求",
        action: "EXPLAIN",
      });
    });
    expect(
      await screen.findByText("阅读来源更新中，识别完成后再使用 AI 操作。"),
    ).toBeInTheDocument();
    expect(apiMocks.sendMessage).not.toHaveBeenCalled();

    act(() => {
      dispatchPdfMessage(frame, {
        type: "pdf-text-selected",
        selectionId: "busy-local-selection",
        page: 2,
        text: "要求",
      });
    });
    await user.click(screen.getByRole("button", { name: "文档辅助信息" }));
    await user.click(screen.getByRole("button", { name: "笔记 0" }));
    expect(screen.getByLabelText("给选中的原文记笔记")).toBeDisabled();
    expect(screen.getByRole("button", { name: "保存笔记" })).toBeDisabled();
    expect(apiMocks.createNote).not.toHaveBeenCalled();
  });

  it("rejects a visual receipt that quality did not activate", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    const pendingManifest = pendingVisualManifestFixture(context);
    const mismatchedManifest = visualManifestFixture(context);
    mismatchedManifest.pages[0] = {
      ...mismatchedManifest.pages[0]!,
      visual_snapshot_id: "different-active-snapshot",
    };
    captureApiMocks.quality
      .mockResolvedValueOnce(pendingManifest)
      .mockResolvedValue(mismatchedManifest);
    captureApiMocks.resumeVisualPage.mockResolvedValue(visualPageFixture(context));
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    await user.click(
      await screen.findByRole("button", { name: "文档可读性：可读 0/1" }),
    );
    await user.click(screen.getByRole("button", { name: "识别本页" }));

    expect(
      await screen.findByText("视觉识别回执未进入当前有效捕获版本，已阻止采用。"),
    ).toBeInTheDocument();
    expect(apiMocks.prepare).not.toHaveBeenCalled();
  });

  it("maps an image-only capture error and disables its retry loop", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    const pendingManifest = pendingVisualManifestFixture(context);
    const failedManifest: CaptureQualityManifestV1 = {
      ...pendingManifest,
      status: "BLOCKED",
      failed_page_count: 1,
      pages: pendingManifest.pages.map((page) => ({
        ...page,
        capture_status: "FAILED",
        failure_code: "visual_image_semantics_not_supported",
      })),
    };
    captureApiMocks.quality
      .mockResolvedValueOnce(pendingManifest)
      .mockResolvedValue(failedManifest);
    captureApiMocks.resumeVisualPage.mockRejectedValue(
      new ApiError("Image semantics unsupported", 409, {
        detail: {
          reason_code: "visual_page_capture_failed",
          failure_code: "visual_image_semantics_not_supported",
        },
      }),
    );
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    await user.click(
      await screen.findByRole("button", { name: "文档可读性：可读 0/1" }),
    );
    await user.click(screen.getByRole("button", { name: "识别本页" }));

    expect(
      await screen.findByText(
        "第 1 页当前仅支持文字层；该页为无文字图像/图纸，暂不能完整阅读。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "当前仅支持文字层；该页为无文字图像/图纸，暂不能完整阅读",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新识别" })).not.toBeInTheDocument();
    expect(captureApiMocks.resumeVisualPage).toHaveBeenCalledOnce();
  });

  it("aborts a pending A send and lets document B ask immediately", async () => {
    const documentA = documentFixture("document-1", "a".repeat(64));
    const documentB = documentFixture("document-2", "b".repeat(64));
    const contexts = configureDocuments([documentA, documentB]);
    const conversationA = conversationFixture(contexts.get(documentA.id)!, "chat-a");
    const conversationB = conversationFixture(contexts.get(documentB.id)!, "chat-b");
    apiMocks.createConversation.mockImplementation(
      async (_projectId: string, documentId: string) =>
        documentId === documentA.id ? conversationA : conversationB,
    );
    let resolveA!: (turn: ReadingChatTurnV1) => void;
    const pendingA = new Promise<ReadingChatTurnV1>((resolve) => {
      resolveA = resolve;
    });
    apiMocks.sendMessage
      .mockReturnValueOnce(pendingA)
      .mockResolvedValueOnce(
        turnFixture(conversationB, "B 文档问题", "B 文档可以正常回答"),
      );
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    let composer = await enabledComposer();
    await user.type(composer, "A 文档慢问题");
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("button", { name: "停止等待" })).toBeInTheDocument();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "当前阅读文档" }),
      documentB.id,
    );
    await waitFor(() =>
      expect(
        screen.getByRole("combobox", { name: "当前阅读文档" }),
      ).toHaveValue(documentB.id),
    );
    composer = await enabledComposer();
    await user.type(composer, "B 文档问题");
    await user.keyboard("{Enter}");
    expect(await screen.findByText("B 文档可以正常回答")).toBeInTheDocument();

    await act(async () => {
      resolveA(turnFixture(conversationA, "A 文档慢问题", "A 文档迟到回答"));
      await pendingA;
    });
    expect(screen.queryByText("A 文档迟到回答")).not.toBeInTheDocument();
  });

  it.each([
    ["快速读懂", "快速读懂这份文档", "QUICK_READ"],
    ["关键技术条件", "提炼关键技术条件", "TECHNICAL_CONDITIONS"],
    ["章节导读", "按章节导读", "SECTION_GUIDE"],
    ["找出易遗漏条件", "找出容易遗漏的条件", "MISSED_CONDITIONS"],
  ])("sends the short %s shortcut with its task type", async (label, content, taskType) => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    const conversation = conversationFixture(context);
    apiMocks.createConversation.mockResolvedValue(conversation);
    apiMocks.sendMessage.mockImplementation(
      async (
        _projectId: string,
        _documentId: string,
        _conversationId: string,
        payload: { content: string },
      ) => turnFixture(conversation, payload.content, "快捷入口回答"),
    );
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    await screen.findByRole("heading", { name: "你想从这份文档了解什么？" });
    await user.click(screen.getByRole("button", { name: new RegExp(label) }));

    await waitFor(() => expect(apiMocks.sendMessage).toHaveBeenCalledOnce());
    expect(apiMocks.sendMessage.mock.calls[0]?.[3]).toMatchObject({
      content,
      task_type: taskType,
    });
    expect(apiMocks.sendMessage.mock.calls[0]?.[3]).not.toHaveProperty(
      "coverage_intent",
    );
  });

  it("preserves the shortcut task type when an answer is retried", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    const conversation = conversationFixture(context);
    apiMocks.createConversation.mockResolvedValue(conversation);
    apiMocks.sendMessage
      .mockRejectedValueOnce(new Error("temporary timeout"))
      .mockImplementationOnce(
        async (
          _projectId: string,
          _documentId: string,
          _conversationId: string,
          payload: { content: string },
        ) => turnFixture(conversation, payload.content, "重试后的概览"),
      );
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    await screen.findByRole("heading", { name: "你想从这份文档了解什么？" });
    await user.click(screen.getByRole("button", { name: /快速读懂/ }));
    await user.click(await screen.findByRole("button", { name: "重试" }));

    expect(await screen.findByText("重试后的概览")).toBeInTheDocument();
    expect(apiMocks.sendMessage).toHaveBeenCalledTimes(2);
    expect(apiMocks.sendMessage.mock.calls[0]?.[3]).toMatchObject({
      task_type: "QUICK_READ",
    });
    expect(apiMocks.sendMessage.mock.calls[1]?.[3]).toMatchObject({
      task_type: "QUICK_READ",
    });
  });

  it("preserves the shortcut task type after stopping and retrying", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    const conversation = conversationFixture(context);
    apiMocks.createConversation.mockResolvedValue(conversation);
    let resolveFirst!: (turn: ReadingChatTurnV1) => void;
    const first = new Promise<ReadingChatTurnV1>((resolve) => {
      resolveFirst = resolve;
    });
    apiMocks.sendMessage
      .mockReturnValueOnce(first)
      .mockImplementationOnce(
        async (
          _projectId: string,
          _documentId: string,
          _conversationId: string,
          payload: { content: string },
        ) => turnFixture(conversation, payload.content, "停止后重试的概览"),
      );
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    await screen.findByRole("heading", { name: "你想从这份文档了解什么？" });
    await user.click(screen.getByRole("button", { name: /快速读懂/ }));
    await user.click(await screen.findByRole("button", { name: "停止等待" }));
    await user.click(await screen.findByRole("button", { name: "重试" }));

    expect(await screen.findByText("停止后重试的概览")).toBeInTheDocument();
    expect(apiMocks.sendMessage.mock.calls[1]?.[3]).toMatchObject({
      task_type: "QUICK_READ",
    });
    await act(async () => {
      resolveFirst(turnFixture(conversation, "旧请求", "不应显示的旧概览"));
      await first;
    });
    expect(screen.queryByText("不应显示的旧概览")).not.toBeInTheDocument();
  });

  it("shows a clear not-configured state without disabling document reading", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    const conversation = conversationFixture(context);
    apiMocks.createConversation.mockResolvedValue(conversation);
    apiMocks.sendMessage.mockRejectedValue(
      new ApiError("Reading AI is not configured.", 409, {
        detail: { reason_code: "reading_ai_not_configured" },
      }),
    );
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    const composer = await enabledComposer();
    await user.type(composer, "请总结");
    await user.keyboard("{Enter}");

    expect(await screen.findByText("Codex Agent 尚未就绪")).toBeInTheDocument();
    expect(screen.getByTitle("当前文档原文 · document-1.pdf")).toBeInTheDocument();
  });

  it("fails closed clearly when the current document exceeds the safe full window", async () => {
    const document = documentFixture("document-1", "a".repeat(64));
    const context = configureDocuments([document]).get(document.id)!;
    const conversation = conversationFixture(context);
    apiMocks.createConversation.mockResolvedValue(conversation);
    apiMocks.sendMessage.mockRejectedValue(
      new ApiError("Full document is too large.", 422, {
        code: "reading_full_document_too_large",
        detail: { reason_code: "FULL_DOCUMENT_TOO_LARGE" },
      }),
    );
    const user = userEvent.setup();

    render(<ReadingWorkspacePage />);
    const composer = await enabledComposer();
    await user.type(composer, "请完整总结");
    await user.keyboard("{Enter}");

    expect(
      await screen.findByText(
        "当前文档超出模型安全全文窗口，系统未生成抽样答案；请换用更大上下文模型或拆分文档。",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });
});

describe("resolvePdfSourceSelections", () => {
  const node = (
    id: string,
    sourceOrder: number,
    text: string,
  ): ReadingSourcePageNodeV1 => ({
    source_node_id: id,
    kind: "PARAGRAPH",
    source_order: sourceOrder,
    page_number: 1,
    text_length: Array.from(text).length,
    preview: text,
    text,
  });

  it("maps a selection across PDF.js spans and SourceIR nodes into ordered offsets", () => {
    expect(
      resolvePdfSourceSelections("ABC\n第二段", [
        node("node-1", 1, "第一段 ABC"),
        node("node-2", 2, "第二段 XYZ"),
      ]),
    ).toEqual([
      {
        sourceNodeId: "node-1",
        startOffset: 4,
        endOffset: 7,
        text: "ABC",
      },
      {
        sourceNodeId: "node-2",
        startOffset: 0,
        endOffset: 3,
        text: "第二段",
      },
    ]);
  });

  it("fails closed for repeated or unbounded matches", () => {
    expect(
      resolvePdfSourceSelections("重复", [
        node("node-1", 1, "重复内容"),
        node("node-2", 2, "又有重复内容"),
      ]),
    ).toBeNull();
    expect(
      resolvePdfSourceSelections(
        "一二三",
        [node("node-1", 1, "一"), node("node-2", 2, "二"), node("node-3", 3, "三")],
        2,
      ),
    ).toBeNull();
  });
});

it("retries a failed project catalog without changing the requested source", async () => {
  configureDocuments([documentFixture("document-1", "a".repeat(64))]);
  apiMocks.projects.mockRejectedValueOnce(new Error("项目列表暂不可用"));
  render(<ReadingWorkspacePage />);
  await screen.findByText("项目列表暂不可用");
  await userEvent.setup().click(screen.getByRole("button", { name: "重试" }));
  await enabledComposer();
  expect(apiMocks.projects).toHaveBeenCalledTimes(2);
  expect(window.location.search).toContain("documentId=document-1");
});

it("retries a failed document catalog", async () => {
  configureDocuments([documentFixture("document-1", "a".repeat(64))]);
  apiMocks.documents.mockRejectedValueOnce(new Error("文档列表暂不可用"));
  render(<ReadingWorkspacePage />);
  await screen.findByText("文档列表暂不可用");
  await userEvent.setup().click(screen.getByRole("button", { name: "重试" }));
  await enabledComposer(); expect(apiMocks.documents).toHaveBeenCalledTimes(2);
});

it.each(["projectId", "documentId"])("keeps an explicitly unavailable %s instead of selecting another source", async field => {
  configureDocuments([documentFixture("document-1", "a".repeat(64))]);
  const params = new URLSearchParams(window.location.search); params.set(field, "missing");
  window.history.replaceState(null, "", `/workspace?${params}`);
  render(<ReadingWorkspacePage />);
  await screen.findByRole("heading", { name: field === "projectId" ? "所选项目不可用" : "所选文档不可用" });
  expect(new URLSearchParams(window.location.search).get(field)).toBe("missing");
  expect(apiMocks.sendMessage).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "返回项目" })).toBeEnabled();
});

it("opens conversation history as a modal and returns focus on Escape", async () => {
  configureDocuments([documentFixture("document-1", "a".repeat(64))]);
  render(<ReadingWorkspacePage />); await enabledComposer();
  const button = screen.getByRole("button", { name: "会话历史" });
  await userEvent.setup().click(button);
  const dialog = screen.getByRole("dialog", { name: "会话历史" });
  act(() => { dialog.dispatchEvent(new Event("cancel", { bubbles: false, cancelable: true })); });
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "会话历史" })).not.toBeInTheDocument());
  expect(button).toHaveFocus();
});
