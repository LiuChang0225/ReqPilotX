import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../../shared/api/client";
import { setSaasAccountIdentity } from "../../shared/api/saasSession";
import { SAAS_KIT_UNAVAILABLE_MESSAGE } from "./saasAgentAvailability";
import type {
  NativeStructureActivityEventV2,
  NativeStructureActivityStreamV1,
  NativeStructureReferencePublicV1,
  NativeStructureTablePreviewV1,
  NativeStructureWorkingRevisionHeadResponseV1,
  NativeStructureWorkingRevisionV1,
  SourceRequirementBaselineOutlineV1,
  SourceRequirementBaselineRunStatusV1,
  SourceRequirementRecordDetailV1,
} from "../../shared/api/contracts";
import {
  activeNativeStructureFocusPages,
  canonicalStructuredTablesAreValid,
  canonicalTableGridIssues,
  NativeStructureReferenceDock,
  nativeStructuredTables,
  reconcileNativeTreeUiState,
} from "./NativeStructureReferenceDock";
import { canonicalJsonSha256 } from "./canonicalJsonSha256";
import { WorkbenchHostContext } from "../workspace/WorkbenchHostContext";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  setSaasAccountIdentity(undefined);
});

const reference: NativeStructureReferencePublicV1 = {
  schema_version: "native-structure-reference-public/v1",
  availability: "READY",
  artifact_id: "pds-native-v1",
  source_sha256: "a".repeat(64),
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
    projection_sha256: "b".repeat(64),
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
  page_ownership: [
    {
      unit_id: "unit-main",
      title: "产品设计规格说明书（PDS）",
      pages: [1, 2, 3, 4, 5, 6, 7, 8, 9],
      title_authority: "VISIBLE_TITLE",
      confidence: "HIGH",
    },
  ],
  main_document_sections: [],
  document_tree: {
    schema_version: "native-document-structure-tree/v1",
    root_ids: ["doc-main", "doc-groups"],
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
            anchor_id: "anchor-root",
            page: 1,
            bbox: [10, 10, 100, 24],
            source_bounds: [0, 0, 595, 842],
            anchor_kind: "TITLE",
            precision: "EXACT_NATIVE_SPAN",
          },
        ],
        source_content: [],
      },
      {
        node_id: "section-3",
        parent_id: "doc-main",
        order: 0,
        kind: "SECTION",
        label: "3",
        title: "车型/产品信息",
        title_authority: "VISIBLE_TITLE",
        pages: [2, 3, 4, 5, 6],
        content_blocks: [],
        finding_codes: [],
        source_anchors: [
          {
            anchor_id: "anchor-section-3",
            page: 2,
            bbox: [30, 100, 180, 114],
            source_bounds: [0, 0, 595, 842],
            anchor_kind: "TITLE",
            precision: "EXACT_NATIVE_SPAN",
          },
        ],
        source_content: [],
      },
      {
        node_id: "section-3.2",
        parent_id: "section-3",
        order: 0,
        kind: "CLAUSE",
        label: "3.2",
        title: "产品技术要求及说明",
        title_authority: "VISIBLE_TITLE",
        pages: [2, 3, 4, 5, 6],
        content_blocks: [],
        finding_codes: [],
        source_anchors: [
          {
            anchor_id: "anchor-section-3-2",
            page: 2,
            bbox: [30, 120, 220, 134],
            source_bounds: [0, 0, 595, 842],
            anchor_kind: "TITLE",
            precision: "EXACT_NATIVE_SPAN",
          },
        ],
        source_content: [],
      },
      {
        node_id: "section-3.2.1",
        parent_id: "section-3.2",
        order: 0,
        kind: "CLAUSE",
        label: "3.2.1",
        title: "产品信息",
        title_authority: "VISIBLE_TITLE",
        pages: [10, 11, 12, 13, 14, 15],
        content_blocks: [
          { kind: "LOGICAL_TABLE", logical_table_scope_id: "lt-front" },
        ],
        finding_codes: [],
        source_anchors: [
          {
            anchor_id: "anchor-section-3-2-1-title",
            page: 10,
            bbox: [30, 140, 160, 154],
            source_bounds: [0, 0, 595, 842],
            anchor_kind: "TITLE",
            precision: "EXACT_NATIVE_SPAN",
          },
          {
            anchor_id: "anchor-section-3-2-1-table",
            page: 10,
            bbox: [30, 170, 560, 760],
            source_bounds: [0, 0, 595, 842],
            anchor_kind: "TABLE_REGION",
            precision: "EXACT_TABLE_REGION",
            logical_table_scope_id: "lt-front",
          },
        ],
        source_content: [
          {
            content_id: "content-section-3-2-1",
            page: 10,
            bbox: [30, 140, 260, 165],
            source_bounds: [0, 0, 595, 842],
            source_handles: ["source-section-3-2-1"],
            text: "3.2.1 产品信息\n当前节点的原文内容。",
            text_sha256: "a".repeat(64),
            precision: "EXACT_SOURCE_RANGE",
            is_title: false,
          },
        ],
      },
      {
        node_id: "doc-groups",
        parent_id: null,
        order: 1,
        kind: "DOCUMENT_UNIT",
        label: "",
        title: "测试分组与测试顺序矩阵",
        title_authority: "DERIVED_FROM_VISIBLE_TABLE_ROLE",
        pages: [23, 24, 25, 26, 27, 28, 29],
        content_blocks: [],
        finding_codes: ["connector_package_parent_scope"],
        source_anchors: [
          {
            anchor_id: "anchor-groups-table",
            page: 24,
            bbox: [40, 80, 800, 520],
            source_bounds: [0, 0, 842, 595],
            anchor_kind: "TABLE_REGION",
            precision: "EXACT_TABLE_REGION",
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
      fragments: [
        { table_token: "T0009", page: 10, row_range: [5, 17] },
      ],
      source_numbering_anomaly: "",
      capture_gap: "",
    },
  ],
  physical_table_dispositions: [],
  unresolved: [
    {
      code: "connector_package_parent_scope",
      scope: "P23-P29",
      detail: "缺少显式总附件标题。",
      pages: [23, 24, 25, 26, 27, 28, 29],
    },
  ],
  implementation_findings: [],
  visual_summary: { opened_page_count: 29, page_count: 29, receipts: [] },
  repeatability: {
    status: "PASS_WITH_BOUNDARY_REFINEMENTS",
    stable_semantics: {},
    refinements_applied: [],
    unresolved_not_settled_by_replay: ["connector_package_parent_scope"],
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
  structure_proposals: [
    {
      proposal_id: "proposal-image-1",
      finding_code: "visual_asset_parent_binding_missing",
      node_id: "section-3.2.1",
      object_kind: "IMAGE_PLACEMENT",
      object_id: "placement-1",
      page: 10,
      bbox: [200, 200, 260, 260],
      source_bounds: [0, 0, 595, 842],
      proposal_title: "图片父级归属",
      observation: "图片唯一落在当前节点的表格区域。",
      recommended_action: "ATTACH_IMAGE_TO_NODE",
      recommendation: "将图片归入当前节点。",
      recommended_target_node_id: "section-3.2.1",
      impact: "只补充图片证据归属。",
      confidence: "HIGH",
      default_policy: "AUTO_APPLY_REVERSIBLE",
      status: "AUTO_APPLIED_PENDING_REVIEW",
      overlap_ratio: 1,
      decision: null,
    },
  ],
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
    review_pending_proposal_count: 1,
    latest_handoff: null,
    message: "保存节点指引或处理建议后，可继续交给 Agent。",
  },
  feedback_capability: {
    status: "QUEUE_READY",
    agent_delivery: "IDLE",
    message: "等待读取",
  },
};

const tablePreview: NativeStructureTablePreviewV1 = {
  schema_version: "native-structure-table-preview/v1",
  artifact_id: reference.artifact_id,
  source_sha256: reference.source_sha256,
  logical_table_scope_id: "lt-front",
  title: "主动前减振器试验大纲",
  summary: { fragment_count: 1, row_count: 2, cell_count: 4 },
  fragments: [
    {
      fragment_id: "table-preview-front-1",
      table_token: "T0009",
      page: 10,
      row_range: [5, 7],
      row_count: 2,
      column_count: 2,
      bbox: [30, 170, 560, 300],
      source_bounds: [0, 0, 595, 842],
      cells: [
        {
          cell_id: "cell-header-1",
          row: 0,
          column: 0,
          row_span: 1,
          column_span: 1,
          text: "序号",
          bbox: [30, 170, 100, 190],
        },
        {
          cell_id: "cell-header-2",
          row: 0,
          column: 1,
          row_span: 1,
          column_span: 1,
          text: "试验项目",
          bbox: [100, 170, 560, 190],
        },
        {
          cell_id: "cell-row-1",
          row: 1,
          column: 0,
          row_span: 1,
          column_span: 1,
          text: "1",
          bbox: [30, 190, 100, 220],
        },
        {
          cell_id: "cell-row-2",
          row: 1,
          column: 1,
          row_span: 1,
          column_span: 1,
          text: "外观检查",
          bbox: [100, 190, 560, 220],
        },
      ],
    },
  ],
};

const workingRunId = `agent-run-${"1".repeat(32)}`;
const workingRevisionId = `native-structure-working-revision-${"2".repeat(32)}`;
const workingEvidenceArtifactId = `native-structure-evidence-${"3".repeat(32)}`;

function workingHead(
  overrides: Partial<
    NonNullable<NativeStructureWorkingRevisionHeadResponseV1["head"]>
  > = {},
): NativeStructureWorkingRevisionHeadResponseV1 {
  return {
    schema_version: "native-structure-working-revision-head-response/v1",
    source_sha256: reference.source_sha256,
    head: {
      schema_version: "native-structure-working-revision-head/v1",
      run_id: workingRunId,
      handoff_id: null,
      source_sha256: reference.source_sha256,
      base_artifact_id: reference.artifact_id,
      base_projection_sha256: reference.structure_projection.projection_sha256,
      revision_id: workingRevisionId,
      revision: 1,
      previous_revision_sha256: null,
      revision_sha256: "c".repeat(64),
      evidence_artifact_id: workingEvidenceArtifactId,
      validator_kind: "D2_HOST_COMPILER",
      d2_compiler_receipt_sha256: "e".repeat(64),
      quality_claim: "D2_HOST_VERIFIED",
      created_at_utc: "2026-09-01T00:00:00Z",
      state: "WORKING",
      ...overrides,
    },
  };
}

function workingRevision(
  overrides: Partial<NativeStructureWorkingRevisionV1> = {},
): NativeStructureWorkingRevisionV1 {
  return {
    schema_version: "native-structure-working-revision/v1",
    run_id: workingRunId,
    handoff_id: null,
    source_sha256: reference.source_sha256,
    base_artifact_id: reference.artifact_id,
    base_projection_sha256: reference.structure_projection.projection_sha256,
    evidence_artifact_id: workingEvidenceArtifactId,
    revision_id: workingRevisionId,
    revision: 1,
    previous_revision_sha256: null,
    revision_sha256: "c".repeat(64),
    validator_receipt_sha256: "d".repeat(64),
    validator_kind: "D2_HOST_COMPILER",
    d2_compiler_receipt_sha256: "e".repeat(64),
    validation_status: "PASS",
    quality_claim: "D2_HOST_VERIFIED",
    created_at_utc: "2026-09-01T00:00:00Z",
    state: "WORKING",
    document_tree: reference.document_tree,
    node_states: Object.fromEntries(
      reference.document_tree.nodes.map((node) => [
        node.node_id,
        "WORKING_VERIFIED",
      ]),
    ),
    inserted_node_ids: reference.document_tree.nodes.map((node) => node.node_id),
    changed_node_ids: [],
    retired_node_ids: [],
    successor_node_ids: {},
    page_ownership: reference.page_ownership,
    logical_tables: reference.logical_tables,
    table_preview_bindings: reference.logical_tables.map((table, index) => ({
      schema_version: "native-structure-table-preview-binding/v1" as const,
      scope_id: table.scope_id,
      preview_sha256: String(index + 1).repeat(64),
      object_key: `working-table-previews/${table.scope_id}.json`,
      object_sha256: String(index + 2).repeat(64),
      object_size: 128,
      media_type:
        "application/vnd.reqpilotx.native-structure-table-preview+json" as const,
      source_sha256: reference.source_sha256,
      base_artifact_id: reference.artifact_id,
      base_projection_sha256:
        reference.structure_projection.projection_sha256,
    })),
    structure_proposals: reference.structure_proposals,
    unresolved: reference.unresolved,
    visual_summary: reference.visual_summary,
    ...overrides,
  };
}

function headFromRevision(
  revision: NativeStructureWorkingRevisionV1,
): NativeStructureWorkingRevisionHeadResponseV1 {
  return workingHead({
    run_id: revision.run_id,
    handoff_id: revision.handoff_id,
    source_sha256: revision.source_sha256,
    base_artifact_id: revision.base_artifact_id,
    base_projection_sha256: revision.base_projection_sha256,
    revision_id: revision.revision_id,
    revision: revision.revision,
    previous_revision_sha256: revision.previous_revision_sha256,
    revision_sha256: revision.revision_sha256,
    evidence_artifact_id: revision.evidence_artifact_id,
    validator_kind: revision.validator_kind,
    d2_compiler_receipt_sha256: revision.d2_compiler_receipt_sha256,
    quality_claim: revision.quality_claim,
    created_at_utc: revision.created_at_utc,
    state: revision.state,
  });
}

function sequencedWorkingRevision(
  revisionNumber: number,
  revisionSha256: string,
  previousRevisionSha256: string | null,
  overrides: Partial<NativeStructureWorkingRevisionV1> = {},
): NativeStructureWorkingRevisionV1 {
  const idDigit = Math.min(15, revisionNumber).toString(16);
  return workingRevision({
    revision_id: `native-structure-working-revision-${idDigit.repeat(32)}`,
    revision: revisionNumber,
    previous_revision_sha256: previousRevisionSha256,
    revision_sha256: revisionSha256,
    evidence_artifact_id: `native-structure-evidence-${idDigit.repeat(32)}`,
    created_at_utc: `2026-09-01T00:00:0${revisionNumber}Z`,
    inserted_node_ids:
      revisionNumber === 1
        ? reference.document_tree.nodes.map((node) => node.node_id)
        : [],
    ...overrides,
  });
}

const sourceItemId = `source-record-${"3".repeat(32)}`;

const sourceRequirementOutline: SourceRequirementBaselineOutlineV1 = {
  schema_version: "source-requirement-baseline-outline/v1",
  source_sha256: reference.source_sha256,
  document_map: {
    artifact_id: `document-map-${"1".repeat(32)}`,
    artifact_sha256: "1".repeat(64),
    revision: 1,
    status: "ACCEPTED",
    summary: "已接受同一原文标题树、内容、表格和证据。",
    root_node_ids: reference.document_tree.root_ids,
    nodes: reference.document_tree.nodes.map((node) => ({
      node_id: node.node_id,
      parent_id: node.parent_id,
      order: node.order,
      kind: node.kind,
      label: node.label,
      title: node.title,
      pages: node.pages,
      record_count: node.node_id === "section-3.2.1" ? 1 : 0,
      requirement_count: node.node_id === "section-3.2.1" ? 1 : 0,
      non_requirement_count: 0,
      scope_status:
        node.node_id === "section-3.2.1" ? "READY" : "NOT_IN_CURRENT_SLICE",
    })),
  },
  baseline: {
    artifact_id: `source-requirement-baseline-${"2".repeat(32)}`,
    artifact_sha256: "2".repeat(64),
    revision: 1,
    status: "SLICE_COMPLETE",
    covered_scope_ids: ["lt-front"],
    authority: "AI_REVIEWED_DEVELOPMENT_REFERENCE_NOT_GOLD",
  },
  summary: {
    record_count: 1,
    source_item_count: 1,
    requirement_count: 1,
    non_requirement_count: 0,
    feedback_count: 0,
  },
  records: [
    {
      source_item_id: sourceItemId,
      source_record_id: sourceItemId,
      source_display_id: "SRC-0001",
      requirement_display_id: "REQ-0001",
      source_order: 0,
      node_id: "section-3.2.1",
      ancestor_path: ["产品设计规格说明书（PDS）", "3 车型/产品信息"],
      logical_table_scope_id: "lt-front",
      label: "1 外观检查",
      display_label: "1 外观检查",
      source_form: "TABLE_RECORD",
      pages: [10],
      requirementness: "REQUIREMENT",
      requirement_kinds: ["CONSTRAINT", "QUALITY"],
      non_requirement_role: null,
      confidence: 0.97,
      evidence_state: "SUFFICIENT",
      decision_summary: "编号1及其完整表格行共同形成一条可独立审核的试验要求。",
      decision_origin: "AGENT_DEFAULT",
      review_state: "UNREVIEWED",
      feedback_count: 0,
    },
  ],
  ledger: {
    schema_version: "source-item-ledger/v1",
    document_map_artifact_id: `document-map-${"1".repeat(32)}`,
    document_map_artifact_sha256: "1".repeat(64),
    baseline_artifact_id: `source-requirement-baseline-${"2".repeat(32)}`,
    baseline_artifact_sha256: "2".repeat(64),
    root_row_ids: reference.document_tree.root_ids,
    rows: [
      ...reference.document_tree.nodes.map((node) => ({
        row_id: node.node_id,
        parent_row_id: node.parent_id,
        child_row_ids: [
          ...reference.document_tree.nodes
            .filter((candidate) => candidate.parent_id === node.node_id)
            .map((candidate) => candidate.node_id),
          ...(node.node_id === "section-3.2.1" ? [sourceItemId] : []),
        ],
        row_role: "DOCUMENT_STRUCTURE" as const,
        structure_node_id: node.node_id,
        source_item_id: null,
        logical_table_scope_ids:
          node.node_id === "section-3.2.1" ? ["lt-front"] : [],
        is_table_structure: node.node_id === "section-3.2.1",
        is_one_to_one_item: false,
        processing_state:
          node.node_id === "section-3.2.1"
            ? ("ITEMS_READY" as const)
            : ("STRUCTURE_ONLY" as const),
        direct_source_item_count: node.node_id === "section-3.2.1" ? 1 : 0,
        requirement_count: node.node_id === "section-3.2.1" ? 1 : 0,
        non_requirement_count: 0,
      })),
      {
        row_id: sourceItemId,
        parent_row_id: "section-3.2.1",
        child_row_ids: [],
        row_role: "SOURCE_ITEM",
        structure_node_id: "section-3.2.1",
        source_item_id: sourceItemId,
        logical_table_scope_ids: ["lt-front"],
        is_table_structure: false,
        is_one_to_one_item: false,
        processing_state: "CLASSIFIED" as const,
        direct_source_item_count: 1,
        requirement_count: 1,
        non_requirement_count: 0,
      },
    ],
    ledger_sha256: "4".repeat(64),
    interchange: {
      target: "REQIF_1_2",
      mapping_profile: "reqpilotx-reqif-1.2-draft/v1",
      mapping_status: "MODEL_READY_EXPORT_NOT_IMPLEMENTED",
      hierarchy_mapping: "SPEC_HIERARCHY",
      item_mapping: "SPEC_OBJECT",
    },
  },
  activity: [
    {
      sequence: 1,
      scope_id: "lt-front",
      state: "COMPLETED",
      title: "Agent 已完成原文条目边界与需求判断",
      summary: "形成 1 条原文条目，使用 gpt-5.6-sol / high，耗时 12 秒。",
      tool_names: [],
      elapsed_milliseconds: 12_000,
    },
  ],
};

const sourceRequirementDetail: SourceRequirementRecordDetailV1 = {
  schema_version: "source-requirement-record-detail/v1",
  source_sha256: reference.source_sha256,
  document_map_artifact_id: sourceRequirementOutline.document_map.artifact_id,
  document_map_artifact_sha256:
    sourceRequirementOutline.document_map.artifact_sha256,
  baseline_artifact_id: sourceRequirementOutline.baseline.artifact_id,
  baseline_artifact_sha256: sourceRequirementOutline.baseline.artifact_sha256,
  record: {
    ...sourceRequirementOutline.records[0],
    document_map_artifact_id: sourceRequirementOutline.document_map.artifact_id,
    document_map_artifact_sha256:
      sourceRequirementOutline.document_map.artifact_sha256,
    table_fragments: [
      {
        fragment_id: "table-preview-front-1",
        table_token: "T0009",
        page: 10,
        row_start: 1,
        row_end: 2,
        cell_ids: ["cell-row-1", "cell-row-2"],
        bbox: [30, 190, 560, 220],
        source_bounds: [0, 0, 595, 842],
      },
    ],
    text_bindings: [],
    shared_header_cell_ids: ["cell-header-1", "cell-header-2"],
    shared_table_cell_ids: [],
    shared_source_content_ids: ["content-section-3-2-1"],
    shared_source_content: [
      reference.document_tree.nodes[3].source_content[0],
    ],
    internal_diagnostics_available: true,
    feedbacks: [],
  },
};

describe("NativeStructureReferenceDock", () => {
  it("applies each Host-verified working revision once without moving the PDF or creating a second tree", async () => {
    vi.useFakeTimers();
    const insertedNode = {
      ...reference.document_tree.nodes[1],
      node_id: "section-working-new",
      parent_id: "doc-main",
      order: 99,
      label: "7",
      title: "新增工作章节",
      pages: [22],
      content_blocks: [],
      finding_codes: [],
      source_anchors: [],
      source_content: [],
    };
    const revision = workingRevision({
      document_tree: {
        ...reference.document_tree,
        nodes: [...reference.document_tree.nodes, insertedNode],
      },
      node_states: {
        ...Object.fromEntries(
          reference.document_tree.nodes.map((node) => [
            node.node_id,
            "WORKING_VERIFIED",
          ]),
        ),
        [insertedNode.node_id]: "WORKING_VERIFIED",
      },
      inserted_node_ids: [
        ...reference.document_tree.nodes.map((node) => node.node_id),
        insertedNode.node_id,
      ],
    });
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    const headRequest = vi
      .spyOn(api, "nativeStructureWorkingRevisionHead")
      .mockResolvedValue(workingHead());
    const revisionRequest = vi
      .spyOn(api, "nativeStructureWorkingRevision")
      .mockResolvedValue(revision);
    const renderedAck = vi.spyOn(
      api,
      "acknowledgeNativeStructureRenderedReopen",
    );
    const onPageChange = vi.fn();

    const view = render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        activePage={8}
        onPageChange={onPageChange}
      />,
    );

    try {
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(screen.getAllByRole("tree")).toHaveLength(1);
      expect(
        screen.getByRole("treeitem", { name: /新增工作章节/ }),
      ).toHaveTextContent("正在确认");
      expect(screen.getByRole("status")).toHaveTextContent(
        `结构已更新：新增 ${revision.document_tree.nodes.length} 项，调整 0 项，移除 0 项`,
      );
      expect(onPageChange).not.toHaveBeenCalled();
      expect(renderedAck).not.toHaveBeenCalled();
      expect(revisionRequest).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(3_200);
      });
      expect(headRequest.mock.calls.length).toBeGreaterThan(1);
      expect(revisionRequest).toHaveBeenCalledTimes(1);
      expect(onPageChange).not.toHaveBeenCalled();
      expect(renderedAck).not.toHaveBeenCalled();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("settles one revision once when Activity repeats the in-flight head", async () => {
    const replay = workingRevision({
      validator_kind: "ACCEPTED_REFERENCE_REPLAY",
      quality_claim: "TRANSPORT_REPLAY_ONLY",
      state: "ACCEPTED",
      node_states: Object.fromEntries(
        reference.document_tree.nodes.map((node) => [node.node_id, "ACCEPTED"]),
      ),
    });
    const head = headFromRevision(replay);
    const activeReference: NativeStructureReferencePublicV1 = {
      ...reference,
      continuation: {
        ...reference.continuation,
        status: "IN_PROGRESS",
        latest_handoff: {
          schema_version: "native-structure-agent-handoff/v1",
          handoff_id: "handoff-working-head-repeat",
          project_id: "project-1",
          document_id: "document-1",
          source_sha256: reference.source_sha256,
          artifact_id: reference.artifact_id,
          command_id: "continue-working-head-repeat",
          input_fingerprint: "e".repeat(64),
          status: "IN_PROGRESS",
          feedback_ids: [],
          proposal_decision_ids: [],
          affected_node_ids: ["doc-main"],
          decision_counts: { CONFIRM: 0, UNDO: 0, KEEP_UNRESOLVED: 0 },
          requires_scoped_structure_revision: true,
          requires_scoped_evidence_recovery: false,
          preserve_unaffected_nodes: true,
          current_action: "正在生成工作投影。",
          created_at_utc: "2026-09-01T00:00:00Z",
        },
        message: "正在生成工作投影。",
      },
    };
    let resolveRevision!: (value: NativeStructureWorkingRevisionV1) => void;
    const revisionPromise = new Promise<NativeStructureWorkingRevisionV1>(
      (resolve) => {
        resolveRevision = resolve;
      },
    );
    let resolveActivity!: (value: NativeStructureActivityStreamV1) => void;
    const activityPromise = new Promise<NativeStructureActivityStreamV1>(
      (resolve) => {
        resolveActivity = resolve;
      },
    );
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(activeReference);
    vi.spyOn(api, "nativeStructureWorkingRevisionHead").mockResolvedValue(head);
    const revisionRequest = vi
      .spyOn(api, "nativeStructureWorkingRevision")
      .mockReturnValue(revisionPromise);
    vi.spyOn(api, "nativeStructureActivity").mockReturnValue(activityPromise);

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    await waitFor(() => expect(revisionRequest).toHaveBeenCalledTimes(1));
    resolveActivity({
      schema_version: "native-structure-activity-stream/v1",
      source_sha256: reference.source_sha256,
      artifact_id: reference.artifact_id,
      handoff_id: "handoff-working-head-repeat",
      events: [],
      cursor: 1,
      continuation: activeReference.continuation,
      working_revision_head: head.head,
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    resolveRevision(replay);

    expect(
      await screen.findAllByText("传输回放 · 非 Agent 重解析"),
    ).not.toHaveLength(0);
    expect(revisionRequest).toHaveBeenCalledTimes(1);
    expect(
      screen.queryByText(/工作结构修订链不连续/),
    ).not.toBeInTheDocument();
    expect(screen.getAllByRole("tree")).toHaveLength(1);
  });

  it("renders a Host-verified working shell when the accepted native reference is not available", async () => {
    vi.spyOn(api, "nativeStructureReference").mockRejectedValue(
      new ApiError("not ready", 404, null),
    );
    vi.spyOn(api, "nativeStructureWorkingRevisionHead").mockResolvedValue(
      workingHead(),
    );
    vi.spyOn(api, "nativeStructureWorkingRevision").mockResolvedValue(
      workingRevision({
        node_states: Object.fromEntries(
          reference.document_tree.nodes.map((node) => [
            node.node_id,
            "WORKING_VERIFIED",
          ]),
        ),
      }),
    );

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    expect(await screen.findByRole("tree", { name: "原文标题层级" })).toBeVisible();
    expect(screen.getAllByRole("tree")).toHaveLength(1);
    expect(screen.getByText("工作结构 · Host 已验证")).toBeVisible();
    expect(screen.getByLabelText("给 Agent 的节点指引")).toBeDisabled();
  });

  it.each([1, 2])("accepts growing revision %i against its stable survey basis, not the current tree digest", async (revisionNumber) => {
    const basisSha256 = "f".repeat(64);
    const revision = workingRevision({
      revision: revisionNumber,
      base_projection_sha256: basisSha256,
      table_preview_bindings: workingRevision().table_preview_bindings.map((binding) => ({
        ...binding,
        base_projection_sha256: basisSha256,
      })),
    });
    const referenceHead = {
      ...headFromRevision(revision).head!,
      revision: 1,
      revision_sha256: revisionNumber === 1 ? revision.revision_sha256 : "8".repeat(64),
    };
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue({
      ...reference,
      coverage_status: "PARTIAL",
      working_revision_head: referenceHead,
    });
    vi.spyOn(api, "nativeStructureWorkingRevisionHead").mockResolvedValue(headFromRevision(revision));
    vi.spyOn(api, "nativeStructureWorkingRevision").mockResolvedValue(revision);
    const onPageChange = vi.fn();
    render(
      <NativeStructureReferenceDock
        projectId="project-1" documentId="document-1"
        sourceSha256={reference.source_sha256} onPageChange={onPageChange}
      />,
    );
    expect(await screen.findByText("工作结构 · Host 已验证")).toBeVisible();
    expect(screen.getByRole("region", { name: "Agent 下一步" })).toHaveTextContent("当前为局部结果");
    expect(screen.getAllByRole("tree")).toHaveLength(1);
    expect(screen.queryByText(/工作结构基线与当前已接受结构不一致/)).not.toBeInTheDocument();
    expect(onPageChange).not.toHaveBeenCalled();
  });

  it("rejects a D2 quality claim without a matching compiler receipt", async () => {
    const revision = workingRevision({ d2_compiler_receipt_sha256: null });
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureWorkingRevisionHead").mockResolvedValue(headFromRevision(revision));
    vi.spyOn(api, "nativeStructureWorkingRevision").mockResolvedValue(revision);
    render(
      <NativeStructureReferenceDock
        projectId="project-1" documentId="document-1"
        sourceSha256={reference.source_sha256} onPageChange={vi.fn()}
      />,
    );
    expect(await screen.findByText(/原文结构的来源验证回执不一致/)).toBeVisible();
    expect(screen.queryByText("工作结构 · Host 已验证")).not.toBeInTheDocument();
  });

  it("uses explicit per-fragment headers and notes instead of assuming every first row is a header", () => {
    const preview = {
      ...tablePreview,
      fragments: [
        { ...tablePreview.fragments[0], source_header_regions: [], source_notes: ["本页原文表注"] },
        {
          ...tablePreview.fragments[0],
          fragment_id: "continuation-with-native-header", page: 11,
          source_header_regions: [{ row: 0, column: 0, row_span: 1, column_span: 2 }],
        },
      ],
    };
    const tables = nativeStructuredTables(preview, []);
    expect(tables[0].headerRegions).toEqual([]);
    expect(tables[0].cells.every((cell) => !cell.header)).toBe(true);
    expect(tables[0].notes).toEqual(["本页原文表注"]);
    expect(tables[1].cells.filter((cell) => cell.header)).toHaveLength(2);
    expect(tables[1].cells.filter((cell) => !cell.header)).toHaveLength(2);
  });

  it("labels transport replay without claiming an Agent re-parse or Host D2 verification", async () => {
    const replay = workingRevision({
      validator_kind: "ACCEPTED_REFERENCE_REPLAY",
      quality_claim: "TRANSPORT_REPLAY_ONLY",
    });
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureWorkingRevisionHead").mockResolvedValue(
      headFromRevision(replay),
    );
    vi.spyOn(api, "nativeStructureWorkingRevision").mockResolvedValue(replay);

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    expect(
      await screen.findAllByText("传输回放 · 非 Agent 重解析"),
    ).not.toHaveLength(0);
    expect(screen.getByRole("region", { name: "Agent 下一步" })).toHaveTextContent(
      "未重新调用 Agent",
    );
    expect(screen.queryByText("工作结构 · Host 已验证")).not.toBeInTheDocument();
  });

  it("keeps an independent handoff working projection authoritative over a D2 revision", async () => {
    const handoffReference: NativeStructureReferencePublicV1 = {
      ...reference,
      structure_projection: {
        ...reference.structure_projection,
        state: "WORKING_VERIFICATION_REQUIRED",
        attempt_id: "native-structure-handoff-attempt-coexist",
        attempt_ordinal: 1,
        affected_scope_ids: ["doc-main"],
        pending_rendered_scope_ids: ["doc-main"],
        validation_status: "FAIL",
      },
    };
    const revision = workingRevision({
      document_tree: {
        ...reference.document_tree,
        nodes: reference.document_tree.nodes.map((node) =>
          node.node_id === "doc-main"
            ? { ...node, title: "不应覆盖独立 handoff 的 D2 节点" }
            : node,
        ),
      },
    });
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(handoffReference);
    vi.spyOn(api, "nativeStructureWorkingRevisionHead").mockResolvedValue(
      headFromRevision(revision),
    );
    vi.spyOn(api, "nativeStructureWorkingRevision").mockResolvedValue(revision);

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    expect(await screen.findByText("工作投影 · 待原文复验")).toBeVisible();
    expect(
      screen.queryByText("不应覆盖独立 handoff 的 D2 节点"),
    ).not.toBeInTheDocument();
    expect(screen.getAllByRole("tree")).toHaveLength(1);
  });

  it("keeps the lightweight bootstrap alive until a later initial D2 head appears", async () => {
    vi.useFakeTimers();
    vi.spyOn(api, "nativeStructureReference").mockRejectedValue(
      new ApiError("not ready", 404, null),
    );
    const emptyHead: NativeStructureWorkingRevisionHeadResponseV1 = {
      schema_version: "native-structure-working-revision-head-response/v1",
      source_sha256: reference.source_sha256,
      head: null,
    };
    const headRequest = vi
      .spyOn(api, "nativeStructureWorkingRevisionHead")
      .mockResolvedValueOnce(emptyHead)
      .mockResolvedValue(workingHead());
    vi.spyOn(api, "nativeStructureWorkingRevision").mockResolvedValue(
      workingRevision(),
    );

    const view = render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );
    try {
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(headRequest).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole("tree")).not.toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(3_000);
      });
      expect(headRequest).toHaveBeenCalledTimes(2);
      expect(screen.getByRole("tree", { name: "原文标题层级" })).toBeVisible();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("uses revision-bound table support while a working revision is active", async () => {
    const workingTable = {
      ...reference.logical_tables[0],
      scope_id: "lt-working",
      title: "工作修订试验表",
      owner_unit_id: "unit-main",
      pages: [10],
      fragments: [{ table_token: "T-WORK", page: 10, row_range: [0, 2] }] as [
        { table_token: string; page: number; row_range: [number, number] },
      ],
    };
    const rootWithTable = {
      ...reference.document_tree.nodes[0],
      content_blocks: [
        { kind: "LOGICAL_TABLE" as const, logical_table_scope_id: "lt-working" },
      ],
    };
    const workingPreview = {
      ...tablePreview,
      logical_table_scope_id: "lt-working",
      title: "工作修订试验表",
    };
    const acceptedReplayRevision = workingRevision({
        validator_kind: "ACCEPTED_REFERENCE_REPLAY",
        quality_claim: "TRANSPORT_REPLAY_ONLY",
        state: "ACCEPTED",
        document_tree: {
          ...reference.document_tree,
          nodes: [rootWithTable, ...reference.document_tree.nodes.slice(1)],
        },
        node_states: Object.fromEntries(
          reference.document_tree.nodes.map((node) => [node.node_id, "ACCEPTED"]),
        ),
        changed_node_ids: [],
        logical_tables: [workingTable],
        table_preview_bindings: [
          {
            ...workingRevision().table_preview_bindings[0],
            scope_id: "lt-working",
            preview_sha256: canonicalJsonSha256(workingPreview),
          },
        ],
      });
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureWorkingRevisionHead").mockResolvedValue(
      headFromRevision(acceptedReplayRevision),
    );
    vi.spyOn(api, "nativeStructureWorkingRevision").mockResolvedValue(
      acceptedReplayRevision,
    );
    const revisionPreview = vi
      .spyOn(api, "nativeStructureWorkingRevisionTablePreview")
      .mockResolvedValue(workingPreview);
    const acceptedPreview = vi.spyOn(api, "nativeStructureTablePreview");

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    await waitFor(() =>
      expect(revisionPreview).toHaveBeenCalledWith(
        "project-1",
        "document-1",
        workingRunId,
        1,
        "lt-working",
        "c".repeat(64),
        expect.any(AbortSignal),
      ),
    );
    expect(await screen.findByText("外观检查")).toBeVisible();
    expect(acceptedPreview).not.toHaveBeenCalled();
  });

  it("fails closed with an object-level recovery message when D2 table evidence is not materialized", async () => {
    const rootWithTable = {
      ...reference.document_tree.nodes[0],
      content_blocks: [
        { kind: "LOGICAL_TABLE" as const, logical_table_scope_id: "lt-front" },
      ],
    };
    const revision = workingRevision({
      document_tree: {
        ...reference.document_tree,
        nodes: [rootWithTable, ...reference.document_tree.nodes.slice(1)],
      },
      logical_tables: reference.logical_tables,
    });
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureWorkingRevisionHead").mockResolvedValue(
      headFromRevision(revision),
    );
    vi.spyOn(api, "nativeStructureWorkingRevision").mockResolvedValue(revision);
    vi.spyOn(
      api,
      "nativeStructureWorkingRevisionTablePreview",
    ).mockRejectedValue(
      new ApiError("not materialized", 409, {
        detail: {
          code: "working_revision_table_evidence_not_materialized",
          message: "This working revision has no immutable table evidence projection yet.",
        },
      }),
    );
    const acceptedPreview = vi.spyOn(api, "nativeStructureTablePreview");

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    expect(
      await screen.findByText(/该工作节点的表格证据尚未完成不可变物化/),
    ).toBeVisible();
    expect(screen.getByText(/不会回退到旧表格/)).toBeVisible();
    expect(acceptedPreview).not.toHaveBeenCalled();
  });

  it("keeps D3 on the accepted ledger tree and does not fetch D2 working revisions", async () => {
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "sourceRequirementBaselineOutline").mockResolvedValue(
      sourceRequirementOutline,
    );
    vi.spyOn(api, "sourceRequirementRecord").mockResolvedValue(
      sourceRequirementDetail,
    );
    vi.spyOn(api, "nativeStructureTablePreview").mockResolvedValue(tablePreview);
    const headRequest = vi.spyOn(
      api,
      "nativeStructureWorkingRevisionHead",
    );
    const revisionRequest = vi.spyOn(api, "nativeStructureWorkingRevision");

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
        mode="SOURCE_REQUIREMENTS"
      />,
    );

    expect(
      await screen.findByRole("tree", { name: "原文层级与条目" }),
    ).toBeVisible();
    expect(headRequest).not.toHaveBeenCalled();
    expect(revisionRequest).not.toHaveBeenCalled();
    expect(screen.queryByText("工作结构 · Host 已验证")).not.toBeInTheDocument();
  });

  it("separates table records from other source items in table-structure counts", async () => {
    const user = userEvent.setup();
    const textItemId = `source-record-${"8".repeat(32)}`;
    const textRecord: SourceRequirementBaselineOutlineV1["records"][number] = {
      ...sourceRequirementOutline.records[0],
      source_item_id: textItemId,
      source_record_id: textItemId,
      source_display_id: "SRC-0002",
      requirement_display_id: null,
      source_order: 1,
      logical_table_scope_id: "",
      label: "项目：YX2516",
      display_label: "项目：YX2516",
      source_form: "NATIVE_LIST",
      requirementness: "NON_REQUIREMENT",
      requirement_kinds: [],
      non_requirement_role: "METADATA",
      decision_summary: "该条用于标识当前项目。",
    };
    const mixedOutline: SourceRequirementBaselineOutlineV1 = {
      ...sourceRequirementOutline,
      summary: {
        ...sourceRequirementOutline.summary,
        record_count: 2,
        source_item_count: 2,
        non_requirement_count: 1,
      },
      records: [...sourceRequirementOutline.records, textRecord],
      ledger: {
        ...sourceRequirementOutline.ledger,
        rows: [
          ...sourceRequirementOutline.ledger.rows.map((row) =>
            row.row_id === "section-3.2.1"
              ? {
                  ...row,
                  child_row_ids: [...row.child_row_ids, textItemId],
                  direct_source_item_count: 2,
                  non_requirement_count: 1,
                }
              : row,
          ),
          {
            row_id: textItemId,
            parent_row_id: "section-3.2.1",
            child_row_ids: [],
            row_role: "SOURCE_ITEM",
            structure_node_id: "section-3.2.1",
            source_item_id: textItemId,
            logical_table_scope_ids: [],
            is_table_structure: false,
            is_one_to_one_item: false,
            processing_state: "CLASSIFIED",
            direct_source_item_count: 1,
            requirement_count: 0,
            non_requirement_count: 1,
          },
        ],
      },
    };
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "sourceRequirementBaselineOutline").mockResolvedValue(
      mixedOutline,
    );
    vi.spyOn(api, "nativeStructureTablePreview").mockResolvedValue(tablePreview);

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
        mode="SOURCE_REQUIREMENTS"
      />,
    );

    await user.click(await screen.findByRole("button", { name: "全部展开" }));
    const tableNode = screen.getByRole("button", {
      name: /^3\.2\.1 产品信息P/,
    });
    expect(tableNode).toHaveTextContent("表格条目 1");
    expect(tableNode).toHaveTextContent("其他原文 1");
    expect(tableNode).not.toHaveTextContent("原表 2 条");
  });

  it("fetches only the current full head when a fresh run is already at revision three", async () => {
    const revision1 = sequencedWorkingRevision(1, "1".repeat(64), null);
    const revision2 = sequencedWorkingRevision(
      2,
      "2".repeat(64),
      revision1.revision_sha256,
    );
    const finalRoot = {
      ...reference.document_tree.nodes[0],
      title: "产品设计规格说明书（第三次工作修订）",
    };
    const revision3 = sequencedWorkingRevision(
      3,
      "3".repeat(64),
      revision2.revision_sha256,
      {
        document_tree: {
          ...reference.document_tree,
          nodes: [finalRoot, ...reference.document_tree.nodes.slice(1)],
        },
        changed_node_ids: ["doc-main"],
      },
    );
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureWorkingRevisionHead").mockResolvedValue(
      headFromRevision(revision3),
    );
    const revisionRequest = vi
      .spyOn(api, "nativeStructureWorkingRevision")
      .mockImplementation(
        async (_project, _document, _run, revisionNumber) =>
          ({ 1: revision1, 2: revision2, 3: revision3 })[
            revisionNumber as 1 | 2 | 3
          ],
      );

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    expect(
      await screen.findByRole("treeitem", {
        name: /产品设计规格说明书（第三次工作修订）/,
      }),
    ).toBeVisible();
    expect(revisionRequest).toHaveBeenCalledTimes(1);
    expect(revisionRequest.mock.calls.map((call) => call[3])).toEqual([3]);
    expect(revisionRequest.mock.calls.map((call) => call[4])).toEqual([
      revision3.revision_sha256,
    ]);
    expect(screen.getAllByRole("tree")).toHaveLength(1);
  });

  it("validates revision-one diffs from an empty lineage while reconciling UI from the accepted tree", async () => {
    const firstNodes = reference.document_tree.nodes.slice(0, 2);
    const secondNodes = reference.document_tree.nodes.slice(0, 4);
    const revision1 = sequencedWorkingRevision(1, "6".repeat(64), null, {
      document_tree: {
        ...reference.document_tree,
        root_ids: ["doc-main"],
        nodes: firstNodes,
      },
      node_states: Object.fromEntries(
        firstNodes.map((node) => [node.node_id, "WORKING_VERIFIED"]),
      ),
      inserted_node_ids: firstNodes.map((node) => node.node_id),
    });
    const revision2 = sequencedWorkingRevision(
      2,
      "7".repeat(64),
      revision1.revision_sha256,
      {
        document_tree: {
          ...reference.document_tree,
          root_ids: ["doc-main"],
          nodes: secondNodes,
        },
        node_states: Object.fromEntries(
          secondNodes.map((node) => [node.node_id, "WORKING_VERIFIED"]),
        ),
        inserted_node_ids: secondNodes.slice(2).map((node) => node.node_id),
      },
    );
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureWorkingRevisionHead").mockResolvedValue(
      headFromRevision(revision2),
    );
    const revisionRequest = vi
      .spyOn(api, "nativeStructureWorkingRevision")
      .mockImplementation(
        async (_project, _document, _run, revisionNumber) =>
          revisionNumber === 1 ? revision1 : revision2,
      );

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    expect(await screen.findByText(/4 个层级/)).toBeVisible();
    expect(revisionRequest.mock.calls.map((call) => call[3])).toEqual([2]);
    expect(screen.queryByText(/工作结构变更摘要与完整投影不一致/)).not.toBeInTheDocument();
  });

  it("serializes a newer head behind an in-flight predecessor without duplicating or regressing", async () => {
    vi.useFakeTimers();
    const firstRoot = {
      ...reference.document_tree.nodes[0],
      title: "第一版工作结构",
    };
    const revision1 = sequencedWorkingRevision(1, "1".repeat(64), null, {
      document_tree: {
        ...reference.document_tree,
        nodes: [firstRoot, ...reference.document_tree.nodes.slice(1)],
      },
      changed_node_ids: [],
    });
    const secondRoot = { ...firstRoot, title: "第二版工作结构" };
    const revision2 = sequencedWorkingRevision(
      2,
      "2".repeat(64),
      revision1.revision_sha256,
      {
        document_tree: {
          ...reference.document_tree,
          nodes: [secondRoot, ...reference.document_tree.nodes.slice(1)],
        },
        changed_node_ids: ["doc-main"],
      },
    );
    let resolveFirst!: (value: NativeStructureWorkingRevisionV1) => void;
    const firstPromise = new Promise<NativeStructureWorkingRevisionV1>(
      (resolve) => {
        resolveFirst = resolve;
      },
    );
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureWorkingRevisionHead")
      .mockResolvedValueOnce(headFromRevision(revision1))
      .mockResolvedValue(headFromRevision(revision2));
    const revisionRequest = vi
      .spyOn(api, "nativeStructureWorkingRevision")
      .mockImplementation(
        async (_project, _document, _run, revisionNumber) =>
          revisionNumber === 1 ? firstPromise : revision2,
      );

    const view = render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );
    try {
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await vi.advanceTimersByTimeAsync(1_500);
      });
      expect(revisionRequest).toHaveBeenCalledTimes(1);
      await act(async () => resolveFirst(revision1));
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(revisionRequest.mock.calls.map((call) => call[3])).toEqual([2]);
      expect(
        screen.getByRole("treeitem", { name: /第二版工作结构/ }),
      ).toBeVisible();
      expect(screen.queryByText("第一版工作结构")).not.toBeInTheDocument();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("retries a rejected revision with a bounded retry and never exposes the invalid tree", async () => {
    vi.useFakeTimers();
    const validRoot = {
      ...reference.document_tree.nodes[0],
      title: "重试后有效结构",
    };
    const validRevision = workingRevision({
      document_tree: {
        ...reference.document_tree,
        nodes: [validRoot, ...reference.document_tree.nodes.slice(1)],
      },
      changed_node_ids: [],
    });
    const invalidRevision = {
      ...validRevision,
      source_sha256: "f".repeat(64),
      document_tree: {
        ...validRevision.document_tree,
        nodes: validRevision.document_tree.nodes.map((node) =>
          node.node_id === "doc-main"
            ? { ...node, title: "绝不能显示的错误结构" }
            : node,
        ),
      },
    };
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureWorkingRevisionHead").mockResolvedValue(
      workingHead(),
    );
    const revisionRequest = vi
      .spyOn(api, "nativeStructureWorkingRevision")
      .mockResolvedValueOnce(invalidRevision)
      .mockResolvedValue(validRevision);

    const view = render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );
    try {
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(screen.queryByText("绝不能显示的错误结构")).not.toBeInTheDocument();
      expect(revisionRequest).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
      expect(revisionRequest).toHaveBeenCalledTimes(2);
      expect(
        screen.getByRole("treeitem", { name: /重试后有效结构/ }),
      ).toBeVisible();
      expect(screen.queryByText("绝不能显示的错误结构")).not.toBeInTheDocument();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("keeps the accepted final revision active and stops working-head polling", async () => {
    vi.useFakeTimers();
    const revision1 = workingRevision();
    const acceptedRoot = {
      ...reference.document_tree.nodes[0],
      title: "已经接受的最终结构",
    };
    const revision2 = sequencedWorkingRevision(
      2,
      "e".repeat(64),
      revision1.revision_sha256,
      {
        state: "ACCEPTED",
        document_tree: {
          ...reference.document_tree,
          nodes: [acceptedRoot, ...reference.document_tree.nodes.slice(1)],
        },
        node_states: Object.fromEntries(
          reference.document_tree.nodes.map((node) => [node.node_id, "ACCEPTED"]),
        ),
        changed_node_ids: ["doc-main"],
      },
    );
    const headRequest = vi
      .spyOn(api, "nativeStructureWorkingRevisionHead")
      .mockResolvedValueOnce(headFromRevision(revision1))
      .mockResolvedValue(headFromRevision(revision2));
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureWorkingRevision").mockImplementation(
      async (_project, _document, _run, revisionNumber) =>
        revisionNumber === 1 ? revision1 : revision2,
    );

    const view = render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );
    try {
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(screen.getAllByText("Host 已验证工作结构").length).toBeGreaterThan(0);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500);
      });
      expect(
        screen.getByRole("treeitem", { name: /已经接受的最终结构/ }),
      ).toBeVisible();
      expect(screen.queryByText("工作结构 · Host 已验证")).not.toBeInTheDocument();
      expect(screen.queryByText("正在确认")).not.toBeInTheDocument();
      const callsAfterAccepted = headRequest.mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(headRequest).toHaveBeenCalledTimes(callsAfterAccepted);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("restores the actual tree viewport scroll anchor and keyboard focus after a partial apply", async () => {
    const insertedNode = {
      ...reference.document_tree.nodes[1],
      node_id: "section-before-current-anchor",
      parent_id: "doc-main",
      order: 0,
      label: "2",
      title: "新插入的前置章节",
      pages: [2],
      content_blocks: [],
      finding_codes: [],
      source_anchors: [],
      source_content: [],
    };
    const movedSection = { ...reference.document_tree.nodes[1], order: 1 };
    const revision = workingRevision({
      document_tree: {
        ...reference.document_tree,
        nodes: [
          reference.document_tree.nodes[0],
          insertedNode,
          movedSection,
          ...reference.document_tree.nodes.slice(2),
        ],
      },
      node_states: {
        ...Object.fromEntries(
          reference.document_tree.nodes.map((node) => [
            node.node_id,
            "WORKING_VERIFIED",
          ]),
        ),
        [insertedNode.node_id]: "WORKING_VERIFIED",
      },
      inserted_node_ids: [
        ...reference.document_tree.nodes.map((node) => node.node_id),
        insertedNode.node_id,
      ],
      changed_node_ids: [],
    });
    let resolveRevision!: (value: NativeStructureWorkingRevisionV1) => void;
    const deferredRevision = new Promise<NativeStructureWorkingRevisionV1>(
      (resolve) => {
        resolveRevision = resolve;
      },
    );
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureWorkingRevisionHead").mockResolvedValue(
      workingHead(),
    );
    vi.spyOn(api, "nativeStructureWorkingRevision").mockReturnValue(
      deferredRevision,
    );
    const onPageChange = vi.fn();
    const rectSpy = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: HTMLElement) {
        if (this.classList.contains("native-hierarchy-tree")) {
          return new DOMRect(0, 0, 320, 400);
        }
        const nodeId = this.getAttribute("data-tree-node");
        if (nodeId === "doc-main") return new DOMRect(0, -80, 200, 40);
        if (nodeId === "section-3") {
          return new DOMRect(
            0,
            document.querySelector(
              '[data-tree-node="section-before-current-anchor"]',
            )
              ? 90
              : 40,
            200,
            40,
          );
        }
        if (nodeId) return new DOMRect(0, 180, 200, 40);
        return new DOMRect(0, 0, 0, 0);
      });

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        activePage={8}
        onPageChange={onPageChange}
      />,
    );
    const viewport = await screen.findByRole("navigation", {
      name: "文档标题树",
    });
    viewport.scrollTop = 100;
    const focusedButton = viewport.querySelector<HTMLElement>(
      '[data-tree-node="section-3"]',
    )!;
    focusedButton.focus();
    expect(focusedButton).toHaveFocus();
    onPageChange.mockClear();

    await act(async () => resolveRevision(revision));
    await waitFor(() =>
      expect(
        screen.getByRole("treeitem", { name: /新插入的前置章节/ }),
      ).toBeVisible(),
    );
    expect(viewport.scrollTop).toBe(150);
    expect(
      viewport.querySelector<HTMLElement>('[data-tree-node="section-3"]'),
    ).toHaveFocus();
    expect(onPageChange).not.toHaveBeenCalled();
    rectSpy.mockRestore();
  });

  it("marks only affected rows when the public tree is a working projection", async () => {
    const workingReference: NativeStructureReferencePublicV1 = {
      ...reference,
      structure_projection: {
        ...reference.structure_projection,
        state: "WORKING_VERIFICATION_REQUIRED",
        attempt_id: "native-structure-handoff-attempt-working",
        attempt_ordinal: 2,
        affected_scope_ids: ["doc-main"],
        pending_rendered_scope_ids: ["doc-main"],
        validation_status: "FAIL",
      },
    };
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(workingReference);

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    expect(
      await screen.findByText("工作投影 · 待原文复验"),
    ).toBeVisible();
    const tree = screen.getByRole("tree", { name: "原文标题层级" });
    expect(screen.getAllByRole("tree")).toHaveLength(1);
    expect(
      within(tree).getByRole("treeitem", {
        name: /产品设计规格说明书（PDS）/,
      }),
    ).toHaveTextContent("正在确认");
  });

  it("does not show working projection markers for an accepted tree", async () => {
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    await screen.findByText("Agent 原文结构草案");
    expect(
      screen.queryByText("工作投影 · 待原文复验"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("正在确认")).not.toBeInTheDocument();
  });

  it("clears stale Agent page focus after the continuation is terminal", () => {
    const activity: NativeStructureActivityEventV2 = {
      schema_version: "reqpilotx-agent-activity-event/v2",
      event_id: "event-1",
      run_id: "run-1",
      source_sha256: reference.source_sha256,
      sequence: 1,
      occurred_at_utc: "2026-08-31T08:00:00Z",
      actor: "AGENT",
      family: "structure",
      kind: "SCOPE_REVIEWED",
      stage: "D2_LOGICAL_STRUCTURE",
      scope: {
        kind: "PAGE_RANGE",
        ref_id: "P12-P13",
        page_numbers: [12, 13],
      },
      visibility: "PRIMARY",
      display: { title: "检查跨页记录", summary: "正在检查P12至P13。" },
      payload: {
        status: "IN_PROGRESS",
        outcome_code: "",
        authorized_parameters: {},
      },
    };

    expect(activeNativeStructureFocusPages("IN_PROGRESS", [activity])).toEqual([
      12,
      13,
    ]);
    expect(activeNativeStructureFocusPages("CONSUMED", [activity])).toEqual([]);
  });

  it("keeps stable tree UI state and falls back to the nearest surviving ancestor", () => {
    const nextTree = {
      ...reference.document_tree,
      nodes: reference.document_tree.nodes.filter(
        (node) => node.node_id !== "section-3.2.1",
      ),
    };

    const reconciled = reconcileNativeTreeUiState({
      previousNodes: reference.document_tree.nodes,
      nextTree,
      selectedNodeId: "section-3.2.1",
      expandedIds: new Set([
        "doc-main",
        "section-3",
        "section-3.2",
        "section-3.2.1",
        "retired-node",
      ]),
    });

    expect(reconciled.selectedNodeId).toBe("section-3.2");
    expect([...reconciled.expandedIds]).toEqual([
      "doc-main",
      "section-3",
      "section-3.2",
    ]);
  });

  it("prefers a declared successor before falling back to an ancestor", () => {
    const successor = {
      ...reference.document_tree.nodes[3],
      node_id: "section-3.2.1-successor",
      title: "产品信息（修订）",
    };
    const nextTree = {
      ...reference.document_tree,
      nodes: [
        ...reference.document_tree.nodes.filter(
          (node) => node.node_id !== "section-3.2.1",
        ),
        successor,
      ],
    };

    const reconciled = reconcileNativeTreeUiState({
      previousNodes: reference.document_tree.nodes,
      nextTree,
      selectedNodeId: "section-3.2.1",
      expandedIds: new Set(["doc-main", "section-3.2.1"]),
      successorNodeIds: {
        "section-3.2.1": [successor.node_id],
      },
    });

    expect(reconciled.selectedNodeId).toBe(successor.node_id);
    expect(reconciled.expandedIds).toContain(successor.node_id);
    expect(reconciled.expandedIds).toContain("doc-main");
  });

  it("grows source items under the original table hierarchy", async () => {
    const user = userEvent.setup();
    const onPageChange = vi.fn();
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureTablePreview").mockResolvedValue(tablePreview);
    vi.spyOn(api, "sourceRequirementBaselineOutline").mockResolvedValue(
      sourceRequirementOutline,
    );
    vi.spyOn(api, "sourceRequirementRecord").mockResolvedValue(
      sourceRequirementDetail,
    );
    const submitFeedback = vi
      .spyOn(api, "submitSourceRequirementFeedback")
      .mockResolvedValue({
        schema_version: "source-requirement-feedback-result/v1",
        feedback: { feedback_id: "feedback-table-1", message: "该试验应保持为完整业务记录。", status: "PENDING_AGENT", created_at_utc: "2026-09-08T00:00:00Z" },
        delivery_message: "指引已绑定当前原文需求记录。",
      });

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={onPageChange}
        sourceFileUrl="/api/document/file"
        activePage={10}
        pageCount={29}
        showSourcePreview
        mode="SOURCE_REQUIREMENTS"
      />,
    );

    expect(await screen.findByRole("region", { name: "Agent 原文需求清单" })).toBeVisible();
    expect(await screen.findByRole("tree", { name: "原文层级与条目" })).toBeVisible();
    const sourceItemButton = await screen.findByRole("button", {
      name: /1 外观检查/,
    });
    expect(sourceItemButton).toHaveTextContent("REQ-0001");
    expect(sourceItemButton).toHaveTextContent("需求");
    await user.click(sourceItemButton);
    await user.click(await screen.findByText("查看分类与边界依据"));
    expect(screen.getByText("编号1及其完整表格行共同形成一条可独立审核的试验要求。")).toBeVisible();
    expect(screen.getByText("需求", { selector: ".source-review-facets span" })).toBeVisible();
    expect(screen.getByText("质量", { selector: ".source-review-facets span" })).toBeVisible();
    expect(screen.queryByText(/A ·|B ·|F ·/)).not.toBeInTheDocument();
    expect(screen.getByRole("table")).toHaveTextContent("外观检查");
    expect(screen.getByRole("table")).toHaveTextContent("序号");
    expect(screen.getByRole("table")).toHaveTextContent("试验项目");
    const sharedContext = screen.getByText(/共用条件与上下文 · 1 段/);
    await user.click(sharedContext);
    expect(sharedContext.closest("details")?.querySelector(":scope > article > div > p")?.textContent).toBe(sourceRequirementDetail.record.shared_source_content[0].text);
    expect(screen.queryByText("Document Map")).not.toBeInTheDocument();
    expect(screen.queryByText("依据代码")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^(提出修改|继续写反馈)$/ }));
    await user.type(screen.getByRole("textbox", { name: /提出修改/ }), "该试验应保持为完整业务记录。");
    await user.click(screen.getByRole("button", { name: "保存反馈" }));
    await waitFor(() => expect(submitFeedback).toHaveBeenCalledTimes(1));
    expect(submitFeedback.mock.calls[0][2]).toMatchObject({
      source_record_id: sourceRequirementOutline.records[0].source_record_id,
      message: "该试验应保持为完整业务记录。",
    });
  });

  it.each([false, true])("preserves local direct-text growth and disables missing-Kit SaaS mode (%s)", async saas => {
    if (saas) setSaasAccountIdentity("account-a");
    const user = userEvent.setup();
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureTablePreview").mockResolvedValue(tablePreview);
    vi.spyOn(api, "sourceRequirementBaselineOutline").mockResolvedValue(
      sourceRequirementOutline,
    );
    const idleTextRun = {
      schema_version: "source-requirement-baseline-run-status/v1",
      status: "IDLE",
      result_ready: false,
      job: null,
      message: "尚未启动当前原文范围的渐进提取。",
    } as const;
    const pendingTextJob = {
      schema_version: "source-requirement-baseline-job-public/v1",
      job_id: "job-text-1",
      status: "PENDING",
      generation: 0,
      scope_id: "text:section-3.2.1",
      progress: {
        stage: "",
        message: "Agent 正在准备当前直接正文。",
        strategy: "TEXT_BATCH",
        completed_window_count: 0,
        total_window_count: 1,
      },
      result: null,
      error_code: "",
    } as const;
    vi.spyOn(api, "sourceRequirementBaselineRunStatus")
      .mockResolvedValueOnce(idleTextRun)
      .mockResolvedValue({
        schema_version: "source-requirement-baseline-run-status/v1",
        status: "PENDING",
        result_ready: false,
        job: pendingTextJob,
        message: pendingTextJob.progress.message,
      });
    const startText = vi
      .spyOn(api, "startSourceItemTextBaselineRun")
      .mockResolvedValue(pendingTextJob);

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
        mode="SOURCE_REQUIREMENTS"
      />,
    );

    await user.click(await screen.findByRole("button", { name: "全部展开" }));
    await user.click(
      screen.getByRole("button", { name: /^3.2.1 产品信息P/ }),
    );
    const start = await screen.findByRole("button", {
      name: "让 Agent 处理当前正文",
    });
    if (saas) {
      expect(start).toBeDisabled();
      expect(start).toHaveAttribute("title", SAAS_KIT_UNAVAILABLE_MESSAGE);
      expect(screen.getByText(SAAS_KIT_UNAVAILABLE_MESSAGE)).toBeVisible();
      await user.click(start);
      expect(startText).not.toHaveBeenCalled();
      return;
    }
    await user.click(start);

    expect(startText).toHaveBeenCalledWith(
      "project-1",
      "document-1",
      expect.objectContaining({
        schema_version: "source-item-text-baseline-start-request/v1",
        expected_source_sha256: reference.source_sha256,
        expected_artifact_id: reference.artifact_id,
        node_ids: ["section-3.2.1"],
      }),
    );
    expect(
      await screen.findByText("Agent 正在处理当前直接正文"),
    ).toBeVisible();
  });

  it("saves direct-text feedback and immediately queues an affected-only revision", async () => {
    const user = userEvent.setup();
    const feedbackId = `source-requirement-feedback-${"f".repeat(32)}`;
    const textOutline: SourceRequirementBaselineOutlineV1 = {
      ...sourceRequirementOutline,
      baseline: {
        ...sourceRequirementOutline.baseline,
        covered_scope_ids: ["text:section-3.2.1"],
      },
      records: [
        {
          ...sourceRequirementOutline.records[0],
          logical_table_scope_id: "",
          source_form: "NUMBERED_CLAUSE",
        },
      ],
    };
    const textDetail: SourceRequirementRecordDetailV1 = {
      ...sourceRequirementDetail,
      record: {
        ...textOutline.records[0],
        document_map_artifact_id: textOutline.document_map.artifact_id,
        document_map_artifact_sha256: textOutline.document_map.artifact_sha256,
        table_fragments: [],
        text_bindings: [
          {
            content_id: "content-section-3-2-1",
            page: 2,
            start_offset: 0,
            end_offset: 12,
            text: "供应商应提交验证报告。",
            text_sha256: "a".repeat(64),
            bbox: [40, 100, 420, 126],
            source_bounds: [0, 0, 595, 842],
            precision: "EXACT_NATIVE_SPAN_RANGE",
          },
        ],
        shared_header_cell_ids: [],
        shared_table_cell_ids: [],
        shared_source_content_ids: [],
        shared_source_content: [],
        internal_diagnostics_available: true,
        feedbacks: [],
      },
    };
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureTablePreview").mockResolvedValue(tablePreview);
    vi.spyOn(api, "sourceRequirementBaselineOutline").mockResolvedValue(
      textOutline,
    );
    vi.spyOn(api, "sourceRequirementRecord").mockResolvedValue(textDetail);
    vi.spyOn(api, "submitSourceRequirementFeedback").mockResolvedValue({
      schema_version: "source-requirement-feedback-result/v1",
      feedback: {
        feedback_id: feedbackId,
        message: "请重新判断本条完整边界。",
        status: "PENDING_AGENT",
        created_at_utc: "2026-09-02T00:00:00Z",
      },
      delivery_message: "指引已保存。",
    });
    const startRevision = vi
      .spyOn(api, "startSourceItemScopeRevisionRun")
      .mockResolvedValue({
        schema_version: "source-requirement-baseline-job-public/v1",
        job_id: "job-scope-revision-1",
        status: "PENDING",
        generation: 0,
        scope_id: "text:section-3.2.1",
        scope_ids: ["text:section-3.2.1"],
        progress: {
          stage: "",
          message: "Agent 局部修订任务已排队。",
          strategy: "TEXT_SCOPE_REVISION",
          completed_window_count: 0,
          total_window_count: 0,
        },
        result: null,
        error_code: "",
      });

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
        mode="SOURCE_REQUIREMENTS"
      />,
    );

    const sourceItemButton = await screen.findByRole("button", {
      name: /1 外观检查/,
    });
    await user.click(sourceItemButton);
    await user.click(await screen.findByRole("button", { name: /^(提出修改|继续写反馈)$/ }));
    expect(screen.getByRole("button", { name: "让 Agent 复核本条" })).toBeEnabled();
    await user.type(
      await screen.findByRole("textbox", { name: /提出修改/ }),
      "请重新判断本条完整边界。",
    );
    await user.click(
      screen.getByRole("button", { name: "保存并交给 Agent" }),
    );

    await waitFor(() => expect(startRevision).toHaveBeenCalledTimes(1));
    expect(startRevision.mock.calls[0][2]).toMatchObject({
      expected_baseline_artifact_id: textOutline.baseline.artifact_id,
      expected_baseline_revision: textOutline.baseline.revision,
      affected_scope_ids: ["text:section-3.2.1"],
      comparison_scope_ids: [],
      feedback_ids: [feedbackId],
    });
    expect(
      await within(screen.getByRole("region", { name: "本条反馈" })).findByRole("status"),
    ).toHaveTextContent("Agent 局部修订任务已排队。");
  });

  it("shows durable boundary-window progress without exposing hidden reasoning", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureTablePreview").mockResolvedValue(tablePreview);
    vi.spyOn(api, "sourceRequirementBaselineOutline").mockResolvedValue(
      sourceRequirementOutline,
    );
    vi.spyOn(api, "sourceRequirementRecord").mockResolvedValue(
      sourceRequirementDetail,
    );
    vi.spyOn(api, "sourceRequirementBaselineRunStatus").mockResolvedValue({
      schema_version: "source-requirement-baseline-run-status/v1",
      status: "RUNNING",
      result_ready: false,
      job: {
        schema_version: "source-requirement-baseline-job-public/v1",
        job_id: "job-source-requirement-1",
        status: "RUNNING",
        generation: 1,
        scope_id: "lt-front",
        progress: {
          stage: "CLASSIFICATION_WINDOW_CHECKPOINTED",
          message: "已保存 1/2 个分类窗口。",
          strategy: "BOUNDARY_WINDOWS",
          completed_window_count: 1,
          total_window_count: 2,
        },
        result: null,
        error_code: "",
      },
      message: "已保存 1/2 个分类窗口。",
    } satisfies SourceRequirementBaselineRunStatusV1);

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
        mode="SOURCE_REQUIREMENTS"
      />,
    );

    const liveStatus = await screen.findByLabelText("Agent 下一步");
    expect(liveStatus).toHaveTextContent("Agent 正在生长原文条目");
    expect(liveStatus).toHaveTextContent("已保存 1/2 个分类窗口。");
    const traceToggle = screen.getByText("运行轨迹");
    await user.click(traceToggle);
    const trace = traceToggle.closest("details");
    expect(trace).not.toBeNull();
    expect(
      screen.getByText("正在识别业务记录并逐窗判断"),
    ).toBeVisible();
    expect(within(trace!).getByText(/1\/2 个窗口已验证/)).toBeVisible();
    expect(screen.getByText(/不包含模型隐藏思维链/)).toBeVisible();
  });

  it("shows a one-to-one clause once with both hierarchy and item identity", async () => {
    const user = userEvent.setup();
    const fusedRecord = {
      ...sourceRequirementOutline.records[0],
      node_id: "doc-groups",
      logical_table_scope_id: "",
      label: "测试分组与测试顺序矩阵",
      display_label: "测试分组与测试顺序矩阵",
      source_form: "NUMBERED_CLAUSE" as const,
      pages: [24],
    };
    const fusedOutline: SourceRequirementBaselineOutlineV1 = {
      ...sourceRequirementOutline,
      records: [fusedRecord],
      ledger: {
        ...sourceRequirementOutline.ledger,
        rows: sourceRequirementOutline.ledger.rows
          .filter((row) => row.row_id !== sourceItemId)
          .map((row) => {
            if (row.row_id === "section-3.2.1") {
              return {
                ...row,
                child_row_ids: row.child_row_ids.filter(
                  (rowId) => rowId !== sourceItemId,
                ),
                direct_source_item_count: 0,
                requirement_count: 0,
                processing_state: "STRUCTURE_ONLY" as const,
              };
            }
            if (row.row_id === "doc-groups") {
              return {
                ...row,
                source_item_id: sourceItemId,
                is_one_to_one_item: true,
                direct_source_item_count: 1,
                requirement_count: 1,
                processing_state: "ITEMS_READY" as const,
              };
            }
            return row;
          }),
      },
    };
    const fusedDetail: SourceRequirementRecordDetailV1 = {
      ...sourceRequirementDetail,
      record: {
        ...sourceRequirementDetail.record,
        ...fusedRecord,
        table_fragments: [],
        text_bindings: [
          {
            content_id: "fused-source-item",
            page: 24,
            start_offset: 0,
            end_offset: 12,
            text: "测试分组与测试顺序矩阵",
            text_sha256: "5".repeat(64),
            bbox: [40, 80, 800, 120],
            source_bounds: [0, 0, 842, 595],
            precision: "EXACT_NATIVE_SPAN_RANGE",
          },
        ],
        shared_header_cell_ids: [],
        shared_table_cell_ids: [],
        shared_source_content_ids: [],
        shared_source_content: [],
      },
    };
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "sourceRequirementBaselineOutline").mockResolvedValue(
      fusedOutline,
    );
    vi.spyOn(api, "sourceRequirementRecord").mockResolvedValue(fusedDetail);

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
        activePage={24}
        pageCount={29}
        mode="SOURCE_REQUIREMENTS"
      />,
    );

    const row = await screen.findByRole("button", {
      name: /测试分组与测试顺序矩阵.*REQ-0001.*需求/,
    });
    expect(
      screen.getAllByRole("button", { name: /测试分组与测试顺序矩阵/ }),
    ).toHaveLength(1);
    await user.click(row);
    expect(await screen.findByText(/来源 SRC-0001/)).toBeVisible();
    expect(screen.getByRole("region", { name: "当前原文条目的逐字内容" })).toBeVisible();
    expect(screen.getByText("测试分组与测试顺序矩阵", { selector: "p" })).toBeVisible();
    expect(screen.queryByText("SOURCE_RECORD")).not.toBeInTheDocument();
  });

  it("shows the verified native candidate and locates a selected scope", async () => {
    const user = userEvent.setup();
    const onPageChange = vi.fn();
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureTablePreview").mockResolvedValue({
      ...tablePreview,
      evidence_recovery: {
        schema_version: "pdf-visual-table-recovery-receipt/v1",
        status: "RECOVERED",
        method: "NATIVE_WORDS_WITH_VECTOR_GRID",
        source_sha256: reference.source_sha256,
        logical_table_scope_id: tablePreview.logical_table_scope_id,
        pages: [10],
        fragment_sha256s: ["b".repeat(64)],
        receipt_sha256: "c".repeat(64),
      },
    });

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={onPageChange}
        sourceFileUrl="/api/document/file"
        activePage={10}
        pageCount={29}
        showSourcePreview
      />,
    );

    expect(
      await screen.findByText("Agent 原文结构草案"),
    ).toBeVisible();
    expect(screen.getByText(/已查看 29 页/)).toBeVisible();
    const progress = screen.getByRole("list", { name: "原文结构运行阶段" });
    expect(progress).toHaveTextContent("准备原文证据");
    expect(progress).toHaveTextContent("理解全文范围");
    expect(progress).toHaveTextContent("逐层生长结构");
    expect(progress).toHaveTextContent("复验来源覆盖");
    expect(screen.getByRole("tree", { name: "原文标题层级" })).toBeVisible();

    await user.click(
      screen.getByRole("button", { name: "展开 3 车型/产品信息" }),
    );
    await user.click(
      screen.getByRole("button", { name: "展开 3.2 产品技术要求及说明" }),
    );
    await user.click(
      screen.getByRole("button", { name: /3.2.1 产品信息/ }),
    );
    expect(
      screen.getByRole("region", { name: "节点直接原文内容" }),
    ).toBeVisible();
    expect(screen.getByRole("region", { name: "Agent 结构说明" })).toBeVisible();
    expect(screen.getByRole("region", { name: "节点直接原文内容" })).toHaveTextContent(
      "当前节点的原文内容",
    );
    expect(screen.getByRole("region", { name: "Agent 处理建议" })).toHaveTextContent(
      "1 张图片的父级归属",
    );
    expect(screen.getByText("已自动应用 · 待抽查")).toBeVisible();
    const tableRegion = await screen.findByRole("region", {
      name: "原文表格内容",
    });
    expect(within(tableRegion).getByRole("table")).toHaveTextContent("外观检查");
    expect(tableRegion).toHaveTextContent("2 行 × 2 列");
    expect(tableRegion).toHaveTextContent("PDF 原生表格已恢复为 2 行 × 2 列");
    expect(tableRegion).toHaveTextContent("当前页面与 Agent 共用这份经来源校验的单元格证据");
    expect(screen.queryByText(/T0009/)).not.toBeInTheDocument();
    expect(screen.queryByText("逻辑表")).not.toBeInTheDocument();
    expect(onPageChange).toHaveBeenCalledWith(10);
    expect(screen.getByRole("button", { name: "节点内容" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    const frame = screen.getByTitle("原文 PDF · 第 10 页") as HTMLIFrameElement;
    const sourceRegion = screen.getByLabelText("原文 PDF 对照");
    expect(sourceRegion).toHaveTextContent("原文 PDF");
    expect(sourceRegion).toHaveTextContent("2 个精确框");
    expect(sourceRegion).not.toHaveTextContent("P10 / 29");
    expect(
      within(sourceRegion).getByRole("link", {
        name: "在新窗口打开原文 PDF",
      }),
    ).toHaveAttribute("href", expect.stringContaining("page=10"));
    const postMessage = vi.spyOn(frame.contentWindow!, "postMessage");
    fireEvent.load(frame);
    expect(postMessage).toHaveBeenCalledWith(
      {
        type: "REQPILOTX_PDF_NAVIGATE",
        page: 10,
        overlays: [
          expect.objectContaining({
            id: "content:content-section-3-2-1",
            type: "TEXT",
            bbox: [30, 140, 260, 165],
          }),
          expect.objectContaining({
            id: "anchor-section-3-2-1-table",
            type: "TABLE",
            bbox: [30, 170, 560, 760],
          }),
        ],
      },
      window.location.origin,
    );
    await user.click(within(tableRegion).getByRole("button", { name: "查看原文" }));
    expect(screen.getByRole("button", { name: "PDF 原文" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await waitFor(() =>
      expect(postMessage).toHaveBeenCalledWith(
        {
          type: "REQPILOTX_PDF_NAVIGATE",
          page: 10,
          overlays: [
            expect.objectContaining({
              id: "table:table-preview-front-1",
              type: "TABLE",
              bbox: [30, 170, 560, 300],
              sourceBounds: [0, 0, 595, 842],
            }),
          ],
        },
        window.location.origin,
      ),
    );
    await user.click(screen.getByRole("button", { name: "框选原文位置" }));
    await waitFor(() =>
      expect(postMessage).toHaveBeenCalledWith(
        {
          type: "REQPILOTX_PDF_NAVIGATE",
          page: 10,
          overlays: [
            expect.objectContaining({
              id: "proposal:proposal-image-1",
              type: "FIGURE",
              bbox: [200, 200, 260, 260],
              label: "图片父级归属",
            }),
          ],
        },
        window.location.origin,
      ),
    );
    expect(screen.queryByText("Agent 已确认收到")).not.toBeInTheDocument();
  });

  it("opens a grouped page atlas and routes page selection to the original PDF", async () => {
    const user = userEvent.setup();
    const onPageChange = vi.fn();
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={onPageChange}
        sourceFileUrl="/api/document/file"
        activePage={10}
        pageCount={29}
        showSourcePreview
      />,
    );

    await screen.findByText("Agent 原文结构草案");
    await user.click(screen.getByRole("button", { name: "全文概览" }));

    const atlas = screen.getByRole("region", { name: "全文页面图谱" });
    expect(atlas).toHaveTextContent("产品设计规格说明书（PDS）");
    expect(atlas).toHaveTextContent("其他页面");
    const page10 = within(atlas).getByRole("button", {
      name: /P10 · 已建立页位 · 1 张逻辑表/,
    });
    const page11 = within(atlas).getByRole("button", {
      name: /P11 · 已建立页位 · 1 张逻辑表/,
    });
    expect(page10).toHaveAttribute("aria-pressed", "true");

    page10.focus();
    await user.keyboard("{ArrowRight}");
    expect(page11).toHaveFocus();
    await user.click(page11);
    expect(onPageChange).toHaveBeenCalledWith(11);

    await user.click(within(atlas).getByRole("button", { name: "表格" }));
    expect(
      within(atlas).getByRole("button", { name: /^P10 ·/ }),
    ).toBeVisible();
    expect(
      within(atlas).queryByRole("button", { name: /^P1 ·/ }),
    ).not.toBeInTheDocument();

    await user.click(within(atlas).getByRole("button", { name: "需关注" }));
    expect(
      within(atlas).getByRole("button", { name: /^P23 ·/ }),
    ).toBeVisible();
    expect(
      within(atlas).queryByRole("button", { name: /^P7 ·/ }),
    ).not.toBeInTheDocument();

    await user.click(within(atlas).getByRole("button", { name: "当前范围" }));
    expect(
      within(atlas).getByText("当前没有运行中的页面范围"),
    ).toBeVisible();

    await user.click(within(atlas).getByRole("button", { name: "关闭概览" }));
    expect(
      screen.queryByRole("region", { name: "全文页面图谱" }),
    ).not.toBeInTheDocument();
  });

  it("reuses the main structured-table presenter for continued fragments", async () => {
    const user = userEvent.setup();
    const continuedPreview: NativeStructureTablePreviewV1 = {
      ...tablePreview,
      summary: { fragment_count: 2, row_count: 3, cell_count: 6 },
      fragments: [
        ...tablePreview.fragments,
        {
          fragment_id: "table-preview-front-2",
          table_token: "T0011",
          page: 11,
          row_range: [0, 1],
          row_count: 1,
          column_count: 2,
          bbox: [30, 40, 560, 90],
          source_bounds: [0, 0, 595, 842],
          cells: [
            {
              cell_id: "cell-continued-1",
              row: 0,
              column: 0,
              row_span: 1,
              column_span: 1,
              text: "2",
              bbox: [30, 40, 100, 90],
            },
            {
              cell_id: "cell-continued-2",
              row: 0,
              column: 1,
              row_span: 1,
              column_span: 1,
              text: "续页试验",
              bbox: [100, 40, 560, 90],
            },
          ],
        },
      ],
    };
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureTablePreview").mockResolvedValue(
      continuedPreview,
    );

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    await user.click(
      await screen.findByRole("button", { name: "展开 3 车型/产品信息" }),
    );
    await user.click(
      screen.getByRole("button", { name: "展开 3.2 产品技术要求及说明" }),
    );
    await user.click(screen.getByRole("button", { name: /3.2.1 产品信息/ }));

    const region = await screen.findByRole("region", {
      name: "跨页表格完整内容",
    });
    expect(within(region).getAllByRole("table")).toHaveLength(2);
    expect(region).toHaveTextContent("2 段续表");
    expect(region).toHaveTextContent("续表 · 第 2 段 · 第 11 页");
    expect(region).toHaveTextContent("续页试验");
    expect(
      within(region).getByRole("button", { name: "展开表格" }),
    ).toBeVisible();
  });

  it("uses one canonical continuation schema and keeps cross-cell images visible", async () => {
    const user = userEvent.setup();
    const correctedPreview: NativeStructureTablePreviewV1 = {
      ...tablePreview,
      summary: { fragment_count: 2, row_count: 3, cell_count: 8 },
      presentation: {
        schema_version: "native-structure-table-presentation/v1",
        mode: "STRUCTURED_CELLS",
        canonical_column_count: 2,
        floating_images: [
          {
            anchor_id: "floating-image-1",
            page: 11,
            bbox: [220, 42, 360, 88],
            source_bounds: [0, 0, 595, 842],
            anchor_kind: "IMAGE",
            precision: "EXACT_IMAGE",
            placement_id: "placement-floating-1",
            asset_id: "asset-floating-1",
          },
        ],
      },
      fragments: [
        ...tablePreview.fragments,
        {
          fragment_id: "table-preview-front-raw-split",
          table_token: "T0011",
          page: 11,
          row_range: [0, 1],
          row_count: 1,
          column_count: 4,
          bbox: [30, 40, 560, 90],
          source_bounds: [0, 0, 595, 842],
          cells: [
            {
              cell_id: "split-sequence",
              row: 0,
              column: 0,
              row_span: 1,
              column_span: 1,
              text: "2",
              bbox: [30, 40, 60, 90],
            },
            {
              cell_id: "split-sequence-empty",
              row: 0,
              column: 1,
              row_span: 1,
              column_span: 1,
              text: "",
              bbox: [60, 40, 100, 90],
            },
            {
              cell_id: "split-description-a",
              row: 0,
              column: 2,
              row_span: 1,
              column_span: 1,
              text: "续页",
              bbox: [100, 40, 300, 90],
            },
            {
              cell_id: "split-description-b",
              row: 0,
              column: 3,
              row_span: 1,
              column_span: 1,
              text: "试验",
              bbox: [300, 40, 560, 90],
            },
          ],
        },
      ],
    };
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureTablePreview").mockResolvedValue(
      correctedPreview,
    );

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    await user.click(
      await screen.findByRole("button", { name: "展开 3 车型/产品信息" }),
    );
    await user.click(
      screen.getByRole("button", { name: "展开 3.2 产品技术要求及说明" }),
    );
    await user.click(screen.getByRole("button", { name: /3.2.1 产品信息/ }));

    const region = await screen.findByRole("region", {
      name: "跨页表格完整内容",
    });
    expect(region).toHaveTextContent("3 行 × 2 列");
    expect(region).toHaveTextContent("续页");
    expect(region).toHaveTextContent("试验");
    expect(within(region).getAllByRole("cell")).toHaveLength(4);
    expect(within(region).getByText("跨单元格图片")).toBeVisible();
    expect(
      within(region).getByAltText("原 PDF 中跨越多个表格单元格的图片"),
    ).toBeVisible();
  });

  it("snaps sub-point cross-page drift onto one exact canonical grid", () => {
    const frontPreview: NativeStructureTablePreviewV1 = {
      schema_version: "native-structure-table-preview/v1",
      artifact_id: "pds-native-v1",
      source_sha256: "a".repeat(64),
      logical_table_scope_id: "lt-front-drift",
      title: "1）前减振器布置要求",
      presentation: {
        schema_version: "native-structure-table-presentation/v1",
        mode: "STRUCTURED_CELLS",
        canonical_column_count: 3,
        floating_images: [],
      },
      summary: { fragment_count: 2, row_count: 2, cell_count: 9 },
      fragments: [
        {
          fragment_id: "front-page-3",
          table_token: "T0003",
          page: 3,
          row_range: [20, 21],
          row_count: 1,
          column_count: 9,
          bbox: [31.44, 503.99, 562.56, 518.4],
          source_bounds: [0, 0, 595, 842],
          cells: [
            {
              cell_id: "front-header-sequence",
              row: 0,
              column: 0,
              row_span: 1,
              column_span: 2,
              text: "序号",
              bbox: [31.44, 503.99, 75.12, 518.4],
            },
            {
              cell_id: "front-header-item",
              row: 0,
              column: 2,
              row_span: 1,
              column_span: 2,
              text: "项目",
              bbox: [75.12, 503.99, 183.72, 518.4],
            },
            {
              cell_id: "front-header-requirement",
              row: 0,
              column: 4,
              row_span: 1,
              column_span: 5,
              text: "要求",
              bbox: [183.72, 503.99, 562.56, 518.4],
            },
          ],
        },
        {
          fragment_id: "front-page-4",
          table_token: "T0004",
          page: 4,
          row_range: [0, 1],
          row_count: 1,
          column_count: 6,
          bbox: [31.98, 14.46, 562.772, 88.78],
          source_bounds: [0, 0, 595, 842],
          cells: [
            ["front-7", 0, "7", 31.98, 53.82],
            ["front-7-gap", 1, "", 53.82, 75.518],
            ["front-space", 2, "空间硬点坐标", 75.518, 97.32],
            ["front-space-gap", 3, "", 97.32, 184.119],
            ["front-value", 4, "", 184.119, 205.92],
            ["front-value-gap", 5, "", 205.92, 562.772],
          ].map(([cell_id, column, text, left, right]) => ({
            cell_id: String(cell_id),
            row: 0,
            column: Number(column),
            row_span: 1,
            column_span: 1,
            text: String(text),
            bbox: [Number(left), 14.46, Number(right), 88.78] as [
              number,
              number,
              number,
              number,
            ],
          })),
        },
      ],
    };

    const frontTables = nativeStructuredTables(frontPreview, []);

    expect(frontTables[1].cells).toHaveLength(3);
    expect(
      frontTables[1].cells.map((cell) => [cell.column, cell.columnSpan]),
    ).toEqual([
      [0, 1],
      [1, 1],
      [2, 1],
    ]);
    expect(canonicalTableGridIssues(frontTables[1])).toEqual([]);
    expect(canonicalStructuredTablesAreValid(frontPreview, frontTables)).toBe(
      true,
    );

    const rearPreview: NativeStructureTablePreviewV1 = {
      ...frontPreview,
      logical_table_scope_id: "lt-rear-drift",
      title: "2）后减振器布置要求",
      fragments: [
        {
          ...frontPreview.fragments[1],
          fragment_id: "rear-page-4",
          cells: [
            ["rear-header-sequence", 0, "序号", 31.98, 53.82],
            ["rear-header-sequence-gap", 1, "", 53.82, 75.518],
            ["rear-header-item", 2, "项目", 75.518, 97.32],
            ["rear-header-item-gap", 3, "", 97.32, 184.119],
            ["rear-header-requirement", 4, "要求", 184.119, 205.92],
            ["rear-header-requirement-gap", 5, "", 205.92, 562.772],
          ].map(([cell_id, column, text, left, right]) => ({
            cell_id: String(cell_id),
            row: 0,
            column: Number(column),
            row_span: 1,
            column_span: 1,
            text: String(text),
            bbox: [Number(left), 304.88, Number(right), 319.28] as [
              number,
              number,
              number,
              number,
            ],
          })),
        },
        {
          fragment_id: "rear-page-5",
          table_token: "T0005",
          page: 5,
          row_range: [0, 1],
          row_count: 1,
          column_count: 3,
          bbox: [31.44, 14.16, 562.56, 75],
          source_bounds: [0, 0, 595, 842],
          cells: [
            ["rear-8", 0, "8", 31.44, 75.12],
            ["rear-space", 1, "空间硬点坐标", 75.12, 183.72],
            ["rear-value", 2, "", 183.72, 562.56],
          ].map(([cell_id, column, text, left, right]) => ({
            cell_id: String(cell_id),
            row: 0,
            column: Number(column),
            row_span: 1,
            column_span: 1,
            text: String(text),
            bbox: [Number(left), 14.16, Number(right), 75] as [
              number,
              number,
              number,
              number,
            ],
          })),
        },
      ],
      summary: { fragment_count: 2, row_count: 2, cell_count: 9 },
    };

    const rearTables = nativeStructuredTables(rearPreview, []);

    expect(
      rearTables[1].cells.map((cell) => [cell.column, cell.columnSpan]),
    ).toEqual([
      [0, 1],
      [1, 1],
      [2, 1],
    ]);
    expect(canonicalTableGridIssues(rearTables[1])).toEqual([]);
    expect(canonicalStructuredTablesAreValid(rearPreview, rearTables)).toBe(
      true,
    );
  });

  it("falls back to the exact PDF region when a tool cell leaks adjacent headings", async () => {
    const user = userEvent.setup();
    const visualPreview: NativeStructureTablePreviewV1 = {
      ...tablePreview,
      presentation: {
        schema_version: "native-structure-table-presentation/v1",
        mode: "VISUAL_REGION_ONLY",
        canonical_column_count: null,
        floating_images: [],
      },
      summary: { fragment_count: 1, row_count: 1, cell_count: 1 },
      fragments: [
        {
          ...tablePreview.fragments[0],
          row_count: 1,
          column_count: 8,
          bbox: [30, 170, 560, 220],
          cells: [
            {
              cell_id: "leaking-cell",
              row: 0,
              column: 0,
              row_span: 1,
              column_span: 8,
              text: "车型信息 3.2 产品技术要求 3.2.1 产品信息",
              bbox: [20, 150, 570, 320],
            },
          ],
        },
      ],
    };
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureTablePreview").mockResolvedValue(visualPreview);

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    await user.click(
      await screen.findByRole("button", { name: "展开 3 车型/产品信息" }),
    );
    await user.click(
      screen.getByRole("button", { name: "展开 3.2 产品技术要求及说明" }),
    );
    await user.click(screen.getByRole("button", { name: /3.2.1 产品信息/ }));

    const region = await screen.findByRole("region", { name: "原文表格内容" });
    expect(region).toHaveTextContent("原 PDF 表格区域 · 未伪造单元格");
    expect(region).toHaveTextContent("原表区域");
    expect(region).not.toHaveTextContent("3.2 产品技术要求");
    expect(
      within(region).getByAltText("原 PDF 表格单元格中的图片"),
    ).toBeVisible();
  });

  it("persists a scope-bound instruction without claiming Agent delivery", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "submitNativeStructureFeedback").mockResolvedValue({
      schema_version: "native-structure-feedback-result/v1",
      delivery_message: "指引已保存到本地开发反馈队列；Codex App Agent 尚未读取。",
      feedback: {
        schema_version: "native-structure-feedback/v1",
        feedback_id: "native-structure-feedback-1",
        project_id: "project-1",
        document_id: "document-1",
        source_sha256: reference.source_sha256,
        artifact_id: reference.artifact_id,
        command_id: "command-1",
        scope_kind: "STRUCTURE_NODE",
        scope_id: "doc-main",
        message: "P23-P29 保持三个独立对象。",
        status: "PENDING_CODEX_APP",
        created_at_utc: "2026-08-25T12:00:00Z",
      },
    });

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    const input = await screen.findByLabelText("给 Agent 的节点指引");
    await user.type(input, "P23-P29 保持三个独立对象。");
    await user.click(
      screen.getByRole("button", { name: /测试分组与测试顺序矩阵/ }),
    );
    expect(screen.getByLabelText("给 Agent 的节点指引")).toHaveValue("");
    expect(screen.getAllByText(/Agent 根据表格用途命名/).length).toBeGreaterThan(0);
    await user.click(
      screen.getByRole("button", { name: /^产品设计规格说明书（PDS）/ }),
    );
    expect(screen.getByLabelText("给 Agent 的节点指引")).toHaveValue(
      "P23-P29 保持三个独立对象。",
    );
    await user.click(screen.getByRole("button", { name: "保存指引" }));

    expect(
      await screen.findByText(/Codex App Agent 尚未读取/),
    ).toBeVisible();
    expect(screen.getByText(/已保存，等待 Agent 读取/)).toBeVisible();
    expect(
      screen.getByRole("button", { name: "继续处理 1 条指引" }),
    ).toBeVisible();
  });

  it("restores expand controls and main-style keyboard tree navigation", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureTablePreview").mockResolvedValue(tablePreview);

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "全部展开" }));
    const root = screen.getByRole("button", {
      name: /^产品设计规格说明书（PDS）/,
    });
    root.focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("heading", { name: "3 车型/产品信息" })).toBeVisible();
    await user.keyboard("{ArrowRight}");
    expect(
      screen.getByRole("heading", { name: "3.2 产品技术要求及说明" }),
    ).toBeVisible();
    await user.keyboard("{End}");
    expect(
      screen.getByRole("heading", { name: "测试分组与测试顺序矩阵" }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "全部收起" }));
    expect(
      screen.queryByRole("button", { name: /3.2.1 产品信息/ }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "定位当前" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /测试分组与测试顺序矩阵/ }),
      ).toHaveFocus(),
    );
  });

  it("keeps the expanded tree when parent callbacks receive new identities", async () => {
    const user = userEvent.setup();
    const referenceSpy = vi
      .spyOn(api, "nativeStructureReference")
      .mockResolvedValue(reference);
    vi.spyOn(api, "nativeStructureTablePreview").mockResolvedValue(tablePreview);
    const { rerender } = render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
        onAvailabilityChange={vi.fn()}
        onPageCountChange={vi.fn()}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "全部展开" }));
    expect(
      screen.getByRole("button", { name: /3.2.1 产品信息/ }),
    ).toBeVisible();

    rerender(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
        onAvailabilityChange={vi.fn()}
        onPageCountChange={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("button", { name: /3.2.1 产品信息/ }),
    ).toBeVisible();
    expect(referenceSpy).toHaveBeenCalledTimes(1);
  });

  it("keeps detail and PDF equal by default and supports accessible column resizing", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);

    const { container } = render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        sourceFileUrl="/api/document/file"
        showSourcePreview
        onPageChange={vi.fn()}
      />,
    );

    await screen.findByText("Agent 原文结构草案");
    const workbench = container.querySelector(
      ".native-hierarchy-workbench",
    ) as HTMLElement;
    expect(workbench.style.getPropertyValue("--native-detail-share")).toBe(
      "38fr",
    );
    expect(workbench.style.getPropertyValue("--native-source-share")).toBe(
      "38fr",
    );
    const first = screen.getByRole("separator", {
      name: "调整标题树和原文条目宽度",
    });
    first.focus();
    await user.keyboard("{ArrowRight}");
    expect(workbench.style.getPropertyValue("--native-tree-share")).toBe(
      "25.5fr",
    );
    expect(workbench.style.getPropertyValue("--native-detail-share")).toBe(
      "36.5fr",
    );
    const second = screen.getByRole("separator", {
      name: "调整原文条目和 PDF 宽度",
    });
    const treeColumn = container.querySelector(
      ".native-hierarchy-tree",
    ) as HTMLElement;
    const detailColumn = container.querySelector(
      ".native-hierarchy-detail",
    ) as HTMLElement;
    const sourceColumn = container.querySelector(
      ".native-hierarchy-source",
    ) as HTMLElement;
    vi.spyOn(treeColumn, "getBoundingClientRect").mockReturnValue({
      width: 300,
    } as DOMRect);
    vi.spyOn(detailColumn, "getBoundingClientRect").mockReturnValue({
      width: 450,
    } as DOMRect);
    vi.spyOn(sourceColumn, "getBoundingClientRect").mockReturnValue({
      width: 450,
    } as DOMRect);
    Object.assign(second, {
      setPointerCapture: vi.fn(),
      hasPointerCapture: vi.fn(() => true),
      releasePointerCapture: vi.fn(),
    });
    fireEvent.pointerDown(second, { pointerId: 1, clientX: 600 });
    fireEvent.pointerMove(second, { pointerId: 1, clientX: 550 });
    await waitFor(() =>
      expect(
        Number.parseFloat(
          workbench.style.getPropertyValue("--native-source-share"),
        ),
      ).toBeGreaterThan(
        Number.parseFloat(
          workbench.style.getPropertyValue("--native-detail-share"),
        ),
      ),
    );
    fireEvent.pointerUp(second, { pointerId: 1, clientX: 550 });
    fireEvent.doubleClick(second);
    expect(workbench.style.getPropertyValue("--native-tree-share")).toBe(
      "24fr",
    );
    expect(workbench.style.getPropertyValue("--native-detail-share")).toBe(
      "38fr",
    );
    expect(workbench.style.getPropertyValue("--native-source-share")).toBe(
      "38fr",
    );
  });

  it("queues all saved feedback through one visible document-level action", async () => {
    const user = userEvent.setup();
    const handoff = {
      schema_version: "native-structure-agent-handoff/v1" as const,
      handoff_id: "handoff-1",
      project_id: "project-1",
      document_id: "document-1",
      source_sha256: reference.source_sha256,
      artifact_id: reference.artifact_id,
      command_id: "continue-1",
      input_fingerprint: "b".repeat(64),
      status: "QUEUED_FOR_CODEX_APP" as const,
      feedback_ids: ["feedback-1"],
      proposal_decision_ids: ["decision-1", "decision-2"],
      affected_node_ids: ["doc-main"],
      decision_counts: { CONFIRM: 2, UNDO: 0, KEEP_UNRESOLVED: 0 },
      requires_scoped_structure_revision: true,
      requires_scoped_evidence_recovery: false,
      preserve_unaffected_nodes: true,
      created_at_utc: "2026-08-27T02:00:00Z",
    };
    const readyReference: NativeStructureReferencePublicV1 = {
      ...reference,
      continuation: {
        ...reference.continuation,
        status: "READY_TO_CONTINUE",
        can_continue: true,
        pending_feedback_count: 1,
        pending_proposal_decision_count: 2,
      },
    };
    const queuedReference: NativeStructureReferencePublicV1 = {
      ...readyReference,
      continuation: {
        ...readyReference.continuation,
        status: "QUEUED_FOR_CODEX_APP",
        can_continue: false,
        pending_feedback_count: 0,
        pending_proposal_decision_count: 0,
        latest_handoff: handoff,
      },
      feedback_capability: {
        ...readyReference.feedback_capability,
        agent_delivery: "QUEUED_FOR_CODEX_APP",
      },
    };
    vi.spyOn(api, "nativeStructureReference")
      .mockResolvedValueOnce(readyReference)
      .mockResolvedValueOnce(queuedReference);
    const continueRequest = vi
      .spyOn(api, "continueNativeStructureReference")
      .mockResolvedValue({
        schema_version: "native-structure-continue-result/v1",
        handoff,
        job: null,
        delivery_message:
          "本轮反馈已进入 Codex App 协作队列；Agent 尚未确认读取。",
      });

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    await user.click(
      await screen.findByRole("button", { name: "继续处理 1 条指引" }),
    );
    await waitFor(() => expect(continueRequest).toHaveBeenCalledTimes(1));
    const agentNext = screen.getByRole("region", { name: "Agent 下一步" });
    expect(agentNext).toHaveTextContent("Agent 待读取");
    expect(agentNext).toHaveTextContent("等待 Agent 真实读取回执");
    expect(
      screen.queryByRole("button", { name: /继续处理/ }),
    ).not.toBeInTheDocument();
  });

  it("shows adaptive tool use, Agent revision, and Host revalidation", async () => {
    const user = userEvent.setup();
    const revisingReference: NativeStructureReferencePublicV1 = {
      ...reference,
      continuation: {
        ...reference.continuation,
        status: "REVISION_REQUIRED",
        can_continue: false,
        completed_work_item_count: 0,
        total_work_item_count: 1,
        latest_handoff: {
          schema_version: "native-structure-agent-handoff/v1",
          handoff_id: "handoff-revision-1",
          project_id: "project-1",
          document_id: "document-1",
          source_sha256: reference.source_sha256,
          artifact_id: reference.artifact_id,
          command_id: "continue-revision-1",
          input_fingerprint: "b".repeat(64),
          status: "REVISION_REQUIRED",
          feedback_ids: ["feedback-1"],
          proposal_decision_ids: [],
          affected_node_ids: ["section-3.2.1"],
          decision_counts: { CONFIRM: 0, UNDO: 0, KEEP_UNRESOLVED: 0 },
          requires_scoped_structure_revision: true,
          requires_scoped_evidence_recovery: false,
          preserve_unaffected_nodes: true,
          current_action: "正在修改续表列结构并重新物化。",
          latest_attempt: {
            schema_version: "native-structure-handoff-attempt-receipt/v1",
            attempt_id: "native-structure-handoff-attempt-1",
            attempt_ordinal: 1,
            status: "REVISION_REQUIRED",
            affected_scope_ids: ["section-3.2.1"],
            tool_calls: [
              {
                call_id: "table-tool-1",
                intent: "RESTORE_TABLE_CANDIDATE",
                outcome: "SUCCEEDED",
                result_role: "ADVISORY_CANDIDATE",
              },
            ],
            candidate_revision_count: 1,
            rendered_output_sha256: "d".repeat(64),
            validation_status: "FAIL",
            selected_pattern_ids: ["continuation_column_topology_drift"],
            accepted_work_item_count: 0,
            current_action: "正在修改续表列结构并重新物化。",
            public_summary: "首个工具候选未通过 Host 复验。",
            kit_version: "0.8.4",
            kit_bundle_sha256: "c".repeat(64),
            created_at_utc: "2026-08-27T10:00:00Z",
          },
          attempt_summary: {
            attempt_count: 1,
            tool_call_count: 1,
            candidate_revision_count: 1,
            tool_intents: ["RESTORE_TABLE_CANDIDATE"],
            validation_status: "FAIL",
          },
          created_at_utc: "2026-08-27T09:59:00Z",
        },
        message: "Host 复验发现局部结果仍需修正。",
      },
    };
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(
      revisingReference,
    );

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    const agentNext = await screen.findByRole("region", {
      name: "Agent 下一步",
    });
    expect(agentNext).toHaveTextContent("Agent 正在修正");
    expect(agentNext).toHaveTextContent("正在修改续表列结构并重新物化");
    await user.click(screen.getByText("运行轨迹"));
    expect(screen.getByText(/工具：1 次 · 生成表格还原候选/)).toBeInTheDocument();
    expect(screen.getByText("Agent 修订：1 次")).toBeInTheDocument();
    expect(screen.getByText("Host 复验：未通过，继续修正")).toBeInTheDocument();
  });

  it("explicitly keeps the current structure when evidence is insufficient", async () => {
    const guidanceReference: NativeStructureReferencePublicV1 = {
      ...reference,
      continuation: {
        ...reference.continuation,
        status: "NO_CHANGE",
        can_continue: false,
        advancement_allowed: true,
        evidence_state: "INSUFFICIENT",
        default_resolution_policy: "KEEP_CURRENT_STRUCTURE",
        nonblocking_unresolved_count: 1,
        completed_work_item_count: 0,
        total_work_item_count: 1,
        latest_handoff: {
          schema_version: "native-structure-agent-handoff/v1",
          handoff_id: "handoff-guidance-1",
          project_id: "project-1",
          document_id: "document-1",
          source_sha256: reference.source_sha256,
          artifact_id: reference.artifact_id,
          command_id: "continue-guidance-1",
          input_fingerprint: "b".repeat(64),
          status: "NO_CHANGE",
          feedback_ids: ["feedback-guidance-1"],
          proposal_decision_ids: [],
          affected_node_ids: ["section-3.2.1"],
          decision_counts: { CONFIRM: 0, UNDO: 0, KEEP_UNRESOLVED: 0 },
          requires_scoped_structure_revision: true,
          requires_scoped_evidence_recovery: false,
          preserve_unaffected_nodes: true,
          current_action: "证据有限，Agent 已明确选择保留当前结构继续。",
          outcome_reason_code: "current_structure_selected",
          evidence_state: "INSUFFICIENT",
          decision_policy: "KEEP_CURRENT_STRUCTURE",
          created_at_utc: "2026-08-28T03:00:00Z",
        },
        message: "Agent 已给出工作判断并保留当前结构。",
      },
    };
    vi.spyOn(api, "nativeStructureReference").mockResolvedValue(
      guidanceReference,
    );
    const activityRequest = vi
      .spyOn(api, "nativeStructureActivity")
      .mockResolvedValue({
        schema_version: "native-structure-activity-stream/v1",
        source_sha256: reference.source_sha256,
        artifact_id: reference.artifact_id,
        handoff_id: "handoff-guidance-1",
        events: [],
        cursor: 0,
        continuation: guidanceReference.continuation,
      });

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    const agentNext = await screen.findByRole("region", {
      name: "Agent 下一步",
    });
    expect(agentNext).toHaveTextContent("已明确保留当前结构");
    expect(agentNext).toHaveTextContent(
      "证据有限，Agent 已明确选择保留当前结构继续。",
    );
    expect(agentNext).toHaveTextContent("不阻断下一阶段");
    await waitFor(() => expect(activityRequest).toHaveBeenCalledTimes(1));
  });

  it("refreshes the authoritative projection when Activity completes verification", async () => {
    const user = userEvent.setup();
    const activeReference: NativeStructureReferencePublicV1 = {
      ...reference,
      structure_projection: {
        ...reference.structure_projection,
        state: "WORKING_VERIFICATION_REQUIRED",
        attempt_id: "native-structure-handoff-attempt-live-1",
        attempt_ordinal: 1,
        affected_scope_ids: ["section-3.2.1"],
        pending_rendered_scope_ids: ["section-3.2.1"],
        validation_status: "FAIL",
      },
      continuation: {
        ...reference.continuation,
        status: "VERIFYING",
        latest_handoff: {
          schema_version: "native-structure-agent-handoff/v1",
          handoff_id: "handoff-live-1",
          project_id: "project-1",
          document_id: "document-1",
          source_sha256: reference.source_sha256,
          artifact_id: reference.artifact_id,
          command_id: "continue-live-1",
          input_fingerprint: "b".repeat(64),
          status: "VERIFYING",
          feedback_ids: ["feedback-live-1"],
          proposal_decision_ids: [],
          affected_node_ids: ["section-3.2.1"],
          decision_counts: { CONFIRM: 0, UNDO: 0, KEEP_UNRESOLVED: 0 },
          requires_scoped_structure_revision: true,
          requires_scoped_evidence_recovery: false,
          preserve_unaffected_nodes: true,
          current_action: "正在等待渲染复验。",
          created_at_utc: "2026-08-27T10:00:00Z",
        },
        message: "局部候选已物化。",
      },
    };
    const completedContinuation = {
      ...activeReference.continuation,
      status: "CONSUMED" as const,
      completed_work_item_count: 1,
      total_work_item_count: 1,
      latest_handoff: {
        ...activeReference.continuation.latest_handoff!,
        status: "CONSUMED" as const,
        current_action: "局部候选已通过 Host 复验。",
      },
      message: "本轮修改已完成。",
    };
    const acceptedReference: NativeStructureReferencePublicV1 = {
      ...reference,
      document_tree: {
        ...reference.document_tree,
        nodes: reference.document_tree.nodes.map((node) =>
          node.node_id === "doc-main"
            ? { ...node, title: "产品设计规格说明书（Host 已验证）" }
            : node,
        ),
      },
      continuation: completedContinuation,
    };
    const stream: NativeStructureActivityStreamV1 = {
      schema_version: "native-structure-activity-stream/v1",
      source_sha256: reference.source_sha256,
      artifact_id: reference.artifact_id,
      handoff_id: "handoff-live-1",
      cursor: 2,
      continuation: completedContinuation,
      events: [
        {
          schema_version: "reqpilotx-agent-activity-event/v2",
          event_id: "agent-activity-" + "1".repeat(32),
          run_id: "agent-run-" + "2".repeat(32),
          source_sha256: reference.source_sha256,
          sequence: 2,
          occurred_at_utc: "2026-08-27T10:00:02Z",
          actor: "VALIDATOR",
          family: "DECISION",
          kind: "STRUCTURE_MATERIALIZATION_VERIFIED",
          stage: "D2_LOGICAL_STRUCTURE",
          scope: {
            kind: "STRUCTURE_NODE",
            ref_id: "section-3.2.1",
            page_numbers: [10],
          },
          visibility: "PRIMARY",
          display: {
            title: "Host 已验证本轮修改",
            summary: "来源、物化结果和重新打开回执均已通过。",
          },
          payload: {
            status: "VERIFIED",
            outcome_code: "PASS",
            authorized_parameters: { accepted_work_item_count: 1 },
          },
        },
      ],
    };
    const referenceRequest = vi
      .spyOn(api, "nativeStructureReference")
      .mockResolvedValueOnce(activeReference)
      .mockResolvedValueOnce(acceptedReference);
    const activityRequest = vi
      .spyOn(api, "nativeStructureActivity")
      .mockResolvedValue(stream);

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    await waitFor(() => expect(activityRequest).toHaveBeenCalledWith(
      "project-1",
      "document-1",
      0,
      expect.any(AbortSignal),
    ));
    expect(
      await within(screen.getByRole("tree", { name: "原文标题层级" })).findByRole(
        "treeitem",
        { name: /产品设计规格说明书（Host 已验证）/ },
      ),
    ).toBeVisible();
    expect(referenceRequest).toHaveBeenCalledTimes(2);
    expect(
      screen.queryByText("工作投影 · 待原文复验"),
    ).not.toBeInTheDocument();
    await user.click(screen.getByText("运行轨迹"));
    expect(await screen.findByText("Host 已验证本轮修改")).toBeVisible();
    expect(
      screen.getByText("来源、物化结果和重新打开回执均已通过。"),
    ).toBeVisible();
    expect(screen.getByRole("region", { name: "Agent 下一步" })).toHaveTextContent(
      "本轮已完成",
    );
  });

  it("does not repeatedly fetch the full reference while Activity lifecycle is unchanged", async () => {
    vi.useFakeTimers();
    const activeReference: NativeStructureReferencePublicV1 = {
      ...reference,
      continuation: {
        ...reference.continuation,
        status: "IN_PROGRESS",
        latest_handoff: {
          schema_version: "native-structure-agent-handoff/v1",
          handoff_id: "handoff-stable-live-1",
          project_id: "project-1",
          document_id: "document-1",
          source_sha256: reference.source_sha256,
          artifact_id: reference.artifact_id,
          command_id: "continue-stable-live-1",
          input_fingerprint: "b".repeat(64),
          status: "IN_PROGRESS",
          feedback_ids: ["feedback-stable-live-1"],
          proposal_decision_ids: [],
          affected_node_ids: ["section-3.2.1"],
          decision_counts: { CONFIRM: 0, UNDO: 0, KEEP_UNRESOLVED: 0 },
          requires_scoped_structure_revision: true,
          requires_scoped_evidence_recovery: false,
          preserve_unaffected_nodes: true,
          current_action: "正在检查局部结构。",
          created_at_utc: "2026-08-31T09:00:00Z",
        },
        message: "正在检查局部结构。",
      },
    };
    const referenceRequest = vi
      .spyOn(api, "nativeStructureReference")
      .mockResolvedValue(activeReference);
    const activityRequest = vi
      .spyOn(api, "nativeStructureActivity")
      .mockResolvedValue({
        schema_version: "native-structure-activity-stream/v1",
        source_sha256: reference.source_sha256,
        artifact_id: reference.artifact_id,
        handoff_id: "handoff-stable-live-1",
        events: [
          {
            schema_version: "reqpilotx-agent-activity-event/v2",
            event_id: "agent-activity-historical-" + "1".repeat(32),
            run_id: "agent-run-historical-" + "2".repeat(32),
            source_sha256: reference.source_sha256,
            sequence: 1,
            occurred_at_utc: "2026-08-31T09:00:01Z",
            actor: "AGENT",
            family: "DECISION",
            kind: "STRUCTURE_CANDIDATE_REVISION_PUBLISHED",
            stage: "D2_LOGICAL_STRUCTURE",
            scope: {
              kind: "STRUCTURE_NODE",
              ref_id: "section-3.2.1",
              page_numbers: [10],
            },
            visibility: "PRIMARY",
            display: {
              title: "历史候选已发布",
              summary: "该事件已经反映在当前权威投影中。",
            },
            payload: {
              status: "IN_PROGRESS",
              outcome_code: "",
              authorized_parameters: {},
            },
          },
        ],
        cursor: 0,
        continuation: activeReference.continuation,
      });
    const view = render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    try {
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(referenceRequest).toHaveBeenCalledTimes(1);
      expect(activityRequest).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(4_600);
      });
      expect(activityRequest.mock.calls.length).toBeGreaterThan(1);
      expect(referenceRequest).toHaveBeenCalledTimes(1);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("recovers a promoted handoff after one bounded refresh cycle is exhausted", async () => {
    vi.useFakeTimers();
    const initialReference: NativeStructureReferencePublicV1 = {
      ...reference,
      continuation: {
        ...reference.continuation,
        status: "IN_PROGRESS",
        latest_handoff: {
          schema_version: "native-structure-agent-handoff/v1",
          handoff_id: "handoff-h2",
          project_id: "project-1",
          document_id: "document-1",
          source_sha256: reference.source_sha256,
          artifact_id: reference.artifact_id,
          command_id: "continue-h2",
          input_fingerprint: "b".repeat(64),
          status: "IN_PROGRESS",
          feedback_ids: ["feedback-h2"],
          proposal_decision_ids: [],
          affected_node_ids: ["doc-main"],
          decision_counts: { CONFIRM: 0, UNDO: 0, KEEP_UNRESOLVED: 0 },
          requires_scoped_structure_revision: true,
          requires_scoped_evidence_recovery: false,
          preserve_unaffected_nodes: true,
          current_action: "H2 正在运行。",
          created_at_utc: "2026-09-01T01:00:00Z",
        },
        message: "H2 正在运行。",
      },
    };
    const promotedAttempt = {
      schema_version: "native-structure-handoff-attempt-receipt/v1" as const,
      attempt_id: "native-structure-handoff-attempt-h1",
      attempt_ordinal: 1,
      status: "VERIFICATION_REQUIRED" as const,
      affected_scope_ids: ["promoted-scope"],
      pending_rendered_scope_ids: ["promoted-scope"],
      tool_calls: [],
      candidate_revision_count: 1,
      rendered_output_sha256: "d".repeat(64),
      validation_status: "FAIL" as const,
      selected_pattern_ids: ["confirmed_proposal_not_verified"],
      accepted_work_item_count: 0,
      current_action: "H1 已成为当前工作 lineage。",
      public_summary: "H1 等待局部复验。",
      kit_version: "0.8.9",
      kit_bundle_sha256: "e".repeat(64),
      created_at_utc: "2026-09-01T01:01:00Z",
    };
    const promotedContinuation = {
      ...initialReference.continuation,
      status: "VERIFYING" as const,
      latest_handoff: {
        ...initialReference.continuation.latest_handoff!,
        handoff_id: "handoff-h1",
        command_id: "continue-h1",
        feedback_ids: ["feedback-h1"],
        affected_node_ids: ["promoted-scope"],
        status: "VERIFYING" as const,
        latest_attempt: promotedAttempt,
        current_action: promotedAttempt.current_action,
      },
      message: "H1 已成为当前工作 lineage。",
    };
    const promotedReference: NativeStructureReferencePublicV1 = {
      ...initialReference,
      structure_projection: {
        ...reference.structure_projection,
        state: "WORKING_VERIFICATION_REQUIRED",
        attempt_id: promotedAttempt.attempt_id,
        attempt_ordinal: promotedAttempt.attempt_ordinal,
        affected_scope_ids: promotedAttempt.affected_scope_ids,
        pending_rendered_scope_ids: promotedAttempt.pending_rendered_scope_ids,
        validation_status: "FAIL",
        projection_sha256: "c".repeat(64),
      },
      continuation: promotedContinuation,
    };
    const promotedStream = {
      schema_version: "native-structure-activity-stream/v1" as const,
      source_sha256: reference.source_sha256,
      artifact_id: reference.artifact_id,
      handoff_id: "handoff-h1",
      events: [],
      cursor: 4,
      continuation: promotedContinuation,
    };
    const referenceRequest = vi
      .spyOn(api, "nativeStructureReference")
      .mockResolvedValueOnce(initialReference)
      .mockRejectedValueOnce(
        new ApiError("promoted projection unavailable", 503, null),
      )
      .mockRejectedValueOnce(
        new ApiError("promoted projection still unavailable", 503, null),
      )
      .mockResolvedValueOnce(promotedReference);
    const activityRequest = vi
      .spyOn(api, "nativeStructureActivity")
      .mockResolvedValueOnce(promotedStream)
      .mockResolvedValueOnce(promotedStream)
      .mockResolvedValue({
        ...promotedStream,
        cursor: 1,
        events: [
          {
            schema_version: "reqpilotx-agent-activity-event/v2",
            event_id: "agent-activity-h1-" + "1".repeat(32),
            run_id: "agent-run-h1-" + "2".repeat(32),
            source_sha256: reference.source_sha256,
            sequence: 1,
            occurred_at_utc: "2026-09-01T01:01:01Z",
            actor: "AGENT",
            family: "DECISION",
            kind: "STRUCTURE_MATERIALIZATION_REOPEN_REQUIRED",
            stage: "D2_LOGICAL_STRUCTURE",
            scope: {
              kind: "STRUCTURE_NODE",
              ref_id: "promoted-scope",
              page_numbers: [10],
            },
            visibility: "PRIMARY",
            display: {
              title: "H1 运行已接续",
              summary: "已订阅新的 active handoff。",
            },
            payload: {
              status: "VERIFYING",
              outcome_code: "",
              authorized_parameters: {},
            },
          },
        ],
      });

    const view = render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    try {
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(activityRequest).toHaveBeenCalledTimes(1);
      expect(referenceRequest).toHaveBeenCalledTimes(2);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(250);
      });
      expect(referenceRequest).toHaveBeenCalledTimes(3);
      expect(activityRequest).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_999);
      });
      expect(activityRequest).toHaveBeenCalledTimes(1);
      expect(referenceRequest).toHaveBeenCalledTimes(3);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(referenceRequest).toHaveBeenCalledTimes(4);
      expect(activityRequest).toHaveBeenCalledTimes(3);
      expect(activityRequest.mock.calls[0]?.[2]).toBe(0);
      expect(activityRequest.mock.calls[1]?.[2]).toBe(0);
      expect(activityRequest.mock.calls[2]?.[2]).toBe(0);
      expect(screen.getByText("工作投影 · 待原文复验")).toBeVisible();
      expect(
        screen.queryByText("运行状态已切换到新的文档版本，请刷新后继续。"),
      ).not.toBeInTheDocument();
      expect(referenceRequest).toHaveBeenCalledTimes(4);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("waits for the authoritative working projection before acknowledging rendered reopen", async () => {
    const attempt = {
      schema_version: "native-structure-handoff-attempt-receipt/v1" as const,
      attempt_id: "native-structure-handoff-attempt-deferred-" + "1".repeat(24),
      attempt_ordinal: 1,
      status: "VERIFICATION_REQUIRED" as const,
      affected_scope_ids: ["doc-main"],
      pending_rendered_scope_ids: ["doc-main"],
      tool_calls: [],
      candidate_revision_count: 1,
      rendered_output_sha256: "d".repeat(64),
      validation_status: "FAIL" as const,
      selected_pattern_ids: ["confirmed_proposal_not_verified"],
      accepted_work_item_count: 0,
      current_action: "正在等待权威工作投影。",
      public_summary: "局部候选已经物化。",
      kit_version: "0.8.9",
      kit_bundle_sha256: "e".repeat(64),
      created_at_utc: "2026-08-31T09:10:00Z",
    };
    const initialReference: NativeStructureReferencePublicV1 = {
      ...reference,
      continuation: {
        ...reference.continuation,
        status: "IN_PROGRESS",
        latest_handoff: {
          schema_version: "native-structure-agent-handoff/v1",
          handoff_id: "handoff-deferred-working-1",
          project_id: "project-1",
          document_id: "document-1",
          source_sha256: reference.source_sha256,
          artifact_id: reference.artifact_id,
          command_id: "continue-deferred-working-1",
          input_fingerprint: "b".repeat(64),
          status: "IN_PROGRESS",
          feedback_ids: ["feedback-deferred-working-1"],
          proposal_decision_ids: [],
          affected_node_ids: ["doc-main"],
          decision_counts: { CONFIRM: 0, UNDO: 0, KEEP_UNRESOLVED: 0 },
          requires_scoped_structure_revision: true,
          requires_scoped_evidence_recovery: false,
          preserve_unaffected_nodes: true,
          current_action: "正在生成工作投影。",
          created_at_utc: "2026-08-31T09:09:00Z",
        },
        message: "正在生成工作投影。",
      },
    };
    const workingContinuation = {
      ...initialReference.continuation,
      status: "VERIFYING" as const,
      latest_handoff: {
        ...initialReference.continuation.latest_handoff!,
        status: "VERIFYING" as const,
        latest_attempt: attempt,
        current_action: "正在等待渲染复验。",
      },
      message: "局部候选已经物化。",
    };
    const workingReference: NativeStructureReferencePublicV1 = {
      ...initialReference,
      structure_projection: {
        ...reference.structure_projection,
        state: "WORKING_VERIFICATION_REQUIRED",
        attempt_id: attempt.attempt_id,
        attempt_ordinal: attempt.attempt_ordinal,
        affected_scope_ids: attempt.affected_scope_ids,
        pending_rendered_scope_ids: attempt.pending_rendered_scope_ids,
        validation_status: "FAIL",
      },
      continuation: workingContinuation,
    };
    let resolveWorkingReference!: (
      value: NativeStructureReferencePublicV1,
    ) => void;
    const deferredWorkingReference =
      new Promise<NativeStructureReferencePublicV1>((resolve) => {
        resolveWorkingReference = resolve;
      });
    const referenceRequest = vi
      .spyOn(api, "nativeStructureReference")
      .mockResolvedValueOnce(initialReference)
      .mockImplementationOnce(() => deferredWorkingReference);
    vi.spyOn(api, "nativeStructureActivity").mockResolvedValue({
      schema_version: "native-structure-activity-stream/v1",
      source_sha256: reference.source_sha256,
      artifact_id: reference.artifact_id,
      handoff_id: "handoff-deferred-working-1",
      events: [],
      cursor: 1,
      continuation: workingContinuation,
    });
    const acknowledge = vi
      .spyOn(api, "acknowledgeNativeStructureRenderedReopen")
      .mockImplementation(() => new Promise(() => undefined));

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    await waitFor(() => expect(referenceRequest).toHaveBeenCalledTimes(2));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await new Promise((resolve) => window.setTimeout(resolve, 40));
    });
    expect(acknowledge).not.toHaveBeenCalled();

    resolveWorkingReference(workingReference);
    await waitFor(() => expect(acknowledge).toHaveBeenCalledTimes(1));
  });

  it("retries a failed authoritative terminal refresh and applies the next success", async () => {
    const activeReference: NativeStructureReferencePublicV1 = {
      ...reference,
      structure_projection: {
        ...reference.structure_projection,
        state: "WORKING_VERIFICATION_REQUIRED",
        attempt_id: "native-structure-handoff-attempt-retry-1",
        attempt_ordinal: 1,
        affected_scope_ids: ["doc-main"],
        pending_rendered_scope_ids: ["doc-main"],
        validation_status: "FAIL",
      },
      continuation: {
        ...reference.continuation,
        status: "VERIFYING",
        latest_handoff: {
          schema_version: "native-structure-agent-handoff/v1",
          handoff_id: "handoff-terminal-retry-1",
          project_id: "project-1",
          document_id: "document-1",
          source_sha256: reference.source_sha256,
          artifact_id: reference.artifact_id,
          command_id: "continue-terminal-retry-1",
          input_fingerprint: "b".repeat(64),
          status: "VERIFYING",
          feedback_ids: ["feedback-terminal-retry-1"],
          proposal_decision_ids: [],
          affected_node_ids: ["doc-main"],
          decision_counts: { CONFIRM: 0, UNDO: 0, KEEP_UNRESOLVED: 0 },
          requires_scoped_structure_revision: true,
          requires_scoped_evidence_recovery: false,
          preserve_unaffected_nodes: true,
          current_action: "正在等待 Host 终验。",
          created_at_utc: "2026-08-31T09:20:00Z",
        },
        message: "正在等待 Host 终验。",
      },
    };
    const terminalContinuation = {
      ...activeReference.continuation,
      status: "CONSUMED" as const,
      latest_handoff: {
        ...activeReference.continuation.latest_handoff!,
        status: "CONSUMED" as const,
        current_action: "Host 终验已完成。",
      },
      message: "本轮修改已完成。",
    };
    const acceptedReference: NativeStructureReferencePublicV1 = {
      ...reference,
      continuation: terminalContinuation,
    };
    const referenceRequest = vi
      .spyOn(api, "nativeStructureReference")
      .mockResolvedValueOnce(activeReference)
      .mockRejectedValueOnce(
        new ApiError("projection temporarily unavailable", 503, null),
      )
      .mockResolvedValueOnce(acceptedReference);
    vi.spyOn(api, "nativeStructureActivity").mockResolvedValue({
      schema_version: "native-structure-activity-stream/v1",
      source_sha256: reference.source_sha256,
      artifact_id: reference.artifact_id,
      handoff_id: "handoff-terminal-retry-1",
      events: [],
      cursor: 1,
      continuation: terminalContinuation,
    });

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    expect(
      await screen.findByText(/最新结构投影同步暂时延迟，正在重试/),
    ).toBeInTheDocument();
    await waitFor(() => expect(referenceRequest).toHaveBeenCalledTimes(3));
    await waitFor(() =>
      expect(
        screen.queryByText("工作投影 · 待原文复验"),
      ).not.toBeInTheDocument(),
    );
  });

  it("acks a single rendered scope only after the workbench opens it", async () => {
    const attempt = {
      schema_version: "native-structure-handoff-attempt-receipt/v1" as const,
      attempt_id: "native-structure-handoff-attempt-" + "1".repeat(32),
      attempt_ordinal: 1,
      status: "VERIFICATION_REQUIRED" as const,
      affected_scope_ids: ["doc-main"],
      tool_calls: [],
      candidate_revision_count: 1,
      rendered_output_sha256: "d".repeat(64),
      validation_status: "FAIL" as const,
      selected_pattern_ids: ["confirmed_proposal_not_verified"],
      accepted_work_item_count: 0,
      current_action: "正在等待渲染复验。",
      public_summary: "局部候选已经物化。",
      kit_version: "0.8.9",
      kit_bundle_sha256: "e".repeat(64),
      created_at_utc: "2026-08-28T01:00:00Z",
    };
    const verifyingReference: NativeStructureReferencePublicV1 = {
      ...reference,
      structure_projection: {
        ...reference.structure_projection,
        state: "WORKING_VERIFICATION_REQUIRED",
        attempt_id: attempt.attempt_id,
        attempt_ordinal: attempt.attempt_ordinal,
        affected_scope_ids: attempt.affected_scope_ids,
        pending_rendered_scope_ids: attempt.affected_scope_ids,
        validation_status: "FAIL",
      },
      continuation: {
        ...reference.continuation,
        status: "VERIFYING",
        latest_handoff: {
          schema_version: "native-structure-agent-handoff/v1",
          handoff_id: "handoff-render-1",
          project_id: "project-1",
          document_id: "document-1",
          source_sha256: reference.source_sha256,
          artifact_id: reference.artifact_id,
          command_id: "continue-render-1",
          input_fingerprint: "b".repeat(64),
          status: "VERIFYING",
          feedback_ids: ["feedback-render-1"],
          proposal_decision_ids: [],
          affected_node_ids: ["doc-main"],
          decision_counts: { CONFIRM: 0, UNDO: 0, KEEP_UNRESOLVED: 0 },
          requires_scoped_structure_revision: true,
          requires_scoped_evidence_recovery: false,
          preserve_unaffected_nodes: true,
          current_action: "正在等待渲染复验。",
          latest_attempt: attempt,
          created_at_utc: "2026-08-28T00:59:00Z",
        },
        message: "局部候选已经物化。",
      },
    };
    const acceptedAfterReopen: NativeStructureReferencePublicV1 = {
      ...reference,
      structure_projection: {
        ...reference.structure_projection,
        attempt_id: attempt.attempt_id,
        attempt_ordinal: attempt.attempt_ordinal,
      },
      continuation: {
        ...reference.continuation,
        status: "CONSUMED",
        latest_handoff: null,
        message: "本轮修改已完成。",
      },
    };
    const referenceRequest = vi
      .spyOn(api, "nativeStructureReference")
      .mockResolvedValueOnce({
        ...verifyingReference,
        structure_projection: {
          ...reference.structure_projection,
          state: "WORKING_VERIFICATION_REQUIRED",
          attempt_id: attempt.attempt_id,
          attempt_ordinal: attempt.attempt_ordinal,
          affected_scope_ids: attempt.affected_scope_ids,
          pending_rendered_scope_ids: attempt.affected_scope_ids,
          validation_status: "FAIL",
        },
      })
      .mockResolvedValueOnce(acceptedAfterReopen);
    vi.spyOn(api, "nativeStructureActivity").mockResolvedValue({
      schema_version: "native-structure-activity-stream/v1",
      source_sha256: reference.source_sha256,
      artifact_id: reference.artifact_id,
      handoff_id: "handoff-render-1",
      events: [],
      cursor: 0,
      continuation: verifyingReference.continuation,
    });
    const acknowledge = vi
      .spyOn(api, "acknowledgeNativeStructureRenderedReopen")
      .mockResolvedValue({
        schema_version: "native-structure-rendered-reopen-result/v1",
        reopen_receipt_sha256: "f".repeat(64),
        attempt: { ...attempt, status: "VERIFIED", validation_status: "PASS" },
        next_action: "COMPLETE",
      });

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    await waitFor(() => expect(acknowledge).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(referenceRequest).toHaveBeenCalledTimes(2));
    expect(
      screen.queryByText("工作投影 · 待原文复验"),
    ).not.toBeInTheDocument();
    expect(acknowledge.mock.calls[0]?.[2]).toEqual(
      expect.objectContaining({
        handoff_id: "handoff-render-1",
        attempt_id: attempt.attempt_id,
        scope_id: "doc-main",
        expected_projection_sha256:
          verifyingReference.structure_projection.projection_sha256,
        expected_rendered_output_sha256: attempt.rendered_output_sha256,
      }),
    );
  });

  it("accepts an attempt pending-scope subsequence inside a union working projection", async () => {
    const attempt = {
      schema_version: "native-structure-handoff-attempt-receipt/v1" as const,
      attempt_id: "native-structure-handoff-attempt-union-1",
      attempt_ordinal: 3,
      status: "VERIFICATION_REQUIRED" as const,
      affected_scope_ids: ["doc-main", "section-3.2.1"],
      pending_rendered_scope_ids: ["doc-main"],
      tool_calls: [],
      candidate_revision_count: 1,
      rendered_output_sha256: "d".repeat(64),
      validation_status: "FAIL" as const,
      selected_pattern_ids: ["confirmed_proposal_not_verified"],
      accepted_work_item_count: 0,
      current_action: "正在复验当前 lineage。",
      public_summary: "当前 lineage 等待复验。",
      kit_version: "0.8.9",
      kit_bundle_sha256: "e".repeat(64),
      created_at_utc: "2026-08-31T10:00:00Z",
    };
    const initialReference: NativeStructureReferencePublicV1 = {
      ...reference,
      structure_projection: {
        ...reference.structure_projection,
        state: "WORKING_VERIFICATION_REQUIRED",
        attempt_id: attempt.attempt_id,
        attempt_ordinal: attempt.attempt_ordinal,
        affected_scope_ids: attempt.affected_scope_ids,
        pending_rendered_scope_ids: ["doc-main"],
        validation_status: "FAIL",
      },
      continuation: {
        ...reference.continuation,
        status: "VERIFYING",
        latest_handoff: {
          schema_version: "native-structure-agent-handoff/v1",
          handoff_id: "handoff-union-1",
          project_id: "project-1",
          document_id: "document-1",
          source_sha256: reference.source_sha256,
          artifact_id: reference.artifact_id,
          command_id: "continue-union-1",
          input_fingerprint: "b".repeat(64),
          status: "VERIFYING",
          feedback_ids: ["feedback-union-1"],
          proposal_decision_ids: [],
          affected_node_ids: attempt.affected_scope_ids,
          decision_counts: { CONFIRM: 0, UNDO: 0, KEEP_UNRESOLVED: 0 },
          requires_scoped_structure_revision: true,
          requires_scoped_evidence_recovery: false,
          preserve_unaffected_nodes: true,
          current_action: "正在复验当前 lineage。",
          latest_attempt: attempt,
          created_at_utc: "2026-08-31T09:59:00Z",
        },
        message: "正在复验当前 lineage。",
      },
    };
    const nextAttempt = {
      ...attempt,
      pending_rendered_scope_ids: ["section-3.2.1", "doc-groups"],
      rendered_reopened_scope_ids: ["doc-main"],
      current_action: "继续复验当前 lineage 的其余范围。",
    };
    const unionReference: NativeStructureReferencePublicV1 = {
      ...initialReference,
      structure_projection: {
        ...initialReference.structure_projection,
        affected_scope_ids: [
          "unrelated-before",
          "doc-main",
          "section-3.2.1",
          "doc-groups",
          "unrelated-after",
        ],
        pending_rendered_scope_ids: [
          "unrelated-before",
          "section-3.2.1",
          "unrelated-between",
          "doc-groups",
          "unrelated-after",
        ],
      },
      continuation: {
        ...initialReference.continuation,
        latest_handoff: {
          ...initialReference.continuation.latest_handoff!,
          latest_attempt: nextAttempt,
          current_action: nextAttempt.current_action,
        },
      },
    };
    const referenceRequest = vi
      .spyOn(api, "nativeStructureReference")
      .mockResolvedValueOnce(initialReference)
      .mockResolvedValue(unionReference);
    vi.spyOn(api, "nativeStructureActivity").mockResolvedValue({
      schema_version: "native-structure-activity-stream/v1",
      source_sha256: reference.source_sha256,
      artifact_id: reference.artifact_id,
      handoff_id: "handoff-union-1",
      events: [],
      cursor: 0,
      continuation: initialReference.continuation,
    });
    vi.spyOn(api, "acknowledgeNativeStructureRenderedReopen").mockResolvedValue({
      schema_version: "native-structure-rendered-reopen-result/v1",
      reopen_receipt_sha256: "f".repeat(64),
      attempt: nextAttempt,
      next_action: "REOPEN_NEXT_SCOPE",
    });

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    await waitFor(() => expect(referenceRequest).toHaveBeenCalledTimes(2));
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 320));
    });
    expect(referenceRequest).toHaveBeenCalledTimes(2);
    expect(
      screen.queryByText(/最新结构投影.*重试/),
    ).not.toBeInTheDocument();
  });

  it("treats a verified handoff as complete when another handoff becomes the working head", async () => {
    const verifiedAttempt = {
      schema_version: "native-structure-handoff-attempt-receipt/v1" as const,
      attempt_id: "native-structure-handoff-attempt-promoted-old",
      attempt_ordinal: 4,
      status: "VERIFICATION_REQUIRED" as const,
      affected_scope_ids: ["doc-main"],
      pending_rendered_scope_ids: ["doc-main"],
      tool_calls: [],
      candidate_revision_count: 1,
      rendered_output_sha256: "d".repeat(64),
      validation_status: "FAIL" as const,
      selected_pattern_ids: ["confirmed_proposal_not_verified"],
      accepted_work_item_count: 0,
      current_action: "正在复验旧 handoff。",
      public_summary: "旧 handoff 等待复验。",
      kit_version: "0.8.9",
      kit_bundle_sha256: "e".repeat(64),
      created_at_utc: "2026-08-31T10:10:00Z",
    };
    const oldReference: NativeStructureReferencePublicV1 = {
      ...reference,
      structure_projection: {
        ...reference.structure_projection,
        state: "WORKING_VERIFICATION_REQUIRED",
        attempt_id: verifiedAttempt.attempt_id,
        attempt_ordinal: verifiedAttempt.attempt_ordinal,
        affected_scope_ids: verifiedAttempt.affected_scope_ids,
        pending_rendered_scope_ids: verifiedAttempt.pending_rendered_scope_ids,
        validation_status: "FAIL",
      },
      continuation: {
        ...reference.continuation,
        status: "VERIFYING",
        latest_handoff: {
          schema_version: "native-structure-agent-handoff/v1",
          handoff_id: "handoff-promoted-old",
          project_id: "project-1",
          document_id: "document-1",
          source_sha256: reference.source_sha256,
          artifact_id: reference.artifact_id,
          command_id: "continue-promoted-old",
          input_fingerprint: "b".repeat(64),
          status: "VERIFYING",
          feedback_ids: ["feedback-promoted-old"],
          proposal_decision_ids: [],
          affected_node_ids: ["doc-main"],
          decision_counts: { CONFIRM: 0, UNDO: 0, KEEP_UNRESOLVED: 0 },
          requires_scoped_structure_revision: true,
          requires_scoped_evidence_recovery: false,
          preserve_unaffected_nodes: true,
          current_action: "正在复验旧 handoff。",
          latest_attempt: verifiedAttempt,
          created_at_utc: "2026-08-31T10:09:00Z",
        },
        message: "正在复验旧 handoff。",
      },
    };
    const promotedAttempt = {
      ...verifiedAttempt,
      attempt_id: "native-structure-handoff-attempt-promoted-new",
      attempt_ordinal: 1,
      affected_scope_ids: ["another-scope"],
      pending_rendered_scope_ids: ["another-scope"],
      current_action: "另一个 handoff 正在等待复验。",
    };
    const promotedReference: NativeStructureReferencePublicV1 = {
      ...oldReference,
      structure_projection: {
        ...oldReference.structure_projection,
        attempt_id: promotedAttempt.attempt_id,
        attempt_ordinal: promotedAttempt.attempt_ordinal,
        affected_scope_ids: promotedAttempt.affected_scope_ids,
        pending_rendered_scope_ids: promotedAttempt.pending_rendered_scope_ids,
      },
      continuation: {
        ...oldReference.continuation,
        latest_handoff: {
          ...oldReference.continuation.latest_handoff!,
          handoff_id: "handoff-promoted-new",
          command_id: "continue-promoted-new",
          feedback_ids: ["feedback-promoted-new"],
          affected_node_ids: ["another-scope"],
          latest_attempt: promotedAttempt,
          current_action: promotedAttempt.current_action,
        },
      },
    };
    const referenceRequest = vi
      .spyOn(api, "nativeStructureReference")
      .mockResolvedValueOnce(oldReference)
      .mockResolvedValue(promotedReference);
    vi.spyOn(api, "nativeStructureActivity")
      .mockResolvedValueOnce({
        schema_version: "native-structure-activity-stream/v1",
        source_sha256: reference.source_sha256,
        artifact_id: reference.artifact_id,
        handoff_id: "handoff-promoted-old",
        events: [],
        cursor: 0,
        continuation: oldReference.continuation,
      })
      .mockResolvedValue({
        schema_version: "native-structure-activity-stream/v1",
        source_sha256: reference.source_sha256,
        artifact_id: reference.artifact_id,
        handoff_id: "handoff-promoted-new",
        events: [],
        cursor: 0,
        continuation: promotedReference.continuation,
      });
    vi.spyOn(api, "acknowledgeNativeStructureRenderedReopen").mockResolvedValue({
      schema_version: "native-structure-rendered-reopen-result/v1",
      reopen_receipt_sha256: "f".repeat(64),
      attempt: {
        ...verifiedAttempt,
        status: "VERIFIED",
        validation_status: "PASS",
        pending_rendered_scope_ids: [],
        rendered_reopened_scope_ids: ["doc-main"],
      },
      next_action: "COMPLETE",
    });

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );

    await waitFor(() => expect(referenceRequest).toHaveBeenCalledTimes(2));
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 320));
    });
    expect(referenceRequest).toHaveBeenCalledTimes(2);
    expect(
      screen.queryByText(/最新结构投影.*重试/),
    ).not.toBeInTheDocument();
  });

  it("reopens the next pending scope from a multi-scope attempt", async () => {
    const onPageChange = vi.fn();
    const attempt = {
      schema_version: "native-structure-handoff-attempt-receipt/v1" as const,
      attempt_id: "native-structure-handoff-attempt-" + "2".repeat(32),
      attempt_ordinal: 2,
      status: "VERIFICATION_REQUIRED" as const,
      affected_scope_ids: ["doc-main", "section-3.2.1"],
      rendered_reopened_scope_ids: ["doc-main"],
      pending_rendered_scope_ids: ["section-3.2.1"],
      tool_calls: [],
      candidate_revision_count: 0,
      rendered_output_sha256: "d".repeat(64),
      validation_status: "FAIL" as const,
      selected_pattern_ids: ["confirmed_proposal_not_verified"],
      accepted_work_item_count: 0,
      default_applied_work_item_count: 0,
      current_action: "已复验第一个范围，正在打开下一个范围。",
      public_summary: "范围 1/2 已复验。",
      kit_version: "0.8.9",
      kit_bundle_sha256: "e".repeat(64),
      created_at_utc: "2026-08-28T04:00:00Z",
    };
    const verifyingReference: NativeStructureReferencePublicV1 = {
      ...reference,
      structure_projection: {
        ...reference.structure_projection,
        state: "WORKING_VERIFICATION_REQUIRED",
        attempt_id: attempt.attempt_id,
        attempt_ordinal: attempt.attempt_ordinal,
        affected_scope_ids: attempt.affected_scope_ids,
        pending_rendered_scope_ids: attempt.pending_rendered_scope_ids,
        validation_status: "FAIL",
      },
      continuation: {
        ...reference.continuation,
        status: "VERIFYING",
        latest_handoff: {
          schema_version: "native-structure-agent-handoff/v1",
          handoff_id: "handoff-render-multi-1",
          project_id: "project-1",
          document_id: "document-1",
          source_sha256: reference.source_sha256,
          artifact_id: reference.artifact_id,
          command_id: "continue-render-multi-1",
          input_fingerprint: "b".repeat(64),
          status: "VERIFYING",
          feedback_ids: ["feedback-render-multi-1"],
          proposal_decision_ids: [],
          affected_node_ids: ["doc-main", "section-3.2.1"],
          decision_counts: { CONFIRM: 0, UNDO: 0, KEEP_UNRESOLVED: 0 },
          requires_scoped_structure_revision: true,
          requires_scoped_evidence_recovery: false,
          preserve_unaffected_nodes: true,
          current_action: "已复验第一个范围，正在打开下一个范围。",
          latest_attempt: attempt,
          created_at_utc: "2026-08-28T03:59:00Z",
        },
        message: "正在按来源顺序复验多个范围。",
      },
    };
    const acceptedAfterMultiReopen: NativeStructureReferencePublicV1 = {
      ...reference,
      structure_projection: {
        ...reference.structure_projection,
        attempt_id: attempt.attempt_id,
        attempt_ordinal: attempt.attempt_ordinal,
      },
      continuation: {
        ...reference.continuation,
        status: "CONSUMED",
        latest_handoff: null,
        message: "本轮修改已完成。",
      },
    };
    vi.spyOn(api, "nativeStructureReference")
      .mockResolvedValueOnce(verifyingReference)
      .mockResolvedValueOnce(acceptedAfterMultiReopen);
    vi.spyOn(api, "nativeStructureActivity").mockResolvedValue({
      schema_version: "native-structure-activity-stream/v1",
      source_sha256: reference.source_sha256,
      artifact_id: reference.artifact_id,
      handoff_id: "handoff-render-multi-1",
      events: [],
      cursor: 0,
      continuation: verifyingReference.continuation,
    });
    vi.spyOn(api, "nativeStructureTablePreview").mockResolvedValue(tablePreview);
    const acknowledge = vi
      .spyOn(api, "acknowledgeNativeStructureRenderedReopen")
      .mockResolvedValue({
        schema_version: "native-structure-rendered-reopen-result/v1",
        reopen_receipt_sha256: "f".repeat(64),
        attempt: {
          ...attempt,
          status: "VERIFIED",
          validation_status: "PASS",
          rendered_reopened_scope_ids: ["doc-main", "section-3.2.1"],
          pending_rendered_scope_ids: [],
        },
        next_action: "COMPLETE",
      });

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={onPageChange}
      />,
    );

    await waitFor(() => expect(acknowledge).toHaveBeenCalledTimes(1));
    expect(acknowledge.mock.calls[0]?.[2]).toEqual(
      expect.objectContaining({
        handoff_id: "handoff-render-multi-1",
        attempt_id: attempt.attempt_id,
        scope_id: "section-3.2.1",
      }),
    );
    expect(onPageChange).toHaveBeenCalledWith(10);
  });

  it("undoes an auto-applied proposal through the reversible projection", async () => {
    const user = userEvent.setup();
    const undoneReference: NativeStructureReferencePublicV1 = {
      ...reference,
      structure_proposals: reference.structure_proposals.map((proposal) => ({
        ...proposal,
        status: "UNDONE",
        decision: {
          schema_version: "native-structure-proposal-decision/v1",
          decision_id: "decision-1",
          project_id: "project-1",
          document_id: "document-1",
          source_sha256: reference.source_sha256,
          artifact_id: reference.artifact_id,
          command_id: "proposal-decision-1",
          proposal_id: proposal.proposal_id,
          object_id: proposal.object_id,
          node_id: proposal.node_id,
          action: "UNDO",
          created_at_utc: "2026-08-26T10:00:00Z",
        },
      })),
    };
    vi.spyOn(api, "nativeStructureReference")
      .mockResolvedValueOnce(reference)
      .mockResolvedValueOnce(undoneReference);
    vi.spyOn(api, "nativeStructureTablePreview").mockResolvedValue(tablePreview);
    const decision = vi
      .spyOn(api, "submitNativeStructureProposalDecision")
      .mockResolvedValue({
        schema_version: "native-structure-proposal-decision-result/v1",
        proposal_status: "UNDONE",
        decision: undoneReference.structure_proposals[0].decision!,
      });

    render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-1"
        sourceSha256={reference.source_sha256}
        onPageChange={vi.fn()}
      />,
    );
    await user.click(await screen.findByRole("button", { name: "展开 3 车型/产品信息" }));
    await user.click(
      screen.getByRole("button", { name: "展开 3.2 产品技术要求及说明" }),
    );
    await user.click(screen.getByRole("button", { name: /3.2.1 产品信息/ }));
    await user.click(screen.getByRole("button", { name: "撤销" }));

    await waitFor(() => expect(decision).toHaveBeenCalledTimes(1));
    expect(decision.mock.calls[0]?.[2]).toEqual(
      expect.objectContaining({
        proposal_id: "proposal-image-1",
        action: "UNDO",
      }),
    );
    expect(await screen.findByText("已撤销")).toBeVisible();
    expect(screen.getByText(/图片保留为未决证据/)).toBeVisible();
  });

  it("stays absent when the current document has no native candidate", async () => {
    vi.spyOn(api, "nativeStructureReference").mockRejectedValue(
      new ApiError("not found", 404, null),
    );
    const { container } = render(
      <NativeStructureReferenceDock
        projectId="project-1"
        documentId="document-2"
        sourceSha256={"b".repeat(64)}
        onPageChange={vi.fn()}
      />,
    );

    await waitFor(() =>
      expect(
        screen.queryByLabelText("Agent 原文结构草案"),
      ).not.toBeInTheDocument(),
    );
    expect(container).toBeEmptyDOMElement();
  });
});

it("renders an external producer through the existing tree, details and PDF without native runtime reads", async () => {
  const native = vi.spyOn(api, "nativeStructureReference");
  const working = vi.spyOn(api, "nativeStructureWorkingRevisionHead");
  const activity = vi.spyOn(api, "nativeStructureActivity");
  const saveFeedback = vi.fn(async () => {});
  const reader = { revision_id: "f".repeat(64), source_sha256: reference.source_sha256,
    reference: { ...reference, continuation: { ...reference.continuation, can_continue: false } },
    outline: null, tables: {}, reasoning: null, review: null, saveFeedback,
    loadRecord: vi.fn() };
  render(<NativeStructureReferenceDock projectId="external-project" documentId="external-document"
    sourceSha256={reference.source_sha256} onPageChange={vi.fn()} sourceFileUrl="/original.pdf" showSourcePreview sourceReader={reader} />);
  expect(await screen.findByRole("tree", {name:"原文标题层级"})).toBeInTheDocument();
  expect(screen.getByRole("separator", {name:"调整标题树和原文条目宽度"})).toBeInTheDocument();
  expect(screen.getByRole("region", {name:"原文 PDF 对照"})).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("给 Agent 的节点指引"), {target:{value:"保留原文所属关系"}});
  fireEvent.click(screen.getByRole("button", {name:"保存指引"}));
  await waitFor(()=>expect(saveFeedback).toHaveBeenCalledWith("保留原文所属关系", "doc-main", []));
  expect(native).not.toHaveBeenCalled(); expect(working).not.toHaveBeenCalled(); expect(activity).not.toHaveBeenCalled();
});

it("keeps legacy structure proposal decisions readonly in a hosted saved result",async()=>{
  const decide=vi.spyOn(api,"submitNativeStructureProposalDecision");
  const reader={revision_id:"f".repeat(64),source_sha256:reference.source_sha256,reference,
    outline:null,tables:{},reasoning:null,review:null,saveFeedback:vi.fn(),loadRecord:vi.fn()};
  render(<WorkbenchHostContext.Provider value={{readOnly:true,resourceUrl:path=>path,pdfViewerUrl:()=>"/pdf-viewer"}}>
    <NativeStructureReferenceDock projectId="readonly" documentId="document" sourceSha256={reference.source_sha256} sourceReader={reader} onPageChange={vi.fn()}/>
  </WorkbenchHostContext.Provider>);
  fireEvent.click(await screen.findByRole("button",{name:"展开 3 车型/产品信息"}));
  fireEvent.click(screen.getByRole("button",{name:"展开 3.2 产品技术要求及说明"}));
  fireEvent.click(screen.getByRole("button",{name:/3.2.1 产品信息/}));
  const undo=screen.getByRole("button",{name:"撤销"});expect(undo).toBeDisabled();fireEvent.click(undo);
  expect(decide).not.toHaveBeenCalled();expect(screen.queryByLabelText("给 Agent 的节点指引")).not.toBeInTheDocument();
});

it("preserves selected structure and expansion after a response-only result revision",async()=>{
  const reader={revision_id:'e'.repeat(64),source_sha256:reference.source_sha256,reference,
    outline:{baseline:{artifact_sha256:'same-ledger'}},tables:{},reasoning:null,review:null,saveFeedback:vi.fn(),loadRecord:vi.fn()} as unknown as NonNullable<Parameters<typeof NativeStructureReferenceDock>[0]['sourceReader']>;
  const props={projectId:'response-preserve',documentId:'document',sourceSha256:reference.source_sha256,onPageChange:vi.fn()};
  const page=render(<NativeStructureReferenceDock {...props} sourceReader={reader}/>);
  fireEvent.click(await screen.findByRole('button',{name:'展开 3 车型/产品信息'}));
  fireEvent.click(screen.getByRole('button',{name:'展开 3.2 产品技术要求及说明'}));
  const target=screen.getByRole('button',{name:/3.2.1 产品信息/});fireEvent.click(target);
  expect(target.closest('[role=treeitem]')).toHaveAttribute('aria-selected','true');
  page.rerender(<NativeStructureReferenceDock {...props} sourceReader={{...reader,revision_id:'f'.repeat(64)}}/>);
  await waitFor(()=>expect(screen.getByRole('button',{name:/3.2.1 产品信息/}).closest('[role=treeitem]')).toHaveAttribute('aria-selected','true'));
  expect(screen.getByRole('button',{name:'收起 3.2 产品技术要求及说明'})).toBeInTheDocument();
});
