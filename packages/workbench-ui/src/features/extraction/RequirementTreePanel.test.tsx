import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { SourceDocumentActivity } from "./SourceDocumentActions";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../../shared/api/client";
import type {
  EntryClassificationResponse,
  EntryClassificationStatus,
  EntryClassificationType,
  JsonRecord,
  NativeStructureReferencePublicV1,
  RequirementAtomizationScopeAttempt,
  RequirementAtomizationScopeReceipt,
  RequirementAtomizationScopeRunResponse,
  RequirementEnrichmentState,
  RequirementSemanticPublicationCapabilityV2,
  RequirementSemanticPublicationProjectionRowV2,
  RequirementSemanticPublicationPreviewRequestV2,
  RequirementSemanticPublicationPreviewResponseV2,
  RequirementSemanticPublishDraftResponseV2,
  RequirementTreeSnapshot,
  RequirementTreeNode,
  RestorationRun,
} from "../../shared/api/contracts";
import { normalizeRestorationPage } from "../restoration/restorationModel";
import {
  WorkspaceProvider,
  type WorkspaceContextValue,
} from "../workspace/WorkspaceContext";
import {
  CompleteContent,
  availableDocumentPageCount,
  extractionDecompositionSummary,
  extractionVisualReceiptSummary,
  parsingDiagnosisOverlays,
  preferredRequirementTreePage,
  RequirementTreePanel,
  requirementTreeReparentOperations,
} from "./RequirementTreePanel";
import {
  canonicalJsonSha256,
  withoutCanonicalField,
} from "./canonicalJsonSha256";

const captureApiMocks = vi.hoisted(() => ({
  quality: vi.fn(),
  visualPage: vi.fn(),
  resumeVisualPage: vi.fn(),
}));
const sourceActionMocks = vi.hoisted(() => ({ activity: null as SourceDocumentActivity | null, openRequest: 0 }));

vi.mock("../document-capture/captureApi", () => ({ captureApi: captureApiMocks }));
// These tests own the legacy publication transaction. The new source-goal
// child's independent reads must not consume its narrowly scripted POST mocks.
vi.mock("./SourceDocumentActions", async (importOriginal) => {
  const { useEffect } = await import("react");
  return {
    ...await importOriginal<typeof import("./SourceDocumentActions")>(),
    SourceDocumentActions: ({ onActivityChange, collaborationRequest }: { onActivityChange?: (activity: SourceDocumentActivity) => void; collaborationRequest?: number }) => {
      useEffect(() => { if (sourceActionMocks.activity) onActivityChange?.(sourceActionMocks.activity); }, [onActivityChange]);
      sourceActionMocks.openRequest = collaborationRequest || 0;
      return null;
    },
  };
});

const sourceSha256 = "b".repeat(64);

function nativeStructureReferenceFixture(): NativeStructureReferencePublicV1 {
  return {
    schema_version: "native-structure-reference-public/v1",
    availability: "READY",
    artifact_id: "pds-native-v1",
    source_sha256: sourceSha256,
    artifact_status: "PARTIAL",
    coverage_status: "COMPLETE",
    boundary_status: "PARTIAL_WITH_EXPLICIT_UNRESOLVED",
    workflow_step: "DOCUMENT_STRUCTURE_RECOVERY",
    structure_projection: {
      schema_version: "native-structure-projection-state/v1",
      state: "ACCEPTED",
      base_artifact_id: "pds-native-v1",
      attempt_id: null,
      attempt_ordinal: null,
      affected_scope_ids: [],
      pending_rendered_scope_ids: [],
      validation_status: "PASS",
      projection_sha256: "c".repeat(64),
    },
    summary: {
      primary_document_units: 7,
      main_document_top_level_sections: 6,
      top_level_logical_tables: 15,
      nested_physical_tables: 2,
      nested_visual_tables: 3,
      opened_high_resolution_pages: 29,
      count_is_quality_gate: false,
    },
    page_ownership: [],
    main_document_sections: [],
    document_tree: {
      schema_version: "native-document-structure-tree/v1",
      root_ids: ["doc-main", "doc-front"],
      nodes: [
        {
          node_id: "doc-main",
          parent_id: null,
          order: 0,
          kind: "DOCUMENT_ROOT",
          label: "",
          title: "产品设计规格说明书（PDS）",
          title_authority: "VISIBLE_TITLE",
          pages: [1, 2, 3, 4, 5, 6, 7, 8, 9],
          content_blocks: [],
          finding_codes: [],
          source_anchors: [
            {
              anchor_id: "anchor-main",
              page: 1,
              bbox: [20, 20, 180, 38],
              source_bounds: [0, 0, 595, 842],
              anchor_kind: "TITLE",
              precision: "EXACT_NATIVE_SPAN",
            },
          ],
          source_content: [],
        },
        {
          node_id: "doc-front",
          parent_id: null,
          order: 1,
          kind: "DOCUMENT_UNIT",
          label: "",
          title: "主动前减振器试验大纲",
          title_authority: "VISIBLE_TITLE",
          pages: [10, 11, 12, 13, 14, 15],
          content_blocks: [
            { kind: "LOGICAL_TABLE", logical_table_scope_id: "lt-front" },
          ],
          finding_codes: [],
          source_anchors: [
            {
              anchor_id: "anchor-front-title",
              page: 10,
              bbox: [30, 40, 240, 58],
              source_bounds: [0, 0, 595, 842],
              anchor_kind: "TITLE",
              precision: "EXACT_NATIVE_SPAN",
            },
            {
              anchor_id: "anchor-front-table",
              page: 10,
              bbox: [30, 80, 560, 780],
              source_bounds: [0, 0, 595, 842],
              anchor_kind: "TABLE_REGION",
              precision: "EXACT_TABLE_REGION",
              logical_table_scope_id: "lt-front",
            },
          ],
          source_content: [],
        },
      ],
    },
    logical_tables: [
      {
        scope_id: "lt-front",
        title: "主动前减振器试验大纲",
        owner_unit_id: "unit-main",
        role: "TEST_OUTLINE",
        pages: [10, 11, 12, 13, 14, 15],
        fragments: [{ table_token: "T0009", page: 10, row_range: [5, 17] }],
        source_numbering_anomaly: "",
        capture_gap: "",
      },
    ],
    physical_table_dispositions: [],
    unresolved: [],
    implementation_findings: [],
    visual_summary: { opened_page_count: 29, page_count: 29, receipts: [] },
    repeatability: {
      status: "PASS_WITH_BOUNDARY_REFINEMENTS",
      stable_semantics: {},
      refinements_applied: [],
      unresolved_not_settled_by_replay: [],
      quality_claim: "NOT_GOLD",
    },
    validation: { status: "PASS", checks: {}, quality_claim: "NOT_GOLD" },
    oracle_diagnostic: {
      authority: "NOT_GOLD",
      oracle_expected_region_count: 40,
      native_region_disposition_count: 51,
      count_is_correctness_signal: false,
      native_boundary_corrections: [],
    },
    structure_proposals: [],
    activity: [],
    feedbacks: [],
    continuation: {
      status: "IDLE",
      can_continue: false,
      pending_feedback_count: 0,
      pending_proposal_decision_count: 0,
      confirmed_proposal_count: 0,
      undone_proposal_count: 0,
      kept_unresolved_proposal_count: 0,
      review_pending_proposal_count: 0,
      latest_handoff: null,
      message: "保存节点指引或处理建议后，可继续交给 Agent。",
    },
    feedback_capability: {
      status: "QUEUE_READY",
      agent_delivery: "IDLE",
      message: "等待读取",
    },
  };
}

describe("extractionDecompositionSummary", () => {
  it("keeps complete parents separate from their atomic obligations", () => {
    const summary = extractionDecompositionSummary({
      requirement_decomposition: [
        { obligations: [{ atomic_statement: "A" }, { atomic_statement: "B" }] },
        { obligations: [{ atomic_statement: "C" }] },
      ],
    });

    expect(summary.parentCount).toBe(2);
    expect(summary.obligationCount).toBe(3);
    expect(summary.parents).toHaveLength(2);
  });
});

describe("availableDocumentPageCount", () => {
  it("keeps the original PDF page range when an old parse artifact exposes fewer pages", () => {
    expect(availableDocumentPageCount(1, 29, 0)).toBe(29);
  });
});

beforeEach(() => {
  sourceActionMocks.activity = null; sourceActionMocks.openRequest = 0;
  Object.values(captureApiMocks).forEach((mock) => mock.mockReset());
  captureApiMocks.quality.mockRejectedValue(
    new ApiError("source capture not prepared", 409, {
      detail: { reason_code: "source_capture_not_prepared" },
    }),
  );
  installNavigationResizeObserver();
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close() {
    this.open = false;
    this.dispatchEvent(new Event("close"));
  };
  vi.spyOn(api, "entryClassifications").mockImplementation(async () =>
    classificationResponseForTree(navigationTree(), { status: "WORKING" }),
  );
  vi.spyOn(api, "parsingDiagnostics").mockImplementation(async () =>
    parsingRiskPayload({ severe: 0, blocking: 0 }),
  );
  vi.spyOn(api, "requirementEnrichment").mockImplementation(async () =>
    atomizationEnrichmentState(
      classificationResponseForTree(navigationTree(), { status: "WORKING" }),
    ),
  );
  vi.spyOn(api, "runRequirementAtomizationScope").mockResolvedValue({
    schema_version: "requirement-atomization-run-response/v2",
    serving_impact: "NONE",
    replayed: false,
    atomization_run: {},
    scope_receipt: atomizationScopeReceipt(),
  });
  vi.spyOn(api, "nativeStructureReference").mockRejectedValue(
    new ApiError("native structure reference not available", 404, null),
  );
  vi.spyOn(api, "nativeStructureWorkingRevisionHead").mockResolvedValue({
    schema_version: "native-structure-working-revision-head-response/v1",
    source_sha256: sourceSha256,
    head: null,
  });
  vi.spyOn(api, "sourceRequirementBaselineStatus").mockResolvedValue({
    schema_version: "source-requirement-baseline-status/v1",
    status: "NOT_READY",
    baseline_ready: false,
    document_map_ready: false,
    source_sha256: sourceSha256,
    message: "Agent 尚未发布原文需求清单切片。",
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function treeNode(contentBlocks: JsonRecord[]): RequirementTreeNode {
  return {
    id: "structure-section",
    parent_id: null,
    kind: "SECTION",
    depth: 0,
    source_order: 0,
    label: "跨页技术要求",
    summary: "完整章节内容",
    child_ids: [],
    structure_node_id: "structure-section",
    requirement_entry_id: null,
    workflow_node_id: null,
    page_start: 5,
    page_end: 7,
    source_refs: [],
    requirement_count: 0,
    attention_count: 0,
    reviewed_count: 0,
    status: "READY",
    classification: "STRUCTURE",
    include_in_view: true,
    content_blocks: contentBlocks,
    table_refs: [],
    figure_refs: [],
  };
}

function tableBlock({
  id,
  sourceId,
  page,
  segmentIndex,
  text,
}: {
  id: string;
  sourceId: string;
  page: number;
  segmentIndex: number;
  text: string;
}): JsonRecord {
  return {
    kind: "table",
    id,
    table_id: id,
    source_node_id: sourceId,
    source_id: sourceId,
    source_order: segmentIndex,
    row_count: 1,
    column_count: 1,
    page_start: page,
    page_end: page,
    cells: [
      {
        id: `${id}-cell`,
        row: 0,
        column: 0,
        text,
        page,
      },
    ],
    continuation: {
      logical_table_id: "logical-table-cross-page",
      status: segmentIndex === 0 ? "START" : "CONTINUE",
      segment_index: segmentIndex,
      segment_count: 2,
    },
  };
}

function currentPagePayload(
  firstTable?: JsonRecord,
  additionalBlocks: JsonRecord[] = [],
): JsonRecord {
  return {
    source_identity: { source_sha256: sourceSha256 },
    page_comparison: {
      pages: [
        {
          page_number: 5,
          bounds: [0, 0, 595, 842],
          blocks: [
            {
              id: "figure-source",
              type: "FIGURE",
              page_number: 5,
              source_order: 0,
              bbox: [1, 1, 11, 11],
              locatable: true,
            },
            ...(firstTable
              ? [
                  {
                    id: String(firstTable.source_id),
                    type: "TABLE",
                    page_number: 5,
                    source_order: 1,
                    table_id: String(firstTable.table_id),
                    bbox: [40, 80, 540, 420],
                    locatable: true,
                  },
                ]
              : []),
            ...additionalBlocks,
          ],
          issues: [],
        },
      ],
    },
    tables: firstTable ? [firstTable] : [],
  };
}

describe("CompleteContent restoration", () => {
  it("keeps descendant text, media and following text in one global source order", () => {
    const section = navigationNode("section-10.2", {
      label: "10.2 物理层",
      projection_role: "STRUCTURE",
      child_ids: ["intro", "outro"],
      content_blocks: [
        {
          kind: "figure",
          source_id: "figure-3",
          source_order: 20,
          location: { page_number: 46, bbox: [40, 80, 420, 300] },
          alt: "图 3 总线接口电路图",
          caption: "图 3 包含 TJA1145A 总线收发器的电路图",
        },
      ],
    });
    const intro = navigationNode("intro", {
      parent_id: section.id,
      depth: 1,
      source_order: 10,
      projection_role: "DIRECT_CONTENT",
      content_blocks: [
        {
          kind: "text",
          source_id: "intro-text",
          source_order: 10,
          text: "图前说明文字",
        },
      ],
    });
    const outro = navigationNode("outro", {
      parent_id: section.id,
      depth: 1,
      source_order: 30,
      projection_role: "DIRECT_CONTENT",
      content_blocks: [
        {
          kind: "text",
          source_id: "outro-text",
          source_order: 30,
          text: "图后说明文字",
        },
      ],
    });
    const orderedTree: RequirementTreeSnapshot = {
      ...navigationTree(),
      root_node_ids: [section.id],
      nodes: [section, intro, outro],
    };

    const { container } = render(
      <CompleteContent
        node={section}
        selectedItem={null}
        tree={orderedTree}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    const events = Array.from(
      container.querySelectorAll('[data-reading-event="block"]'),
    );
    expect(events).toHaveLength(3);
    expect(events[0]).toHaveTextContent("图前说明文字");
    expect(
      within(events[1] as HTMLElement).getByRole("img"),
    ).toHaveAccessibleName("图 3 总线接口电路图");
    expect(events[1]).toHaveTextContent(
      "图 3 包含 TJA1145A 总线收发器的电路图",
    );
    expect(events[2]).toHaveTextContent("图后说明文字");
  });

  it("keeps source-offset-only spans distinct and sorts them physically", () => {
    const node = navigationNode("split-source-spans", {
      content_blocks: [
        {
          kind: "text",
          source_id: "shared-source",
          source_order: 10,
          source_offset: 4,
          text: "第二段",
        },
        {
          kind: "text",
          source_id: "shared-source",
          source_order: 10,
          source_offset: 0,
          text: "第一段",
        },
      ],
    });

    const { container } = render(
      <CompleteContent
        node={node}
        selectedItem={null}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    const events = Array.from(
      container.querySelectorAll('[data-reading-event="block"]'),
    );
    expect(events).toHaveLength(2);
    expect(events[0]).toHaveTextContent("第一段");
    expect(events[1]).toHaveTextContent("第二段");
  });

  it("keeps repeated unbound paragraphs as separate occurrences", () => {
    const node = navigationNode("repeated-unbound", {
      content_blocks: [
        { kind: "text", source_order: 10, text: "相同原文" },
        { kind: "text", source_order: 11, text: "相同原文" },
      ],
    });

    const { container } = render(
      <CompleteContent
        node={node}
        selectedItem={null}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    expect(
      container.querySelectorAll('[data-reading-event="block"]'),
    ).toHaveLength(2);
    expect(screen.getAllByText("相同原文")).toHaveLength(2);
  });

  it("renders a bound caption at its own physical position", () => {
    const section = navigationNode("caption-stream", {
      projection_role: "STRUCTURE",
      child_ids: ["caption-stream-body"],
      content_blocks: [
        {
          kind: "figure",
          source_id: "caption-stream-image",
          source_order: 10,
          source_offset: 0,
          src: "/caption-stream.png",
          alt: "系统图",
          caption: "图 5 系统方案",
          caption_source_order: 30,
          caption_source_offset: 0,
        },
      ],
    });
    const body = navigationNode("caption-stream-body", {
      parent_id: section.id,
      depth: 1,
      kind: "DIRECT_CONTENT",
      source_order: 20,
      projection_role: "DIRECT_CONTENT",
      content_blocks: [
        {
          kind: "text",
          source_id: "caption-stream-body-source",
          source_order: 20,
          source_offset: 0,
          text: "图与题注之间的正文",
        },
      ],
    });
    const captionTree: RequirementTreeSnapshot = {
      ...navigationTree(),
      root_node_ids: [section.id],
      nodes: [section, body],
    };

    const { container } = render(
      <CompleteContent
        node={section}
        selectedItem={null}
        tree={captionTree}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    const events = Array.from(
      container.querySelectorAll("[data-reading-event]"),
    );
    expect(events).toHaveLength(3);
    const image = within(events[0] as HTMLElement).getByRole("img");
    expect(image).toHaveAccessibleName("系统图");
    expect(events[1]).toHaveTextContent("图与题注之间的正文");
    expect(events[2]).toHaveAttribute("data-reading-event", "caption");
    expect(events[2]).toHaveTextContent("图 5 系统方案");
    const captionId = image.getAttribute("aria-describedby");
    expect(captionId).toBeTruthy();
    expect(document.getElementById(String(captionId))).toHaveTextContent(
      "图 5 系统方案",
    );
  });

  it("associates a separately ordered caption with a media placeholder", () => {
    const node = navigationNode("caption-placeholder", {
      content_blocks: [
        {
          kind: "figure",
          source_id: "caption-placeholder-image",
          source_order: 10,
          page_start: 4,
          caption: "图 6 占位图片题注",
          caption_source_order: 20,
        },
      ],
    });

    render(
      <CompleteContent
        node={node}
        selectedItem={null}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    const placeholder = screen.getByRole("button", {
      name: "查看原文第 4 页图片",
    });
    const captionId = placeholder.getAttribute("aria-describedby");
    expect(captionId).toBeTruthy();
    expect(document.getElementById(String(captionId))).toHaveTextContent(
      "图 6 占位图片题注",
    );
  });

  it("treats null reading positions as absent instead of physical zero", () => {
    const node = navigationNode("legacy-null-order", {
      source_order: 30,
      content_blocks: [
        {
          kind: "text",
          source_id: "shared-null-span",
          source_order: null as unknown as number,
          source_offset: null as unknown as number,
          start_offset: null,
          end_offset: null,
          text: "使用节点顺序的段落",
        },
        {
          kind: "text",
          source_id: "shared-null-span",
          source_order: 50,
          source_offset: 4,
          start_offset: null,
          end_offset: null,
          text: "同源后续段落",
        },
        {
          kind: "text",
          source_id: "early-source",
          source_order: 10,
          text: "明确较早的段落",
        },
        {
          kind: "figure",
          source_id: "legacy-inline-caption",
          source_order: 20,
          src: "/legacy-inline-caption.png",
          alt: "旧版图片",
          caption: "旧版内联题注",
          caption_source_order: null as unknown as number,
        },
      ],
    });

    const { container } = render(
      <CompleteContent
        node={node}
        selectedItem={null}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    const events = Array.from(
      container.querySelectorAll('[data-reading-event="block"]'),
    );
    expect(events).toHaveLength(4);
    expect(events[0]).toHaveTextContent("明确较早的段落");
    expect(events[1]).toHaveTextContent("旧版内联题注");
    expect(events[2]).toHaveTextContent("使用节点顺序的段落");
    expect(events[3]).toHaveTextContent("同源后续段落");
    expect(
      container.querySelector('[data-reading-event="caption"]'),
    ).toBeNull();
  });

  it("renders a table of contents as page-addressable entries without raw-text fallback", async () => {
    const onPageChange = vi.fn();
    const onStructureNodeChange = vi.fn();
    const toc = navigationNode("toc", {
      kind: "GROUP",
      label: "目 录",
      summary: "1 说明........1 10 附件........54",
      classification: "TOC",
      content_blocks: [
        {
          kind: "text",
          source_id: "toc-raw",
          text: "1 说明........1 10 附件........54",
        },
      ],
      toc_entries: [
        {
          entry_id: "toc-entry-1",
          number: "1",
          title: "说明",
          printed_page_label: "1",
          target_page: 9,
          target_structure_node_id: "section-1",
          source_order: 1,
          source_refs: [],
        },
        {
          entry_id: "toc-entry-10",
          number: "10",
          title: "附件",
          target_page: 54,
          target_structure_node_id: "section-10",
          source_order: 2,
          source_refs: [],
        },
        {
          entry_id: "toc-entry-11",
          number: "11",
          title: "未解析章节",
          printed_page_label: "7",
          target_page: null,
          source_order: 3,
          source_refs: [],
        },
      ],
    });

    render(
      <CompleteContent
        node={toc}
        selectedItem={toc}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={onPageChange}
        onStructureNodeChange={onStructureNodeChange}
        recordFocus={null}
      />,
    );

    expect(
      screen.getByRole("navigation", { name: "目录条目" }),
    ).toHaveTextContent("1说明1");
    expect(
      screen.queryByText("1 说明........1 10 附件........54"),
    ).not.toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "打开 PDF 第 54 页" }),
    );
    expect(onPageChange).toHaveBeenCalledWith(54);
    expect(
      screen.getByRole("button", {
        name: "目录印刷页 1；打开 PDF 第 9 页",
      }),
    ).toHaveTextContent("1");
    await userEvent.click(screen.getByRole("button", { name: "说明" }));
    expect(onStructureNodeChange).toHaveBeenCalledWith("section-1");
    expect(
      screen.queryByRole("button", { name: "打开第 7 页" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByTitle("印刷页码尚未解析到物理 PDF 页"),
    ).toHaveTextContent("7");
  });

  it.each(["DOCUMENT", "GROUP"] as const)(
    "uses a compact navigation overview for a populated root %s instead of mounting the whole document",
    (kind) => {
      const node: RequirementTreeNode = {
        ...treeNode(
          Array.from({ length: 400 }, (_, index) => ({
            kind: "paragraph",
            source_id: `root-content-${index}`,
            text: `整篇根正文 ${index}`,
          })),
        ),
        id: `root-${kind.toLowerCase()}`,
        kind,
        label: "技术标准文档",
        child_ids: ["chapter-1", "chapter-2"],
        review_item_count: 38,
      };

      const { container } = render(
        <CompleteContent
          node={node}
          selectedItem={null}
          pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
          projectId="project"
          documentId="document"
          sourceSha256={sourceSha256}
          onPageChange={vi.fn()}
          recordFocus={null}
        />,
      );

      expect(screen.getByLabelText("文档结构概览")).toBeInTheDocument();
      expect(screen.getByText("请选择具体章节或条款")).toBeInTheDocument();
      expect(screen.getByText("2 项")).toBeInTheDocument();
      expect(screen.getByText("38 项")).toBeInTheDocument();
      expect(screen.queryByText("整篇根正文 399")).not.toBeInTheDocument();
      expect(container.querySelector(".tree-content-blocks")).toBeNull();
    },
  );

  it("keeps complete content for a section container even when it is a root with children", () => {
    const node: RequirementTreeNode = {
      ...treeNode([
        { kind: "paragraph", text: "章节完整正文第一段" },
        { kind: "paragraph", text: "章节完整正文第二段" },
      ]),
      child_ids: ["clause-1"],
    };

    const { container } = render(
      <CompleteContent
        node={node}
        selectedItem={null}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    expect(screen.getByText("章节完整正文第一段")).toBeInTheDocument();
    expect(screen.getByText("章节完整正文第二段")).toBeInTheDocument();
    expect(container.querySelector(".tree-content-blocks")).not.toBeNull();
    expect(screen.queryByLabelText("文档结构概览")).not.toBeInTheDocument();
  });

  it("keeps the complete parent clause and marks the selected direct-content boundary in place", () => {
    const sharedFigure: JsonRecord = {
      kind: "image",
      source_id: "source-shared-figure",
      src: "/shared-figure.png",
      alt: "条款共享方案图",
    };
    const parent: RequirementTreeNode = {
      ...treeNode([
        {
          kind: "text",
          source_id: "source-intro",
          text: "本条款适用于全部供货范围。",
        },
        {
          kind: "text",
          source_id: "source-candidate",
          text: "供应商应提交验证资料。",
        },
        sharedFigure,
        {
          kind: "text",
          source_id: "source-note",
          text: "验证方法见附录。",
        },
      ]),
      id: "parent-clause",
      label: "5.6 验证资料",
      child_ids: ["direct-content"],
    };
    const selected: RequirementTreeNode = {
      ...treeNode([
        {
          kind: "text",
          source_id: "source-candidate",
          text: "供应商应提交验证资料。",
        },
      ]),
      id: "direct-content",
      parent_id: parent.id,
      kind: "REQUIREMENT",
      label: "",
      summary: "供应商应提交验证资料。",
      projection_role: "DIRECT_CONTENT",
      status: "PENDING",
      source_refs: [{ source_id: "source-candidate" }],
    };

    const { container } = render(
      <CompleteContent
        node={parent}
        selectedItem={selected}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "5.6 验证资料" }),
    ).toBeInTheDocument();
    expect(screen.getByText("本条款适用于全部供货范围。")).toBeInTheDocument();
    expect(screen.getByText("验证方法见附录。")).toBeInTheDocument();
    expect(screen.getByText("供应商应提交验证资料。")).toHaveClass(
      "is-review-focus",
    );
    expect(
      screen.getByRole("img", { name: "条款共享方案图" }).closest("figure"),
    ).not.toHaveClass("is-review-focus");
    expect(screen.getByText("当前聚焦")).toBeInTheDocument();
    expect(container.querySelector(".tree-review-focus-fallback")).toBeNull();
    expect(container.querySelector(".requirement-boundary")).toBeNull();
  });

  it("focuses a shared figure only when the selected content explicitly references it", () => {
    const sharedFigure: JsonRecord = {
      kind: "image",
      source_id: "source-linked-figure",
      src: "/linked-figure.png",
      alt: "显式关联方案图",
    };
    const parent: RequirementTreeNode = {
      ...treeNode([
        {
          kind: "text",
          source_id: "source-linked-body",
          text: "系统方案见图 1。",
        },
        sharedFigure,
      ]),
      id: "parent-linked-clause",
      label: "9 系统方案",
      child_ids: ["direct-linked-content"],
    };
    const selected: RequirementTreeNode = {
      ...treeNode([
        {
          kind: "text",
          source_id: "source-linked-body",
          text: "系统方案见图 1。",
        },
      ]),
      id: "direct-linked-content",
      parent_id: parent.id,
      kind: "REQUIREMENT",
      label: "",
      summary: "系统方案见图 1。",
      projection_role: "DIRECT_CONTENT",
      status: "PENDING",
      source_refs: [{ source_id: "source-linked-body" }],
      figure_refs: [sharedFigure],
    };

    render(
      <CompleteContent
        node={parent}
        selectedItem={selected}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    expect(
      screen.getByRole("img", { name: "显式关联方案图" }).closest("figure"),
    ).toHaveClass("is-review-focus");
  });

  it("renders direct content as one exact undecided candidate without page-wide fallback or a repeated boundary card", () => {
    const node: RequirementTreeNode = {
      ...treeNode([
        {
          kind: "table_record",
          record_id: "candidate-record",
          row_bindings: [],
        },
      ]),
      id: "direct-content-1",
      kind: "REQUIREMENT",
      label: "",
      summary: "供应商应提交验证资料",
      projection_role: "DIRECT_CONTENT",
      requirement_entry_id: "candidate-entry-1",
      status: "PENDING",
    };
    const pageSnapshot = normalizeRestorationPage(
      currentPagePayload(undefined, [
        {
          id: "unrelated-page-text",
          type: "TEXT",
          text: "不相关页面正文",
          page_number: 5,
          source_order: 2,
          bbox: [20, 440, 500, 470],
          locatable: true,
        },
      ]),
      5,
    );

    const { container } = render(
      <CompleteContent
        node={node}
        selectedItem={node}
        pageSnapshot={pageSnapshot}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    expect(screen.getByText("条款内容")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        name: "供应商应提交验证资料",
      }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("供应商应提交验证资料")).toHaveLength(2);
    expect(screen.queryByText("不相关页面正文")).not.toBeInTheDocument();
    expect(container.querySelector(".requirement-boundary")).toBeNull();
  });

  it("does not repeat a structure proxy as a nested boundary card", () => {
    const node: RequirementTreeNode = {
      ...treeNode([
        {
          kind: "text",
          source_id: "proxy-source",
          text: "安装空间应满足图纸要求",
        },
      ]),
      id: "proxy-clause",
      kind: "CLAUSE",
      label: "3.2 安装条件",
      projection_role: "STRUCTURE_PROXY",
      status: "PENDING",
    };

    const { container } = render(
      <CompleteContent
        node={node}
        selectedItem={node}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    expect(screen.getAllByText("安装空间应满足图纸要求")).toHaveLength(1);
    expect(container.querySelector(".requirement-boundary")).toBeNull();
  });

  it("uses a figure block's own page geometry across page boundaries", () => {
    const node = treeNode([
      {
        kind: "figure",
        source_id: "figure-source",
        location: {
          page_number: 7,
          bbox: [20, 30, 120, 230],
        },
        alt: "跨页来源图片",
      },
    ]);
    const pageSnapshot = normalizeRestorationPage(currentPagePayload(), 5);

    render(
      <CompleteContent
        node={node}
        selectedItem={null}
        pageSnapshot={pageSnapshot}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    const image = screen.getByRole("img", { name: "跨页来源图片" });
    expect(image).toHaveAttribute("loading", "lazy");
    expect(image).toHaveAttribute("decoding", "async");
    expect(image.getAttribute("src")).toContain("page=7");
    expect(image.getAttribute("src")).toContain("x0=20.000");
    expect(image.getAttribute("src")).not.toContain("page=5");
  });

  it("opens a media-only clause on the page that contains its image", () => {
    const mediaOnly = treeNode([
      {
        kind: "image",
        source_id: "cross-page-image",
        page_start: 7,
      },
    ]);
    const mixed = treeNode([
      {
        kind: "text",
        source_id: "cross-page-intro",
        text: "附件说明。",
        page_start: 5,
      },
      {
        kind: "image",
        source_id: "cross-page-image",
        page_start: 7,
      },
    ]);

    expect(preferredRequirementTreePage(mediaOnly)).toBe(7);
    expect(preferredRequirementTreePage(mixed)).toBe(5);
  });

  it("lets the user load an off-page image when its bbox is page-addressable", () => {
    const onPageChange = vi.fn();
    const node = treeNode([
      {
        kind: "image",
        source_id: "cross-page-image",
        page_start: 7,
      },
    ]);

    render(
      <CompleteContent
        node={node}
        selectedItem={node}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={onPageChange}
        recordFocus={null}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "查看原文第 7 页图片" }),
    );
    expect(onPageChange).toHaveBeenCalledWith(7);
  });

  it("composes continued table segments once without reusing the current page table", () => {
    const first = tableBlock({
      id: "table-segment-one",
      sourceId: "source-table-one",
      page: 5,
      segmentIndex: 0,
      text: "首段唯一值",
    });
    const second = tableBlock({
      id: "table-segment-two",
      sourceId: "source-table-two",
      page: 6,
      segmentIndex: 1,
      text: "续段唯一值",
    });
    const node = treeNode([first, second]);
    const pageSnapshot = normalizeRestorationPage(currentPagePayload(first), 5);

    render(
      <CompleteContent
        node={node}
        selectedItem={null}
        pageSnapshot={pageSnapshot}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    expect(screen.getByText("2 段续表")).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "跨页表格完整内容" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/章节中显示有界预览/)).toBeInTheDocument();
    expect(screen.getAllByRole("table")).toHaveLength(2);
    expect(screen.getAllByText("首段唯一值")).toHaveLength(1);
    expect(screen.getAllByText("续段唯一值")).toHaveLength(1);
  });

  it("retains record focus when the selected record is in a later segment", () => {
    const first = tableBlock({
      id: "table-segment-one",
      sourceId: "source-table-one",
      page: 5,
      segmentIndex: 0,
      text: "首段唯一值",
    });
    const second = tableBlock({
      id: "table-segment-two",
      sourceId: "source-table-two",
      page: 6,
      segmentIndex: 1,
      text: "当前记录",
    });
    const node = {
      ...treeNode([first, second]),
      child_ids: ["table-record-node"],
    };
    const selectedItem: RequirementTreeNode = {
      ...treeNode([
        {
          kind: "table_record",
          record_id: "record-two",
          logical_table_id: "logical-table-cross-page",
        },
      ]),
      id: "table-record-node",
      parent_id: node.id,
      kind: "TABLE_RECORD",
      label: "当前记录",
      summary: "当前记录",
      projection_role: "TABLE_RECORD",
      table_record_id: "record-two",
      status: "PENDING",
    };
    const pageSnapshot = normalizeRestorationPage(currentPagePayload(first), 5);

    render(
      <CompleteContent
        node={node}
        selectedItem={selectedItem}
        pageSnapshot={pageSnapshot}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={{
          recordId: "record-two",
          recordLabel: "当前记录",
          mode: "ROW",
          logicalTableId: "logical-table-cross-page",
          rowBindings: [
            {
              tableId: "table-segment-two",
              index: 0,
              cellIds: ["table-segment-two-cell"],
            },
          ],
          columnBindings: [],
          headerCellIdsByTable: {},
          headerTexts: [],
          sharedCellIdsByTable: {},
          sharedValues: [],
          notes: [],
          imageCount: 0,
        }}
      />,
    );

    expect(
      screen.getByRole("region", { name: "当前表格条目" }),
    ).toHaveTextContent("当前记录");
    expect(screen.getByRole("cell", { name: "当前记录" })).toHaveClass(
      "is-record-focus-cell",
    );
    expect(screen.getByText("首段唯一值")).toBeInTheDocument();
    expect(screen.getByText("2 段续表")).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "跨页表格完整内容" }),
    ).toHaveClass("is-review-focus");
  });

  it("shows only the focused record for an exact table candidate leaf", () => {
    const first = tableBlock({
      id: "table-segment-one",
      sourceId: "source-table-one",
      page: 5,
      segmentIndex: 0,
      text: "首段唯一值",
    });
    const second = tableBlock({
      id: "table-segment-two",
      sourceId: "source-table-two",
      page: 6,
      segmentIndex: 1,
      text: "当前记录",
    });
    const node: RequirementTreeNode = {
      ...treeNode([first, second]),
      id: "table-record-node",
      kind: "TABLE_RECORD",
      label: "当前记录",
      summary: "当前记录",
      projection_role: "TABLE_RECORD",
      table_record_id: "record-two",
      status: "PENDING",
    };
    const pageSnapshot = normalizeRestorationPage(currentPagePayload(first), 5);

    const { container } = render(
      <CompleteContent
        node={node}
        selectedItem={node}
        pageSnapshot={pageSnapshot}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={{
          recordId: "record-two",
          recordLabel: "当前记录",
          mode: "ROW",
          logicalTableId: "logical-table-cross-page",
          rowBindings: [
            {
              tableId: "table-segment-two",
              index: 0,
              cellIds: ["table-segment-two-cell"],
            },
          ],
          columnBindings: [],
          headerCellIdsByTable: {},
          headerTexts: [],
          sharedCellIdsByTable: {},
          sharedValues: [],
          notes: [],
          imageCount: 0,
        }}
      />,
    );

    expect(screen.getAllByText("表格条目").length).toBeGreaterThan(0);
    expect(screen.queryByText("首段唯一值")).not.toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "当前记录" })).toHaveClass(
      "is-record-focus-cell",
    );
    expect(container.querySelector(".requirement-boundary")).toBeNull();
  });

  it("renders a whole-table review leaf as one complete structured table instead of flattened summary text", () => {
    const first = tableBlock({
      id: "table-segment-one",
      sourceId: "source-table-one",
      page: 5,
      segmentIndex: 0,
      text: "首段唯一值",
    });
    const second = tableBlock({
      id: "table-segment-two",
      sourceId: "source-table-two",
      page: 6,
      segmentIndex: 1,
      text: "续段唯一值",
    });
    const node: RequirementTreeNode = {
      ...treeNode([first, second]),
      id: "whole-table-review",
      kind: "TABLE",
      label: "表 10-1 性能参数",
      summary: "首段唯一值 | 续段唯一值",
      projection_role: "WHOLE_TABLE_REVIEW",
      review_item_count: 1,
      table_refs: [first, second],
      status: "PENDING",
    };

    const { container } = render(
      <CompleteContent
        node={node}
        selectedItem={node}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(first), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    expect(screen.getByText("完整表格")).toBeInTheDocument();
    expect(screen.getByText("2 段续表")).toBeInTheDocument();
    expect(screen.getAllByRole("table")).toHaveLength(2);
    expect(screen.getAllByText("首段唯一值")).toHaveLength(1);
    expect(screen.getAllByText("续段唯一值")).toHaveLength(1);
    expect(
      screen.queryByText("首段唯一值 | 续段唯一值"),
    ).not.toBeInTheDocument();
    expect(container.querySelector(".requirement-boundary")).toBeNull();
  });

  it("composes a section with its child clauses once and suppresses duplicated table-cell text", () => {
    const table: JsonRecord = {
      kind: "table",
      id: "responsibility-table",
      source_id: "table-source",
      source_order: 20,
      cells: [
        { id: "cell-duty", row: 0, column: 0, text: "任务活动" },
        { id: "cell-owner", row: 0, column: 1, text: "供应商" },
      ],
    };
    const section = navigationNode("section-1.3", {
      label: "1.3 职责与分工",
      child_ids: ["intro", "clause-a", "clause-e", "whole-table"],
      content_blocks: [
        { kind: "text", source_id: "intro-source", text: "职责说明" },
        table,
        { kind: "text", source_id: "cell-duty", text: "任务活动" },
        { kind: "text", source_id: "cell-owner", text: "供应商" },
      ],
    });
    const intro = navigationNode("intro", {
      parent_id: section.id,
      depth: 1,
      source_order: 1,
      kind: "REQUIREMENT",
      projection_role: "DIRECT_CONTENT",
      content_blocks: [
        { kind: "text", source_id: "intro-source", text: "职责说明" },
      ],
    });
    const clauseA = navigationNode("clause-a", {
      parent_id: section.id,
      depth: 1,
      source_order: 2,
      kind: "CLAUSE",
      projection_role: "STRUCTURE_PROXY",
      label: "a) 提交开发资料",
      content_blocks: [
        { kind: "text", source_id: "a-source", text: "a) 提交开发资料" },
      ],
    });
    const clauseE = navigationNode("clause-e", {
      parent_id: section.id,
      depth: 1,
      source_order: 3,
      kind: "CLAUSE",
      projection_role: "STRUCTURE_PROXY",
      label: "e) 对质量负责",
      content_blocks: [
        { kind: "text", source_id: "e-source", text: "e) 对质量负责" },
      ],
    });
    const wholeTable = navigationNode("whole-table", {
      parent_id: section.id,
      depth: 1,
      source_order: 4,
      kind: "TABLE",
      projection_role: "WHOLE_TABLE_REVIEW",
      content_blocks: [table],
    });
    const sectionTree: RequirementTreeSnapshot = {
      ...navigationTree(),
      root_node_ids: [section.id],
      nodes: [section, intro, clauseA, clauseE, wholeTable],
    };

    const { container } = render(
      <CompleteContent
        node={section}
        selectedItem={null}
        tree={sectionTree}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    expect(screen.getByText("职责说明")).toBeInTheDocument();
    expect(screen.getByText("a) 提交开发资料")).toBeInTheDocument();
    expect(screen.getByText("e) 对质量负责")).toBeInTheDocument();
    expect(screen.getAllByText("任务活动")).toHaveLength(1);
    expect(screen.getAllByText("供应商")).toHaveLength(1);
    expect(
      container.querySelector(".tree-content-table-block.is-context-preview"),
    ).not.toBeNull();
  });

  it("keeps a label/body-separated section subtree readable instead of flattening descendant text", () => {
    const section = navigationNode("section-1.2", {
      label: "1.2 报价要求",
      projection_role: "STRUCTURE",
      child_ids: ["section-1.2.1", "section-1.2.3"],
      content_blocks: [],
    });
    const basicPrinciples = navigationNode("section-1.2.1", {
      parent_id: section.id,
      depth: 1,
      source_order: 1,
      label: "1.2.1 基本原则",
      projection_role: "STRUCTURE_PROXY",
      content_blocks: [
        {
          kind: "text",
          source_id: "basic-principles-body",
          text: "报价应覆盖完成供货所需的全部活动。",
        },
      ],
    });
    const nonConformingOffer = navigationNode("section-1.2.3", {
      parent_id: section.id,
      depth: 1,
      source_order: 2,
      label: "1.2.3 非一致性报价",
      projection_role: "STRUCTURE",
      child_ids: ["section-1.2.3.1", "section-1.2.3.2"],
      content_blocks: [],
    });
    const cannotMeet = navigationNode("section-1.2.3.1", {
      parent_id: nonConformingOffer.id,
      depth: 2,
      source_order: 3,
      label: "1.2.3.1 无法满足要求",
      projection_role: "STRUCTURE_PROXY",
      content_blocks: [
        {
          kind: "text",
          source_id: "cannot-meet-body",
          text: "无法满足时必须提交书面说明。",
        },
      ],
    });
    const equivalentAlternative = navigationNode("section-1.2.3.2", {
      parent_id: nonConformingOffer.id,
      depth: 2,
      source_order: 4,
      label: "1.2.3.2 等效替代",
      projection_role: "STRUCTURE_PROXY",
      content_blocks: [
        {
          kind: "text",
          source_id: "alternative-body",
          text: "等效替代必须经过书面批准。",
        },
      ],
    });
    const sectionTree: RequirementTreeSnapshot = {
      ...navigationTree(),
      root_node_ids: [section.id],
      nodes: [
        section,
        basicPrinciples,
        nonConformingOffer,
        cannotMeet,
        equivalentAlternative,
      ],
    };

    const { container } = render(
      <CompleteContent
        node={section}
        selectedItem={null}
        tree={sectionTree}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    expect(screen.getByRole("heading", { name: "1.2 报价要求" })).toHaveClass(
      "tree-content-focus-title",
    );
    expect(
      screen.getByRole("heading", { name: "1.2.1 基本原则" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "1.2.3 非一致性报价" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "1.2.3.1 无法满足要求" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("报价应覆盖完成供货所需的全部活动。"),
    ).not.toHaveClass("is-review-focus");
    expect(screen.getByText("无法满足时必须提交书面说明。")).not.toHaveClass(
      "is-review-focus",
    );
    const pureContainer = container.querySelector(
      '[data-content-node-id="section-1.2.3"]',
    );
    expect(pureContainer).not.toBeNull();
    expect(
      within(pureContainer as HTMLElement).getByText("仅标题 · 2 个子条款"),
    ).toBeInTheDocument();
  });

  it("renders a content structure proxy once when its label is assembled from consecutive physical text blocks", () => {
    const firstPhysicalBlock: JsonRecord = {
      kind: "text",
      source_id: "f06-b-line-1",
      source_order: 11,
      text: "b) 按照广汽集团提供的要求和开发计划等对清单中零件进行硬件开发或者选型，进行零部件的生",
    };
    const secondPhysicalBlock: JsonRecord = {
      kind: "text",
      source_id: "f06-b-line-2",
      source_order: 12,
      text: "产。",
    };
    const section = navigationNode("section-1.3", {
      label: "1.3 职责与分工",
      projection_role: "STRUCTURE",
      child_ids: ["section-1.3-b", "section-1.3-table"],
      content_blocks: [],
    });
    const splitClause = navigationNode("section-1.3-b", {
      parent_id: section.id,
      depth: 1,
      source_order: 11,
      kind: "CLAUSE",
      label:
        "b) 按照广汽集团提供的要求和开发计划等对清单中零件进行硬件开发或者选型，进行零部件的生产。",
      projection_role: "STRUCTURE_PROXY",
      content_blocks: [firstPhysicalBlock, secondPhysicalBlock],
    });
    const tableBlock: JsonRecord = {
      kind: "table",
      id: "f06-responsibility-table",
      source_order: 20,
      cells: [
        { id: "f06-duty", row: 0, column: 0, text: "任务活动" },
        { id: "f06-supplier", row: 0, column: 1, text: "供应商" },
      ],
    };
    const table = navigationNode("section-1.3-table", {
      parent_id: section.id,
      depth: 1,
      source_order: 20,
      kind: "TABLE",
      projection_role: "WHOLE_TABLE_REVIEW",
      content_blocks: [tableBlock],
    });
    const sectionTree: RequirementTreeSnapshot = {
      ...navigationTree(),
      root_node_ids: [section.id],
      nodes: [section, splitClause, table],
    };

    const { container } = render(
      <CompleteContent
        node={section}
        selectedItem={null}
        tree={sectionTree}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    expect(
      screen.getByRole("heading", {
        name: "b) 按照广汽集团提供的要求和开发计划等对清单中零件进行硬件开发或者选型，进行零部件的生产。",
      }),
    ).toBeInTheDocument();
    const clauseGroup = container.querySelector(
      '[data-content-node-id="section-1.3-b"]',
    );
    expect(clauseGroup).not.toBeNull();
    expect(
      (clauseGroup as HTMLElement).querySelector(".tree-content-node-body"),
    ).toBeNull();
    expect(
      screen.queryByText(firstPhysicalBlock.text as string),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(secondPhysicalBlock.text as string),
    ).not.toBeInTheDocument();
    expect(screen.getAllByText("任务活动")).toHaveLength(1);
    expect(screen.getAllByText("供应商")).toHaveLength(1);
  });

  it("explains a selected pure structure container while keeping child bodies neutral", () => {
    const containerNode = navigationNode("section-1.2.3", {
      label: "1.2.3 非一致性报价",
      projection_role: "STRUCTURE",
      child_ids: ["section-1.2.3.1", "section-1.2.3.2"],
      content_blocks: [],
    });
    const firstChild = navigationNode("section-1.2.3.1", {
      parent_id: containerNode.id,
      depth: 1,
      source_order: 1,
      label: "1.2.3.1 无法满足要求",
      projection_role: "STRUCTURE_PROXY",
      content_blocks: [
        { kind: "text", source_id: "first-body", text: "第一项正文" },
      ],
    });
    const secondChild = navigationNode("section-1.2.3.2", {
      parent_id: containerNode.id,
      depth: 1,
      source_order: 2,
      label: "1.2.3.2 等效替代",
      projection_role: "STRUCTURE_PROXY",
      content_blocks: [
        { kind: "text", source_id: "second-body", text: "第二项正文" },
      ],
    });
    const sectionTree: RequirementTreeSnapshot = {
      ...navigationTree(),
      root_node_ids: [containerNode.id],
      nodes: [containerNode, firstChild, secondChild],
    };

    render(
      <CompleteContent
        node={containerNode}
        selectedItem={null}
        tree={sectionTree}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    expect(screen.getByText("仅标题 · 2 个子条款")).toBeInTheDocument();
    expect(screen.getByText("第一项正文")).not.toHaveClass("is-review-focus");
    expect(screen.getByText("第二项正文")).not.toHaveClass("is-review-focus");
    expect(
      screen.getByRole("heading", { name: "1.2.3.1 无法满足要求" }),
    ).toBeInTheDocument();
  });

  it("keeps a selected structure proxy on its own scope and focuses only its direct body", () => {
    const parent = navigationNode("section-1.2", {
      label: "1.2 报价要求",
      projection_role: "STRUCTURE",
      child_ids: ["section-1.2.1"],
      content_blocks: [],
    });
    const selected = navigationNode("section-1.2.1", {
      parent_id: parent.id,
      depth: 1,
      source_order: 1,
      label: "1.2.1 基本原则",
      projection_role: "STRUCTURE_PROXY",
      content_blocks: [
        {
          kind: "text",
          source_id: "basic-principles-direct-body",
          text: "报价应包含完成供货所需的全部活动。",
        },
      ],
    });
    const sectionTree: RequirementTreeSnapshot = {
      ...navigationTree(),
      root_node_ids: [parent.id],
      nodes: [parent, selected],
    };

    render(
      <CompleteContent
        node={selected}
        selectedItem={selected}
        tree={sectionTree}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    expect(screen.getByRole("heading", { name: "1.2.1 基本原则" })).toHaveClass(
      "tree-content-focus-title",
    );
    expect(
      screen.queryByRole("heading", { name: "1.2 报价要求" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("报价应包含完成供货所需的全部活动。")).toHaveClass(
      "is-review-focus",
    );
  });

  it("renders a descendant figure from its own source crop inside a structured subtree", () => {
    const appendix = navigationNode("appendix-10", {
      label: "10 附件",
      projection_role: "STRUCTURE",
      child_ids: ["appendix-10.4"],
      content_blocks: [],
    });
    const imageAppendix = navigationNode("appendix-10.4", {
      parent_id: appendix.id,
      depth: 1,
      source_order: 1,
      label: "10.4 附录3：电能管理报价技术资料",
      projection_role: "STRUCTURE_PROXY",
      summary: "附录图片",
      content_blocks: [
        {
          kind: "figure",
          source_id: "appendix-figure-source",
          location: { page_number: 59, bbox: [40, 80, 420, 640] },
          alt: "附录3原始图片",
        },
      ],
      figure_refs: [{ source_id: "appendix-figure-source" }],
    });
    const appendixTree: RequirementTreeSnapshot = {
      ...navigationTree(),
      root_node_ids: [appendix.id],
      nodes: [appendix, imageAppendix],
    };

    render(
      <CompleteContent
        node={appendix}
        selectedItem={null}
        tree={appendixTree}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    expect(
      screen.getByRole("heading", {
        name: "10.4 附录3：电能管理报价技术资料",
      }),
    ).toBeInTheDocument();
    const image = screen.getByRole("img", { name: "附录3原始图片" });
    expect(image.getAttribute("src")).toContain("page=59");
    expect(image.getAttribute("src")).toContain("x0=40.000");
  });

  it("labels unmatched summary content as a neutral degraded view instead of exact source content", () => {
    const parent = navigationNode("fallback-parent", {
      kind: "CLAUSE",
      child_ids: ["fallback-child"],
      content_blocks: [
        { kind: "text", source_id: "other", text: "父条款正文" },
      ],
    });
    const selected = navigationNode("fallback-child", {
      parent_id: parent.id,
      depth: 1,
      kind: "REQUIREMENT",
      projection_role: "DIRECT_CONTENT",
      summary: "第一行\n第二行",
      source_refs: [{ source_id: "missing-source" }],
      content_blocks: [],
    });

    render(
      <CompleteContent
        node={parent}
        selectedItem={selected}
        pageSnapshot={normalizeRestorationPage(currentPagePayload(), 5)}
        projectId="project"
        documentId="document"
        sourceSha256={sourceSha256}
        onPageChange={vi.fn()}
        recordFocus={null}
      />,
    );

    expect(
      screen.getByRole("region", { name: "当前候选来源内容降级显示" }),
    ).toHaveTextContent("来源内容暂未原位匹配 · 降级显示");
    expect(screen.getByText(/第一行\s+第二行/)).toBeInTheDocument();
  });
});

describe("requirementTreeReparentOperations", () => {
  it("moves every editable physical node in contract order and appends them atomically", () => {
    const structureNodeById = new Map<string, JsonRecord>([
      ["content-a", { id: "content-a", parent_id: "clause-e" }],
      ["content-b", { id: "content-b", parent_id: "clause-e" }],
      ["existing", { id: "existing", parent_id: "section-7.1" }],
    ]);

    expect(
      requirementTreeReparentOperations({
        editableStructureNodeIds: ["content-a", "content-b", "content-a"],
        targetStructureNodeId: "section-7.1",
        structureNodeById,
        targetChildIds: ["existing"],
      }),
    ).toEqual([
      {
        operation: "re_parent",
        structure_node_id: "content-a",
        new_parent_structure_node_id: "section-7.1",
        new_sibling_index: 1,
      },
      {
        operation: "re_parent",
        structure_node_id: "content-b",
        new_parent_structure_node_id: "section-7.1",
        new_sibling_index: 2,
      },
    ]);
  });
});

function navigationNode(
  id: string,
  overrides: Partial<RequirementTreeNode> = {},
): RequirementTreeNode {
  return {
    id,
    parent_id: null,
    kind: "SECTION",
    depth: 0,
    source_order: 0,
    label: id,
    summary: `${id} content`,
    child_ids: [],
    structure_node_id: id,
    requirement_entry_id: null,
    workflow_node_id: null,
    page_start: 1,
    page_end: 1,
    source_refs: [],
    requirement_count: 0,
    attention_count: 0,
    reviewed_count: 0,
    status: "READY",
    classification: "STRUCTURE",
    include_in_view: true,
    content_blocks: [{ kind: "text", text: `${id} content` }],
    table_refs: [],
    figure_refs: [],
    ...overrides,
  };
}

function navigationTree(): RequirementTreeSnapshot {
  const root = navigationNode("persist-root", {
    kind: "DOCUMENT",
    label: "技术规范",
    child_ids: ["persist-section"],
  });
  const section = navigationNode("persist-section", {
    parent_id: root.id,
    depth: 1,
    source_order: 1,
    label: "2 技术要求",
    child_ids: ["persist-leaf"],
  });
  const leaf = navigationNode("persist-leaf", {
    parent_id: section.id,
    depth: 2,
    source_order: 2,
    kind: "CLAUSE",
    label: "2.3.1 持久化叶节点",
    projection_role: "STRUCTURE_PROXY",
    classification: "CLAUSE",
  });
  return {
    schema_version: "requirement-tree-snapshot/v1",
    source_identity: {
      project_id: "persist-project",
      document_id: "persist-document",
      source_ir_id: "source-ir",
      source_sha256: sourceSha256,
      preview_generation_id: "preview",
      requirement_set_id: "persist-set",
    },
    structure_identity: {
      base_snapshot_id: "base",
      effective_snapshot_id: "effective",
      effective_tree_sha256: "c".repeat(64),
      revision: 1,
    },
    mode: "WORKING",
    state: "READY",
    root_node_ids: [root.id],
    nodes: [root, section, leaf],
  };
}

function tableRowGroupNavigationTree(): RequirementTreeSnapshot {
  const table = navigationNode("logical-table-manual", {
    kind: "TABLE",
    label: "表 4 复杂技术参数",
    projection_role: "WHOLE_TABLE_REVIEW",
    classification: "TABLE",
    table_refs: [
      {
        logical_table_id: "logical-table-manual",
        table_id: "physical-table-1",
      },
    ],
    content_blocks: [],
  });
  return {
    ...navigationTree(),
    root_node_ids: [table.id],
    nodes: [table],
    table_record_projections: [
      {
        projection_id: "table-record-projection-manual",
        projection_revision: 4,
        mode: "WHOLE_TABLE",
        records: [],
        decision: {
          reason_code: "NO_PROVEN_RECORD_BOUNDARY",
          message: "复杂表格保留整表，等待人工确认行组。",
        },
        table_fact: {
          owner_structure_node_id: table.structure_node_id,
          logical_table_id: "logical-table-manual",
        },
      },
    ],
  };
}

function tableRowGroupReview() {
  const rows = [1, 2, 3].map((rowIndex) => ({
    table_id: "physical-table-1",
    row_index: rowIndex,
    page_number: rowIndex === 3 ? 9 : 8,
    display_text: `业务行 ${rowIndex}`,
  }));
  return {
    schema_version: "table-record-projection-row-group-review/v1" as const,
    project_id: "persist-project",
    document_id: "persist-document",
    projection_id: "table-record-projection-manual",
    logical_table_id: "logical-table-manual",
    expected_projection_revision: 4,
    expected_source_sha256: sourceSha256,
    expected_structure_snapshot_id:
      "effective-structure-" + "a".repeat(32),
    editable: true,
    read_only_reason: null,
    eligible_rows: rows,
    current_row_groups: [],
    suggested_row_groups: [{ rows }],
  };
}

function classificationResponseForTree(
  tree: RequirementTreeSnapshot,
  options: {
    status?: EntryClassificationStatus;
    type?: EntryClassificationType | null;
    snapshotId?: string;
    payloadSha256?: string;
  } = {},
): EntryClassificationResponse {
  const status = options.status || "ACCEPTED";
  const entryType = options.type === undefined ? "TECHNICAL" : options.type;
  const classifiableRoles = new Set([
    "STRUCTURE_PROXY",
    "DIRECT_CONTENT",
    "WHOLE_TABLE_REVIEW",
    "TABLE_RECORD",
    "REQUIREMENT",
  ]);
  const entries = tree.nodes
    .filter(
      (node) =>
        node.include_in_view &&
        classifiableRoles.has(String(node.projection_role || "")),
    )
    .map((node) => ({
      tree_node_id: node.id,
      workflow_node_id: node.workflow_node_id || node.id,
      projection_role: String(node.projection_role || ""),
      suggested_type: null,
      suggestion_source: "NONE" as const,
      effective_type: entryType,
      human_override: entryType,
      reason_codes: ["NO_DEDICATED_CLASSIFICATION_SUGGESTION"],
      confidence: null,
    }));
  const technical = entries.filter(
    (entry) => entry.effective_type === "TECHNICAL",
  ).length;
  const nonTechnical = entries.filter(
    (entry) => entry.effective_type === "NON_TECHNICAL",
  ).length;
  const information = entries.filter(
    (entry) => entry.effective_type === "INFORMATION",
  ).length;
  const unclassified = entries.filter(
    (entry) => entry.effective_type === null,
  ).length;
  return {
    schema_version: "entry-classification-response/v1",
    snapshot: {
      schema_version: "entry-classification-snapshot/v1",
      snapshot_id: options.snapshotId || `${status.toLowerCase()}-fixture`,
      payload_sha256: options.payloadSha256 || "e".repeat(64),
      project_id: tree.source_identity.project_id,
      sor_document_id: tree.source_identity.document_id,
      source_sha256: tree.source_identity.source_sha256,
      working_tree_id: "d".repeat(64),
      working_tree_sha256: "d".repeat(64),
      effective_structure_id: tree.structure_identity.effective_snapshot_id,
      semantic_snapshot_id: "",
      policy_version: "entry-classification-policy/v1",
      revision: status === "ACCEPTED" ? 2 : 1,
      status,
      entries,
      supersedes_id: "initial-fixture",
      created_at: "2026-08-07T00:00:00Z",
      accepted_at: status === "ACCEPTED" ? "2026-08-07T00:01:00Z" : "",
      accepted_by: status === "ACCEPTED" ? "LOCAL_USER" : "",
    },
    counts: {
      total: entries.length,
      technical,
      non_technical: nonTechnical,
      information,
      unclassified,
      human_override: entries.length - unclassified,
    },
    can_accept: status === "WORKING" && unclassified === 0,
    can_publish: status === "ACCEPTED" && unclassified === 0,
    actions: {
      can_override: status === "WORKING",
      can_accept: status === "WORKING" && unclassified === 0,
      can_publish: status === "ACCEPTED" && unclassified === 0,
    },
    changed: false,
    serving_impact: "NONE",
  };
}

function atomizationScopeReceipt(
  overrides: Partial<RequirementAtomizationScopeReceipt> = {},
): RequirementAtomizationScopeReceipt {
  return {
    schema_version: "atomization-scope-receipt/v1",
    receipt_id: "atomization-scope-receipt-" + "1".repeat(32),
    payload_sha256: "2".repeat(64),
    scope_revision: 3,
    operation: "APPLY",
    scope: "BRANCH",
    requested_review_node_ids: ["persist-leaf"],
    executed_review_node_ids: ["persist-leaf"],
    new_review_node_ids: ["persist-leaf"],
    reused_review_node_ids: [],
    skipped_targets: [],
    succeeded_review_node_ids: ["persist-leaf"],
    failed_review_node_ids: [],
    pass_through_review_node_ids: [],
    cumulative_review_node_ids: ["persist-leaf"],
    provider_call_count: 1,
    checkpoint_created_count: 1,
    checkpoint_reuse_count: 0,
    resumable: false,
    active_revision: 1,
    entry_classification_content_sha256: "f".repeat(64),
    atomization_run_id: "atomization-run-" + "6".repeat(32),
    atomization_run_revision: 4,
    atomization_run_sha256: "7".repeat(64),
    publication_preview_id: "requirement-publication-preview-" + "8".repeat(32),
    publication_preview_sha256: "9".repeat(64),
    publication_key: "a".repeat(64),
    publication_lineage_sha256: "b".repeat(64),
    ...overrides,
  };
}

function semanticPublicationCapability(): RequirementSemanticPublicationCapabilityV2 {
  return {
    schema_version: "requirement-enrichment-semantic-publication-capability/v2",
    preview_request_schema_version:
      "requirement-enrichment-publication-preview-request/v2",
    preview_response_schema_version:
      "requirement-enrichment-publication-preview-response/v2",
    publish_request_schema_version:
      "requirement-enrichment-publish-draft-request/v2",
    publish_response_schema_version:
      "requirement-enrichment-publish-draft-response/v2",
    atomization_states: ["NOT_RUN", "APPLIED"],
    not_run_preview_invokes_atomization: false,
    publish_starts_analysis: false,
    default_dispositions: {
      TECHNICAL: "ANALYZE",
      NON_TECHNICAL: "RETAIN",
      INFORMATION: "REFERENCE",
    },
  };
}

function semanticPublicationPreviewResponse(
  classification: EntryClassificationResponse,
  request: RequirementSemanticPublicationPreviewRequestV2,
  overrides: Partial<RequirementSemanticPublicationPreviewResponseV2> = {},
): RequirementSemanticPublicationPreviewResponseV2 {
  const atomizationApplied = request.atomization_state === "APPLIED";
  const counts = {
    total: 1,
    default_analyze: 1,
    default_retain: 0,
    reference: 0,
    atomized: atomizationApplied ? 1 : 0,
  };
  const classificationEntry = classification.snapshot.entries[0];
  if (!classificationEntry) {
    throw new Error("semantic publication fixture needs one business row");
  }
  const sourceRefs = [
    {
      kind: "SOURCE_REF",
      source_id: "persist-leaf-source",
      owner_structure_node_id: "persist-leaf",
      start_offset: 0,
      end_offset: 20,
      page_start: 1,
      page_end: 1,
    },
  ];
  const sourceSpans = [
    {
      semantic_unit_id: "semantic-unit-" + "1".repeat(32),
      owner_structure_node_id: "persist-leaf",
      start_offset: 0,
      end_offset: 20,
    },
  ];
  const row: RequirementSemanticPublicationProjectionRowV2 = {
    requirement_tree_node_id: classificationEntry.tree_node_id,
    source_working_tree_node_id: classificationEntry.tree_node_id,
    parent_requirement_tree_node_id: null,
    workflow_node_id: classificationEntry.workflow_node_id,
    atomization_node_id: atomizationApplied
      ? "atomization-node-" + "4".repeat(32)
      : null,
    atomic_requirement_id: atomizationApplied
      ? "atomic-obligation-" + "5".repeat(32)
      : null,
    classification: "TECHNICAL",
    entry_kind: "REQUIREMENT",
    builder_kind: "REQUIREMENT",
    decision_mode: "DIRECT",
    builder_binding_role: classificationEntry.projection_role,
    final_projection_role: "REQUIREMENT",
    intrinsic_analysis_eligible: true,
    default_disposition: "ANALYZE",
    source_order: 2,
    source_offset: 0,
    label: "2.3.1 持久化叶节点",
    original_statement: "persist-leaf content",
    published_statement: "persist-leaf content",
    structure_node_id: "persist-leaf",
    page_start: 1,
    page_end: 1,
    source_spans: sourceSpans,
    source_refs: sourceRefs,
    status: "READY",
    table_record_id: null,
    table_evidence_binding_id: null,
    deterministic_rule_evidence: [],
    include_in_view: true,
    content_blocks: [
      {
        kind: "text",
        text: "persist-leaf content",
        source_id: "persist-leaf-source",
        structure_node_id: "persist-leaf",
      },
    ],
    editable_structure_node_ids: ["persist-leaf"],
    table_refs: [],
    figure_refs: [],
  };
  const projection = {
    schema_version: "requirement-enrichment-publication-projection/v2" as const,
    project_id: "persist-project",
    document_id: "persist-document",
    source_sha256: classification.snapshot.source_sha256,
    effective_structure_id: classification.snapshot.effective_structure_id,
    accepted_working_tree_id: classification.snapshot.working_tree_id,
    accepted_working_tree_sha256: classification.snapshot.working_tree_sha256,
    entry_classification_snapshot_id: classification.snapshot.snapshot_id,
    entry_classification_revision: classification.snapshot.revision,
    entry_classification_sha256: classification.snapshot.payload_sha256,
    entry_classification_content_sha256: "f".repeat(64),
    atomization_state: request.atomization_state,
    atomization_scope_receipt_id:
      request.atomization_state === "APPLIED"
        ? request.atomization_scope_receipt_id
        : "",
    atomization_scope_receipt_sha256:
      request.atomization_state === "APPLIED"
        ? request.atomization_scope_receipt_sha256
        : "",
    atomization_scope_revision:
      request.atomization_state === "APPLIED"
        ? request.atomization_scope_revision
        : 0,
    atomization_active_revision: atomizationApplied ? 1 : 0,
    atomization_classification_content_sha256: atomizationApplied
      ? "f".repeat(64)
      : "",
    atomization_run_id: atomizationApplied
      ? "atomization-run-" + "6".repeat(32)
      : "",
    atomization_revision: atomizationApplied ? 4 : 0,
    atomization_run_sha256: atomizationApplied ? "7".repeat(64) : "",
    publication_preview_id: atomizationApplied
      ? "requirement-publication-preview-" + "8".repeat(32)
      : "",
    publication_preview_sha256: atomizationApplied ? "9".repeat(64) : "",
    publication_key: atomizationApplied ? "a".repeat(64) : "",
    publication_lineage_sha256: atomizationApplied ? "b".repeat(64) : "",
    applied_review_node_ids: atomizationApplied
      ? [classificationEntry.workflow_node_id]
      : [],
    source_business_tree_node_ids: classification.snapshot.entries.map(
      (entry) => entry.tree_node_id,
    ),
    rows: [row],
    projection_sha256: "",
  };
  projection.projection_sha256 = canonicalJsonSha256(
    withoutCanonicalField(projection, "projection_sha256"),
  );
  const bindingPreview = {
    schema_version:
      "requirement-enrichment-publication-binding-preview/v2" as const,
    binding_preview_id:
      "requirement-enrichment-binding-preview-" + "3".repeat(32),
    project_id: "persist-project",
    document_id: "persist-document",
    session_id: "review-session",
    review_revision: 7,
    base_active_requirement_set_id: "persist-set",
    projection,
    payload_sha256: "",
  };
  bindingPreview.payload_sha256 = canonicalJsonSha256(
    withoutCanonicalField(bindingPreview, "payload_sha256"),
  );
  return {
    schema_version: "requirement-enrichment-publication-preview-response/v2",
    project_id: "persist-project",
    document_id: "persist-document",
    serving_impact: "NONE",
    analysis_started: false,
    atomization_invoked: false,
    classification: {
      snapshot_id: classification.snapshot.snapshot_id,
      revision: classification.snapshot.revision,
      sha256: classification.snapshot.payload_sha256,
    },
    binding_preview: bindingPreview,
    counts,
    ...overrides,
  };
}

function semanticPublishDraftResponse(
  tree: RequirementTreeSnapshot,
  classification: EntryClassificationResponse,
  preview: RequirementSemanticPublicationPreviewResponseV2,
  overrides: Partial<RequirementSemanticPublishDraftResponseV2> = {},
): RequirementSemanticPublishDraftResponseV2 {
  const bindingId = "requirement-enrichment-binding-" + "7".repeat(32);
  const previewRow = preview.binding_preview.projection.rows[0];
  if (!previewRow)
    throw new Error("semantic publication fixture needs one row");
  const requirementEntryId = `entry-${previewRow.requirement_tree_node_id}`;
  const evidenceSha256 = canonicalJsonSha256({
    atomic_requirement_id: previewRow.atomic_requirement_id,
    requirement_entry_id: requirementEntryId,
    source_refs: previewRow.source_refs,
    source_spans: previewRow.source_spans,
    table_evidence_binding_id: previewRow.table_evidence_binding_id,
  });
  const evidenceBinding = {
    schema_version: "requirement-evidence-binding/v1",
    binding_id: `requirement-evidence-binding-${evidenceSha256.slice(0, 32)}`,
    requirement_entry_id: requirementEntryId,
    atomic_requirement_id: previewRow.atomic_requirement_id,
    source_spans: previewRow.source_spans,
    source_refs: previewRow.source_refs,
    table_evidence_binding_id: previewRow.table_evidence_binding_id,
    evidence_sha256: evidenceSha256,
  };
  const evidenceBundle = {
    schema_version: "requirement-evidence-bundle/v1",
    source_ir_id: tree.source_identity.source_ir_id,
    source_sha256: tree.source_identity.source_sha256,
    structure_snapshot_id: tree.structure_identity.effective_snapshot_id,
    requirement_bindings: [evidenceBinding],
    table_bindings: [],
    evidence_sha256: "",
  };
  evidenceBundle.evidence_sha256 = canonicalJsonSha256({
    requirement_bindings: evidenceBundle.requirement_bindings,
    source_ir_id: evidenceBundle.source_ir_id,
    source_sha256: evidenceBundle.source_sha256,
    structure_snapshot_id: evidenceBundle.structure_snapshot_id,
    table_bindings: evidenceBundle.table_bindings,
  });
  const publishedTree: RequirementTreeSnapshot = {
    schema_version: "requirement-tree-snapshot/v1",
    mode: "REQUIREMENT_SET",
    state: "READY",
    source_identity: {
      project_id: tree.source_identity.project_id,
      document_id: tree.source_identity.document_id,
      source_ir_id: tree.source_identity.source_ir_id,
      source_sha256: tree.source_identity.source_sha256,
      preview_generation_id: tree.source_identity.preview_generation_id,
      requirement_set_id: "semantic-draft",
      geometry_index_id: "",
      geometry_index_sha256: "",
    },
    structure_identity: tree.structure_identity,
    root_node_ids: tree.root_node_ids,
    nodes: tree.nodes.map((node) => {
      const isPublishedRow = node.id === previewRow.requirement_tree_node_id;
      return {
        id: node.id,
        parent_id: node.parent_id ?? null,
        kind: isPublishedRow ? "REQUIREMENT" : node.kind,
        depth: node.depth,
        source_order: isPublishedRow
          ? previewRow.source_order
          : node.source_order,
        source_offset: isPublishedRow ? previewRow.source_offset : 0,
        label: isPublishedRow ? previewRow.label : node.label,
        summary: isPublishedRow ? previewRow.published_statement : node.summary,
        child_ids: node.child_ids,
        structure_node_id: isPublishedRow
          ? previewRow.structure_node_id
          : node.structure_node_id,
        requirement_entry_id: isPublishedRow ? requirementEntryId : null,
        requirement_evidence_binding_id: isPublishedRow
          ? evidenceBinding.binding_id
          : null,
        workflow_node_id: isPublishedRow ? previewRow.workflow_node_id : null,
        page_start: isPublishedRow ? previewRow.page_start : node.page_start,
        page_end: isPublishedRow ? previewRow.page_end : node.page_end,
        source_refs: isPublishedRow ? previewRow.source_refs : node.source_refs,
        requirement_count: isPublishedRow ? 1 : node.requirement_count,
        review_item_count: 0,
        attention_count: node.attention_count,
        reviewed_count: node.reviewed_count,
        status: isPublishedRow ? previewRow.status : node.status,
        semantic_state: "RESOLVED",
        classification: isPublishedRow
          ? previewRow.classification
          : node.classification,
        projection_role: isPublishedRow
          ? previewRow.final_projection_role
          : String(node.projection_role || "STRUCTURE"),
        include_in_view: isPublishedRow
          ? previewRow.include_in_view
          : node.include_in_view,
        deterministic_rule_evidence: isPublishedRow
          ? previewRow.deterministic_rule_evidence
          : [],
        content_blocks: isPublishedRow
          ? previewRow.content_blocks
          : node.content_blocks,
        editable_structure_node_ids: isPublishedRow
          ? previewRow.editable_structure_node_ids
          : [],
        table_record_id: isPublishedRow ? previewRow.table_record_id : null,
        table_evidence_binding_id: isPublishedRow
          ? previewRow.table_evidence_binding_id
          : null,
        table_refs: isPublishedRow ? previewRow.table_refs : node.table_refs,
        figure_refs: isPublishedRow ? previewRow.figure_refs : node.figure_refs,
        toc_entries: [],
      } as RequirementTreeNode;
    }),
    table_record_projections: [],
    table_evidence_bindings: [],
    requirement_evidence_bundle: evidenceBundle,
  };
  const rowBinding = {
    schema_version: "requirement-enrichment-publication-row-binding/v2",
    binding_id: bindingId,
    publication_order: 0,
    requirement_tree_node_id: previewRow.requirement_tree_node_id,
    requirement_entry_id: requirementEntryId,
    projection_row_sha256: canonicalJsonSha256(previewRow),
    decision_trace_sha256: "",
    projection_row: previewRow,
    payload_sha256: "",
  };
  rowBinding.decision_trace_sha256 = canonicalJsonSha256({
    schema_version: "requirement-enrichment-decision-trace/v2",
    binding_id: bindingId,
    publication_order: 0,
    requirement_entry_id: requirementEntryId,
    projection_row_sha256: rowBinding.projection_row_sha256,
  });
  rowBinding.payload_sha256 = canonicalJsonSha256(
    withoutCanonicalField(rowBinding, "payload_sha256"),
  );
  const publication = {
    schema_version: "requirement-enrichment-publication-binding/v2",
    binding_id: bindingId,
    binding_preview_id: preview.binding_preview.binding_preview_id,
    binding_preview_sha256: preview.binding_preview.payload_sha256,
    project_id: "persist-project",
    document_id: "persist-document",
    session_id: "review-session",
    review_revision: 7,
    base_active_requirement_set_id:
      preview.binding_preview.base_active_requirement_set_id,
    projection_sha256: preview.binding_preview.projection.projection_sha256,
    requirement_set_id: "semantic-draft",
    requirement_set_version: 2,
    requirement_tree_object_key: "trees/semantic-draft.json",
    requirement_tree_sha256: canonicalJsonSha256(publishedTree),
    requirement_tree_schema_version: "requirement-tree-snapshot/v1",
    row_bindings: [rowBinding],
    payload_sha256: "",
  };
  publication.payload_sha256 = canonicalJsonSha256(
    withoutCanonicalField(publication, "payload_sha256"),
  );
  return {
    schema_version: "requirement-enrichment-publish-draft-response/v2",
    project_id: "persist-project",
    document_id: "persist-document",
    serving_impact: "DRAFT_ONLY",
    analysis_started: false,
    requirement_set_created: true,
    requirement_set_activated: false,
    classification: {
      snapshot_id: classification.snapshot.snapshot_id,
      revision: classification.snapshot.revision,
      sha256: classification.snapshot.payload_sha256,
    },
    binding: {
      binding_id: bindingId,
      sha256: publication.payload_sha256,
      preview_id: preview.binding_preview.binding_preview_id,
      preview_sha256: preview.binding_preview.payload_sha256,
      state: "READY",
    },
    counts: preview.counts,
    publication,
    requirement_tree: publishedTree,
    ...overrides,
  };
}

function semanticActivationResponse(
  published: RequirementSemanticPublishDraftResponseV2,
  overrides: JsonRecord = {},
): JsonRecord {
  return {
    project_id: "persist-project",
    document_id: "persist-document",
    active_requirement_set: {
      id: "semantic-draft",
      enrichment_publication_binding_id: published.binding.binding_id,
      enrichment_publication_binding_sha256: published.binding.sha256,
    },
    document: {
      id: "persist-document",
      project_id: "persist-project",
      active_requirement_set_id: "semantic-draft",
    },
    ...overrides,
  };
}

function atomizationScopeAttempt(
  overrides: Partial<RequirementAtomizationScopeAttempt> = {},
): RequirementAtomizationScopeAttempt {
  return {
    schema_version: "atomization-scope-attempt/v1",
    operation: "APPLY",
    scope: "BRANCH",
    status: "INTERRUPTED",
    requested_review_node_ids: ["persist-leaf"],
    executed_review_node_ids: ["persist-leaf"],
    skipped_targets: [],
    cumulative_review_node_ids: [],
    completed_review_node_ids: [],
    failed_review_node_ids: ["persist-leaf"],
    checkpoint_created_review_node_ids: [],
    checkpoint_reused_review_node_ids: [],
    checkpoint_invalidated_review_node_ids: [],
    provider_call_count: 1,
    retryable: false,
    checkpoint_resumable: false,
    error_code: "INVALID_PROVIDER_OUTPUT",
    ...overrides,
  };
}

function atomizationEnrichmentState(
  classification: EntryClassificationResponse,
  overrides: Partial<RequirementEnrichmentState["atomization_scope"]> = {},
): RequirementEnrichmentState {
  return {
    schema_version: "requirement-enrichment-state/v1",
    project_id: classification.snapshot.project_id,
    document_id: classification.snapshot.sor_document_id,
    session_id: "review-session",
    review_revision: 7,
    atomization_scope: {
      schema_version: "requirement-atomization-scope-state/v1",
      capability: {
        schema_version: "requirement-atomization-scope-capability/v1",
        available: true,
        explicit_trigger_required: true,
        read_has_side_effects: false,
        operations: ["APPLY", "UNDO"],
        scopes: ["CURRENT", "SELECTED", "BRANCH", "ALL"],
        unselected_policy: "SOURCE_BOUND_PASSTHROUGH",
      },
      current_scope_revision: 3,
      eligible_review_node_ids: classification.snapshot.entries.flatMap(
        (entry) =>
          entry.effective_type === "TECHNICAL" ? [entry.workflow_node_id] : [],
      ),
      eligible_review_node_count: classification.counts.technical,
      classification_binding_state: "CURRENT",
      classification_snapshot_id: classification.snapshot.snapshot_id,
      classification_revision: classification.snapshot.revision,
      classification_sha256: classification.snapshot.payload_sha256,
      classification_content_sha256: "f".repeat(64),
      can_undo: false,
      current_atomization_run_id: "atomization-run-" + "6".repeat(32),
      current_atomization_run_revision: 4,
      current_receipt: null,
      last_attempt: null,
      ...overrides,
    },
  };
}

function navigationWorkspaceValue(
  tree: RequirementTreeSnapshot = navigationTree(),
): WorkspaceContextValue {
  const noop = async () => {};
  return {
    route: {
      task: "extraction",
      mode: "organize",
      checkpoint: "structure",
      projectId: "persist-project",
      documentId: "persist-document",
      requirementSetId: "",
    },
    updateRoute: vi.fn(),
    projects: [],
    documents: [],
    knowledge: [],
    workspace: null,
    requirementSets: {
      active_requirement_set_id: "persist-set",
      requirement_sets: [
        { id: "persist-set", version_label: "版本 1", status: "DRAFT" },
      ],
    },
    extraction: null,
    structure: {
      structure_workbench: {
        requirement_tree: tree,
        source_pages: {
          state: "READY",
          snapshot_id: "d".repeat(64),
          source_sha256: sourceSha256,
          page_count: 1,
        },
        agent: { status: "COMPLETED" },
        readiness: { can_atomize: true },
        capabilities: {},
        structure: {
          effective_snapshot_id: tree.structure_identity.effective_snapshot_id,
          nodes: [],
        },
      },
    },
    structureOptimization: null,
    aiStatus: null,
    exports: [],
    readiness: null,
    aiSetup: null,
    runtimeCapabilities: [],
    processingProfile: null,
    loading: false,
    refreshing: false,
    error: null,
    notice: "",
    busyAction: "",
    selectedProject: null,
    selectedDocument: {
      id: "persist-document",
      project_id: "persist-project",
      file_name: "persist-source.pdf",
      page_count: 1,
      source_sha256: sourceSha256,
    },
    refreshAll: noop,
    refreshProject: noop,
    refreshDocument: noop,
    refreshExtraction: noop,
    refreshStructure: noop,
    refreshStructureOptimization: noop,
    refreshAi: noop,
    refreshExports: noop,
    runAction: async (_label, action) => action(),
    setNotice: vi.fn(),
  };
}

function installNavigationResizeObserver(): void {
  class TestResizeObserver {
    constructor(private readonly callback: ResizeObserverCallback) {}
    observe = (element: Element) =>
      this.callback(
        [
          {
            target: element,
            contentRect: { width: 340, height: 560 },
          } as ResizeObserverEntry,
        ],
        this as unknown as ResizeObserver,
      );
    unobserve = vi.fn();
    disconnect = vi.fn();
  }
  vi.stubGlobal("ResizeObserver", TestResizeObserver);
}

async function resetNavigationViewportToTop(): Promise<void> {
  const viewport = await screen.findByRole("tree", { name: "需求结构树" });
  Object.defineProperty(viewport, "scrollTop", {
    configurable: true,
    value: 0,
    writable: true,
  });
  await act(async () => {
    fireEvent.scroll(viewport);
    await new Promise((resolve) => window.setTimeout(resolve, 20));
  });
}

function semanticListWorkspace(
  tree: RequirementTreeSnapshot = navigationTree(),
  reviewRevision = 7,
): WorkspaceContextValue {
  const value = navigationWorkspaceValue(tree);
  value.route = { ...value.route, checkpoint: "list" };
  const workbench = value.structure?.structure_workbench as JsonRecord;
  value.structure = {
    structure_workbench: {
      ...workbench,
      workflow: {
        review: {
          session_id: "review-session",
          revision: reviewRevision,
        },
      },
      result: {},
      readiness: { can_atomize: true },
    },
  };
  return value;
}

function setStructureArtifactEditability(
  workspace: WorkspaceContextValue,
  {
    editable,
    reason = "",
  }: {
    editable: boolean;
    reason?: string;
  },
): void {
  const workbench = workspace.structure?.structure_workbench as JsonRecord;
  workspace.structure = {
    ...workspace.structure,
    structure_agent: {
      ...(workspace.structure?.structure_agent || {}),
      editable,
      read_only_reason: reason || undefined,
    },
    structure_workbench: {
      ...workbench,
      agent: {
        ...((workbench.agent as JsonRecord | undefined) || {}),
        editable,
      },
      structure: {
        ...((workbench.structure as JsonRecord | undefined) || {}),
        editable,
        state: "STABLE",
      },
      capabilities: {
        ...((workbench.capabilities as JsonRecord | undefined) || {}),
        can_edit_structure: editable,
      },
    },
  };
}

describe("RequirementTreePanel recovery reason", () => {
  it("keeps the Agent workflow inside the existing workbench and separates the source baseline from atomization", async () => {
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const user = userEvent.setup();
    const workspace = navigationWorkspaceValue();
    workspace.extraction = {
      result: {
        requirement_decomposition: [
          {
            parent_provision_id: "parent-1",
            obligations: [
              { atomic_statement: "派生候选 A" },
              { atomic_statement: "派生候选 B" },
            ],
          },
        ],
      },
    };

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    const cockpit = await screen.findByRole("region", {
      name: "Agent 工作流驾驶舱",
    });
    expect(cockpit).toHaveTextContent(
      "将授权原文逐层还原为可定位、可修订的文档结构",
    );
    expect(cockpit).toHaveTextContent("理解全文并识别主要文档范围");
    expect(cockpit).toHaveTextContent("逐层还原标题、正文、表格与图片归属");
    expect(
      await screen.findByText(/检测到历史原子化候选：1 个父条目、2 个派生条目/),
    ).toBeInTheDocument();
    expect(screen.getByText(/不会应用到当前来源忠实基线/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "暂停" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "跳过" })).not.toBeInTheDocument();

    const expandCockpit = within(cockpit).getByRole("button", { name: "展开" });
    expect(expandCockpit).toHaveAttribute("aria-expanded", "false");
    await user.click(expandCockpit);
    await user.click(
      await screen.findByRole("button", { name: "进入原文需求清单" }),
    );
    expect(workspace.updateRoute).toHaveBeenCalledWith(
      expect.objectContaining({ checkpoint: "list" }),
    );
  });

  it("keeps the hook order stable while the selected document becomes available", async () => {
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const loadingWorkspace = navigationWorkspaceValue();
    loadingWorkspace.selectedDocument = null;
    loadingWorkspace.loading = true;
    const readyWorkspace = navigationWorkspaceValue();

    const view = render(
      <WorkspaceProvider value={loadingWorkspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      screen.getByText("先在项目管理中选择客户文件"),
    ).toBeInTheDocument();

    view.rerender(
      <WorkspaceProvider value={readyWorkspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("tree", { name: "需求结构树" }),
    ).toBeInTheDocument();
  });

  it("surfaces typed bilingual mismatches as an explicit review warning", async () => {
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const leaf = tree.nodes.find((node) => node.id === "persist-leaf");
    if (!leaf) throw new Error("navigation leaf fixture is missing");
    leaf.status = "NEEDS_ATTENTION";
    leaf.attention_count = 1;
    leaf.bilingual_context = {
      counterpart_group_id: "bilingual-group-1",
      document_language_policy_id: null,
      canonical_structure_node_id: leaf.id,
      member_structure_node_ids: [leaf.id, "persist-leaf-en"],
      authority_language: "UNKNOWN",
      authority_basis: "UNKNOWN",
      equal_validity: null,
      source_spans: [],
      mismatch_signal_ids: ["bilingual-mismatch-1"],
      mismatch_review_required: true,
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={navigationWorkspaceValue(tree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("treeitem", {
        name: /2\.3\.1 持久化叶节点/,
      }),
    );
    expect(
      await screen.findByText("双语内容存在不一致"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/未自动选择或改写其中一侧/),
    ).toBeInTheDocument();
  });

  it("regenerates a runtime-incompatible result without claiming source loss", async () => {
    const user = userEvent.setup();
    const tree = navigationTree();
    tree.state = "REQUIRES_REEXTRACTION";
    tree.nodes = [];
    tree.root_node_ids = [];
    const workspace = navigationWorkspaceValue(tree);
    const workbench = workspace.structure?.structure_workbench as JsonRecord;
    workbench.recovery = {
      reason_code: "RESULT_REGENERATION_REQUIRED",
      detail_code: "RUNTIME_INCOMPATIBLE",
      recommended_action: "REGENERATE_EXTRACTION_RESULT",
    };
    workbench.issues = [
      {
        code: "STRUCTURE_RESULT_REGENERATION_REQUIRED",
        severity: "BLOCKING",
      },
    ];
    enableCodexBaseline(workspace);
    const startExtraction = vi
      .spyOn(api, "startExtraction")
      .mockResolvedValue({ job_id: "runtime-refresh-job" });

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByText("当前解析结果需要重新生成"),
    ).toBeInTheDocument();
    expect(screen.queryByText("正文需要视觉恢复")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新解析全文…" }));
    await user.click(screen.getByRole("button", { name: "启动 Agent 原文基线" }));
    expect(startExtraction).toHaveBeenCalledWith(
      "persist-project",
      "persist-document",
    );
  });

  it("offers visual restoration only for a source capture gap", async () => {
    const tree = navigationTree();
    tree.state = "REQUIRES_REEXTRACTION";
    tree.nodes = [];
    tree.root_node_ids = [];
    const workspace = navigationWorkspaceValue(tree);
    const workbench = workspace.structure?.structure_workbench as JsonRecord;
    workbench.recovery = {
      reason_code: "SOURCE_CAPTURE_GAP",
      recommended_action: "VISUAL_RESTORE_AND_REEXTRACT",
    };
    workbench.issues = [
      {
        code: "SOURCE_TEXT_RESTORATION_REQUIRED",
        severity: "BLOCKING",
      },
    ];

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(await screen.findByText("正文需要视觉恢复")).toBeInTheDocument();
    expect(
      screen.queryByText("当前解析结果需要重新生成"),
    ).not.toBeInTheDocument();
  });
});

describe("RequirementTreePanel parsing diagnosis", () => {
  it.each(["PENDING", "RUNNING", "CANCEL_REQUESTED"])("uses the current original-file %s task instead of stale parser failures", async status => {
    const tree = navigationTree(); tree.state = "NOT_STARTED"; tree.nodes = []; tree.root_node_ids = [];
    const workspace = navigationWorkspaceValue(tree);
    (workspace.structure!.structure_workbench as JsonRecord).source_pages = { state: "UNAVAILABLE", page_count: 0 };
    workspace.runAction = vi.fn();
    sourceActionMocks.activity = { projectId: "persist-project", documentId: "persist-document", sourceSha256,
      checking: false, statusUnavailable: false, submitting: false, jobs: [{ job_id: "current-original-task", kind: "agent.source-item.extract-loop", status }] };
    render(<WorkspaceProvider value={workspace}><RequirementTreePanel /></WorkspaceProvider>);
    const open = await screen.findByRole("button", { name: "查看 Agent 协作" });
    expect(screen.queryByLabelText("Agent 工作流驾驶舱")).not.toBeInTheDocument();
    expect(screen.queryByText("结构尚未生成")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "开始需求提取" })).not.toBeInTheDocument();
    fireEvent.click(open);
    expect(sourceActionMocks.openRequest).toBe(1);
    expect(workspace.runAction).not.toHaveBeenCalled();
  });

  it.each(["structure", "list"] as const)("keeps the native tree visible in %s before source items exist, despite a stale parser snapshot", async (checkpoint) => {
    const user = userEvent.setup();
    const tree = navigationTree();
    tree.state = "REQUIRES_REEXTRACTION";
    tree.nodes = [];
    tree.root_node_ids = [];
    const workspace = navigationWorkspaceValue(tree);
    workspace.route = { ...workspace.route, checkpoint };
    const workbench = workspace.structure?.structure_workbench as JsonRecord;
    workbench.source_pages = { state: "UNAVAILABLE", page_count: 0 };
    workspace.selectedDocument = {
      ...workspace.selectedDocument!,
      page_count: 1,
    };
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(
      nativeStructureReferenceFixture(),
    );

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByLabelText("Agent 原文结构草案"),
    ).toBeVisible();
    expect(screen.getByRole("tab", { name: checkpoint === "structure" ? /原文结构/ : /原文需求清单/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      screen.queryByLabelText("Agent 工作流驾驶舱"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("当前解析结果需要重新生成"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("最近一次提取未完成")).not.toBeInTheDocument();
    expect(workspace.updateRoute).not.toHaveBeenCalled();
    if (checkpoint === "list") {
      expect(screen.getByText("原文条目尚未生成")).toBeVisible();
    }

    await user.click(
      screen.getByRole("button", { name: /主动前减振器试验大纲/ }),
    );

    expect(
      screen.getByTitle("原文 PDF · 第 10 页"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByLabelText("原文 PDF 对照")).getByRole("link", {
        name: "在新窗口打开原文 PDF",
      }),
    ).toHaveAttribute(
      "href",
      expect.stringContaining("page=10"),
    );
  });

  it.each([
    {
      name: "Qwen OCR",
      primaryParser: "pymupdf",
      enhancementChain: ["bailian_ocr"],
      adoptedProviderId: "aliyun-bailian-qwen-ocr",
      expectedRoute: "QWEN_OCR",
    },
    {
      name: "Bailian layout",
      primaryParser: "pymupdf",
      enhancementChain: ["bailian_layout"],
      // The current low-level layout adapter can share this identifier. The
      // frozen route must win so the UI cannot relabel layout as Qwen OCR.
      adoptedProviderId: "aliyun-bailian-qwen-ocr",
      expectedRoute: "BAILIAN_LAYOUT",
    },
    {
      name: "Azure primary",
      primaryParser: "azure_document_intelligence",
      enhancementChain: [],
      adoptedProviderId: "azure-document-intelligence",
      expectedRoute: "AZURE_DOCUMENT_INTELLIGENCE",
    },
  ])(
    "retains the frozen $name route and adopted provider in terminal receipts",
    ({ primaryParser, enhancementChain, adoptedProviderId, expectedRoute }) => {
      expect(
        extractionVisualReceiptSummary({
          processing_plan: {
            document: {
              primary_parser: primaryParser,
              enhancement_chain: enhancementChain,
            },
          },
          page_processing_receipts: [
            {
              page_number: 1,
              state: "READY",
              adopted_provider_id: adoptedProviderId,
            },
          ],
        }),
      ).toMatchObject({
        routeIdentity: expectedRoute,
        adoptedProviderIds: [adoptedProviderId],
        adoptedProviderIdByPage: { 1: adoptedProviderId },
      });
    },
  );

  it("projects the terminal Qwen OCR receipt from the extraction result and does not query reading capture", async () => {
    const user = userEvent.setup();
    const workspace = diagnosticWorkspaceValue();
    const runAction = vi.fn(
      async (_label: string, action: () => Promise<unknown>) => action(),
    );
    workspace.runAction = runAction as WorkspaceContextValue["runAction"];
    const workbench = workspace.structure?.structure_workbench as JsonRecord;
    (workbench.source_pages as JsonRecord).page_count = 30;
    workspace.selectedDocument = {
      ...workspace.selectedDocument!,
      page_count: 30,
    };
    workspace.extraction = {
      job: {
        job_id: "job-terminalvisual",
        status: "COMPLETED",
        processing_plan: {
          document: {
            primary_parser: "pymupdf",
            enhancement_chain: ["bailian_ocr"],
          },
        },
      },
      result: {
        processing_plan: {
          document: {
            primary_parser: "pymupdf",
            enhancement_chain: ["bailian_ocr"],
          },
        },
        page_processing_receipts: Array.from({ length: 27 }, (_, index) => {
          const pageNumber = index + 1;
          const degraded = [15, 18, 26, 27].includes(pageNumber);
          return {
            schema_version: "page-processing-receipt/v1",
            page_number: pageNumber,
            state: degraded ? "DEGRADED" : "READY",
            adopted_provider_id: degraded
              ? "upstream:primary-parser"
              : "aliyun-bailian-qwen-ocr",
            table_parsing:
              pageNumber === 1
                ? {
                    mode: "NATIVE_TABLE_CROPS",
                    candidate_count: 3,
                    attempt_count: 2,
                    ready_count: 2,
                    failed_count: 0,
                    budget_skipped_count: 1,
                    request_ceiling_per_page: 8,
                    request_ceiling_per_document: 64,
                  }
                : {
                    mode: "OFF",
                    candidate_count: 0,
                    attempt_count: 0,
                    ready_count: 0,
                    failed_count: 0,
                    budget_skipped_count: 0,
                    request_ceiling_per_page: 8,
                    request_ceiling_per_document: 64,
                  },
            degraded_reason_codes: degraded
              ? ["aliyun-bailian-qwen-ocr:provider_failed"]
              : [],
          };
        }),
        degraded_page_numbers: [15, 18, 26, 27],
      },
      visual_repair: {
        schema_version: "document-extraction-visual-repair-eligibility/v1",
        state: "AVAILABLE",
        reason_code: "",
        mode: "FAILED_OCR_PAGES_ONLY",
        base_job_id: "job-terminalvisual",
        base_job_generation: 2,
        base_artifact_sha256: "a".repeat(64),
        required_page_count: 27,
        ready_page_count: 23,
        failed_page_count: 4,
        failed_page_numbers: [15, 18, 26, 27],
        remote_request_ceiling: 4,
        deep_layout_request_ceiling: 0,
      },
    };
    const sourcePage = vi
      .spyOn(api, "sourcePage")
      .mockImplementation(async (_projectId, _documentId, page) =>
        diagnosticPagePayload(page),
      );
    vi.spyOn(api, "parsingDiagnostics").mockResolvedValue(
      parsingRiskPayload({ severe: 0, blocking: 0 }),
    );
    const repairVisualPages = vi
      .spyOn(api, "repairExtractionVisualPages")
      .mockResolvedValue({});

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("status", { name: "Qwen OCR 结构恢复终态回执" }),
    ).toHaveTextContent("Qwen OCR 结构恢复 23/27 页就绪，4 页降级");
    expect(
      screen.getByRole("status", { name: "Qwen OCR 结构恢复终态回执" }),
    ).toHaveTextContent("表格处理 2 次尝试 · 2 就绪 · 0 失败 · 1 项因预算跳过");
    expect(captureApiMocks.quality).not.toHaveBeenCalled();

    await user.click(
      screen.getByRole("button", { name: "查看 Qwen OCR 结构恢复降级的第 18 页" }),
    );
    await waitFor(() =>
      expect(sourcePage).toHaveBeenCalledWith(
        "persist-project",
        "persist-document",
        18,
      ),
    );

    await user.click(
      screen.getByRole("button", { name: "只重试降级的 4 页" }),
    );
    await waitFor(() => expect(repairVisualPages).toHaveBeenCalledOnce());
    expect(repairVisualPages).toHaveBeenCalledWith(
      "persist-project",
      "persist-document",
      {
        schema_version: "document-extraction-visual-repair-request/v1",
        expected_base_job_id: "job-terminalvisual",
        expected_base_job_generation: 2,
        expected_base_artifact_sha256: "a".repeat(64),
      },
    );
    expect(runAction).toHaveBeenCalledWith(
      "只重试降级的 4 页",
      expect.any(Function),
      {
        refresh: "document",
        successNotice: "Qwen OCR 结构恢复降级页重试已提交；其余 23 页将复用已验证结果",
      },
    );
  });

  it("keeps the exact repair receipt actionable when an incompatible runtime withholds the old result body", async () => {
    const workspace = diagnosticWorkspaceValue();
    workspace.extraction = {
      job: {
        job_id: "job-terminalvisual",
        status: "COMPLETED",
        processing_plan: {
          document: {
            primary_parser: "pymupdf",
            enhancement_chain: ["bailian_ocr"],
          },
        },
      },
      result: null,
      visual_repair: {
        schema_version: "document-extraction-visual-repair-eligibility/v1",
        state: "AVAILABLE",
        reason_code: "",
        mode: "FAILED_OCR_PAGES_ONLY",
        base_job_id: "job-terminalvisual",
        base_job_generation: 2,
        base_artifact_sha256: "a".repeat(64),
        required_page_count: 27,
        ready_page_count: 23,
        failed_page_count: 4,
        failed_page_numbers: [15, 18, 26, 27],
        remote_request_ceiling: 4,
        deep_layout_request_ceiling: 0,
      },
    };
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("status", { name: "Qwen OCR 结构恢复终态回执" }),
    ).toHaveTextContent("Qwen OCR 结构恢复 23/27 页就绪，4 页降级");
    expect(
      screen.getByRole("button", { name: "只重试降级的 4 页" }),
    ).toBeEnabled();
  });

  it("defaults to completed pages from the active attempt and keeps the previous formal facts as an explicit alternate view", async () => {
    const workspace = diagnosticWorkspaceValue();
    workspace.extraction = extractionWithStagedPages({
      generation: 11,
      readyPageCount: 2,
      readyPageRanges: [{ start_page: 1, end_page: 2 }],
    });
    const stagedSourcePage = vi
      .spyOn(api, "stagedSourcePage")
      .mockResolvedValue(diagnosticPagePayloadWithText(1, "本轮第 1 页暂存解析文字"));
    const sourcePage = vi
      .spyOn(api, "sourcePage")
      .mockResolvedValue(diagnosticPagePayload());
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByText("本轮第 1 页暂存解析文字"),
    ).toBeInTheDocument();
    expect(stagedSourcePage).toHaveBeenCalledWith(
      "staged-job",
      11,
      1,
      "/api/extraction/attempts/staged-job/generations/11/source-pages/{page_number}",
    );
    expect(sourcePage).not.toHaveBeenCalled();
    expect(screen.getByText(/本轮已有 2 页可浏览/)).toBeInTheDocument();
    expect(screen.getByText(/结构与风险摘要尚未生成/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "返回结构" })).toBeEnabled();

    await user.click(
      screen.getByRole("button", { name: "查看上一正式结果" }),
    );
    expect(await screen.findByText("本页完整解析文字")).toBeInTheDocument();
    expect(sourcePage).toHaveBeenCalledWith(
      "persist-project",
      "persist-document",
      1,
    );
  });

  it("does not fill an unfinished staged page with facts from the previous formal artifact", async () => {
    const workspace = diagnosticWorkspaceValue();
    workspace.extraction = extractionWithStagedPages({
      generation: 11,
      readyPageCount: 1,
      readyPageRanges: [{ start_page: 1, end_page: 1 }],
    });
    const stagedSourcePage = vi
      .spyOn(api, "stagedSourcePage")
      .mockResolvedValue(diagnosticPagePayloadWithText(1, "本轮第 1 页暂存解析文字"));
    const sourcePage = vi
      .spyOn(api, "sourcePage")
      .mockResolvedValue(diagnosticPagePayload(2));
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByText("本轮第 1 页暂存解析文字"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(
      await screen.findByText("本轮第 2 页尚未处理"),
    ).toBeInTheDocument();
    expect(stagedSourcePage).toHaveBeenCalledTimes(1);
    expect(sourcePage).not.toHaveBeenCalled();
    expect(screen.queryByText("第 2 页完整解析文字")).not.toBeInTheDocument();
  });

  it("rejects a late staged-page response after the attempt generation changes", async () => {
    let resolveGeneration11!: (payload: JsonRecord) => void;
    const generation11 = new Promise<JsonRecord>((resolve) => {
      resolveGeneration11 = resolve;
    });
    vi.spyOn(api, "stagedSourcePage").mockImplementation(
      async (_jobId, generation) => {
        if (generation === 11) return generation11;
        return diagnosticPagePayloadWithText(1, "第 12 代暂存解析文字");
      },
    );
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    const generation11Workspace = diagnosticWorkspaceValue();
    generation11Workspace.extraction = extractionWithStagedPages({
      generation: 11,
      readyPageCount: 1,
      readyPageRanges: [{ start_page: 1, end_page: 1 }],
    });
    const generation12Workspace = diagnosticWorkspaceValue();
    generation12Workspace.extraction = extractionWithStagedPages({
      generation: 12,
      readyPageCount: 1,
      readyPageRanges: [{ start_page: 1, end_page: 1 }],
    });

    const view = render(
      <WorkspaceProvider value={generation11Workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    expect(await screen.findByText(/正在读取第 1 页解析事实/)).toBeInTheDocument();

    view.rerender(
      <WorkspaceProvider value={generation12Workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    expect(
      await screen.findByText("第 12 代暂存解析文字"),
    ).toBeInTheDocument();

    await act(async () => {
      resolveGeneration11(
        diagnosticPagePayloadWithText(1, "迟到的第 11 代暂存解析文字"),
      );
      await generation11;
    });
    expect(screen.getByText("第 12 代暂存解析文字")).toBeInTheDocument();
    expect(
      screen.queryByText("迟到的第 11 代暂存解析文字"),
    ).not.toBeInTheDocument();
  });

  it("labels a legacy page preview without treating it as the current parse artifact", async () => {
    const tree = navigationTree();
    tree.state = "NOT_STARTED";
    tree.nodes = [];
    tree.root_node_ids = [];
    const workspace = diagnosticWorkspaceValue({ sourcePagesReady: false });
    const workbench = workspace.structure?.structure_workbench as JsonRecord;
    workbench.requirement_tree = tree;
    const legacyRun: RestorationRun = {
      run_id: "legacy-page-preview",
      status: "READY",
      progress: { page_count: 21 },
    };
    vi.spyOn(api, "latestRestoration").mockResolvedValue(legacyRun);
    const restorationPage = vi
      .spyOn(api, "restorationPage")
      .mockResolvedValue(diagnosticPagePayload());
    const sourcePage = vi.spyOn(api, "sourcePage");

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByText(
        "已有旧版逐页预览和解析事实可供核对，但尚未生成当前结构整理所需的正式解析工件；请使用当前配置重新解析。",
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText("本页完整解析文字")).toBeInTheDocument();
    expect(restorationPage).toHaveBeenCalledWith(
      "persist-project",
      "persist-document",
      "legacy-page-preview",
      1,
    );
    expect(sourcePage).not.toHaveBeenCalled();
    expect(
      screen.queryByText("当前文件尚未形成解析与结构结果，请先显式开始解析。"),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "返回结构" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "重新解析全文…" })).toBeEnabled();
  });

  it("distinguishes ready page facts from a missing current structure result", async () => {
    const tree = navigationTree();
    tree.state = "NOT_STARTED";
    tree.nodes = [];
    tree.root_node_ids = [];
    const workspace = diagnosticWorkspaceValue();
    const workbench = workspace.structure?.structure_workbench as JsonRecord;
    workbench.requirement_tree = tree;
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findAllByText(
        /逐页解析事实已可核对.*尚未形成可进入结构整理/,
      ),
    ).not.toHaveLength(0);
    expect(screen.getByText("本页完整解析文字")).toBeInTheDocument();
    expect(
      screen.queryByText("当前文件尚未形成解析与结构结果，请先显式开始解析。"),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "返回结构" })).toBeEnabled();
  });

  it.each(["structure", "list"] as const)(
    "keeps a direct %s checkpoint browseable while non-blocking risks remain visible",
    async (checkpoint) => {
      const workspace = diagnosticWorkspaceValue();
      workspace.route = { ...workspace.route, checkpoint };
      vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
      vi.mocked(api.parsingDiagnostics).mockResolvedValue(
        parsingRiskPayload({ severe: 1, blocking: 0 }),
      );

      render(
        <WorkspaceProvider value={workspace}>
          <RequirementTreePanel />
        </WorkspaceProvider>,
      );

      expect(screen.queryByLabelText("解析核对")).not.toBeInTheDocument();
      expect(
        screen.getByRole("tab", {
          name:
            checkpoint === "structure"
              ? "第 1 步：原文结构"
              : "第 2 步：原文需求清单",
        }),
      ).toHaveAttribute("aria-selected", "true");
      if (checkpoint === "list") {
        await screen.findByRole("region", { name: "清单分类统计" });
        expect(api.entryClassifications).toHaveBeenCalledOnce();
      } else {
        expect(api.entryClassifications).not.toHaveBeenCalled();
      }
      expect(workspace.updateRoute).not.toHaveBeenCalled();
    },
  );

  it("keeps a direct list checkpoint usable while the source summary is loading", async () => {
    const workspace = diagnosticWorkspaceValue();
    workspace.route = { ...workspace.route, checkpoint: "list" };
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    vi.mocked(api.parsingDiagnostics).mockImplementation(
      () => new Promise<JsonRecord>(() => undefined),
    );

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(screen.queryByLabelText("解析核对")).not.toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "第 2 步：原文需求清单" }),
    ).toHaveAttribute("aria-selected", "true");
    await screen.findByRole("region", { name: "清单分类统计" });
    expect(api.entryClassifications).toHaveBeenCalledOnce();
  });

  it("keeps a direct structure checkpoint usable when the source summary fails", async () => {
    const workspace = diagnosticWorkspaceValue();
    workspace.route = { ...workspace.route, checkpoint: "structure" };
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    vi.mocked(api.parsingDiagnostics).mockRejectedValue(
      new Error("diagnostics unavailable"),
    );

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(screen.queryByLabelText("解析核对")).not.toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "第 1 步：原文结构" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("button", { name: "来源证据" })).toBeEnabled();
  });

  it("shows the latest stable extraction failure details before any not-started copy", async () => {
    const workspace = diagnosticWorkspaceValue();
    workspace.extraction = {
      job: {
        status: "FAILED",
        error_stage: "visual_validation",
        error_code: "llm_shadow_preview_visual_capture_rejected",
        error_cause_code: "pdf_visual_text_provider_failed",
      },
    };
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    const failure = await screen.findByText("最新重新解析失败");
    expect(failure.closest(".inline-notice")).toHaveTextContent(
      "visual_validation",
    );
    expect(failure.closest(".inline-notice")).toHaveTextContent(
      "llm_shadow_preview_visual_capture_rejected",
    );
    expect(failure.closest(".inline-notice")).toHaveTextContent(
      "pdf_visual_text_provider_failed",
    );
    expect(screen.queryByText(/尚未开始/)).not.toBeInTheDocument();
    expect(screen.queryByText("下一步暂不可用")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新解析全文…" })).toBeEnabled();
    expect(
      await screen.findByRole("button", { name: "返回结构" }),
    ).toBeEnabled();
  });

  it("blocks a connection-revision rerun until the worker fleet has synchronized", async () => {
    const tree = navigationTree();
    tree.state = "REQUIRES_REEXTRACTION";
    tree.nodes = [];
    tree.root_node_ids = [];
    const workspace = diagnosticWorkspaceValue({ sourcePagesReady: false });
    const workbench = workspace.structure?.structure_workbench as JsonRecord;
    workbench.requirement_tree = tree;
    workspace.extraction = {
      job: {
        job_id: "revision-mismatch-job",
        status: "FAILED",
        error_stage: "pre_execution",
        error_code: "document_extraction_connection_revision_mismatch",
        error_cause_code: "document_extraction_connection_revision_mismatch",
        processing_plan: { connection_revision: 27 },
      },
      execution_readiness: {
        schema_version: "document-extraction-execution-readiness/v1",
        state: "WAITING_FOR_WORKER_SYNC",
        revision_required: true,
        required_connection_revision: 28,
        api_connection_revision: 28,
        worker_fleet_state: "SYNCING",
        retryable: false,
        recommended_action: "WAIT_FOR_WORKER_SYNC",
      },
      error_context: {
        failure_phase: "PRE_EXECUTION",
        pages_started: 0,
        submitted_connection_revision: 27,
        api_connection_revision: 28,
        worker_connection_revisions: [27],
        recommended_action: "WAIT_FOR_WORKER_SYNC",
      },
      previous_artifact: {
        available: true,
        runtime_compatible: false,
        read_mode: "HISTORICAL_ONLY",
        can_browse_structure: false,
      },
    };

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    const noticeTitle = await screen.findByText(
      "后台解析服务未同步当前配置",
    );
    const notice = noticeTitle.closest(".inline-notice");
    expect(notice).not.toBeNull();
    expect(notice).toHaveTextContent(
      "本次任务在开始逐页解析前已停止，没有发布任何新解析结果",
    );
    expect(notice).toHaveTextContent(
      "任务方案 revision 27 · 当前 API revision 28 · 后台执行器 revision 27",
    );
    expect(notice).toHaveTextContent("同步完成前普通重跑已停用");
    expect(notice).toHaveTextContent(
      "上一成功结果已保留为历史版本，但与当前运行合同不兼容",
    );
    const technicalDetails = within(notice as HTMLElement).getByText(
      "技术详情",
    ).parentElement;
    expect(technicalDetails).not.toBeNull();
    expect(
      technicalDetails?.textContent?.match(
        /document_extraction_connection_revision_mismatch/g,
      ),
    ).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: "重新解析全文…" }),
    ).toBeDisabled();
    expect(notice).not.toHaveTextContent(
      "请使用“重新解析全文”生成可进入下一步的当前结果",
    );
  });

  it("enables a fresh submission after the current worker fleet is ready", async () => {
    const tree = navigationTree();
    tree.state = "REQUIRES_REEXTRACTION";
    tree.nodes = [];
    tree.root_node_ids = [];
    const workspace = diagnosticWorkspaceValue({ sourcePagesReady: false });
    const workbench = workspace.structure?.structure_workbench as JsonRecord;
    workbench.requirement_tree = tree;
    workspace.extraction = {
      job: {
        job_id: "revision-mismatch-job",
        status: "FAILED",
        error_code: "document_extraction_connection_revision_mismatch",
        processing_plan: { connection_revision: 27 },
      },
      execution_readiness: {
        schema_version: "document-extraction-execution-readiness/v1",
        state: "CONFIGURATION_SUPERSEDED",
        revision_required: true,
        required_connection_revision: 28,
        api_connection_revision: 28,
        worker_fleet_state: "READY",
        retryable: true,
        recommended_action: "RESUBMIT_WITH_CURRENT_CONFIGURATION",
      },
      error_context: {
        failure_phase: "PRE_EXECUTION",
        pages_started: 0,
        submitted_connection_revision: 27,
        api_connection_revision: 28,
        worker_connection_revisions: [28],
        recommended_action: "RESUBMIT_WITH_CURRENT_CONFIGURATION",
      },
    };

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    const noticeTitle = await screen.findByText(
      "后台已恢复，可按当前方案重新提交",
    );
    expect(noticeTitle.closest(".inline-notice")).toHaveTextContent(
      "后台已加载当前连接配置",
    );
    expect(
      screen.getByRole("button", { name: "重新解析全文…" }),
    ).toBeEnabled();
  });

  it("polls blocked mismatch readiness and stops after the fleet becomes ready", async () => {
    vi.useFakeTimers();
    const workspace = diagnosticWorkspaceValue();
    const refreshExtraction = vi.fn(async () => undefined);
    workspace.refreshExtraction = refreshExtraction;
    workspace.extraction = {
      job: {
        job_id: "revision-mismatch-job",
        status: "FAILED",
        error_code: "document_extraction_connection_revision_mismatch",
      },
      execution_readiness: {
        schema_version: "document-extraction-execution-readiness/v1",
        state: "WAITING_FOR_WORKER_SYNC",
        revision_required: true,
        required_connection_revision: 28,
        api_connection_revision: 28,
        worker_fleet_state: "SYNCING",
        retryable: true,
        recommended_action: "WAIT_FOR_WORKER_SYNC",
      },
    };
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    const view = render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    try {
      await act(async () => {
        await Promise.resolve();
      });
      expect(refreshExtraction).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500);
      });
      expect(refreshExtraction).toHaveBeenCalledTimes(2);

      workspace.extraction = {
        ...workspace.extraction,
        execution_readiness: {
          schema_version: "document-extraction-execution-readiness/v1",
          state: "READY",
          revision_required: true,
          required_connection_revision: 28,
          api_connection_revision: 28,
          worker_fleet_state: "READY",
          retryable: true,
          recommended_action: "RETRY_CURRENT_CONFIGURATION",
        },
      };
      view.rerender(
        <WorkspaceProvider value={{ ...workspace }}>
          <RequirementTreePanel />
        </WorkspaceProvider>,
      );
      expect(
        screen.getByRole("button", { name: "重新解析全文…" }),
      ).toBeEnabled();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      expect(refreshExtraction).toHaveBeenCalledTimes(2);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("keeps the compatible previous structure reachable after a failed rerun", async () => {
    const workspace = diagnosticWorkspaceValue();
    workspace.extraction = {
      job: {
        job_id: "failed-rerun",
        status: "FAILED",
        error_code: "document_extraction_connection_revision_mismatch",
      },
      execution_readiness: {
        schema_version: "document-extraction-execution-readiness/v1",
        state: "WAITING_FOR_WORKER_SYNC",
        revision_required: true,
        required_connection_revision: 28,
        api_connection_revision: 28,
        worker_fleet_state: "SYNCING",
        retryable: false,
        recommended_action: "WAIT_FOR_WORKER_SYNC",
      },
      previous_artifact: {
        available: true,
        runtime_compatible: true,
        read_mode: "READ_ONLY",
        can_browse_structure: true,
        can_continue_structure: false,
      },
    };
    setStructureArtifactEditability(workspace, {
      editable: false,
      reason: "structure_derivation_rerun_not_complete",
    });
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    const previousStructure = await screen.findByRole("button", {
      name: "查看上一版结构（只读）",
    });
    expect(previousStructure).toBeEnabled();
    await user.click(previousStructure);
    expect(workspace.updateRoute).toHaveBeenCalledWith({
      task: "extraction",
      mode: "organize",
      checkpoint: "structure",
      requirementSetId: "",
    });
    expect(
      screen.getByRole("button", { name: "重新解析全文…" }),
    ).toBeDisabled();
  });

  it("allows a direct structure checkpoint when a failed rerun did not replace the current READY artifact", async () => {
    const workspace = diagnosticWorkspaceValue();
    workspace.route = { ...workspace.route, checkpoint: "structure" };
    workspace.extraction = {
      job: {
        status: "FAILED",
        error_stage: "source_ir",
        error_code: "document_extraction_failed",
        error_cause_code: "provider_unavailable",
      },
    };
    vi.spyOn(api, "sourcePage").mockResolvedValue({});

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("button", { name: "进入原文需求清单" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("tab", { name: "第 1 步：原文结构" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByText("最近一次解析失败")).not.toBeInTheDocument();
  });

  it("does not label a cancelled extraction as failed", async () => {
    const workspace = diagnosticWorkspaceValue();
    workspace.extraction = { job: { status: "CANCELLED" } };
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("button", { name: "返回结构" }),
    ).toBeEnabled();
    expect(screen.getByText("最新重新解析已取消")).toBeInTheDocument();
    expect(screen.getByText(/没有发布新结果/)).toBeInTheDocument();
    expect(screen.queryByText("最近一次解析失败")).not.toBeInTheDocument();
    expect(screen.queryByText("最新重新解析失败")).not.toBeInTheDocument();
  });

  it("explains a cancelled extraction when there is no current artifact", async () => {
    const tree = navigationTree();
    tree.state = "NOT_STARTED";
    tree.nodes = [];
    tree.root_node_ids = [];
    const workspace = diagnosticWorkspaceValue({ sourcePagesReady: false });
    workspace.extraction = { job: { status: "CANCELLED" } };
    const workbench = workspace.structure?.structure_workbench as JsonRecord;
    workbench.requirement_tree = tree;

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(await screen.findByText("最近一次解析已取消")).toBeInTheDocument();
    expect(
      screen.getAllByText(/未生成可进入下一步的新结果/),
    ).not.toHaveLength(0);
    expect(screen.getByRole("button", { name: "返回结构" })).toBeEnabled();
    expect(screen.queryByText(/解析失败/)).not.toBeInTheDocument();
  });

  it("reports an interrupted rerun while keeping the current READY artifact usable", async () => {
    const workspace = diagnosticWorkspaceValue();
    workspace.extraction = {
      job: {
        status: "INTERRUPTED",
        error_stage: "worker_recovery",
        error_code: "document_extraction_interrupted",
      },
    };
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    const notice = await screen.findByText("最新重新解析中断");
    expect(notice.closest(".inline-notice")).toHaveTextContent(
      "旧结果未被替换",
    );
    expect(
      await screen.findByRole("button", { name: "返回结构" }),
    ).toBeEnabled();
  });

  it("blocks continuation when an interrupted run has no current artifact", async () => {
    const tree = navigationTree();
    tree.state = "NOT_STARTED";
    tree.nodes = [];
    tree.root_node_ids = [];
    const workspace = diagnosticWorkspaceValue();
    workspace.extraction = { job: { status: "INTERRUPTED" } };
    const workbench = workspace.structure?.structure_workbench as JsonRecord;
    workbench.requirement_tree = tree;
    workbench.source_pages = {
      state: "UNAVAILABLE",
      page_count: 0,
      source_sha256: "",
      snapshot_id: "",
    };

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(await screen.findByText("最近一次解析中断")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "返回结构" })).toBeEnabled();
  });

  it("distinguishes queued extraction from running work and explains the retained artifact", async () => {
    const user = userEvent.setup();
    const workspace = diagnosticWorkspaceValue();
    workspace.extraction = {
      job: {
        job_id: "job-queued",
        status: "PENDING",
        stage: "queued",
        processing_plan: {
          project_profile_revision: 4,
          connection_revision: 12,
          llm_provider: "none",
          document: {
            primary_parser: "pymupdf",
            enhancement_chain: [],
            complex_vision: null,
          },
          force_visual_pages: [],
        },
        queue: {
          state: "WAITING_BEHIND_JOBS",
          position: 2,
          ahead_count: 1,
          running_count: 1,
          wait_seconds: 84,
          can_cancel: true,
          recoverable: true,
        },
      },
      previous_artifact: {
        available: true,
        runtime_compatible: false,
        read_mode: "HISTORICAL_ONLY",
      },
    };
    workspace.requirementSets = {
      active_requirement_set_id: "persist-set",
      requirement_sets: [
        {
          id: "persist-set",
          version: 4,
          requirement_count: 635,
          requirement_tree_state: "READY",
        },
      ],
    };
    const runAction = vi.fn(
      async (_label: string, action: () => Promise<unknown>) => action(),
    );
    workspace.runAction = runAction as WorkspaceContextValue["runAction"];
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    const cancel = vi
      .spyOn(api, "cancelExtraction")
      .mockResolvedValue({ job: { job_id: "job-queued", status: "CANCELLED" } });

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(await screen.findByText("重新解析正在排队")).toBeInTheDocument();
    expect(screen.getByText(/前方约有 1 个任务/)).toBeInTheDocument();
    expect(
      screen.getByText(
        /本次冻结方案：本地 PyMuPDF · 无视觉增强 · LLM 未启用 · 项目方案修订 4 · 连接修订 12/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/上一版成功结果仍保留.*运行版本已变化/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "查看当前确认清单（v4，635 条）",
      }),
    ).toBeEnabled();
    expect(screen.queryByText("正在生成新的整理结果")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "取消本次解析" }));
    expect(cancel).toHaveBeenCalledWith(
      "persist-project",
      "persist-document",
      "job-queued",
    );
    expect(runAction).toHaveBeenCalledWith(
      "取消本次解析",
      expect.any(Function),
      expect.objectContaining({ refresh: "document" }),
    );
  });

  it("states the server-derived scope and request ceilings while failed-page OCR repair is active", async () => {
    const workspace = diagnosticWorkspaceValue();
    workspace.extraction = {
      job: {
        job_id: "job-visualrepair",
        status: "RUNNING",
        stage: "source_ir",
        progress: {
          completed_pages: 23,
          total_pages: 27,
          current_page: 15,
          page_progress_kind: "SOURCE",
        },
      },
      visual_repair: {
        schema_version: "document-extraction-visual-repair-eligibility/v1",
        state: "ACTIVE",
        reason_code: "",
        mode: "FAILED_OCR_PAGES_ONLY",
        base_job_id: "job-terminalvisual",
        base_job_generation: 2,
        base_artifact_sha256: "a".repeat(64),
        required_page_count: 27,
        ready_page_count: 23,
        failed_page_count: 4,
        failed_page_numbers: [15, 18, 26, 27],
        remote_request_ceiling: 4,
        deep_layout_request_ceiling: 0,
      },
    };
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByText("正在重试 Qwen OCR 结构恢复降级页"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "本次只重试正式回执中的 P15、P18、P26、P27；23 页复用已验证结果。文字 OCR 请求上限 4 次；table_parsing 另受每页 8 次、整文 64 次上限；深度版面请求上限 0 次。",
      ),
    ).toBeInTheDocument();
  });

  it("shows real source page progress only after a worker starts", async () => {
    const user = userEvent.setup();
    const workspace = diagnosticWorkspaceValue();
    workspace.extraction = {
      job: {
        job_id: "job-running",
        status: "RUNNING",
        stage: "source_ir",
        processing_plan: {
          project_profile_revision: 4,
          connection_revision: 12,
          llm_provider: "aliyun",
          document: {
            primary_parser: "pymupdf",
            enhancement_chain: ["bailian_layout"],
            complex_vision: null,
          },
          provider_bindings: {
            semantic_deployment: "qwen3.8-max",
            structure_overview_deployment: "qwen3.8-max",
            structure_repair_deployment: "qwen3.7-flash",
          },
          force_visual_pages: [],
        },
        last_progress: "已处理 37/137 页，当前第 38 页。",
        progress: {
          completed_pages: 37,
          total_pages: 137,
          current_page: 38,
          page_progress_kind: "SOURCE",
        },
        latest_visual_page_event: {
          page_number: 37,
          provider_id: "bailian-visual-text",
          provider_version: "1.4.0+qwen3.5-ocr+deadline.45s",
          request_count: 1,
          latency_ms: 36708,
          cache_hit: false,
          outcome: "READY",
          updated_at: "2026-08-18T12:00:37Z",
        },
        queue: { state: "CLAIMED", can_cancel: true },
      },
      previous_artifact: {
        available: true,
        runtime_compatible: true,
        read_mode: "READ_ONLY",
      },
    };
    workspace.requirementSets = {
      active_requirement_set_id: "persist-set",
      requirement_sets: [
        {
          id: "persist-set",
          version: 4,
          requirement_count: 635,
          requirement_tree_state: "READY",
        },
      ],
    };
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(await screen.findByText("正在读取逐页解析事实")).toBeInTheDocument();
    expect(screen.getByText("37/137 页 · 当前 P38")).toBeInTheDocument();
    expect(
      screen.getByRole("progressbar", { name: "全文解析页进度：已处理 37 页，共 137 页" }),
    ).toHaveAttribute("value", "37");
    const compactProgress = screen.getByRole("region", {
      name: "全文解析运行进度",
    });
    expect(within(compactProgress).getByRole("status")).not.toHaveTextContent(
      "处理阶段已持续",
    );
    const detailToggle = screen.getByRole("button", {
      name: "展开解析运行详情",
    });
    expect(detailToggle).toHaveAttribute("aria-expanded", "false");
    const detailRegion = document.getElementById(
      "extraction-progress-running-details",
    );
    expect(detailRegion).toHaveAttribute("hidden");

    await user.click(detailToggle);

    expect(
      screen.getByRole("button", { name: "收起解析运行详情" }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(detailRegion).not.toHaveAttribute("hidden");
    expect(detailRegion).toBeVisible();
    expect(screen.getByText("最近视觉页成功")).toBeInTheDocument();
    expect(screen.getByText("第 37 页 · 36.7 秒 · 1 次请求")).toBeInTheDocument();
    expect(
      screen.getByText("“已处理”只表示流程已推进，不等于视觉识别成功。"),
    ).toBeInTheDocument();
    expect(screen.getByText(/上一次成功解析仍可只读浏览/)).toBeInTheDocument();
    expect(
      screen.getByText(
        /Aliyun LLM · 语义模型 qwen3\.8-max · 结构模型 qwen3\.8-max\/qwen3\.7-flash/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "查看当前确认清单（v4，635 条）",
      }),
    ).toBeEnabled();
  });

  it.each([
    ["CACHE_HIT", true, 0, 4, "最近视觉页命中缓存", "第 7 页 · 4 毫秒 · 0 次请求"],
    ["FAILED", false, 1, 45001, "最近视觉页失败", "第 8 页 · 45 秒 · 1 次请求"],
  ] as const)(
    "shows the latest %s provider outcome without equating it to page progress",
    async (outcome, cacheHit, requestCount, latencyMs, title, detail) => {
      const workspace = diagnosticWorkspaceValue();
      workspace.extraction = {
        job: {
          job_id: `job-${outcome.toLowerCase()}`,
          status: "RUNNING",
          stage: "source_ir",
          progress: {
            completed_pages: 8,
            total_pages: 137,
            current_page: 9,
            page_progress_kind: "SOURCE",
          },
          latest_visual_page_event: {
            page_number: outcome === "CACHE_HIT" ? 7 : 8,
            provider_id: "bailian-visual-text",
            provider_version: "1.4.0",
            request_count: requestCount,
            latency_ms: latencyMs,
            cache_hit: cacheHit,
            outcome,
            updated_at: "2026-08-18T12:00:37Z",
          },
          queue: { state: "CLAIMED", can_cancel: true },
        },
      };
      vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());

      render(
        <WorkspaceProvider value={workspace}>
          <RequirementTreePanel />
        </WorkspaceProvider>,
      );

      expect(await screen.findByText(title)).toBeInTheDocument();
      expect(screen.getByText(detail)).toBeInTheDocument();
      if (outcome === "FAILED") {
        expect(screen.getByText("P8 视觉失败")).toBeVisible();
      }
      expect(
        screen.getByText("“已处理”只表示流程已推进，不等于视觉识别成功。"),
      ).toBeInTheDocument();
    },
  );

  it("keeps a long-running page visibly alive without estimating completion", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-18T12:00:00Z"));
    const workspace = diagnosticWorkspaceValue();
    workspace.extraction = {
      job: {
        job_id: "job-running-heartbeat",
        status: "RUNNING",
        stage: "source_ir",
        updated_at_utc: "2026-08-18T11:59:55Z",
        last_progress: "已处理 37/137 页，当前第 38 页。",
        progress: {
          completed_pages: 37,
          total_pages: 137,
          current_page: 38,
          page_progress_kind: "SOURCE",
          page_started_at_utc: "2026-08-18T11:59:40Z",
        },
        queue: { state: "CLAIMED", can_cancel: true },
      },
    };
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    const view = render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    try {
      fireEvent.click(
        screen.getByRole("button", { name: "展开解析运行详情" }),
      );
      expect(
        screen.getByText(
          "后台心跳正常 · 当前第 38 页处理阶段已持续 20 秒 · 仅表示运行时长，不是完成度估算",
        ),
      ).toBeInTheDocument();

      act(() => {
        vi.advanceTimersByTime(5000);
      });

      expect(
        screen.getByText(
          "后台心跳正常 · 当前第 38 页处理阶段已持续 25 秒 · 仅表示运行时长，不是完成度估算",
        ),
      ).toBeInTheDocument();

      act(() => {
        vi.advanceTimersByTime(40000);
      });

      expect(
        screen.getByText(
          "最近一次后台心跳在 50 秒前，正在继续确认任务状态。",
        ),
      ).toBeInTheDocument();
      expect(screen.getByText("后台响应延迟 · 50 秒")).toBeVisible();
      expect(screen.queryByText(/后台心跳正常/)).not.toBeInTheDocument();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("allows a direct structure route only for the compatible previous artifact", async () => {
    const user = userEvent.setup();
    const workspace = navigationWorkspaceValue();
    workspace.extraction = {
      job: {
        job_id: "job-running",
        status: "RUNNING",
        stage: "source_ir",
        queue: { state: "CLAIMED", can_cancel: true },
      },
      previous_artifact: {
        available: true,
        runtime_compatible: true,
        read_mode: "READ_ONLY",
        can_browse_structure: true,
        can_continue_structure: false,
      },
    };
    setStructureArtifactEditability(workspace, {
      editable: false,
      reason: "structure_derivation_rerun_in_progress",
    });
    vi.spyOn(api, "sourcePage").mockResolvedValue({});

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByText("当前展示上一次可用结果（只读）"),
    ).toBeInTheDocument();
    expect(screen.getByRole("tree", { name: "需求结构树" })).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "展开解析运行详情" }),
    );
    expect(
      screen.getByRole("button", { name: "查看上一版结构（只读）" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "进入原文需求清单" }),
    ).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "重新整理…" }),
    ).not.toBeInTheDocument();
  });

  it("keeps the v4 confirmation list reachable while cancellation is pending", async () => {
    const workspace = diagnosticWorkspaceValue();
    workspace.extraction = {
      job: {
        job_id: "job-cancelling",
        status: "CANCEL_REQUESTED",
        stage: "source_ir",
        queue: { state: "CANCELLING", can_cancel: false },
      },
    };
    workspace.requirementSets = {
      active_requirement_set_id: "persist-set",
      requirement_sets: [
        {
          id: "persist-set",
          version: 4,
          requirement_count: 635,
          requirement_tree_state: "READY",
        },
      ],
    };
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(await screen.findByText("正在安全停止解析")).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "查看当前确认清单（v4，635 条）",
      }),
    ).toBeEnabled();
  });

  it("marks a first queued job with no worker as a recoverable queue anomaly", async () => {
    const workspace = diagnosticWorkspaceValue();
    workspace.extraction = {
      job: {
        job_id: "job-stalled",
        status: "PENDING",
        stage: "queued",
        queue: {
          state: "WORKER_NOT_CLAIMING",
          position: 1,
          ahead_count: 0,
          running_count: 0,
          wait_seconds: 91,
          can_cancel: true,
          recoverable: true,
        },
      },
    };
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(await screen.findByText("解析排队异常")).toBeInTheDocument();
    expect(screen.getByText(/后台服务尚未领取/)).toBeInTheDocument();
    expect(screen.getByText("当前任务已等待 1 分钟。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "取消本次解析" })).toBeEnabled();
  });

  it("shows a scheduled retry time instead of claiming that a worker is pending", async () => {
    const workspace = diagnosticWorkspaceValue();
    workspace.extraction = {
      job: {
        job_id: "job-retry",
        status: "PENDING",
        stage: "queued",
        queue: {
          state: "RETRY_SCHEDULED",
          wait_seconds: 120,
          retry_available_at_utc: "2026-08-18T12:10:00+00:00",
          can_cancel: true,
          recoverable: true,
        },
      },
    };
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByText("解析将在稍后自动重试"),
    ).toBeInTheDocument();
    expect(screen.getByText(/计划重试时间：/)).toBeInTheDocument();
    expect(screen.getByText(/到达重试时间前不会有页面正在解析/)).toBeInTheDocument();
    expect(screen.queryByText(/正在等待后台服务领取/)).not.toBeInTheDocument();
  });

  it("keeps risk and scan-support overlays synchronized with the PDF lens", () => {
    const payload = diagnosticPagePayload();
    const page = (
      (payload.page_comparison as JsonRecord).pages as JsonRecord[]
    )[0];
    (page.blocks as JsonRecord[]).push({
      id: "page-raster",
      type: "PAGE_RASTER",
      label: "PGR-001",
      page_number: 1,
      source_order: 2,
      bbox: [0, 0, 595, 842],
      locatable: true,
      overlay_default: false,
    });
    page.issues = [
      {
        id: "warning-1",
        label: "ISS-001",
        severity: "warning",
        blocking: false,
        overlay: true,
        page_number: 1,
        bbox: [40, 80, 520, 120],
        locatable: true,
      },
    ];
    const snapshot = normalizeRestorationPage(payload, 1);

    expect(
      parsingDiagnosisOverlays({
        pageSnapshot: snapshot,
        focusedFactId: "",
        riskOnly: false,
        showSupportLayer: false,
      }).map((overlay) => overlay.type),
    ).toEqual(["TEXT", "ISSUE"]);
    expect(
      parsingDiagnosisOverlays({
        pageSnapshot: snapshot,
        focusedFactId: "page-raster",
        riskOnly: false,
        showSupportLayer: true,
      }).map((overlay) => overlay.type),
    ).toEqual(["TEXT", "PAGE_RASTER", "ISSUE"]);
    expect(
      parsingDiagnosisOverlays({
        pageSnapshot: snapshot,
        focusedFactId: "warning-1",
        riskOnly: true,
        showSupportLayer: true,
      }).map((overlay) => overlay.type),
    ).toEqual(["ISSUE"]);
  });

  it("uses the same source-channel filter for diagnosis overlays as the result cards", () => {
    const snapshot = normalizeRestorationPage(diagnosticOcrPagePayload(), 1);

    expect(
      parsingDiagnosisOverlays({
        pageSnapshot: snapshot,
        focusedFactId: "",
        riskOnly: false,
        showSupportLayer: false,
        contentView: "EFFECTIVE",
      }).map((overlay) => [overlay.id, overlay.channel, overlay.precision]),
    ).toEqual([
      ["diagnostic-ocr-text", "REMOTE_VISUAL", "APPROXIMATE"],
    ]);
    expect(
      parsingDiagnosisOverlays({
        pageSnapshot: snapshot,
        focusedFactId: "",
        riskOnly: false,
        showSupportLayer: false,
        contentView: "COMPARE",
      }).map((overlay) => [overlay.id, overlay.channel, overlay.precision]),
    ).toEqual([
      ["diagnostic-text", "NATIVE", "EXACT"],
      ["diagnostic-ocr-text", "REMOTE_VISUAL", "APPROXIMATE"],
    ]);
  });

  it("resets the diagnosis source view when the page identity changes", async () => {
    const user = userEvent.setup();
    const workspace = diagnosticWorkspaceValue();
    vi.spyOn(api, "sourcePage").mockImplementation(
      async (_projectId, _documentId, page) =>
        page === 1 ? diagnosticOcrPagePayload(1) : diagnosticPagePayload(2),
    );
    vi.spyOn(api, "parsingDiagnostics").mockResolvedValue(
      parsingRiskPayload({ severe: 0, blocking: 0 }),
    );

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await screen.findByText("OCR 视觉正文");
    await user.click(screen.getByRole("button", { name: "差异对照" }));
    expect(screen.getByRole("button", { name: "差异对照" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    await user.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByText("第 2 页完整解析文字");
    expect(screen.getByRole("button", { name: "最终采用" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(
      screen.getByRole("status", { name: "最终采用通道" }),
    ).toHaveTextContent("本页正文采用本地解析");
  });

  it("renders the page-synchronous lens and continues without creating a job", async () => {
    const user = userEvent.setup();
    const workspace = diagnosticWorkspaceValue();
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    vi.spyOn(api, "parsingDiagnostics").mockResolvedValue(
      parsingRiskPayload({ severe: 0, blocking: 0 }),
    );
    const latestRestoration = vi
      .spyOn(api, "latestRestoration")
      .mockResolvedValue(null);
    const startRestoration = vi.spyOn(api, "startRestoration");
    const startExtraction = vi.spyOn(api, "startExtraction");
    const geometryFocus = vi.spyOn(api, "geometryFocus");

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(await screen.findByLabelText("解析核对")).toBeInTheDocument();
    expect(screen.getByLabelText("原 PDF · 第 1 页")).toBeInTheDocument();
    expect(screen.getByLabelText("第 1 页解析结果")).toBeInTheDocument();
    expect(await screen.findByText("本页完整解析文字")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "返回结构" }));

    expect(screen.queryByLabelText("解析核对")).not.toBeInTheDocument();
    expect(workspace.updateRoute).toHaveBeenCalledWith({
      task: "extraction",
      mode: "organize",
      checkpoint: "structure",
      requirementSetId: "",
    });
    expect(latestRestoration).not.toHaveBeenCalled();
    expect(startRestoration).not.toHaveBeenCalled();
    expect(startExtraction).not.toHaveBeenCalled();
    expect(geometryFocus).not.toHaveBeenCalled();
  });

  it("fails closed immediately when the current parse artifact identity changes", async () => {
    const workspace = diagnosticWorkspaceValue();
    let resolveNextDiagnostics!: (value: JsonRecord) => void;
    const nextDiagnostics = new Promise<JsonRecord>((resolve) => {
      resolveNextDiagnostics = resolve;
    });
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    vi.spyOn(api, "parsingDiagnostics")
      .mockResolvedValueOnce(parsingRiskPayload({ severe: 0, blocking: 0 }))
      .mockReturnValueOnce(nextDiagnostics);

    const view = render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("button", { name: "返回结构" }),
    ).toBeEnabled();
    const nextArtifactId = "f".repeat(64);
    const workbench = workspace.structure?.structure_workbench as JsonRecord;
    (workbench.source_pages as JsonRecord).snapshot_id = nextArtifactId;
    view.rerender(
      <WorkspaceProvider value={{ ...workspace }}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(screen.getByRole("button", { name: "返回结构" })).toBeEnabled();

    resolveNextDiagnostics({
      ...parsingRiskPayload({ severe: 0, blocking: 0 }),
      active_parse_artifact_id: nextArtifactId,
    });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "返回结构" }),
      ).toBeEnabled(),
    );
  });

  it("does not create a shadow run when formal page facts are unavailable", async () => {
    const workspace = diagnosticWorkspaceValue({ sourcePagesReady: false });
    vi.spyOn(api, "latestRestoration").mockResolvedValue(null);
    const startRestoration = vi.spyOn(api, "startRestoration");
    const parsingDiagnostics = vi.spyOn(api, "parsingDiagnostics");

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByText("没有可读取的逐页解析工件"),
    ).toBeInTheDocument();
    await waitFor(() => expect(api.latestRestoration).toHaveBeenCalledOnce());
    expect(startRestoration).not.toHaveBeenCalled();
    expect(parsingDiagnostics).not.toHaveBeenCalled();
  });

  it("does not reuse a legacy restoration run or error across documents", async () => {
    const workspace = diagnosticWorkspaceValue({ sourcePagesReady: false });
    const oldRun: RestorationRun = {
      run_id: "old-run",
      status: "READY",
      progress: { page_count: 2 },
    };
    const newRun: RestorationRun = {
      run_id: "new-run",
      status: "READY",
      progress: { page_count: 1 },
    };
    let resolveNewRun!: (value: RestorationRun | null) => void;
    const pendingNewRun = new Promise<RestorationRun | null>((resolve) => {
      resolveNewRun = resolve;
    });
    vi.spyOn(api, "latestRestoration").mockImplementation(
      async (_projectId, documentId) =>
        documentId === "persist-document" ? oldRun : pendingNewRun,
    );
    const restorationPage = vi
      .spyOn(api, "restorationPage")
      .mockImplementation(async (_projectId, documentId, _runId, page) => {
        if (documentId === "persist-document") {
          throw new Error("old legacy page failed");
        }
        return diagnosticPagePayload(page);
      });

    const view = render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    await waitFor(() =>
      expect(restorationPage).toHaveBeenCalledWith(
        "persist-project",
        "persist-document",
        "old-run",
        1,
      ),
    );
    expect(
      await screen.findByText(/old legacy page failed/),
    ).toBeInTheDocument();

    const nextWorkspace = diagnosticWorkspaceValue({ sourcePagesReady: false });
    nextWorkspace.route = {
      ...nextWorkspace.route,
      documentId: "next-document",
    };
    nextWorkspace.selectedDocument = {
      ...nextWorkspace.selectedDocument!,
      id: "next-document",
    };
    view.rerender(
      <WorkspaceProvider value={nextWorkspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      screen.queryByText(/old legacy page failed/),
    ).not.toBeInTheDocument();
    expect(restorationPage).not.toHaveBeenCalledWith(
      "persist-project",
      "next-document",
      "old-run",
      expect.any(Number),
    );
    resolveNewRun(newRun);
    await waitFor(() =>
      expect(restorationPage).toHaveBeenCalledWith(
        "persist-project",
        "next-document",
        "new-run",
        1,
      ),
    );
  });

  it("keeps non-blocking risks visible without creating a human acknowledgement gate", async () => {
    const user = userEvent.setup();
    const workspace = diagnosticWorkspaceValue();
    const warning = parsingRiskPayload({ severe: 2, blocking: 0 });
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    vi.spyOn(api, "parsingDiagnostics").mockResolvedValue(warning);
    const acknowledge = vi.spyOn(api, "acknowledgeParsingRisks");

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(await screen.findByText(/2 项严重风险/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "返回结构" }));
    expect(acknowledge).not.toHaveBeenCalled();
    expect(workspace.updateRoute).toHaveBeenCalledWith(
      expect.objectContaining({ checkpoint: "structure" }),
    );
  });

  it("keeps an explicit parsing hard blocker visible without hiding the structure lens", async () => {
    const user = userEvent.setup();
    const workspace = diagnosticWorkspaceValue();
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    vi.spyOn(api, "parsingDiagnostics").mockResolvedValue(
      parsingRiskPayload({ severe: 0, blocking: 1 }),
    );

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(await screen.findByText("1 项硬阻断需要处理")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "返回结构" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "返回结构" }));
    expect(
      screen.getByRole("tab", { name: "第 1 步：原文结构" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(workspace.updateRoute).toHaveBeenCalledWith(
      expect.objectContaining({ checkpoint: "structure" }),
    );
  });

  it("fails closed when the document risk summary cannot be verified", async () => {
    const workspace = diagnosticWorkspaceValue();
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    vi.spyOn(api, "parsingDiagnostics").mockRejectedValue(
      new Error("risk summary unavailable"),
    );

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByText(/risk summary unavailable/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "返回结构" })).toBeEnabled();
  });

  it("keeps an existing acknowledgement as history without requiring another PUT", async () => {
    const user = userEvent.setup();
    const workspace = diagnosticWorkspaceValue();
    const warning = parsingRiskPayload({ severe: 2, blocking: 0 });
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    vi.spyOn(api, "parsingDiagnostics").mockResolvedValue({
      ...warning,
      risk_acknowledged_at: "2026-08-06T08:00:00Z",
      risk_acknowledged_artifact_id: "d".repeat(64),
    });
    const acknowledge = vi.spyOn(api, "acknowledgeParsingRisks");

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "返回结构" }),
    );
    expect(acknowledge).not.toHaveBeenCalled();
    expect(workspace.updateRoute).toHaveBeenCalledWith(
      expect.objectContaining({ checkpoint: "structure" }),
    );
  });

  it("opens whole-document rerun without a job and starts one explicit Codex baseline", async () => {
    const user = userEvent.setup();
    const workspace = diagnosticWorkspaceValue();
    const runAction = vi.fn(
      async (_label: string, action: () => Promise<unknown>) => action(),
    );
    workspace.runAction = runAction as WorkspaceContextValue["runAction"];
    enableCodexBaseline(workspace);
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    vi.spyOn(api, "parsingDiagnostics").mockResolvedValue(
      parsingRiskPayload({ severe: 0, blocking: 0 }),
    );
    const startExtraction = vi
      .spyOn(api, "startExtraction")
      .mockResolvedValue({ job_id: "job-1" });

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "重新解析全文…" }),
    );
    expect(startExtraction).not.toHaveBeenCalled();
    expect(screen.getByText("生成新的全文解析结果")).toBeInTheDocument();
    expect(
      screen.getByText(/系统目前只支持整份文档重新解析/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "启动 Agent 原文基线" }));
    await waitFor(() => expect(startExtraction).toHaveBeenCalledOnce());
    expect(startExtraction).toHaveBeenCalledWith(
      "persist-project",
      "persist-document",
    );
    expect(runAction).toHaveBeenCalledWith(
      "Codex Agent 原文基线重新生成",
      expect.any(Function),
      {
        refresh: "document",
        successNotice: "Codex Agent 原文基线任务已提交，正在生成",
      },
    );
  });

  it("starts the authorized whole-document fast OCR override from the recovery dialog", async () => {
    const user = userEvent.setup();
    const workspace = diagnosticWorkspaceValue();
    enableCodexBaseline(workspace);
    workspace.runtimeCapabilities = [
      ...(workspace.runtimeCapabilities || []),
      {
        capability_id: "document.bailian_ocr",
        category: "document_enhancement",
        provider: "aliyun",
        label: "Bailian OCR",
        state: "READY",
        ready: true,
      },
    ];
    const runAction = vi.fn(
      async (_label: string, action: () => Promise<unknown>) => action(),
    );
    workspace.runAction = runAction as WorkspaceContextValue["runAction"];
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    vi.spyOn(api, "parsingDiagnostics").mockResolvedValue(
      parsingRiskPayload({ severe: 0, blocking: 1 }),
    );
    const startExtraction = vi
      .spyOn(api, "startExtraction")
      .mockResolvedValue({ job_id: "visual-job-1" });

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(await screen.findByText("1 项硬阻断需要处理")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新解析全文…" }));
    expect(startExtraction).not.toHaveBeenCalled();
    await user.click(
      screen.getByRole("button", { name: "使用Qwen OCR 结构恢复解析整个文档" }),
    );

    await waitFor(() => expect(startExtraction).toHaveBeenCalledOnce());
    expect(startExtraction).toHaveBeenCalledWith(
      "persist-project",
      "persist-document",
      {
        schema_version: "run-profile-override/v1",
        document: {
          primary_parser: "pymupdf",
          enhancement_chain: ["bailian_ocr"],
          complex_vision: null,
          auto_escalation: false,
        },
        llm_provider: "codex",
        force_visual_pages: [],
      },
    );
    expect(runAction).toHaveBeenCalledWith(
      "Qwen OCR 结构恢复并重新提取",
      expect.any(Function),
      {
        refresh: "document",
        successNotice: "Qwen OCR 结构恢复任务已提交，正在生成全文结果",
      },
    );
    await waitFor(() =>
      expect(
        screen.getByText("生成新的全文解析结果").closest("dialog"),
      ).not.toHaveAttribute("open"),
    );
  });

  it("keeps the current facts visible when an explicit whole-document rerun fails", async () => {
    const user = userEvent.setup();
    const workspace = diagnosticWorkspaceValue();
    enableCodexBaseline(workspace);
    workspace.runAction = async (_label, action) => {
      try {
        return await action();
      } catch {
        return null;
      }
    };
    vi.spyOn(api, "sourcePage").mockResolvedValue(diagnosticPagePayload());
    vi.spyOn(api, "parsingDiagnostics").mockResolvedValue(
      parsingRiskPayload({ severe: 0, blocking: 0 }),
    );
    vi.spyOn(api, "startExtraction").mockRejectedValue(
      new Error("rerun failed"),
    );

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(await screen.findByText("本页完整解析文字")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新解析全文…" }));
    await user.click(screen.getByRole("button", { name: "启动 Agent 原文基线" }));

    expect(screen.getByText("生成新的全文解析结果")).toBeInTheDocument();
    expect(screen.getByText("本页完整解析文字")).toBeInTheDocument();
    expect(workspace.updateRoute).not.toHaveBeenCalled();
  });

  it("loads pages on demand and keeps only the five most recently used payloads", async () => {
    const user = userEvent.setup();
    const workspace = diagnosticWorkspaceValue();
    const workbench = workspace.structure?.structure_workbench as JsonRecord;
    (workbench.source_pages as JsonRecord).page_count = 6;
    const sourcePage = vi
      .spyOn(api, "sourcePage")
      .mockImplementation(async (_projectId, _documentId, page) =>
        diagnosticPagePayload(page),
      );
    vi.spyOn(api, "parsingDiagnostics").mockResolvedValue({
      ...parsingRiskPayload({ severe: 0, blocking: 0 }),
      page_count: 6,
    });

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(await screen.findByText("本页完整解析文字")).toBeInTheDocument();
    for (let page = 2; page <= 6; page += 1) {
      await user.click(screen.getByRole("button", { name: "下一页" }));
      expect(
        await screen.findByText(`第 ${page} 页完整解析文字`),
      ).toBeInTheDocument();
    }
    expect(sourcePage).toHaveBeenCalledTimes(6);

    for (let page = 5; page >= 1; page -= 1) {
      await user.click(screen.getByRole("button", { name: "上一页" }));
      expect(
        await screen.findByText(
          page === 1 ? "本页完整解析文字" : `第 ${page} 页完整解析文字`,
        ),
      ).toBeInTheDocument();
    }
    expect(sourcePage).toHaveBeenCalledTimes(7);
    expect(sourcePage).toHaveBeenLastCalledWith(
      "persist-project",
      "persist-document",
      1,
    );
  });

  it("resets page navigation immediately when the selected document changes", async () => {
    const user = userEvent.setup();
    const workspace = diagnosticWorkspaceValue();
    const workbench = workspace.structure?.structure_workbench as JsonRecord;
    (workbench.source_pages as JsonRecord).page_count = 3;
    const sourcePage = vi
      .spyOn(api, "sourcePage")
      .mockImplementation(async (_projectId, _documentId, page) =>
        diagnosticPagePayload(page),
      );
    vi.spyOn(api, "parsingDiagnostics").mockImplementation(
      async (_projectId, documentId) => ({
        ...parsingRiskPayload({ severe: 0, blocking: 0 }),
        active_parse_artifact_id:
          documentId === "next-document" ? "f".repeat(64) : "d".repeat(64),
      }),
    );

    const view = render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    expect(await screen.findByText("本页完整解析文字")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("第 2 页完整解析文字")).toBeInTheDocument();

    const nextWorkspace = diagnosticWorkspaceValue();
    nextWorkspace.route = {
      ...nextWorkspace.route,
      documentId: "next-document",
    };
    nextWorkspace.selectedDocument = {
      ...nextWorkspace.selectedDocument!,
      id: "next-document",
      page_count: 1,
    };
    const nextWorkbench = nextWorkspace.structure
      ?.structure_workbench as JsonRecord;
    nextWorkbench.source_pages = {
      state: "READY",
      snapshot_id: "f".repeat(64),
      source_sha256: sourceSha256,
      page_count: 1,
    };
    view.rerender(
      <WorkspaceProvider value={nextWorkspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(screen.getByLabelText("第 1 页解析结果")).toBeInTheDocument();
    await waitFor(() =>
      expect(sourcePage).toHaveBeenCalledWith(
        "persist-project",
        "next-document",
        1,
      ),
    );
    expect(sourcePage).not.toHaveBeenCalledWith(
      "persist-project",
      "next-document",
      2,
    );
  });

  it("treats the formal snapshot page count as authoritative when it shrinks", async () => {
    const user = userEvent.setup();
    const workspace = diagnosticWorkspaceValue();
    const workbench = workspace.structure?.structure_workbench as JsonRecord;
    (workbench.source_pages as JsonRecord).page_count = 3;
    workspace.selectedDocument = {
      ...workspace.selectedDocument!,
      page_count: 3,
    };
    const sourcePage = vi
      .spyOn(api, "sourcePage")
      .mockImplementation(async (_projectId, _documentId, page) =>
        diagnosticPagePayload(page),
      );
    vi.spyOn(api, "parsingDiagnostics").mockResolvedValue({
      ...parsingRiskPayload({ severe: 0, blocking: 0 }),
      page_count: 3,
    });

    const view = render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    expect(await screen.findByText("本页完整解析文字")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "下一页" }));
    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("第 3 页完整解析文字")).toBeInTheDocument();
    expect(sourcePage).toHaveBeenCalledTimes(3);

    (workbench.source_pages as JsonRecord).page_count = 1;
    view.rerender(
      <WorkspaceProvider value={{ ...workspace }}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(screen.getByLabelText("第 1 页解析结果")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    await waitFor(() => expect(sourcePage).toHaveBeenCalledTimes(3));
  });
});

function diagnosticWorkspaceValue({
  sourcePagesReady = true,
}: { sourcePagesReady?: boolean } = {}): WorkspaceContextValue {
  const workspace = navigationWorkspaceValue();
  workspace.route = {
    ...workspace.route,
    checkpoint: "diagnose",
  };
  const workbench = workspace.structure?.structure_workbench as JsonRecord;
  workbench.source_pages = sourcePagesReady
    ? {
        state: "READY",
        snapshot_id: "d".repeat(64),
        source_sha256: sourceSha256,
        page_count: 2,
      }
    : { state: "UNAVAILABLE", page_count: 0 };
  return workspace;
}

function enableCodexBaseline(workspace: WorkspaceContextValue): void {
  workspace.processingProfile = {
    schema_version: "project-processing-profile/v1",
    project_id: "persist-project",
    document: {
      primary_parser: "pymupdf",
      enhancement_chain: [],
      complex_vision: null,
      auto_escalation: true,
    },
    llm_provider: "codex",
    knowledge_backend: "none",
    revision: 0,
    canonical_sha256: "9".repeat(64),
  };
  workspace.runtimeCapabilities = [
    {
      capability_id: "document.pymupdf",
      category: "document_parser",
      provider: "local",
      label: "PyMuPDF",
      state: "READY",
      ready: true,
      reason_code: "",
      message: "",
    },
    {
      capability_id: "codex.agent",
      category: "llm",
      provider: "codex",
      label: "Codex Agent",
      state: "READY",
      ready: true,
      reason_code: "",
      message: "",
    },
  ];
}

function diagnosticPagePayload(page = 1): JsonRecord {
  return {
    source_identity: { source_sha256: sourceSha256 },
    page_comparison: {
      pages: [
        {
          page_number: page,
          bounds: [0, 0, 595, 842],
          blocks: [
            {
              id: "diagnostic-text",
              type: "TEXT",
              label: "TXT-001",
              text:
                page === 1 ? "本页完整解析文字" : `第 ${page} 页完整解析文字`,
              page_number: page,
              source_order: 1,
              bbox: [40, 80, 520, 120],
              locatable: true,
              provenance: { channel: "NATIVE" },
            },
          ],
          issues: [],
          coverage: {
            remote_text: {
              evaluated: false,
              status: "NOT_EVALUATED",
              observation_count: 0,
            },
            remote_visual: { evaluated_types: [] },
          },
        },
      ],
    },
    tables: [],
  };
}

function diagnosticOcrPagePayload(page = 1): JsonRecord {
  const payload = diagnosticPagePayload(page);
  const comparison = payload.page_comparison as JsonRecord;
  const pagePayload = (comparison.pages as JsonRecord[])[0];
  (pagePayload.blocks as JsonRecord[]).push({
    id: "diagnostic-ocr-text",
    type: "TEXT",
    label: "TXT-002",
    text: "OCR 视觉正文",
    page_number: page,
    source_order: 2,
    bbox: [40, 80, 520, 120],
    locatable: true,
    provenance: { channel: "REMOTE_VISUAL" },
  });
  pagePayload.coverage = {
    remote_text: {
      evaluated: true,
      status: "OBSERVED",
      observation_count: 1,
    },
    remote_visual: { evaluated_types: ["TEXT"] },
  };
  return payload;
}

function diagnosticPagePayloadWithText(page: number, text: string): JsonRecord {
  const payload = diagnosticPagePayload(page);
  const comparison = payload.page_comparison as JsonRecord;
  const pages = comparison.pages as JsonRecord[];
  const blocks = pages[0].blocks as JsonRecord[];
  blocks[0].text = text;
  return payload;
}

function extractionWithStagedPages({
  generation,
  readyPageCount,
  readyPageRanges,
}: {
  generation: number;
  readyPageCount: number;
  readyPageRanges: Array<{ start_page: number; end_page: number }>;
}): JsonRecord {
  return {
    job: {
      job_id: "staged-job",
      status: "RUNNING",
      stage: "source_ir",
      progress: {
        completed_pages: readyPageCount,
        total_pages: 2,
        current_page: Math.min(readyPageCount + 1, 2),
      },
      page_staging: {
        state: "ACTIVE",
        job_id: "staged-job",
        attempt_generation: generation,
        total_pages: 2,
        ready_page_count: readyPageCount,
        ready_page_ranges: readyPageRanges,
        page_url_template: `/api/extraction/attempts/staged-job/generations/${generation}/source-pages/{page_number}`,
      },
    },
    previous_artifact: {
      available: true,
      job_id: "formal-job",
      runtime_compatible: true,
      read_mode: "READ_ONLY",
      can_browse_structure: true,
      can_continue_structure: false,
    },
  };
}

function parsingRiskPayload({
  severe,
  blocking,
}: {
  severe: number;
  blocking: number;
}): JsonRecord {
  const risks = Array.from({ length: severe + blocking }, (_, index) => ({
    id: `risk-${index + 1}`,
    code: index < blocking ? "SOURCE_UNREADABLE" : "VISUAL_GAP",
    severity: index < blocking ? "error" : "warning",
    blocking: index < blocking,
    stage: "SOURCE_IR",
    message: index < blocking ? "Source is unreadable." : "Visual gap.",
    page_number: 1,
  }));
  return {
    schema_version: "parsing-diagnostics/v1",
    status:
      blocking > 0 ? "BLOCKED" : severe > 0 ? "READY_WITH_RISKS" : "READY",
    source_sha256: sourceSha256,
    active_parse_artifact_id: "d".repeat(64),
    parse_identity_sha256: "e".repeat(64),
    page_count: 2,
    hard_blocker_count: blocking,
    severe_warning_count: severe,
    risk_count: severe + blocking,
    severe_issue_ids: risks
      .filter((risk) => !risk.blocking)
      .map((risk) => risk.id),
    blocking_issue_ids: risks
      .filter((risk) => risk.blocking)
      .map((risk) => risk.id),
    unassigned_risk_count: 0,
    risk_pages:
      severe + blocking > 0
        ? [
            {
              page_number: 1,
              hard_blocker_count: blocking,
              severe_warning_count: severe,
              risk_ids: risks.map((risk) => risk.id),
              issue_codes: risks.map((risk) => risk.code),
            },
          ]
        : [],
    risks,
  };
}

describe("RequirementTreePanel navigation continuity", () => {
  it("opens and cancels the manual row-group editor without writing", async () => {
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const review = {
      ...tableRowGroupReview(),
      current_row_groups: [
        {
          rows: [{ table_id: "physical-table-1", row_index: 1 }],
        },
      ],
    };
    const readGroups = vi
      .spyOn(api, "tableRecordProjectionRowGroups")
      .mockResolvedValue(review);
    const revise = vi.spyOn(api, "reviseTableRecordProjection");
    const user = userEvent.setup();

    render(
      <WorkspaceProvider
        value={navigationWorkspaceValue(tableRowGroupNavigationTree())}
      >
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("treeitem", { name: /表 4 复杂技术参数/ }),
    );
    await user.click(
      screen.getByRole("button", { name: "人工调整行组" }),
    );
    expect(await screen.findByText("业务行 3")).toBeInTheDocument();
    expect(readGroups).toHaveBeenCalledTimes(1);
    expect(revise).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(revise).not.toHaveBeenCalled();
  });

  it("submits one exact v2 row-group partition and keeps stale conflicts visible", async () => {
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "tableRecordProjectionRowGroups").mockResolvedValue(
      tableRowGroupReview(),
    );
    const revise = vi
      .spyOn(api, "reviseTableRecordProjection")
      .mockRejectedValueOnce(
        new ApiError("conflict", 409, {
          detail: { code: "table_projection_revision_conflict" },
        }),
      );
    const user = userEvent.setup();

    render(
      <WorkspaceProvider
        value={navigationWorkspaceValue(tableRowGroupNavigationTree())}
      >
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("treeitem", { name: /表 4 复杂技术参数/ }),
    );
    await user.click(
      screen.getByRole("button", { name: "人工调整行组" }),
    );
    await screen.findByText("业务行 3");
    await user.click(
      screen.getByRole("button", { name: "在原表第 2 行后拆分" }),
    );
    await user.click(screen.getByRole("button", { name: "保存 2 个行组" }));

    expect(revise).toHaveBeenCalledTimes(1);
    expect(revise.mock.calls[0]?.[3]).toEqual({
      schema_version: "table-record-projection-revision-request/v2",
      action: "SPLIT_ROWS",
      expected_projection_revision: 4,
      expected_source_sha256: sourceSha256,
      expected_structure_snapshot_id:
        "effective-structure-" + "a".repeat(32),
      row_groups: [
        {
          rows: [
            { table_id: "physical-table-1", row_index: 1 },
          ],
        },
        {
          rows: [
            { table_id: "physical-table-1", row_index: 2 },
            { table_id: "physical-table-1", row_index: 3 },
          ],
        },
      ],
    });
    expect(
      await screen.findByText(/表格或结构状态已变化/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "保存 2 个行组" }),
    ).toBeEnabled();
  });

  it("shows row groups without allowing writes on a read-only artifact", async () => {
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "tableRecordProjectionRowGroups").mockResolvedValue(
      tableRowGroupReview(),
    );
    const revise = vi.spyOn(api, "reviseTableRecordProjection");
    const workspace = navigationWorkspaceValue(tableRowGroupNavigationTree());
    setStructureArtifactEditability(workspace, {
      editable: false,
      reason: "structure_derivation_rerun_not_complete",
    });
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    await user.click(
      await screen.findByRole("treeitem", { name: /表 4 复杂技术参数/ }),
    );
    await user.click(
      screen.getByRole("button", { name: "查看行组（只读）" }),
    );

    const dialog = await screen.findByRole("dialog", {
      name: "人工调整表格行组",
    });
    expect(within(dialog).getByText("当前结果只读")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "保存 1 个行组" }),
    ).toBeDisabled();
    expect(revise).not.toHaveBeenCalled();
  });

  it("keeps an incompatible direct list checkpoint browseable while showing regeneration recovery", async () => {
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    tree.state = "REQUIRES_REEXTRACTION";
    tree.nodes = [];
    tree.root_node_ids = [];
    const value = navigationWorkspaceValue(tree);
    value.route = { ...value.route, checkpoint: "list" };
    const workbench = value.structure?.structure_workbench as JsonRecord;
    workbench.recovery = {
      reason_code: "RESULT_REGENERATION_REQUIRED",
      detail_code: "RUNTIME_INCOMPATIBLE",
    };

    render(
      <WorkspaceProvider value={value}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(screen.queryByLabelText("解析核对")).not.toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "第 2 步：原文需求清单" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(
      screen.getByText(/当前解析结果与运行版本或结果合同不兼容/),
    ).toBeInTheDocument();
    expect(screen.queryByText("正在读取清单分类")).not.toBeInTheDocument();
    expect(screen.queryByText("正在读取分类")).not.toBeInTheDocument();
    expect(api.entryClassifications).not.toHaveBeenCalled();
  });

  it("keeps old structure and PDF browsing available without exposing editable structure actions", async () => {
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const patchStructure = vi.spyOn(api, "patchStructure");
    const saveReview = vi.spyOn(api, "saveReviewSession");
    const reviseTable = vi.spyOn(api, "reviseTableRecordProjection");
    const previewBoundary = vi.spyOn(api, "previewBoundaryCalibration");
    const optimize = vi.spyOn(api, "startStructureOptimization");
    const workspace = navigationWorkspaceValue();
    workspace.extraction = {
      job: { job_id: "interrupted-rerun", status: "INTERRUPTED" },
    };
    setStructureArtifactEditability(workspace, {
      editable: false,
      reason: "structure_derivation_rerun_not_complete",
    });
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByText("当前展示上一次可用结果（只读）"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tree", { name: "需求结构树" }),
    ).toBeInTheDocument();
    expect(screen.getByTitle("原 PDF · 第 1 页")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "进入原文需求清单" }),
    ).toBeEnabled();
    const calibration = screen.getByRole("tab", { name: "版面校准" });
    expect(calibration).toBeDisabled();
    expect(screen.queryByRole("button", { name: "调整结构" })).toBeNull();

    expect(
      screen.queryByRole("button", { name: "重新整理…" }),
    ).not.toBeInTheDocument();
    fireEvent.click(calibration);
    expect(patchStructure).not.toHaveBeenCalled();
    expect(saveReview).not.toHaveBeenCalled();
    expect(reviseTable).not.toHaveBeenCalled();
    expect(previewBoundary).not.toHaveBeenCalled();
    expect(optimize).not.toHaveBeenCalled();
  });

  it("searches a bounded parent list by full ancestor path and excludes descendants", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const value = navigationWorkspaceValue();
    const workbench = value.structure?.structure_workbench as JsonRecord;
    const fillerNodes = Array.from({ length: 520 }, (_, index) => ({
      id: `filler-${index}`,
      parent_id: "persist-root",
      label: `附录候选 ${index + 1}`,
      order: index + 20,
      allows_children: true,
    }));
    workbench.capabilities = { can_edit_structure: true };
    workbench.structure = {
      effective_snapshot_id: "effective",
      nodes: [
        {
          id: "persist-root",
          parent_id: "",
          label: "技术规范",
          order: 0,
          allows_children: true,
        },
        {
          id: "persist-section",
          parent_id: "persist-root",
          label: "2 技术要求",
          order: 1,
          allows_children: true,
        },
        {
          id: "persist-leaf",
          parent_id: "persist-section",
          label: "2.3.1 持久化叶节点",
          order: 2,
          allows_children: true,
        },
        {
          id: "target-section",
          parent_id: "persist-root",
          label: "5 目标章节",
          order: 5,
          allows_children: true,
        },
        {
          id: "target-parent",
          parent_id: "target-section",
          label: "5.2 目标父级",
          order: 6,
          allows_children: true,
        },
        {
          id: "selected-descendant",
          parent_id: "persist-leaf",
          label: "不可选后代节点",
          order: 3,
          allows_children: true,
        },
        ...fillerNodes,
      ],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByTitle("选择结构树展开范围"));
    await user.click(screen.getByRole("button", { name: "全部展开" }));
    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );
    await user.click(screen.getByRole("button", { name: "调整结构" }));
    const parentSearch = screen.getByRole("combobox", {
      name: "搜索新的父级",
    });
    const initialOptions = screen.getAllByRole("option");
    expect(initialOptions.length).toBeLessThanOrEqual(8);
    expect(
      Number(initialOptions[0].getAttribute("aria-setsize")),
    ).toBeGreaterThan(initialOptions.length);
    expect(initialOptions[0]).toHaveAttribute("aria-posinset", "1");
    expect(initialOptions[0]).toHaveAttribute("tabindex", "-1");

    await user.type(parentSearch, "附录候选 520");
    const previousMatch = await screen.findByRole("option", {
      name: /附录候选 520/,
    });
    expect(previousMatch).toHaveAttribute("aria-setsize", "1");

    await user.clear(parentSearch);
    await user.type(parentSearch, "目标父级{Enter}");
    expect(parentSearch).toHaveFocus();
    expect(screen.getByText(/已选择.*5\.2 目标父级/)).toBeInTheDocument();

    await user.clear(parentSearch);
    await user.type(parentSearch, "不可选后代节点");
    expect(
      screen.queryByRole("option", { name: /不可选后代节点/ }),
    ).not.toBeInTheDocument();
  });

  it("loads the exact classification snapshot only while the list checkpoint is open", async () => {
    class TestResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe = (element: Element) =>
        this.callback(
          [
            {
              target: element,
              contentRect: { width: 340, height: 560 },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      unobserve = vi.fn();
      disconnect = vi.fn();
    }
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const user = userEvent.setup();
    const value = navigationWorkspaceValue();
    value.route = { ...value.route, checkpoint: "structure" };
    const view = render(
      <WorkspaceProvider value={value}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await screen.findByRole("button", { name: "进入原文需求清单" });
    expect(api.entryClassifications).not.toHaveBeenCalled();
    const listValue = {
      ...value,
      route: { ...value.route, checkpoint: "list" as const },
    };
    view.rerender(
      <WorkspaceProvider value={listValue}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await screen.findByRole("region", { name: "清单分类统计" });
    expect(api.entryClassifications).toHaveBeenCalledTimes(1);
    await user.type(
      screen.getByPlaceholderText("搜索章节、条款、表格或候选"),
      "持久化",
    );
    await user.click(screen.getByRole("button", { name: "技术需求筛选" }));
    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );
    expect(api.entryClassifications).toHaveBeenCalledTimes(1);
  });

  it("keeps a previously successful list readable but disables every mutation when the artifact is read-only", async () => {
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const working = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
    });
    vi.mocked(api.entryClassifications).mockResolvedValue(working);
    vi.mocked(api.requirementEnrichment).mockResolvedValue(
      atomizationEnrichmentState(working),
    );
    const update = vi.spyOn(api, "updateEntryClassifications");
    const accept = vi.spyOn(api, "acceptEntryClassifications");
    const preview = vi.spyOn(api, "previewRequirementSemanticPublication");
    const publish = vi.spyOn(api, "publishRequirementSemanticDraft");
    const activate = vi.spyOn(api, "activateRequirementSet");
    const legacyPublish = vi.fn();
    vi.stubGlobal("fetch", legacyPublish);
    const workspace = semanticListWorkspace(tree);
    workspace.extraction = {
      job: {
        job_id: "failed-rerun",
        status: "FAILED",
        error_stage: "structure",
        error_code: "STRUCTURE_DERIVATION_FAILED",
      },
    };
    setStructureArtifactEditability(workspace, {
      editable: false,
      reason: "structure_derivation_rerun_not_complete",
    });
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByText("当前展示上一次可用结果（只读）"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/分类、原子化和生成确认清单均已停用/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "查看来源证据" }),
    ).toBeEnabled();
    const batch = await screen.findByRole("button", { name: "批量分类" });
    const generate = screen.getByRole("button", {
      name: "当前结果只读",
    });
    expect(batch).toBeDisabled();
    expect(generate).toBeDisabled();

    await resetNavigationViewportToTop();
    await user.click(screen.getByText("展开", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "全部展开" }));
    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );
    expect(screen.getByRole("button", { name: "技术需求" })).toBeDisabled();
    expect(
      await screen.findByRole("checkbox", {
        name: "加入“所选项”原子化范围",
      }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "选择原子化范围" }),
    ).toBeDisabled();

    fireEvent.click(batch);
    fireEvent.click(generate);
    fireEvent.click(screen.getByRole("button", { name: "技术需求" }));
    fireEvent.click(
      screen.getByRole("button", { name: "选择原子化范围" }),
    );
    expect(update).not.toHaveBeenCalled();
    expect(accept).not.toHaveBeenCalled();
    expect(api.runRequirementAtomizationScope).not.toHaveBeenCalled();
    expect(preview).not.toHaveBeenCalled();
    expect(publish).not.toHaveBeenCalled();
    expect(activate).not.toHaveBeenCalled();
    expect(legacyPublish).not.toHaveBeenCalled();
  });

  it("saves one selected row immediately with the exact optimistic receipt", async () => {
    class TestResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe = (element: Element) =>
        this.callback(
          [
            {
              target: element,
              contentRect: { width: 340, height: 560 },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      unobserve = vi.fn();
      disconnect = vi.fn();
    }
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const working = classificationResponseForTree(tree, {
      status: "WORKING",
      type: null,
      snapshotId: "working-ui",
      payloadSha256: "1".repeat(64),
    });
    const saved = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
      snapshotId: "working-ui-2",
      payloadSha256: "2".repeat(64),
    });
    vi.mocked(api.entryClassifications).mockResolvedValue(working);
    const update = vi
      .spyOn(api, "updateEntryClassifications")
      .mockResolvedValue(saved);
    const user = userEvent.setup();
    const value = navigationWorkspaceValue(tree);
    value.route = { ...value.route, checkpoint: "list" };
    setStructureArtifactEditability(value, { editable: true });
    render(
      <WorkspaceProvider value={value}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );
    await user.click(screen.getByRole("button", { name: "技术需求" }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update.mock.calls[0]?.[2]).toEqual(
      expect.objectContaining({
        expected_revision: 1,
        expected_snapshot_sha256: "1".repeat(64),
        expected_source_sha256: sourceSha256,
        expected_working_tree_sha256: "d".repeat(64),
        expected_effective_structure_id: "effective",
        expected_policy_version: "entry-classification-policy/v1",
        overrides: { "persist-leaf": "TECHNICAL" },
      }),
    );
    expect(screen.getByRole("button", { name: "技术需求" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("ignores a late classification patch after switching documents without clearing the new patch", async () => {
    const treeA = navigationTree();
    const treeB: RequirementTreeSnapshot = {
      ...navigationTree(),
      source_identity: {
        ...navigationTree().source_identity,
        document_id: "persist-document-b",
      },
      structure_identity: {
        ...navigationTree().structure_identity,
        effective_snapshot_id: "effective-b",
        effective_tree_sha256: "f".repeat(64),
      },
    };
    const workingA = classificationResponseForTree(treeA, {
      status: "WORKING",
      type: null,
      snapshotId: "working-document-a",
      payloadSha256: "1".repeat(64),
    });
    const workingB = classificationResponseForTree(treeB, {
      status: "WORKING",
      type: null,
      snapshotId: "working-document-b",
      payloadSha256: "2".repeat(64),
    });
    const savedA = classificationResponseForTree(treeA, {
      status: "WORKING",
      type: "TECHNICAL",
      snapshotId: "saved-document-a",
      payloadSha256: "3".repeat(64),
    });
    const savedB = classificationResponseForTree(treeB, {
      status: "WORKING",
      type: "TECHNICAL",
      snapshotId: "saved-document-b",
      payloadSha256: "4".repeat(64),
    });
    let resolvePatchA: (value: EntryClassificationResponse) => void = () => {};
    let resolvePatchB: (value: EntryClassificationResponse) => void = () => {};
    const patchA = new Promise<EntryClassificationResponse>((resolve) => {
      resolvePatchA = resolve;
    });
    const patchB = new Promise<EntryClassificationResponse>((resolve) => {
      resolvePatchB = resolve;
    });
    vi.mocked(api.entryClassifications).mockImplementation(
      async (_projectId, documentId) =>
        documentId === "persist-document-b" ? workingB : workingA,
    );
    const update = vi
      .spyOn(api, "updateEntryClassifications")
      .mockImplementation(async (_projectId, documentId) =>
        documentId === "persist-document-b" ? patchB : patchA,
      );
    const valueA = navigationWorkspaceValue(treeA);
    valueA.route = { ...valueA.route, checkpoint: "list" };
    setStructureArtifactEditability(valueA, { editable: true });
    const user = userEvent.setup();
    const view = render(
      <WorkspaceProvider value={valueA}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );
    await user.click(screen.getByRole("button", { name: "技术需求" }));
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(
        "persist-project",
        "persist-document",
        expect.any(Object),
      ),
    );

    const valueB = navigationWorkspaceValue(treeB);
    valueB.route = {
      ...valueB.route,
      checkpoint: "list",
      documentId: "persist-document-b",
    };
    valueB.selectedDocument = {
      ...valueB.selectedDocument!,
      id: "persist-document-b",
    };
    setStructureArtifactEditability(valueB, { editable: true });
    view.rerender(
      <WorkspaceProvider value={valueB}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );
    const documentBTechnical = await screen.findByRole("button", {
      name: "技术需求",
    });
    await waitFor(() => expect(documentBTechnical).toBeEnabled());
    await user.click(documentBTechnical);
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(
        "persist-project",
        "persist-document-b",
        expect.any(Object),
      ),
    );
    expect(documentBTechnical).toBeDisabled();

    await act(async () => {
      resolvePatchA(savedA);
      await patchA;
    });
    expect(
      screen.getByRole("button", { name: "技术需求" }),
    ).toBeDisabled();

    await act(async () => {
      resolvePatchB(savedB);
      await patchB;
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "技术需求" })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );
    expect(screen.getByRole("button", { name: "技术需求" })).toBeEnabled();
  });

  it("previews a batch scope locally and sends only classifiable workflow IDs", async () => {
    class TestResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe = (element: Element) =>
        this.callback(
          [
            {
              target: element,
              contentRect: { width: 340, height: 560 },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      unobserve = vi.fn();
      disconnect = vi.fn();
    }
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const working = classificationResponseForTree(tree, {
      status: "WORKING",
      type: null,
    });
    const saved = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "NON_TECHNICAL",
      snapshotId: "working-batch-2",
      payloadSha256: "8".repeat(64),
    });
    vi.mocked(api.entryClassifications).mockResolvedValue(working);
    const update = vi
      .spyOn(api, "updateEntryClassifications")
      .mockResolvedValue(saved);
    const user = userEvent.setup();
    const value = navigationWorkspaceValue(tree);
    value.route = { ...value.route, checkpoint: "list" };
    render(
      <WorkspaceProvider value={value}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "批量分类" }));
    expect(
      screen.getByText("只修改所选清单内容；标题仅用于显示，不会被分类。"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/服务端分类快照/)).not.toBeInTheDocument();
    expect(update).not.toHaveBeenCalled();
    await user.click(screen.getByRole("radio", { name: /全部条目/ }));
    await user.click(
      screen.getByRole("button", { name: "批量设为非技术需求" }),
    );
    await user.click(screen.getByRole("button", { name: "应用到 1 项" }));

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update.mock.calls[0]?.[2]).toEqual(
      expect.objectContaining({
        overrides: { "persist-leaf": "NON_TECHNICAL" },
      }),
    );
  });

  it("ignores a hidden structure filter and restores content controls when entering list", async () => {
    class TestResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe = (element: Element) =>
        this.callback(
          [
            {
              target: element,
              contentRect: { width: 340, height: 560 },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      unobserve = vi.fn();
      disconnect = vi.fn();
    }
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    vi.mocked(api.entryClassifications).mockResolvedValue(
      classificationResponseForTree(tree, {
        status: "WORKING",
        type: "TECHNICAL",
      }),
    );
    const user = userEvent.setup();
    const structureValue = navigationWorkspaceValue(tree);
    structureValue.route = { ...structureValue.route, checkpoint: "structure" };
    const view = render(
      <WorkspaceProvider value={structureValue}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "需关注" }));
    expect(
      screen.queryByRole("treeitem", { name: /持久化叶节点/ }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "PDF 原文" }));

    const listValue = {
      ...structureValue,
      route: { ...structureValue.route, checkpoint: "list" as const },
    };
    view.rerender(
      <WorkspaceProvider value={listValue}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "内容与分类" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      screen.getByRole("searchbox", { name: "搜索清单内容" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "全部分类筛选" }),
    ).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "批量分类" }));
    expect(
      screen.getByRole("radio", { name: /当前筛选.*1 项/ }),
    ).toBeInTheDocument();
  });

  it("keeps a failed batch mutation visible and recoverable inside its dialog", async () => {
    class TestResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe = (element: Element) =>
        this.callback(
          [
            {
              target: element,
              contentRect: { width: 340, height: 560 },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      unobserve = vi.fn();
      disconnect = vi.fn();
    }
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const working = classificationResponseForTree(tree, {
      status: "WORKING",
      type: null,
    });
    vi.mocked(api.entryClassifications).mockResolvedValue(working);
    vi.spyOn(api, "updateEntryClassifications").mockRejectedValue(
      new Error("分类快照已变化，请重新读取"),
    );
    const user = userEvent.setup();
    const value = navigationWorkspaceValue(tree);
    value.route = { ...value.route, checkpoint: "list" };
    render(
      <WorkspaceProvider value={value}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "批量分类" }));
    await user.click(screen.getByRole("button", { name: "应用到 1 项" }));

    const dialog = screen.getByRole("dialog", { name: "批量分类" });
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("分类快照已变化，请重新读取");
    await waitFor(() => expect(alert).toHaveFocus());
    await user.click(
      within(dialog).getByRole("button", { name: "重新读取分类" }),
    );
    await waitFor(() =>
      expect(api.entryClassifications).toHaveBeenCalledTimes(2),
    );
  });

  it("offers a clear recovery when a classification filter has no matches", async () => {
    class TestResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe = (element: Element) =>
        this.callback(
          [
            {
              target: element,
              contentRect: { width: 340, height: 560 },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      unobserve = vi.fn();
      disconnect = vi.fn();
    }
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const suggested = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
    });
    suggested.snapshot.entries = suggested.snapshot.entries.map((entry) => ({
      ...entry,
      suggested_type: "TECHNICAL",
      suggestion_source: "MODEL",
      human_override: null,
    }));
    suggested.counts.human_override = 0;
    vi.mocked(api.entryClassifications).mockResolvedValue(suggested);
    const value = navigationWorkspaceValue(tree);
    value.route = { ...value.route, checkpoint: "list" };
    const user = userEvent.setup();
    render(
      <WorkspaceProvider value={value}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "人工修改筛选" }),
    );
    expect(screen.getByText("当前筛选没有匹配内容")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "清除筛选" }));
    expect(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    ).toBeInTheDocument();
  });

  it("keeps a human override and its ancestors visible in the manual filter", async () => {
    class TestResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe = (element: Element) =>
        this.callback(
          [
            {
              target: element,
              contentRect: { width: 340, height: 560 },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      unobserve = vi.fn();
      disconnect = vi.fn();
    }
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    vi.mocked(api.entryClassifications).mockResolvedValue(
      classificationResponseForTree(tree, {
        status: "WORKING",
        type: "TECHNICAL",
      }),
    );
    const value = navigationWorkspaceValue(tree);
    value.route = { ...value.route, checkpoint: "list" };
    const user = userEvent.setup();
    render(
      <WorkspaceProvider value={value}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "人工修改筛选" }),
    );
    expect(
      screen.getByRole("treeitem", { name: /技术规范/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("treeitem", { name: /2 技术要求/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("treeitem", {
        name: /持久化叶节点.*技术需求.*人工/,
      }),
    ).toBeInTheDocument();
  });

  it("keeps an acceptance failure focused and reloadable inside the publication dialog", async () => {
    class TestResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe = (element: Element) =>
        this.callback(
          [
            {
              target: element,
              contentRect: { width: 340, height: 560 },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      unobserve = vi.fn();
      disconnect = vi.fn();
    }
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    vi.mocked(api.entryClassifications).mockResolvedValue(
      classificationResponseForTree(tree, {
        status: "WORKING",
        type: "TECHNICAL",
      }),
    );
    vi.spyOn(api, "acceptEntryClassifications").mockRejectedValue(
      new Error("接受身份已过期，请重新读取"),
    );
    const value = navigationWorkspaceValue(tree);
    value.route = { ...value.route, checkpoint: "list" };
    const user = userEvent.setup();
    render(
      <WorkspaceProvider value={value}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "生成确认清单" }),
    );
    const dialog = screen.getByRole("dialog", { name: "生成确认清单" });
    await user.click(
      within(dialog).getByRole("button", {
        name: "接受分类并生成清单",
      }),
    );

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("接受身份已过期，请重新读取");
    await waitFor(() => expect(alert).toHaveFocus());
    await user.click(
      within(dialog).getByRole("button", { name: "重新读取分类" }),
    );
    await waitFor(() =>
      expect(api.entryClassifications).toHaveBeenCalledTimes(2),
    );
  });

  it("accepts a complete classification before publish and activates only the bound DRAFT", async () => {
    class TestResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe = (element: Element) =>
        this.callback(
          [
            {
              target: element,
              contentRect: { width: 340, height: 560 },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      unobserve = vi.fn();
      disconnect = vi.fn();
    }
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const working = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
      snapshotId: "working-complete",
      payloadSha256: "3".repeat(64),
    });
    const accepted = classificationResponseForTree(tree, {
      status: "ACCEPTED",
      type: "TECHNICAL",
      snapshotId: "accepted-ui",
      payloadSha256: "4".repeat(64),
    });
    vi.mocked(api.entryClassifications).mockResolvedValue(working);
    const order: string[] = [];
    vi.spyOn(api, "acceptEntryClassifications").mockImplementation(async () => {
      order.push("accept");
      return accepted;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        order.push("publish");
        return new Response(
          JSON.stringify({
            requirement_set_created: true,
            publication: {
              requirement_set_id: "draft-1",
              version: 1,
              status: "DRAFT",
              publication_state: "READY",
              requirement_tree_persisted: true,
              entry_classification_snapshot_id: "accepted-ui",
              entry_classification_snapshot_sha256: "4".repeat(64),
            },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }),
    );
    vi.spyOn(api, "activateRequirementSet").mockImplementation(async () => {
      order.push("activate");
      return {};
    });
    const user = userEvent.setup();
    const value = navigationWorkspaceValue(tree);
    value.route = { ...value.route, checkpoint: "list" };
    render(
      <WorkspaceProvider value={value}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "生成确认清单" }),
    );
    await user.click(
      screen.getByRole("button", { name: "接受分类并生成清单" }),
    );

    await waitFor(() =>
      expect(order).toEqual(["accept", "publish", "activate"]),
    );
    expect(value.updateRoute).toHaveBeenCalledWith({
      task: "extraction",
      mode: "review",
      checkpoint: "review",
      requirementSetId: "draft-1",
    });
  });

  it("keeps an accepted non-semantic result retryable when draft publication fails", async () => {
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const working = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
      snapshotId: "working-before-publication-failure",
      payloadSha256: "5".repeat(64),
    });
    const accepted = classificationResponseForTree(tree, {
      status: "ACCEPTED",
      type: "TECHNICAL",
      snapshotId: "accepted-before-publication-failure",
      payloadSha256: "6".repeat(64),
    });
    vi.mocked(api.entryClassifications).mockResolvedValue(working);
    const accept = vi
      .spyOn(api, "acceptEntryClassifications")
      .mockResolvedValue(accepted);
    let publicationAttempts = 0;
    const publish = vi.fn(async () => {
      publicationAttempts += 1;
      if (publicationAttempts === 1) {
        return new Response(
          JSON.stringify({
            detail: {
              code: "entry_classification_acceptance_required",
              message: "请先完成清单分类并接受当前结果，再生成确认清单。",
            },
          }),
          { status: 409, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          requirement_set_created: true,
          publication: {
            requirement_set_id: "draft-after-retry",
            version: 2,
            status: "DRAFT",
            publication_state: "READY",
            requirement_tree_persisted: true,
            entry_classification_snapshot_id:
              "accepted-before-publication-failure",
            entry_classification_snapshot_sha256: "6".repeat(64),
          },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", publish);
    vi.spyOn(api, "activateRequirementSet").mockResolvedValue({});
    const value = navigationWorkspaceValue(tree);
    value.route = { ...value.route, checkpoint: "list" };
    value.runAction = async (_label, action) => {
      try {
        return await action();
      } catch {
        return null;
      }
    };
    const user = userEvent.setup();
    render(
      <WorkspaceProvider value={value}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "生成确认清单" }),
    );
    const dialog = screen.getByRole("dialog", { name: "生成确认清单" });
    await user.click(
      within(dialog).getByRole("button", {
        name: "接受分类并生成清单",
      }),
    );

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("生成确认清单未完成");
    expect(alert).not.toHaveTextContent("接受分类未完成");
    expect(alert).not.toHaveTextContent(
      "请先完成清单分类并接受当前结果，再生成确认清单。",
    );
    expect(alert).toHaveTextContent(
      "当前分类结果已接受，可直接重试生成确认清单，无需重新分类。",
    );
    expect(accept).toHaveBeenCalledOnce();

    await user.click(
      within(dialog).getByRole("button", {
        name: "按已接受结果重试生成",
      }),
    );

    await waitFor(() => expect(publish).toHaveBeenCalledTimes(2));
    expect(accept).toHaveBeenCalledOnce();
    expect(api.activateRequirementSet).toHaveBeenCalledOnce();
    expect(value.updateRoute).toHaveBeenCalledWith({
      task: "extraction",
      mode: "review",
      checkpoint: "review",
      requirementSetId: "draft-after-retry",
    });
  });

  it("retries activation without republishing an already READY non-semantic draft", async () => {
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const working = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
      snapshotId: "working-before-activation-conflict",
      payloadSha256: "7".repeat(64),
    });
    const accepted = classificationResponseForTree(tree, {
      status: "ACCEPTED",
      type: "TECHNICAL",
      snapshotId: "accepted-before-activation-conflict",
      payloadSha256: "8".repeat(64),
    });
    vi.mocked(api.entryClassifications).mockResolvedValue(working);
    const accept = vi
      .spyOn(api, "acceptEntryClassifications")
      .mockResolvedValue(accepted);
    const publish = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            requirement_set_created: true,
            publication: {
              requirement_set_id: "ready-draft-awaiting-activation",
              version: 2,
              status: "DRAFT",
              publication_state: "READY",
              requirement_tree_persisted: true,
              entry_classification_snapshot_id:
                "accepted-before-activation-conflict",
              entry_classification_snapshot_sha256: "8".repeat(64),
            },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
    );
    vi.stubGlobal("fetch", publish);
    const activate = vi
      .spyOn(api, "activateRequirementSet")
      .mockRejectedValueOnce(
        new ApiError("当前版本暂时无法设为当前版本", 409, {
          detail: { code: "requirement_set_activation_conflict" },
        }),
      )
      .mockResolvedValueOnce({});
    const value = navigationWorkspaceValue(tree);
    value.route = { ...value.route, checkpoint: "list" };
    value.runAction = async (_label, action) => {
      try {
        return await action();
      } catch {
        return null;
      }
    };
    const user = userEvent.setup();
    render(
      <WorkspaceProvider value={value}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "生成确认清单" }),
    );
    const dialog = screen.getByRole("dialog", { name: "生成确认清单" });
    await user.click(
      within(dialog).getByRole("button", {
        name: "接受分类并生成清单",
      }),
    );

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("生成确认清单未完成");
    expect(alert).toHaveTextContent(
      "确认清单已生成，但尚未设为当前版本。",
    );
    expect(alert).toHaveTextContent(
      "重试只会再次设为当前版本，不会重复生成确认清单。",
    );

    await user.click(
      within(dialog).getByRole("button", {
        name: "重试设为当前版本",
      }),
    );

    await waitFor(() => expect(activate).toHaveBeenCalledTimes(2));
    expect(publish).toHaveBeenCalledOnce();
    expect(accept).toHaveBeenCalledOnce();
    expect(value.updateRoute).toHaveBeenCalledWith({
      task: "extraction",
      mode: "review",
      checkpoint: "review",
      requirementSetId: "ready-draft-awaiting-activation",
    });
  });

  it("keeps a matching PUBLISHING draft inactive and asks the user to retry", async () => {
    class TestResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe = (element: Element) =>
        this.callback(
          [
            {
              target: element,
              contentRect: { width: 340, height: 560 },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      unobserve = vi.fn();
      disconnect = vi.fn();
    }
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const working = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
      snapshotId: "working-publishing",
      payloadSha256: "8".repeat(64),
    });
    const accepted = classificationResponseForTree(tree, {
      status: "ACCEPTED",
      type: "TECHNICAL",
      snapshotId: "accepted-publishing",
      payloadSha256: "9".repeat(64),
    });
    vi.mocked(api.entryClassifications).mockResolvedValue(working);
    vi.spyOn(api, "acceptEntryClassifications").mockResolvedValue(accepted);
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              requirement_set_created: false,
              publication: {
                requirement_set_id: "draft-still-publishing",
                version: 1,
                status: "DRAFT",
                publication_state: "PUBLISHING",
                requirement_tree_persisted: false,
                entry_classification_snapshot_id: "accepted-publishing",
                entry_classification_snapshot_sha256: "9".repeat(64),
              },
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
      ),
    );
    const activate = vi.spyOn(api, "activateRequirementSet");
    const value = navigationWorkspaceValue(tree);
    value.route = { ...value.route, checkpoint: "list" };
    value.runAction = async (_label, action) => {
      try {
        return await action();
      } catch {
        return null;
      }
    };
    const user = userEvent.setup();
    render(
      <WorkspaceProvider value={value}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "生成确认清单" }),
    );
    await user.click(
      screen.getByRole("button", { name: "接受分类并生成清单" }),
    );

    expect(
      await screen.findAllByText(/版本仍在生成中，请稍后重试/),
    ).not.toHaveLength(0);
    expect(activate).not.toHaveBeenCalled();
    expect(value.updateRoute).not.toHaveBeenCalledWith(
      expect.objectContaining({ checkpoint: "review" }),
    );
  });

  it("does not activate or navigate when publication returns a different classification receipt", async () => {
    class TestResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe = (element: Element) =>
        this.callback(
          [
            {
              target: element,
              contentRect: { width: 340, height: 560 },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      unobserve = vi.fn();
      disconnect = vi.fn();
    }
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const working = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
      snapshotId: "working-mismatch",
      payloadSha256: "5".repeat(64),
    });
    const accepted = classificationResponseForTree(tree, {
      status: "ACCEPTED",
      type: "TECHNICAL",
      snapshotId: "accepted-mismatch",
      payloadSha256: "6".repeat(64),
    });
    vi.mocked(api.entryClassifications).mockResolvedValue(working);
    vi.spyOn(api, "acceptEntryClassifications").mockResolvedValue(accepted);
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              requirement_set_created: true,
              publication: {
                requirement_set_id: "unsafe-draft",
                entry_classification_snapshot_id: "different-receipt",
                entry_classification_snapshot_sha256: "7".repeat(64),
              },
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
      ),
    );
    const activate = vi.spyOn(api, "activateRequirementSet");
    const value = navigationWorkspaceValue(tree);
    value.route = { ...value.route, checkpoint: "list" };
    value.runAction = async (_label, action) => {
      try {
        return await action();
      } catch {
        return null;
      }
    };
    const user = userEvent.setup();
    render(
      <WorkspaceProvider value={value}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "生成确认清单" }),
    );
    await user.click(
      screen.getByRole("button", { name: "接受分类并生成清单" }),
    );

    await waitFor(() => expect(activate).not.toHaveBeenCalled());
    expect(value.updateRoute).not.toHaveBeenCalledWith(
      expect.objectContaining({ checkpoint: "review" }),
    );
  });

  it("moves from structure to list as URL-only navigation before publication is offered", async () => {
    class TestResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}

      observe = (element: Element) => {
        this.callback(
          [
            {
              target: element,
              contentRect: {
                width: 340,
                height: 560,
                x: 0,
                y: 0,
                top: 0,
                right: 340,
                bottom: 560,
                left: 0,
                toJSON: () => ({}),
              },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      };

      unobserve = vi.fn();
      disconnect = vi.fn();
    }

    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const user = userEvent.setup();
    const structureWorkspace = navigationWorkspaceValue();
    structureWorkspace.route = {
      ...structureWorkspace.route,
      checkpoint: "structure",
    };
    structureWorkspace.runAction = vi.fn();

    const view = render(
      <WorkspaceProvider value={structureWorkspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("button", { name: "进入原文需求清单" }),
    ).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "生成确认清单" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "进入原文需求清单" }));
    expect(structureWorkspace.updateRoute).toHaveBeenCalledWith({
      task: "extraction",
      mode: "organize",
      checkpoint: "list",
      requirementSetId: "",
    });
    expect(structureWorkspace.runAction).not.toHaveBeenCalled();

    view.unmount();
    const listWorkspace = navigationWorkspaceValue();
    listWorkspace.route = {
      ...listWorkspace.route,
      checkpoint: "list",
    };
    render(
      <WorkspaceProvider value={listWorkspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("button", { name: "生成确认清单" }),
    ).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "进入原文需求清单" }),
    ).not.toBeInTheDocument();
  });

  it("closes structure-only calibration and mutation controls outside the structure checkpoint", async () => {
    class TestResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}

      observe = (element: Element) => {
        this.callback(
          [
            {
              target: element,
              contentRect: {
                width: 340,
                height: 560,
                x: 0,
                y: 0,
                top: 0,
                right: 340,
                bottom: 560,
                left: 0,
                toJSON: () => ({}),
              },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      };

      unobserve = vi.fn();
      disconnect = vi.fn();
    }

    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "boundaryCalibrationPage").mockResolvedValue({});
    const user = userEvent.setup();
    const structureWorkspace = navigationWorkspaceValue();
    structureWorkspace.route = {
      ...structureWorkspace.route,
      checkpoint: "structure",
    };
    const structureWorkbench = structureWorkspace.structure
      ?.structure_workbench as JsonRecord;
    structureWorkbench.capabilities = {
      ...((structureWorkbench.capabilities as JsonRecord | undefined) || {}),
      can_calibrate_structure_boundaries: true,
    };
    setStructureArtifactEditability(structureWorkspace, { editable: true });
    const view = render(
      <WorkspaceProvider value={structureWorkspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByRole("tab", { name: "版面校准" }));
    expect(screen.getByRole("tab", { name: "版面校准" })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    const listWorkspace = {
      ...structureWorkspace,
      route: { ...structureWorkspace.route, checkpoint: "list" as const },
    };
    view.rerender(
      <WorkspaceProvider value={listWorkspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("button", { name: "生成确认清单" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("tab", { name: "版面校准" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "调整结构" }),
    ).not.toBeInTheDocument();
  });

  it("fails closed when the semantic review publisher cannot bind the accepted receipt", async () => {
    class TestResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}

      observe = (element: Element) => {
        this.callback(
          [
            {
              target: element,
              contentRect: {
                width: 340,
                height: 560,
                x: 0,
                y: 0,
                top: 0,
                right: 340,
                bottom: 560,
                left: 0,
                toJSON: () => ({}),
              },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      };

      unobserve = vi.fn();
      disconnect = vi.fn();
    }

    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const user = userEvent.setup();
    const value = navigationWorkspaceValue();
    value.route = { ...value.route, checkpoint: "list" };
    const currentWorkbench = value.structure?.structure_workbench as JsonRecord;
    value.structure = {
      structure_workbench: {
        ...currentWorkbench,
        workflow: {
          review: { session_id: "review-session", revision: 7 },
        },
        result: {},
        readiness: { can_atomize: true },
      },
    };
    value.runAction = vi.fn();

    render(
      <WorkspaceProvider value={value}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    const publishButton = await screen.findByRole("button", {
      name: "正在同步已接受的分类结果",
    });

    expect(publishButton).toBeDisabled();
    expect(value.runAction).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(
        screen.getByText(/已接受的分类结果尚未同步完成/),
      ).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("dialog", { name: /生成确认清单/ }),
    ).not.toBeInTheDocument();
  });

  it("restores expansion, selection and scroll after organize unmounts for review", async () => {
    class TestResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}

      observe = (element: Element) => {
        this.callback(
          [
            {
              target: element,
              contentRect: {
                width: 340,
                height: 560,
                x: 0,
                y: 0,
                top: 0,
                right: 340,
                bottom: 560,
                left: 0,
                toJSON: () => ({}),
              },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      };

      unobserve = vi.fn();
      disconnect = vi.fn();
    }

    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const user = userEvent.setup();
    const workspace = navigationWorkspaceValue();

    const first = render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await screen.findByRole("tree", { name: "需求结构树" });
    await user.click(screen.getByRole("button", { name: "全部收起" }));
    await user.click(screen.getByText("展开", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "全部展开" }));
    const leaf = await screen.findByRole("treeitem", {
      name: /2\.3\.1 持久化叶节点/,
    });
    await user.click(leaf);
    const firstViewport = screen.getByRole("tree", { name: "需求结构树" });
    Object.defineProperty(firstViewport, "scrollTop", {
      configurable: true,
      value: 96,
      writable: true,
    });
    await act(async () => {
      fireEvent.scroll(firstViewport);
      await new Promise((resolve) => window.setTimeout(resolve, 20));
    });
    await waitFor(() => expect(leaf).toHaveAttribute("aria-selected", "true"));
    first.unmount();

    render(
      <WorkspaceProvider value={navigationWorkspaceValue()}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    const restoredLeaf = await screen.findByRole("treeitem", {
      name: /2\.3\.1 持久化叶节点/,
    });
    expect(restoredLeaf).toHaveAttribute("aria-selected", "true");
    expect(
      screen.getByRole("treeitem", { name: /2 技术要求/ }),
    ).toHaveAttribute("aria-expanded", "true");
    await waitFor(() =>
      expect(screen.getByRole("tree", { name: "需求结构树" }).scrollTop).toBe(
        96,
      ),
    );
  });

  it("clears active filters and reveals a distant TOC target in the virtual tree", async () => {
    class TocResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}

      observe = (element: Element) => {
        this.callback(
          [
            {
              target: element,
              contentRect: {
                width: 340,
                height: 240,
                x: 0,
                y: 0,
                top: 0,
                right: 340,
                bottom: 240,
                left: 0,
                toJSON: () => ({}),
              },
            } as ResizeObserverEntry,
          ],
          this as unknown as ResizeObserver,
        );
      };

      unobserve = vi.fn();
      disconnect = vi.fn();
    }

    vi.stubGlobal("ResizeObserver", TocResizeObserver);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const toc = navigationNode("toc-navigation", {
      parent_id: "toc-root",
      depth: 1,
      kind: "GROUP",
      label: "文档目录",
      classification: "TOC",
      attention_count: 1,
      source_refs: [{ source_kind: "TOC_ENTRY", source_id: "toc-source" }],
      toc_entries: [
        {
          entry_id: "toc-target-entry",
          number: "99",
          title: "99 后部章节",
          printed_page_label: "88",
          target_page: 1,
          source_order: 1,
          source_refs: [{ source_id: "toc-source" }],
          target_structure_node_id: "toc-target",
        },
      ],
    });
    const fillers = Array.from({ length: 36 }, (_, index) =>
      navigationNode(`toc-filler-${index}`, {
        parent_id: "toc-root",
        depth: 1,
        source_order: index + 2,
        label: `中间章节 ${index + 1}`,
      }),
    );
    const targetParent = navigationNode("toc-target-parent", {
      parent_id: "toc-root",
      depth: 1,
      source_order: 100,
      kind: "GROUP",
      label: "后部章节组",
      child_ids: ["toc-target"],
    });
    const target = navigationNode("toc-target", {
      parent_id: targetParent.id,
      depth: 2,
      source_order: 101,
      kind: "CLAUSE",
      label: "99 后部章节",
    });
    const root = navigationNode("toc-root", {
      kind: "DOCUMENT",
      label: "TOC 导航文档",
      child_ids: [toc.id, ...fillers.map((node) => node.id), targetParent.id],
    });
    const tocTree: RequirementTreeSnapshot = {
      ...navigationTree(),
      root_node_ids: [root.id],
      nodes: [root, toc, ...fillers, targetParent, target],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={navigationWorkspaceValue(tocTree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    const treeViewport = await screen.findByRole("tree", {
      name: "需求结构树",
    });
    const scrollTo = vi.fn();
    Object.defineProperty(treeViewport, "scrollTo", {
      configurable: true,
      value: scrollTo,
    });
    const search = screen.getByPlaceholderText("搜索章节、条款、表格或候选");
    await user.type(search, "目录");
    await user.click(await screen.findByRole("treeitem", { name: /文档目录/ }));
    await user.click(screen.getByRole("button", { name: "需关注" }));
    await user.click(screen.getByRole("button", { name: "99 后部章节" }));

    expect(search).toHaveValue("");
    expect(screen.getByRole("button", { name: "全部" })).toHaveClass(
      "is-active",
    );
    const targetRow = await screen.findByRole("treeitem", {
      name: /99 后部章节/,
    });
    await waitFor(() =>
      expect(targetRow).toHaveAttribute("aria-selected", "true"),
    );
    await waitFor(() => expect(targetRow).toHaveFocus());
    expect(
      screen.getByRole("treeitem", { name: /后部章节组/ }),
    ).toHaveAttribute("aria-expanded", "true");
    await waitFor(() => expect(scrollTo).toHaveBeenCalled());
    const lastScroll = scrollTo.mock.calls.at(-1)?.[0] as
      { top?: number } | undefined;
    expect(lastScroll?.top).toBeGreaterThan(0);
  });
});

describe("RequirementTreePanel semantic publication v2", () => {
  it("accepts first, publishes NOT_RUN without scope fields, and never calls atomization or analysis", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const working = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
      snapshotId: "semantic-working-not-run",
      payloadSha256: "a".repeat(64),
    });
    const accepted = classificationResponseForTree(tree, {
      status: "ACCEPTED",
      type: "TECHNICAL",
      snapshotId: "semantic-accepted-not-run",
      payloadSha256: "b".repeat(64),
    });
    let currentClassification = working;
    const enrichmentForCurrent = () => {
      const state = atomizationEnrichmentState(currentClassification);
      state.semantic_publication_capability = semanticPublicationCapability();
      return state;
    };
    vi.mocked(api.entryClassifications).mockResolvedValue(working);
    const order: string[] = [];
    vi.spyOn(api, "acceptEntryClassifications").mockImplementation(async () => {
      order.push("accept");
      currentClassification = accepted;
      return accepted;
    });
    vi.mocked(api.requirementEnrichment).mockImplementation(async () =>
      enrichmentForCurrent(),
    );
    let previewRequest: RequirementSemanticPublicationPreviewRequestV2 | null =
      null;
    let previewResponse: RequirementSemanticPublicationPreviewResponseV2 | null =
      null;
    let publishedResponse: RequirementSemanticPublishDraftResponseV2 | null =
      null;
    vi.spyOn(api, "previewRequirementSemanticPublication").mockImplementation(
      async (_projectId, _documentId, _sessionId, request) => {
        order.push("preview");
        previewRequest = request;
        previewResponse = semanticPublicationPreviewResponse(accepted, request);
        return previewResponse;
      },
    );
    vi.spyOn(api, "publishRequirementSemanticDraft").mockImplementation(
      async (_projectId, _documentId, _sessionId, request) => {
        order.push("publish");
        expect(request).toEqual({
          schema_version: "requirement-enrichment-publish-draft-request/v2",
          expected_review_revision: 7,
          binding_preview_id:
            previewResponse?.binding_preview.binding_preview_id,
          binding_preview_sha256:
            previewResponse?.binding_preview.payload_sha256,
        });
        publishedResponse = semanticPublishDraftResponse(
          tree,
          accepted,
          previewResponse as RequirementSemanticPublicationPreviewResponseV2,
        );
        return publishedResponse;
      },
    );
    vi.spyOn(api, "activateRequirementSet").mockImplementation(
      async (...args) => {
        order.push("activate");
        expect(args[3]).toEqual({
          schema_version: "requirement-set-semantic-activation-request/v1",
          expected_active_requirement_set_id: "persist-set",
          expected_enrichment_publication_binding_id:
            publishedResponse?.binding.binding_id,
          expected_enrichment_publication_binding_sha256:
            publishedResponse?.binding.sha256,
        });
        expect(args[4]).toBeInstanceOf(AbortSignal);
        return semanticActivationResponse(
          publishedResponse as RequirementSemanticPublishDraftResponseV2,
        );
      },
    );
    const user = userEvent.setup();
    const workspace = semanticListWorkspace(tree);
    const runAction = vi.fn(
      async (_label: string, action: () => Promise<unknown>) => action(),
    );
    workspace.runAction = runAction as WorkspaceContextValue["runAction"];
    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    const generate = await screen.findByRole("button", {
      name: "生成确认清单",
    });
    await waitFor(() => expect(generate).toBeEnabled());
    await user.click(generate);
    const dialog = screen.getByRole("dialog", { name: "生成确认清单" });
    expect(dialog).toHaveTextContent("保留当前原条目");
    expect(dialog).not.toHaveTextContent("NOT_RUN");
    expect(dialog).toHaveTextContent("技术需求默认进入分析");
    expect(dialog).toHaveTextContent("本动作不调用原子化");
    expect(dialog).toHaveTextContent("不会启动需求分析");
    expect(dialog).not.toHaveTextContent("不改变当前分析资格");
    await user.click(
      within(dialog).getByRole("button", {
        name: "接受分类并生成清单",
      }),
    );

    await waitFor(() =>
      expect(order).toEqual(["accept", "preview", "publish", "activate"]),
    );
    expect(previewRequest).toEqual({
      schema_version: "requirement-enrichment-publication-preview-request/v2",
      expected_review_revision: 7,
      accepted_entry_classification_snapshot_id: "semantic-accepted-not-run",
      accepted_entry_classification_revision: accepted.snapshot.revision,
      accepted_entry_classification_sha256: "b".repeat(64),
      atomization_state: "NOT_RUN",
    });
    expect(previewRequest).not.toHaveProperty("atomization_scope_receipt_id");
    expect(api.runRequirementAtomizationScope).not.toHaveBeenCalled();
    expect(runAction).toHaveBeenCalledWith(
      "生成确认清单",
      expect.any(Function),
      { refresh: "none" },
    );
    expect(workspace.updateRoute).toHaveBeenCalledWith(
      expect.objectContaining({
        checkpoint: "review",
        requirementSetId: "semantic-draft",
      }),
    );
  });

  it("publishes APPLIED with the exact current receipt identity", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const accepted = classificationResponseForTree(tree, {
      status: "ACCEPTED",
      type: "TECHNICAL",
      snapshotId: "semantic-accepted-applied",
      payloadSha256: "c".repeat(64),
    });
    const receipt = atomizationScopeReceipt({
      receipt_id: "atomization-scope-receipt-" + "4".repeat(32),
      payload_sha256: "d".repeat(64),
      scope_revision: 6,
      cumulative_review_node_ids: ["persist-leaf"],
    });
    const enrichment = atomizationEnrichmentState(accepted, {
      current_scope_revision: 6,
      classification_binding_state: "CONTENT_EQUIVALENT",
      current_receipt: receipt,
    });
    enrichment.semantic_publication_capability =
      semanticPublicationCapability();
    vi.mocked(api.entryClassifications).mockResolvedValue(accepted);
    vi.mocked(api.requirementEnrichment).mockResolvedValue(enrichment);
    let previewRequest: RequirementSemanticPublicationPreviewRequestV2 | null =
      null;
    let previewResponse: RequirementSemanticPublicationPreviewResponseV2 | null =
      null;
    let publishedResponse: RequirementSemanticPublishDraftResponseV2 | null =
      null;
    vi.spyOn(api, "previewRequirementSemanticPublication").mockImplementation(
      async (_projectId, _documentId, _sessionId, request) => {
        previewRequest = request;
        previewResponse = semanticPublicationPreviewResponse(accepted, request);
        return previewResponse;
      },
    );
    vi.spyOn(api, "publishRequirementSemanticDraft").mockImplementation(
      async () => {
        publishedResponse = semanticPublishDraftResponse(
          tree,
          accepted,
          previewResponse as RequirementSemanticPublicationPreviewResponseV2,
        );
        return publishedResponse;
      },
    );
    vi.spyOn(api, "activateRequirementSet").mockImplementation(async () =>
      semanticActivationResponse(
        publishedResponse as RequirementSemanticPublishDraftResponseV2,
      ),
    );
    const user = userEvent.setup();
    render(
      <WorkspaceProvider value={semanticListWorkspace(tree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    const generate = await screen.findByRole("button", {
      name: "生成确认清单",
    });
    await waitFor(() => expect(generate).toBeEnabled());
    await user.click(generate);
    const dialog = screen.getByRole("dialog", { name: "生成确认清单" });
    expect(dialog).toHaveTextContent("采用已应用的原子化范围");
    expect(dialog).not.toHaveTextContent("APPLIED");
    await user.click(
      within(dialog).getByRole("button", {
        name: "继续生成确认清单",
      }),
    );

    await waitFor(() =>
      expect(api.activateRequirementSet).toHaveBeenCalledOnce(),
    );
    expect(previewRequest).toEqual(
      expect.objectContaining({
        atomization_state: "APPLIED",
        atomization_scope_receipt_id:
          "atomization-scope-receipt-" + "4".repeat(32),
        atomization_scope_receipt_sha256: "d".repeat(64),
        atomization_scope_revision: 6,
      }),
    );
    expect(api.runRequirementAtomizationScope).not.toHaveBeenCalled();
  });

  it("blocks a stale non-empty scope with an executable explanation", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const accepted = classificationResponseForTree(tree, {
      status: "ACCEPTED",
      type: "TECHNICAL",
    });
    const enrichment = atomizationEnrichmentState(accepted, {
      classification_binding_state: "STALE",
      current_receipt: atomizationScopeReceipt({
        cumulative_review_node_ids: ["persist-leaf"],
      }),
    });
    enrichment.semantic_publication_capability =
      semanticPublicationCapability();
    vi.mocked(api.entryClassifications).mockResolvedValue(accepted);
    vi.mocked(api.requirementEnrichment).mockResolvedValue(enrichment);
    const accept = vi.spyOn(api, "acceptEntryClassifications");
    const preview = vi.spyOn(api, "previewRequirementSemanticPublication");
    const user = userEvent.setup();
    render(
      <WorkspaceProvider value={semanticListWorkspace(tree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    const generate = await screen.findByRole("button", {
      name: "生成确认清单",
    });
    await waitFor(() => expect(generate).toBeEnabled());
    await user.click(generate);
    const dialog = screen.getByRole("dialog", { name: "生成确认清单" });
    expect(dialog).toHaveTextContent("重新执行或撤销原子化");
    expect(
      within(dialog).getByRole("button", {
        name: "继续生成确认清单",
      }),
    ).toBeDisabled();
    expect(accept).not.toHaveBeenCalled();
    expect(preview).not.toHaveBeenCalled();
  });

  it("keeps the dialog and independent selection when preview identity validation fails", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const working = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
    });
    const accepted = classificationResponseForTree(tree, {
      status: "ACCEPTED",
      type: "TECHNICAL",
      snapshotId: "semantic-preview-accepted",
      payloadSha256: "e".repeat(64),
    });
    let current = working;
    const enrichmentForCurrent = () => {
      const state = atomizationEnrichmentState(current);
      state.semantic_publication_capability = semanticPublicationCapability();
      return state;
    };
    vi.mocked(api.entryClassifications).mockResolvedValue(working);
    vi.spyOn(api, "acceptEntryClassifications").mockImplementation(async () => {
      current = accepted;
      return accepted;
    });
    vi.mocked(api.requirementEnrichment).mockImplementation(async () =>
      enrichmentForCurrent(),
    );
    vi.spyOn(api, "previewRequirementSemanticPublication").mockImplementation(
      async (_projectId, _documentId, _sessionId, request) =>
        semanticPublicationPreviewResponse(accepted, request, {
          classification: {
            snapshot_id: "wrong-classification",
            revision: accepted.snapshot.revision,
            sha256: accepted.snapshot.payload_sha256,
          },
        }),
    );
    const publish = vi.spyOn(api, "publishRequirementSemanticDraft");
    const activate = vi.spyOn(api, "activateRequirementSet");
    const user = userEvent.setup();
    render(
      <WorkspaceProvider value={semanticListWorkspace(tree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    await resetNavigationViewportToTop();
    await user.click(screen.getByText("展开", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "全部展开" }));
    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );
    const selected = await screen.findByRole("checkbox", {
      name: "加入“所选项”原子化范围",
    });
    await user.click(selected);
    await user.click(
      await screen.findByRole("button", { name: "生成确认清单" }),
    );
    const dialog = screen.getByRole("dialog", { name: "生成确认清单" });
    await user.click(
      within(dialog).getByRole("button", {
        name: "接受分类并生成清单",
      }),
    );

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("并非基于刚接受的分类结果");
    expect(dialog).toBeInTheDocument();
    expect(selected).toBeChecked();
    expect(publish).not.toHaveBeenCalled();
    expect(activate).not.toHaveBeenCalled();
  });

  it("does not activate when publish counts drift from the verified preview", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const accepted = classificationResponseForTree(tree, {
      status: "ACCEPTED",
      type: "TECHNICAL",
    });
    const enrichment = atomizationEnrichmentState(accepted);
    enrichment.semantic_publication_capability =
      semanticPublicationCapability();
    vi.mocked(api.entryClassifications).mockResolvedValue(accepted);
    vi.mocked(api.requirementEnrichment).mockResolvedValue(enrichment);
    let previewResponse: RequirementSemanticPublicationPreviewResponseV2 | null =
      null;
    vi.spyOn(api, "previewRequirementSemanticPublication").mockImplementation(
      async (_projectId, _documentId, _sessionId, request) => {
        previewResponse = semanticPublicationPreviewResponse(accepted, request);
        return previewResponse;
      },
    );
    vi.spyOn(api, "publishRequirementSemanticDraft").mockImplementation(
      async () => {
        const response = semanticPublishDraftResponse(
          tree,
          accepted,
          previewResponse as RequirementSemanticPublicationPreviewResponseV2,
        );
        response.counts = { ...response.counts, total: 2 };
        return response;
      },
    );
    const activate = vi.spyOn(api, "activateRequirementSet");
    const user = userEvent.setup();
    render(
      <WorkspaceProvider value={semanticListWorkspace(tree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    const generate = await screen.findByRole("button", {
      name: "生成确认清单",
    });
    await waitFor(() => expect(generate).toBeEnabled());
    await user.click(generate);
    const dialog = screen.getByRole("dialog", { name: "生成确认清单" });
    await user.click(
      within(dialog).getByRole("button", {
        name: "继续生成确认清单",
      }),
    );

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "计数与已验证预览不一致",
    );
    expect(activate).not.toHaveBeenCalled();
  });

  it.each(["preview", "publish"] as const)(
    "aborts a late %s response after the document identity changes",
    async (lateStage) => {
      installNavigationResizeObserver();
      vi.spyOn(api, "sourcePage").mockResolvedValue({});
      const tree = navigationTree();
      const accepted = classificationResponseForTree(tree, {
        status: "ACCEPTED",
        type: "TECHNICAL",
      });
      let currentClassification = accepted;
      vi.mocked(api.entryClassifications).mockImplementation(
        async () => currentClassification,
      );
      vi.mocked(api.requirementEnrichment).mockImplementation(async () => {
        const enrichment = atomizationEnrichmentState(currentClassification);
        enrichment.semantic_publication_capability =
          semanticPublicationCapability();
        return enrichment;
      });
      let pendingSignal: AbortSignal | null = null;
      const pendingUntilAbort = <T,>(signal?: AbortSignal): Promise<T> =>
        new Promise<T>((_resolve, reject) => {
          if (!signal) {
            reject(new Error("semantic publication request has no signal"));
            return;
          }
          pendingSignal = signal;
          const rejectAbort = () =>
            reject(new DOMException("request superseded", "AbortError"));
          if (signal.aborted) rejectAbort();
          else signal.addEventListener("abort", rejectAbort, { once: true });
        });
      let previewResult: RequirementSemanticPublicationPreviewResponseV2 | null =
        null;
      vi.spyOn(api, "previewRequirementSemanticPublication").mockImplementation(
        async (_projectId, _documentId, _sessionId, request, signal) => {
          if (lateStage === "preview") {
            return pendingUntilAbort(signal);
          }
          previewResult = semanticPublicationPreviewResponse(accepted, request);
          return previewResult;
        },
      );
      const publish = vi
        .spyOn(api, "publishRequirementSemanticDraft")
        .mockImplementation(
          async (_projectId, _documentId, _sessionId, _request, signal) => {
            if (lateStage === "publish") {
              return pendingUntilAbort(signal);
            }
            return semanticPublishDraftResponse(
              tree,
              accepted,
              previewResult as RequirementSemanticPublicationPreviewResponseV2,
            );
          },
        );
      const activate = vi.spyOn(api, "activateRequirementSet");
      const user = userEvent.setup();
      const workspace = semanticListWorkspace(tree);
      const view = render(
        <WorkspaceProvider value={workspace}>
          <RequirementTreePanel />
        </WorkspaceProvider>,
      );
      await user.click(
        await screen.findByRole("button", { name: "生成确认清单" }),
      );
      await user.click(
        screen.getByRole("button", { name: "继续生成确认清单" }),
      );
      await waitFor(() => expect(pendingSignal).not.toBeNull());

      const nextTree: RequirementTreeSnapshot = {
        ...tree,
        source_identity: {
          ...tree.source_identity,
          document_id: "next-document",
        },
      };
      currentClassification = classificationResponseForTree(nextTree, {
        status: "ACCEPTED",
        type: "TECHNICAL",
      });
      const nextWorkspace = semanticListWorkspace(nextTree);
      nextWorkspace.route = {
        ...nextWorkspace.route,
        documentId: "next-document",
      };
      nextWorkspace.selectedDocument = {
        ...nextWorkspace.selectedDocument!,
        id: "next-document",
      };
      view.rerender(
        <WorkspaceProvider value={nextWorkspace}>
          <RequirementTreePanel />
        </WorkspaceProvider>,
      );

      await waitFor(() => expect(pendingSignal?.aborted).toBe(true));
      await waitFor(() =>
        expect(
          screen.queryByRole("dialog", { name: "生成确认清单" }),
        ).not.toBeInTheDocument(),
      );
      expect(activate).not.toHaveBeenCalled();
      expect(workspace.updateRoute).not.toHaveBeenCalledWith(
        expect.objectContaining({ checkpoint: "review" }),
      );
      expect(nextWorkspace.updateRoute).not.toHaveBeenCalledWith(
        expect.objectContaining({ checkpoint: "review" }),
      );
      expect(publish).toHaveBeenCalledTimes(lateStage === "publish" ? 1 : 0);
    },
  );

  it("keeps delayed ACCEPT inside the cancellable publication transaction", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const working = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
    });
    vi.mocked(api.entryClassifications).mockResolvedValue(working);
    const enrichment = atomizationEnrichmentState(working);
    enrichment.semantic_publication_capability =
      semanticPublicationCapability();
    vi.mocked(api.requirementEnrichment).mockResolvedValue(enrichment);
    let acceptSignal: AbortSignal | null = null;
    vi.spyOn(api, "acceptEntryClassifications").mockImplementation(
      (...args) =>
        new Promise<EntryClassificationResponse>((_resolve, reject) => {
          acceptSignal = (args[3] as AbortSignal | undefined) || null;
          if (!acceptSignal) {
            reject(new Error("classification acceptance has no signal"));
            return;
          }
          const rejectAbort = () =>
            reject(new DOMException("request superseded", "AbortError"));
          if (acceptSignal.aborted) rejectAbort();
          else
            acceptSignal.addEventListener("abort", rejectAbort, { once: true });
        }),
    );
    const preview = vi.spyOn(api, "previewRequirementSemanticPublication");
    const publish = vi.spyOn(api, "publishRequirementSemanticDraft");
    const activate = vi.spyOn(api, "activateRequirementSet");
    const user = userEvent.setup();
    const workspace = semanticListWorkspace(tree);
    const view = render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    await resetNavigationViewportToTop();
    await user.click(screen.getByText("展开", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "全部展开" }));
    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );
    const selected = await screen.findByRole("checkbox", {
      name: "加入“所选项”原子化范围",
    });
    await user.click(selected);
    await user.click(
      await screen.findByRole("button", { name: "生成确认清单" }),
    );
    const dialog = screen.getByRole("dialog", { name: "生成确认清单" });
    await user.click(
      within(dialog).getByRole("button", {
        name: "接受分类并生成清单",
      }),
    );

    await waitFor(() => expect(acceptSignal).toBeInstanceOf(AbortSignal));
    expect(
      within(dialog).getByRole("button", { name: "关闭生成确认清单" }),
    ).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "返回检查" }),
    ).toBeDisabled();
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(dialog).toBeInTheDocument();
    expect(selected).toBeChecked();

    const nextWorkspace = semanticListWorkspace(tree);
    nextWorkspace.route = {
      ...nextWorkspace.route,
      mode: "review",
    };
    view.rerender(
      <WorkspaceProvider value={nextWorkspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await waitFor(() => expect(acceptSignal?.aborted).toBe(true));
    expect(preview).not.toHaveBeenCalled();
    expect(publish).not.toHaveBeenCalled();
    expect(activate).not.toHaveBeenCalled();
    expect(workspace.updateRoute).not.toHaveBeenCalledWith(
      expect.objectContaining({ checkpoint: "review" }),
    );
  });

  it("aborts a late activation when the active base set changes", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const accepted = classificationResponseForTree(tree, {
      status: "ACCEPTED",
      type: "TECHNICAL",
    });
    const enrichment = atomizationEnrichmentState(accepted);
    enrichment.semantic_publication_capability =
      semanticPublicationCapability();
    vi.mocked(api.entryClassifications).mockResolvedValue(accepted);
    vi.mocked(api.requirementEnrichment).mockResolvedValue(enrichment);
    let previewResponse: RequirementSemanticPublicationPreviewResponseV2 | null =
      null;
    vi.spyOn(api, "previewRequirementSemanticPublication").mockImplementation(
      async (_projectId, _documentId, _sessionId, request) => {
        previewResponse = semanticPublicationPreviewResponse(accepted, request);
        return previewResponse;
      },
    );
    vi.spyOn(api, "publishRequirementSemanticDraft").mockImplementation(
      async () =>
        semanticPublishDraftResponse(
          tree,
          accepted,
          previewResponse as RequirementSemanticPublicationPreviewResponseV2,
        ),
    );
    let activationSignal: AbortSignal | null = null;
    vi.spyOn(api, "activateRequirementSet").mockImplementation(
      (...args) =>
        new Promise<JsonRecord>((_resolve, reject) => {
          activationSignal = (args[4] as AbortSignal | undefined) || null;
          if (!activationSignal) {
            reject(new Error("semantic activation has no signal"));
            return;
          }
          const rejectAbort = () =>
            reject(new DOMException("request superseded", "AbortError"));
          if (activationSignal.aborted) rejectAbort();
          else
            activationSignal.addEventListener("abort", rejectAbort, {
              once: true,
            });
        }),
    );
    const user = userEvent.setup();
    const workspace = semanticListWorkspace(tree);
    const view = render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    await user.click(
      await screen.findByRole("button", { name: "生成确认清单" }),
    );
    await user.click(screen.getByRole("button", { name: "继续生成确认清单" }));
    await waitFor(() => expect(activationSignal).toBeInstanceOf(AbortSignal));

    const changedWorkspace = semanticListWorkspace(tree);
    changedWorkspace.requirementSets = {
      active_requirement_set_id: "new-active-set",
      requirement_sets: [
        { id: "new-active-set", version_label: "版本 2", status: "DRAFT" },
      ],
    };
    view.rerender(
      <WorkspaceProvider value={changedWorkspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await waitFor(() => expect(activationSignal?.aborted).toBe(true));
    expect(workspace.setNotice).not.toHaveBeenCalled();
    expect(workspace.updateRoute).not.toHaveBeenCalledWith(
      expect.objectContaining({ checkpoint: "review" }),
    );
  });

  it.each([
    {
      name: "CAS conflict",
      error: new ApiError("当前活动版本已变化", 409, {
        detail: { code: "active_requirement_set_changed" },
      }),
      malformed: false,
      expectedMessage: "当前活动版本已变化",
    },
    {
      name: "malformed response",
      error: null,
      malformed: true,
      expectedMessage: "启用结果无法验证",
    },
  ])(
    "keeps the dialog open on activation $name",
    async ({ error, malformed, expectedMessage }) => {
      installNavigationResizeObserver();
      vi.spyOn(api, "sourcePage").mockResolvedValue({});
      const tree = navigationTree();
      const accepted = classificationResponseForTree(tree, {
        status: "ACCEPTED",
        type: "TECHNICAL",
      });
      const enrichment = atomizationEnrichmentState(accepted);
      enrichment.semantic_publication_capability =
        semanticPublicationCapability();
      vi.mocked(api.entryClassifications).mockResolvedValue(accepted);
      vi.mocked(api.requirementEnrichment).mockResolvedValue(enrichment);
      let previewResponse: RequirementSemanticPublicationPreviewResponseV2 | null =
        null;
      let publishedResponse: RequirementSemanticPublishDraftResponseV2 | null =
        null;
      vi.spyOn(api, "previewRequirementSemanticPublication").mockImplementation(
        async (_projectId, _documentId, _sessionId, request) => {
          previewResponse = semanticPublicationPreviewResponse(
            accepted,
            request,
          );
          return previewResponse;
        },
      );
      vi.spyOn(api, "publishRequirementSemanticDraft").mockImplementation(
        async () => {
          publishedResponse = semanticPublishDraftResponse(
            tree,
            accepted,
            previewResponse as RequirementSemanticPublicationPreviewResponseV2,
          );
          return publishedResponse;
        },
      );
      const activate = vi.spyOn(api, "activateRequirementSet");
      if (error) activate.mockRejectedValue(error);
      else {
        activate.mockImplementation(async () =>
          semanticActivationResponse(
            publishedResponse as RequirementSemanticPublishDraftResponseV2,
            malformed
              ? {
                  active_requirement_set: {
                    id: "another-draft",
                    enrichment_publication_binding_id:
                      publishedResponse?.binding.binding_id,
                    enrichment_publication_binding_sha256:
                      publishedResponse?.binding.sha256,
                  },
                }
              : {},
          ),
        );
      }
      const user = userEvent.setup();
      const workspace = semanticListWorkspace(tree);
      render(
        <WorkspaceProvider value={workspace}>
          <RequirementTreePanel />
        </WorkspaceProvider>,
      );
      await user.click(
        await screen.findByRole("button", { name: "生成确认清单" }),
      );
      const dialog = screen.getByRole("dialog", { name: "生成确认清单" });
      await user.click(
        within(dialog).getByRole("button", { name: "继续生成确认清单" }),
      );

      expect(await within(dialog).findByRole("alert")).toHaveTextContent(
        expectedMessage,
      );
      expect(dialog).toBeInTheDocument();
      expect(workspace.setNotice).not.toHaveBeenCalled();
      expect(workspace.updateRoute).not.toHaveBeenCalledWith(
        expect.objectContaining({ checkpoint: "review" }),
      );
    },
  );
});

describe("RequirementTreePanel explicit atomization scope", () => {
  it("does not expose atomization selection without an exact semantic session", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    vi.mocked(api.entryClassifications).mockResolvedValue(
      classificationResponseForTree(tree, {
        status: "WORKING",
        type: "TECHNICAL",
      }),
    );
    const workspace = navigationWorkspaceValue(tree);
    workspace.route = { ...workspace.route, checkpoint: "list" };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={workspace}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    await screen.findByRole("region", { name: "清单分类统计" });
    await resetNavigationViewportToTop();
    await user.click(screen.getByText("展开", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "全部展开" }));
    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );

    expect(
      screen.queryByRole("checkbox", {
        name: "加入“所选项”原子化范围",
      }),
    ).not.toBeInTheDocument();
    expect(api.requirementEnrichment).not.toHaveBeenCalled();
    expect(api.runRequirementAtomizationScope).not.toHaveBeenCalled();
  });

  it("does not expose atomization selection when the bound capability is unavailable", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const classification = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
    });
    const enrichment = atomizationEnrichmentState(classification);
    enrichment.atomization_scope.capability.available = false;
    vi.mocked(api.entryClassifications).mockResolvedValue(classification);
    vi.mocked(api.requirementEnrichment).mockResolvedValue(enrichment);
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={semanticListWorkspace(tree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    await screen.findByRole("region", { name: "需求原子化" });
    await resetNavigationViewportToTop();
    await user.click(screen.getByText("展开", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "全部展开" }));
    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );

    expect(
      screen.queryByRole("checkbox", {
        name: "加入“所选项”原子化范围",
      }),
    ).not.toBeInTheDocument();
    expect(api.runRequirementAtomizationScope).not.toHaveBeenCalled();
  });

  it("polls a RUNNING server attempt every 2.5 seconds and cancels the read when leaving the checkpoint", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const classification = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
    });
    let resolveInitialEnrichment!: (value: RequirementEnrichmentState) => void;
    let polledSignal: AbortSignal | undefined;
    const initialEnrichment = new Promise<RequirementEnrichmentState>(
      (resolve) => {
        resolveInitialEnrichment = resolve;
      },
    );
    const pendingPoll = new Promise<RequirementEnrichmentState>(() => {});
    vi.mocked(api.entryClassifications).mockResolvedValue(classification);
    vi.mocked(api.requirementEnrichment)
      .mockImplementationOnce(async () => initialEnrichment)
      .mockImplementationOnce(
        async (_projectId, _documentId, _sessionId, signal) => {
          polledSignal = signal;
          return pendingPoll;
        },
      );
    const user = userEvent.setup();
    const view = render(
      <WorkspaceProvider value={semanticListWorkspace(tree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await screen.findByRole("region", { name: "清单分类统计" });
    await waitFor(() =>
      expect(api.requirementEnrichment).toHaveBeenCalledTimes(1),
    );
    await resetNavigationViewportToTop();
    await user.click(screen.getByText("展开", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "全部展开" }));
    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );
    vi.useFakeTimers();
    try {
      await act(async () => {
        resolveInitialEnrichment(
          atomizationEnrichmentState(classification, {
            can_undo: true,
            current_receipt: atomizationScopeReceipt(),
            last_attempt: atomizationScopeAttempt({
              status: "RUNNING",
              completed_review_node_ids: [],
              failed_review_node_ids: [],
              checkpoint_created_review_node_ids: [],
              provider_call_count: 0,
              retryable: false,
              checkpoint_resumable: false,
              error_code: "",
            }),
          }),
        );
        await Promise.resolve();
      });

      const inspector = screen.getByRole("region", { name: "需求原子化" });
      expect(inspector).toHaveTextContent("正在执行原子化 · 每 2.5 秒自动更新");
      expect(
        within(inspector).getByRole("button", {
          name: "选择原子化范围",
        }),
      ).toBeDisabled();
      expect(
        within(inspector).getByRole("button", {
          name: "撤销最近一次原子化",
        }),
      ).toBeDisabled();
      expect(
        screen.getByRole("checkbox", {
          name: "加入“所选项”原子化范围",
        }),
      ).toBeDisabled();
      expect(api.runRequirementAtomizationScope).not.toHaveBeenCalled();

      await act(async () => {
        vi.advanceTimersByTime(2_500);
        await Promise.resolve();
      });
      expect(api.requirementEnrichment).toHaveBeenCalledTimes(2);
      expect(polledSignal).toBeInstanceOf(AbortSignal);
      expect(polledSignal?.aborted).toBe(false);

      const structureWorkspace = semanticListWorkspace(tree);
      structureWorkspace.route = {
        ...structureWorkspace.route,
        checkpoint: "structure",
      };
      view.rerender(
        <WorkspaceProvider value={structureWorkspace}>
          <RequirementTreePanel />
        </WorkspaceProvider>,
      );
      await act(async () => Promise.resolve());
      expect(polledSignal?.aborted).toBe(true);
      expect(api.runRequirementAtomizationScope).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("blocks only the matching non-retryable APPLY range and enables a changed scope", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const classification = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
    });
    vi.mocked(api.entryClassifications).mockResolvedValue(classification);
    vi.mocked(api.requirementEnrichment).mockResolvedValue(
      atomizationEnrichmentState(classification, {
        last_attempt: atomizationScopeAttempt(),
      }),
    );
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={semanticListWorkspace(tree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    await resetNavigationViewportToTop();
    await user.click(screen.getByText("展开", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "全部展开" }));
    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );

    const inspector = await screen.findByRole("region", {
      name: "需求原子化",
    });
    expect(inspector).toHaveTextContent("请调整范围后再执行");
    const open = within(inspector).getByRole("button", {
      name: "选择原子化范围",
    });
    expect(open).toBeEnabled();
    await user.click(open);
    const dialog = screen.getByRole("dialog", { name: "选择原子化范围" });
    const matchingApply = within(dialog).getByRole("button", {
      name: "原子化 1 项技术需求",
    });
    expect(matchingApply).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "全部条目" }));
    expect(
      within(dialog).getByRole("button", {
        name: "原子化 1 项技术需求",
      }),
    ).toBeEnabled();
    expect(
      within(dialog).queryByText(/调整范围后再执行/),
    ).not.toBeInTheDocument();
    expect(api.runRequirementAtomizationScope).not.toHaveBeenCalled();
  });

  it("offers an explicit retry for retryable APPLY but blocks a non-retryable UNDO", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const classification = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
    });
    let enrichment = atomizationEnrichmentState(classification, {
      can_undo: true,
      current_receipt: atomizationScopeReceipt(),
      last_attempt: atomizationScopeAttempt({ retryable: true }),
    });
    vi.mocked(api.entryClassifications).mockResolvedValue(classification);
    vi.mocked(api.requirementEnrichment).mockImplementation(
      async () => enrichment,
    );
    const user = userEvent.setup();
    const view = render(
      <WorkspaceProvider value={semanticListWorkspace(tree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    await resetNavigationViewportToTop();
    await user.click(screen.getByText("展开", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "全部展开" }));
    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );
    await user.click(
      await screen.findByRole("button", { name: "选择原子化范围" }),
    );
    expect(
      screen.getByRole("button", {
        name: "重试原子化 1 项技术需求",
      }),
    ).toBeEnabled();

    enrichment = atomizationEnrichmentState(classification, {
      can_undo: true,
      current_receipt: atomizationScopeReceipt(),
      last_attempt: atomizationScopeAttempt({
        operation: "UNDO",
        scope: "CURRENT",
        requested_review_node_ids: [],
        executed_review_node_ids: [],
        failed_review_node_ids: [],
      }),
    });
    enrichment.review_revision = 8;
    view.rerender(
      <WorkspaceProvider value={semanticListWorkspace(tree, 8)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    await waitFor(() =>
      expect(api.requirementEnrichment).toHaveBeenCalledTimes(2),
    );
    const inspector = await screen.findByRole("region", {
      name: "需求原子化",
    });
    expect(inspector).toHaveTextContent("最近一次撤销不可重试");
    expect(
      within(inspector).getByRole("button", {
        name: "撤销最近一次原子化",
      }),
    ).toBeDisabled();
    expect(
      within(inspector).getByRole("button", {
        name: "选择原子化范围",
      }),
    ).toBeEnabled();
    expect(api.runRequirementAtomizationScope).not.toHaveBeenCalled();
  });

  it("loads scope state without implicit writes and keeps selection independent from highlight and filters", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const classification = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
    });
    vi.mocked(api.entryClassifications).mockResolvedValue(classification);
    vi.mocked(api.requirementEnrichment).mockResolvedValue(
      atomizationEnrichmentState(classification),
    );
    const runScope = vi.mocked(api.runRequirementAtomizationScope);
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={semanticListWorkspace(tree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await screen.findByRole("region", { name: "需求原子化" });
    expect(api.requirementEnrichment).toHaveBeenCalledWith(
      "persist-project",
      "persist-document",
      "review-session",
      expect.any(AbortSignal),
    );
    expect(runScope).not.toHaveBeenCalled();

    await resetNavigationViewportToTop();
    await user.click(screen.getByText("展开", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "全部展开" }));
    const leaf = await screen.findByRole("treeitem", {
      name: /持久化叶节点/,
    });
    expect(leaf.querySelector('input[type="checkbox"]')).toBeNull();
    await user.click(leaf);
    const selectedToggle = screen.getByRole("checkbox", {
      name: "加入“所选项”原子化范围",
    });
    await user.click(selectedToggle);
    expect(selectedToggle).toBeChecked();

    await user.click(screen.getByRole("button", { name: "技术需求筛选" }));
    expect(selectedToggle).toBeChecked();
    await user.click(screen.getByRole("button", { name: "选择原子化范围" }));
    await user.click(screen.getByRole("button", { name: "全部条目" }));
    expect(runScope).not.toHaveBeenCalled();
  });

  it("clears the independent selection only when the working identity changes", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    let classification = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
      snapshotId: "classification-before",
    });
    vi.mocked(api.entryClassifications).mockImplementation(
      async () => classification,
    );
    vi.mocked(api.requirementEnrichment).mockImplementation(async () =>
      atomizationEnrichmentState(classification),
    );
    const user = userEvent.setup();
    const view = render(
      <WorkspaceProvider value={semanticListWorkspace(tree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await resetNavigationViewportToTop();
    await user.click(screen.getByText("展开", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "全部展开" }));
    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );
    const checkbox = await screen.findByRole("checkbox", {
      name: "加入“所选项”原子化范围",
    });
    await user.click(checkbox);
    expect(checkbox).toBeChecked();

    const changedTree: RequirementTreeSnapshot = {
      ...tree,
      structure_identity: {
        ...tree.structure_identity,
        effective_tree_sha256: "9".repeat(64),
      },
    };
    classification = classificationResponseForTree(changedTree, {
      status: "WORKING",
      type: "TECHNICAL",
      snapshotId: "classification-after",
    });
    classification.snapshot.working_tree_sha256 = "8".repeat(64);
    view.rerender(
      <WorkspaceProvider value={semanticListWorkspace(changedTree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );

    await waitFor(() =>
      expect(
        screen.getByRole("checkbox", {
          name: "加入“所选项”原子化范围",
        }),
      ).not.toBeChecked(),
    );
  });

  it("posts an exact plan-aware APPLY and keeps the dialog open on a conflict", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const classification = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
      snapshotId: "classification-scope",
      payloadSha256: "7".repeat(64),
    });
    let rejectScopeReload!: (reason?: unknown) => void;
    const scopeReload = new Promise<RequirementEnrichmentState>(
      (_resolve, reject) => {
        rejectScopeReload = reject;
      },
    );
    vi.mocked(api.entryClassifications).mockResolvedValue(classification);
    vi.mocked(api.requirementEnrichment)
      .mockResolvedValueOnce(atomizationEnrichmentState(classification))
      .mockImplementationOnce(async () => scopeReload)
      .mockResolvedValueOnce(atomizationEnrichmentState(classification));
    const runScope = vi
      .mocked(api.runRequirementAtomizationScope)
      .mockRejectedValue(
        new ApiError("范围版本已变化，请重试", 409, {
          detail: "范围版本已变化，请重试",
        }),
      );
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={semanticListWorkspace(tree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    await resetNavigationViewportToTop();
    await user.click(screen.getByText("展开", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "全部展开" }));
    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );
    const selectedScopeItem = screen.getByRole("checkbox", {
      name: "加入“所选项”原子化范围",
    });
    await user.click(selectedScopeItem);
    await user.click(screen.getByRole("button", { name: "选择原子化范围" }));
    const dialog = screen.getByRole("dialog", { name: "选择原子化范围" });
    await user.click(
      within(dialog).getByRole("button", {
        name: "原子化 1 项技术需求",
      }),
    );

    await waitFor(() => expect(runScope).toHaveBeenCalledTimes(1));
    expect(runScope.mock.calls[0]?.slice(0, 3)).toEqual([
      "persist-project",
      "persist-document",
      "review-session",
    ]);
    expect(runScope.mock.calls[0]?.[3]).toEqual(
      expect.objectContaining({
        schema_version: "requirement-atomization-run-request/v2",
        operation: "APPLY",
        scope: "BRANCH",
        expected_review_revision: 7,
        expected_scope_revision: 3,
        expected_entry_classification_snapshot_id: "classification-scope",
        expected_entry_classification_revision: 1,
        expected_entry_classification_sha256: "7".repeat(64),
        expected_working_tree_id: "d".repeat(64),
        expected_working_tree_sha256: "d".repeat(64),
        expected_effective_structure_id: "effective",
        anchor_tree_node_id: "persist-leaf",
        target_review_node_ids: ["persist-leaf"],
        unselected_policy: "SOURCE_BOUND_PASSTHROUGH",
      }),
    );
    expect(runScope.mock.calls[0]?.[4]).toBeInstanceOf(AbortSignal);
    const alert = within(dialog).getByRole("alert");
    expect(alert).toHaveTextContent("范围版本已变化，请重试");
    expect(alert).not.toHaveTextContent("已重新读取");
    expect(dialog).toBeInTheDocument();
    await waitFor(() =>
      expect(api.requirementEnrichment).toHaveBeenCalledTimes(2),
    );
    const apply = within(dialog).getByRole("button", {
      name: "原子化 1 项技术需求",
    });
    expect(apply).toBeDisabled();
    expect(selectedScopeItem).toBeChecked();
    await user.click(apply);
    expect(runScope).toHaveBeenCalledTimes(1);

    rejectScopeReload(new Error("原子化范围自动重读失败"));
    await waitFor(() =>
      expect(within(dialog).getByRole("alert")).toHaveTextContent(
        "原子化范围自动重读失败",
      ),
    );
    expect(apply).toBeDisabled();
    expect(selectedScopeItem).toBeChecked();

    await user.click(
      within(dialog).getByRole("button", { name: "重新读取范围" }),
    );
    await waitFor(() =>
      expect(api.requirementEnrichment).toHaveBeenCalledTimes(3),
    );
    await waitFor(() => expect(apply).toBeEnabled());
    await waitFor(() =>
      expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument(),
    );
  });

  it("clears a conflict only after the automatic scope reread succeeds", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const classification = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
      snapshotId: "classification-auto-reread",
    });
    vi.mocked(api.entryClassifications).mockResolvedValue(classification);
    vi.mocked(api.requirementEnrichment).mockResolvedValue(
      atomizationEnrichmentState(classification),
    );
    vi.mocked(api.runRequirementAtomizationScope).mockRejectedValue(
      new ApiError("范围版本已变化", 409, {
        detail: "范围版本已变化",
      }),
    );
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={semanticListWorkspace(tree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    await resetNavigationViewportToTop();
    await user.click(screen.getByText("展开", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "全部展开" }));
    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );
    await user.click(
      await screen.findByRole("button", { name: "选择原子化范围" }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "选择原子化范围",
    });
    await user.click(
      within(dialog).getByRole("button", {
        name: "原子化 1 项技术需求",
      }),
    );

    await waitFor(() =>
      expect(api.requirementEnrichment).toHaveBeenCalledTimes(2),
    );
    await waitFor(() =>
      expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument(),
    );
    expect(
      within(dialog).getByRole("button", {
        name: "原子化 1 项技术需求",
      }),
    ).toBeEnabled();
    expect(dialog).toBeInTheDocument();
  });

  it("refetches after classification, APPLY and accessible UNDO mutations", async () => {
    installNavigationResizeObserver();
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const tree = navigationTree();
    const initial = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "TECHNICAL",
      snapshotId: "classification-initial",
      payloadSha256: "5".repeat(64),
    });
    const saved = classificationResponseForTree(tree, {
      status: "WORKING",
      type: "NON_TECHNICAL",
      snapshotId: "classification-saved",
      payloadSha256: "6".repeat(64),
    });
    let currentClassification = initial;
    let canUndo = false;
    vi.mocked(api.entryClassifications).mockResolvedValue(initial);
    vi.spyOn(api, "updateEntryClassifications").mockImplementation(async () => {
      currentClassification = saved;
      return saved;
    });
    vi.mocked(api.requirementEnrichment).mockImplementation(async () =>
      atomizationEnrichmentState(currentClassification, {
        current_scope_revision: canUndo ? 4 : 3,
        can_undo: canUndo,
        current_atomization_run_id: canUndo ? "run-4" : "",
        current_atomization_run_revision: canUndo ? 4 : 0,
      }),
    );
    vi.mocked(api.runRequirementAtomizationScope).mockImplementation(
      async (_projectId, _documentId, _sessionId, command) => {
        canUndo = command.operation === "APPLY";
        return {
          schema_version: "requirement-atomization-run-response/v2",
          serving_impact: "NONE",
          replayed: false,
          atomization_run: {},
          scope_receipt: atomizationScopeReceipt(),
        };
      },
    );
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={semanticListWorkspace(tree)}>
        <RequirementTreePanel />
      </WorkspaceProvider>,
    );
    await resetNavigationViewportToTop();
    await user.click(screen.getByText("展开", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "全部展开" }));
    await user.click(
      await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
    );
    await user.click(screen.getByRole("button", { name: "选择原子化范围" }));
    await user.click(
      screen.getByRole("button", { name: "原子化 1 项技术需求" }),
    );
    await waitFor(() =>
      expect(api.requirementEnrichment).toHaveBeenCalledTimes(2),
    );

    const undo = await screen.findByRole("button", {
      name: "撤销最近一次原子化",
    });
    expect(undo).toBeEnabled();
    await user.click(undo);
    await waitFor(() =>
      expect(api.requirementEnrichment).toHaveBeenCalledTimes(3),
    );
    expect(api.runRequirementAtomizationScope).toHaveBeenLastCalledWith(
      "persist-project",
      "persist-document",
      "review-session",
      expect.objectContaining({
        operation: "UNDO",
        expected_review_revision: 7,
        expected_scope_revision: 4,
        target_review_node_ids: [],
      }),
      expect.any(AbortSignal),
    );

    await user.click(screen.getByRole("button", { name: "非技术需求" }));
    await waitFor(() =>
      expect(api.requirementEnrichment).toHaveBeenCalledTimes(4),
    );
  });

  it.each(["resolve", "reject"] as const)(
    "ignores a delayed %s from an atomization POST after the bound identity changes",
    async (settlement) => {
      installNavigationResizeObserver();
      vi.spyOn(api, "sourcePage").mockResolvedValue({});
      const tree = navigationTree();
      let currentClassification = classificationResponseForTree(tree, {
        status: "WORKING",
        type: "TECHNICAL",
        snapshotId: "classification-before-post",
      });
      vi.mocked(api.entryClassifications).mockImplementation(
        async () => currentClassification,
      );
      vi.mocked(api.requirementEnrichment).mockImplementation(async () =>
        atomizationEnrichmentState(currentClassification),
      );
      let resolvePost!: (value: RequirementAtomizationScopeRunResponse) => void;
      let rejectPost!: (reason?: unknown) => void;
      let postSignal: AbortSignal | undefined;
      const delayedPost = new Promise<RequirementAtomizationScopeRunResponse>(
        (resolve, reject) => {
          resolvePost = resolve;
          rejectPost = reject;
        },
      );
      vi.mocked(api.runRequirementAtomizationScope).mockImplementation(
        async (_projectId, _documentId, _sessionId, _command, signal) => {
          postSignal = signal;
          return delayedPost;
        },
      );
      const notice = vi.fn();
      const initialWorkspace = semanticListWorkspace(tree);
      initialWorkspace.setNotice = notice;
      const user = userEvent.setup();
      const view = render(
        <WorkspaceProvider value={initialWorkspace}>
          <RequirementTreePanel />
        </WorkspaceProvider>,
      );
      await resetNavigationViewportToTop();
      await user.click(screen.getByText("展开", { selector: "summary" }));
      await user.click(screen.getByRole("button", { name: "全部展开" }));
      await user.click(
        await screen.findByRole("treeitem", { name: /持久化叶节点/ }),
      );
      await user.click(
        await screen.findByRole("button", { name: "选择原子化范围" }),
      );
      await user.click(
        screen.getByRole("button", { name: "原子化 1 项技术需求" }),
      );
      await waitFor(() =>
        expect(api.runRequirementAtomizationScope).toHaveBeenCalledTimes(1),
      );
      expect(postSignal).toBeInstanceOf(AbortSignal);
      expect(postSignal?.aborted).toBe(false);

      const changedTree: RequirementTreeSnapshot = {
        ...tree,
        structure_identity: {
          ...tree.structure_identity,
          effective_tree_sha256: "9".repeat(64),
        },
      };
      currentClassification = classificationResponseForTree(changedTree, {
        status: "WORKING",
        type: "TECHNICAL",
        snapshotId: "classification-after-post",
        payloadSha256: "8".repeat(64),
      });
      currentClassification.snapshot.working_tree_sha256 = "7".repeat(64);
      const changedWorkspace = semanticListWorkspace(changedTree);
      changedWorkspace.setNotice = notice;
      view.rerender(
        <WorkspaceProvider value={changedWorkspace}>
          <RequirementTreePanel />
        </WorkspaceProvider>,
      );

      await waitFor(() => expect(postSignal?.aborted).toBe(true));
      await waitFor(() =>
        expect(api.requirementEnrichment).toHaveBeenCalledTimes(2),
      );
      expect(
        screen.queryByRole("dialog", { name: "选择原子化范围" }),
      ).not.toBeInTheDocument();

      await act(async () => {
        if (settlement === "resolve") {
          resolvePost({
            schema_version: "requirement-atomization-run-response/v2",
            serving_impact: "NONE",
            replayed: false,
            atomization_run: {},
            scope_receipt: atomizationScopeReceipt(),
          });
        } else {
          rejectPost(
            new ApiError("旧身份服务失败", 503, {
              detail: "旧身份服务失败",
            }),
          );
        }
        await Promise.resolve();
      });

      expect(notice).not.toHaveBeenCalled();
      expect(api.requirementEnrichment).toHaveBeenCalledTimes(2);
      expect(screen.queryByText(/旧身份服务失败/)).not.toBeInTheDocument();
      expect(
        screen.queryByRole("dialog", { name: "选择原子化范围" }),
      ).not.toBeInTheDocument();
    },
  );
});
