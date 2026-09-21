import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../../shared/api/client";
import type {
  EntryClassificationResponse,
  JsonRecord,
  RequirementAtomizationScopeState,
  RequirementSemanticPublicationProjectionRowV2,
  RequirementSemanticPublicationProjectionV2,
  RequirementSemanticPublicationPreviewResponseV2,
  RequirementSemanticPublishDraftResponseV2,
  RequirementTreeSnapshot,
} from "../../shared/api/contracts";
import {
  canonicalJson,
  canonicalJsonSha256,
  withoutCanonicalField,
} from "./canonicalJsonSha256";
import canonicalHashFixture from "../../../../../prototype/contracts/fixtures/semantic_publication_v2_canonical_hash.json";
import { parseJsonWithNumberSources } from "../../shared/api/jsonNumberSources";
import {
  assertRequirementSemanticPublicationPreviewV2,
  assertRequirementSemanticPublishDraftV2,
  buildRequirementSemanticPublicationPreviewRequestV2,
  exactRequirementSemanticPublicationCapabilityV2,
} from "./semanticPublicationV2Model";

const sha = (value: string) => value.repeat(64).slice(0, 64);
const bindingPreviewId =
  "requirement-enrichment-binding-preview-" + "1".repeat(32);
const bindingId = "requirement-enrichment-binding-" + "2".repeat(32);

afterEach(() => {
  vi.unstubAllGlobals();
});

function projectionRow(
  overrides: Partial<RequirementSemanticPublicationProjectionRowV2>,
): RequirementSemanticPublicationProjectionRowV2 {
  const id = String(overrides.requirement_tree_node_id || "technical");
  return {
    requirement_tree_node_id: id,
    source_working_tree_node_id: id,
    parent_requirement_tree_node_id: null,
    workflow_node_id: `workflow-${id}`,
    atomization_node_id: null,
    atomic_requirement_id: null,
    entry_kind: "REQUIREMENT",
    builder_kind: "REQUIREMENT",
    decision_mode: "DIRECT",
    classification: "TECHNICAL",
    builder_binding_role: "REQUIREMENT",
    final_projection_role: "REQUIREMENT",
    intrinsic_analysis_eligible: true,
    default_disposition: "ANALYZE",
    source_order: 0,
    source_offset: 0,
    label: id,
    original_statement: `${id} original`,
    published_statement: `${id} published`,
    structure_node_id: `root-${id}`,
    page_start: 1,
    page_end: 1,
    source_spans: [],
    source_refs: [
      {
        kind: "SOURCE_REF",
        source_id: `source-${id}`,
        owner_structure_node_id: `root-${id}`,
        start_offset: null,
        end_offset: null,
        page_start: 1,
        page_end: 1,
      },
    ],
    status: "PENDING",
    table_record_id: null,
    table_evidence_binding_id: null,
    deterministic_rule_evidence: [],
    include_in_view: true,
    content_blocks: [],
    editable_structure_node_ids: [],
    table_refs: [],
    figure_refs: [],
    ...overrides,
  };
}

function sealProjection(
  projection: RequirementSemanticPublicationProjectionV2,
): RequirementSemanticPublicationProjectionV2 {
  projection.projection_sha256 = canonicalJsonSha256(
    withoutCanonicalField(
      projection as unknown as JsonRecord,
      "projection_sha256",
    ),
  );
  return projection;
}

function resealPreviewResponse(
  response: RequirementSemanticPublicationPreviewResponseV2,
): RequirementSemanticPublicationPreviewResponseV2 {
  sealProjection(response.binding_preview.projection);
  response.binding_preview.payload_sha256 = canonicalJsonSha256(
    withoutCanonicalField(
      response.binding_preview as unknown as JsonRecord,
      "payload_sha256",
    ),
  );
  return response;
}

function resealPublishResponse(
  response: RequirementSemanticPublishDraftResponseV2,
): RequirementSemanticPublishDraftResponseV2 {
  response.publication.requirement_tree_sha256 = canonicalJsonSha256(
    response.requirement_tree,
  );
  response.publication.payload_sha256 = canonicalJsonSha256(
    withoutCanonicalField(response.publication, "payload_sha256"),
  );
  response.binding.sha256 = String(response.publication.payload_sha256);
  return response;
}

function acceptedClassification(): EntryClassificationResponse {
  return {
    schema_version: "entry-classification-response/v1",
    snapshot: {
      schema_version: "entry-classification-snapshot/v1",
      snapshot_id: "accepted-classification",
      project_id: "project",
      sor_document_id: "document",
      source_sha256: sha("a"),
      working_tree_id: "working-tree",
      working_tree_sha256: sha("b"),
      effective_structure_id: "effective",
      semantic_snapshot_id: "semantic",
      policy_version: "policy/v1",
      status: "ACCEPTED",
      revision: 3,
      entries: [
        {
          tree_node_id: "technical",
          workflow_node_id: "workflow-technical",
          projection_role: "STRUCTURE_PROXY",
          suggested_type: "TECHNICAL",
          suggestion_source: "MODEL",
          effective_type: "TECHNICAL",
          human_override: null,
          reason_codes: [],
          confidence: 0.9,
        },
        {
          tree_node_id: "non-technical",
          workflow_node_id: "workflow-non-technical",
          projection_role: "DIRECT_CONTENT",
          suggested_type: "NON_TECHNICAL",
          suggestion_source: "MODEL",
          effective_type: "NON_TECHNICAL",
          human_override: null,
          reason_codes: [],
          confidence: 0.9,
        },
        {
          tree_node_id: "information",
          workflow_node_id: "workflow-information",
          projection_role: "STRUCTURE_PROXY",
          suggested_type: "INFORMATION",
          suggestion_source: "MODEL",
          effective_type: "INFORMATION",
          human_override: null,
          reason_codes: [],
          confidence: 0.9,
        },
      ],
      supersedes_id: "working-classification",
      payload_sha256: sha("c"),
      content_sha256: sha("d"),
      created_at: "2026-08-07T00:00:00Z",
      accepted_at: "2026-08-07T00:01:00Z",
      accepted_by: "USER",
    },
    counts: {
      total: 3,
      technical: 1,
      non_technical: 1,
      information: 1,
      unclassified: 0,
      human_override: 0,
    },
    can_accept: false,
    can_publish: true,
    actions: {
      can_override: false,
      can_accept: false,
      can_publish: true,
    },
    changed: false,
    serving_impact: "NONE",
  };
}

function capability() {
  return {
    schema_version:
      "requirement-enrichment-semantic-publication-capability/v2",
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

function scopeState(
  overrides: Partial<RequirementAtomizationScopeState> = {},
): RequirementAtomizationScopeState {
  return {
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
    current_scope_revision: 4,
    eligible_review_node_ids: ["review-1"],
    eligible_review_node_count: 1,
    classification_binding_state: "CURRENT",
    classification_snapshot_id: "accepted-classification",
    classification_revision: 3,
    classification_sha256: sha("c"),
    classification_content_sha256: sha("d"),
    can_undo: false,
    current_atomization_run_id: "",
    current_atomization_run_revision: 0,
    current_receipt: null,
    last_attempt: null,
    ...overrides,
  };
}

const counts = {
  total: 3,
  default_analyze: 1,
  default_retain: 2,
  reference: 1,
  atomized: 0,
};

function previewResponse(
  overrides: Partial<RequirementSemanticPublicationPreviewResponseV2> = {},
): RequirementSemanticPublicationPreviewResponseV2 {
  const projection = sealProjection({
    schema_version: "requirement-enrichment-publication-projection/v2",
    project_id: "project",
    document_id: "document",
    source_sha256: sha("a"),
    effective_structure_id: "effective",
    accepted_working_tree_id: "working-tree",
    accepted_working_tree_sha256: sha("b"),
    entry_classification_snapshot_id: "accepted-classification",
    entry_classification_revision: 3,
    entry_classification_sha256: sha("c"),
    entry_classification_content_sha256: sha("d"),
    atomization_state: "NOT_RUN",
    atomization_scope_receipt_id: "",
    atomization_scope_receipt_sha256: "",
    atomization_scope_revision: 0,
    atomization_active_revision: 0,
    atomization_classification_content_sha256: "",
    atomization_run_id: "",
    atomization_revision: 0,
    atomization_run_sha256: "",
    publication_preview_id: "",
    publication_preview_sha256: "",
    publication_key: "",
    publication_lineage_sha256: "",
    applied_review_node_ids: [],
    source_business_tree_node_ids: [
      "technical",
      "non-technical",
      "information",
    ],
    rows: [
      projectionRow({
        requirement_tree_node_id: "technical",
        workflow_node_id: "workflow-technical",
        source_order: 1,
      }),
      projectionRow({
        requirement_tree_node_id: "non-technical",
        workflow_node_id: "workflow-non-technical",
        classification: "NON_TECHNICAL",
        builder_binding_role: "DIRECT_CONTENT",
        final_projection_role: "DIRECT_CONTENT",
        default_disposition: "RETAIN",
        source_order: 3,
      }),
      projectionRow({
        requirement_tree_node_id: "information",
        workflow_node_id: "workflow-information",
        classification: "INFORMATION",
        entry_kind: "REFERENCE",
        decision_mode: "NONE",
        builder_binding_role: "STRUCTURE_PROXY",
        final_projection_role: "STRUCTURE_PROXY",
        intrinsic_analysis_eligible: false,
        default_disposition: "RETAIN",
        source_order: 5,
      }),
    ],
    projection_sha256: sha("0"),
  });
  const bindingPreview = {
    schema_version:
      "requirement-enrichment-publication-binding-preview/v2" as const,
    binding_preview_id: bindingPreviewId,
    project_id: "project",
    document_id: "document",
    session_id: "review-session",
    review_revision: 7,
    base_active_requirement_set_id: "set-active",
    projection,
    payload_sha256: "",
  };
  bindingPreview.payload_sha256 = canonicalJsonSha256(
    withoutCanonicalField(
      bindingPreview as unknown as JsonRecord,
      "payload_sha256",
    ),
  );
  return {
    schema_version: "requirement-enrichment-publication-preview-response/v2",
    project_id: "project",
    document_id: "document",
    serving_impact: "NONE",
    analysis_started: false,
    atomization_invoked: false,
    classification: {
      snapshot_id: "accepted-classification",
      revision: 3,
      sha256: sha("c"),
    },
    binding_preview: bindingPreview,
    counts,
    ...overrides,
  };
}

function publishedTree(): RequirementTreeSnapshot {
  const rows = previewResponse().binding_preview.projection.rows;
  const entryIdByNodeId = new Map([
    ["technical", "entry-technical"],
    ["non-technical", "entry-non-technical"],
    ["information", "entry-information"],
  ]);
  const evidenceByNodeId = new Map<string, JsonRecord>();
  for (const row of rows) {
    const entryId = entryIdByNodeId.get(row.requirement_tree_node_id) || "";
    const evidenceSha256 = canonicalJsonSha256({
      atomic_requirement_id: row.atomic_requirement_id,
      requirement_entry_id: entryId,
      source_refs: row.source_refs,
      source_spans: row.source_spans,
      table_evidence_binding_id: row.table_evidence_binding_id,
    });
    evidenceByNodeId.set(row.requirement_tree_node_id, {
      schema_version: "requirement-evidence-binding/v1",
      binding_id: `requirement-evidence-binding-${evidenceSha256.slice(0, 32)}`,
      requirement_entry_id: entryId,
      atomic_requirement_id: row.atomic_requirement_id,
      source_spans: row.source_spans,
      source_refs: row.source_refs,
      table_evidence_binding_id: row.table_evidence_binding_id,
      evidence_sha256: evidenceSha256,
    });
  }
  const node = (
    id: string,
    overrides: Partial<RequirementTreeSnapshot["nodes"][number]> = {},
  ): RequirementTreeSnapshot["nodes"][number] => ({
    id,
    parent_id: null,
    kind: "SECTION",
    depth: 0,
    source_order: 0,
    source_offset: 0,
    label: id,
    summary: id,
    child_ids: [],
    structure_node_id: id,
    requirement_entry_id: null,
    workflow_node_id: id,
    page_start: 1,
    page_end: 1,
    source_refs: [],
    requirement_count: 0,
    attention_count: 0,
    reviewed_count: 0,
    status: "PENDING",
    classification: "",
    projection_role: "STRUCTURE_PROXY",
    semantic_state: "",
    deterministic_rule_evidence: [],
    table_record_id: null,
    table_evidence_binding_id: null,
    requirement_evidence_binding_id: null,
    review_item_count: 0,
    include_in_view: true,
    content_blocks: [],
    editable_structure_node_ids: [],
    toc_entries: [],
    table_refs: [],
    figure_refs: [],
    ...overrides,
  });
  const businessNodes = rows.map((row) => {
    const evidence = evidenceByNodeId.get(row.requirement_tree_node_id)!;
    const parentId = `root-${row.requirement_tree_node_id}`;
    return node(row.requirement_tree_node_id, {
      parent_id: parentId,
      depth: 1,
      kind:
        row.final_projection_role === "DIRECT_CONTENT"
          ? "DIRECT_CONTENT"
          : row.final_projection_role === "REQUIREMENT"
            ? "REQUIREMENT"
            : "CLAUSE",
      source_order: row.source_order,
      source_offset: row.source_offset,
      label: row.label,
      summary: row.published_statement,
      child_ids: [],
      structure_node_id:
        row.final_projection_role === "DIRECT_CONTENT"
          ? parentId
          : row.structure_node_id,
      requirement_entry_id: entryIdByNodeId.get(row.requirement_tree_node_id),
      workflow_node_id: row.workflow_node_id,
      page_start: row.page_start,
      page_end: row.page_end,
      source_refs: row.source_refs,
      requirement_count: 1,
      classification: row.classification,
      projection_role:
        row.final_projection_role as RequirementTreeSnapshot["nodes"][number]["projection_role"],
      semantic_state: row.entry_kind === "REQUIREMENT" ? "PROPOSED" : "",
      deterministic_rule_evidence: row.deterministic_rule_evidence,
      table_record_id: row.table_record_id,
      table_evidence_binding_id: row.table_evidence_binding_id,
      requirement_evidence_binding_id: String(evidence.binding_id),
      review_item_count: 1,
      include_in_view: row.include_in_view,
      content_blocks: row.content_blocks,
      editable_structure_node_ids: row.editable_structure_node_ids,
      table_refs: row.table_refs,
      figure_refs: row.figure_refs,
    });
  });
  const roots = rows.map((row, index) =>
    node(`root-${row.requirement_tree_node_id}`, {
      source_order: index * 2,
      child_ids: [row.requirement_tree_node_id],
      requirement_count: 1,
      review_item_count: 1,
    }),
  );
  const orderedNodes = roots.flatMap((root, index) => [
    root,
    businessNodes[index],
  ]);
  const requirementBindings = rows.map((row) =>
    evidenceByNodeId.get(row.requirement_tree_node_id)!,
  );
  const bundleBase = {
    schema_version: "requirement-evidence-bundle/v1",
    source_ir_id: "source-ir",
    source_sha256: sha("a"),
    structure_snapshot_id: "effective",
    table_bindings: [],
    requirement_bindings: requirementBindings,
  };
  return {
    schema_version: "requirement-tree-snapshot/v1",
    source_identity: {
      project_id: "project",
      document_id: "document",
      source_ir_id: "source-ir",
      source_sha256: sha("a"),
      preview_generation_id: "preview",
      requirement_set_id: "draft-set",
      geometry_index_id: "",
      geometry_index_sha256: "",
    },
    structure_identity: {
      base_snapshot_id: "base",
      effective_snapshot_id: "effective",
      effective_tree_sha256: sha("6"),
      revision: 1,
    },
    mode: "REQUIREMENT_SET",
    state: "READY",
    root_node_ids: roots.map((root) => root.id),
    nodes: orderedNodes,
    table_record_projections: [],
    table_evidence_bindings: [],
    requirement_evidence_bundle: {
      ...bundleBase,
      evidence_sha256: canonicalJsonSha256({
        requirement_bindings: requirementBindings,
        source_ir_id: bundleBase.source_ir_id,
        source_sha256: bundleBase.source_sha256,
        structure_snapshot_id: bundleBase.structure_snapshot_id,
        table_bindings: bundleBase.table_bindings,
      }),
    },
  };
}

function workingTree(): RequirementTreeSnapshot {
  const tree = publishedTree();
  return {
    ...tree,
    mode: "WORKING",
    source_identity: {
      ...tree.source_identity,
      requirement_set_id: "set-active",
    },
    nodes: tree.nodes.map((node) => ({
      ...node,
      requirement_entry_id: null,
      requirement_evidence_binding_id: null,
    })),
    requirement_evidence_bundle: null,
  };
}

function publishResponse(
  overrides: Partial<RequirementSemanticPublishDraftResponseV2> = {},
): RequirementSemanticPublishDraftResponseV2 {
  const preview = previewResponse().binding_preview;
  const projection = preview.projection;
  const requirementEntryIds = [
    "entry-technical",
    "entry-non-technical",
    "entry-information",
  ];
  const rowBindings = projection.rows.map((row, index) => {
    const projectionRowSha256 = canonicalJsonSha256(row);
    const decisionTraceSha256 = canonicalJsonSha256({
      schema_version: "requirement-enrichment-decision-trace/v2",
      binding_id: bindingId,
      publication_order: index,
      requirement_entry_id: requirementEntryIds[index],
      projection_row_sha256: projectionRowSha256,
    });
    const bindingRow: JsonRecord = {
      schema_version: "requirement-enrichment-publication-row-binding/v2",
      binding_id: bindingId,
      publication_order: index,
      requirement_tree_node_id: row.requirement_tree_node_id,
      requirement_entry_id: requirementEntryIds[index],
      projection_row_sha256: projectionRowSha256,
      decision_trace_sha256: decisionTraceSha256,
      projection_row: row,
      payload_sha256: "",
    };
    bindingRow.payload_sha256 = canonicalJsonSha256(
      withoutCanonicalField(bindingRow, "payload_sha256"),
    );
    return bindingRow;
  });
  const tree = publishedTree();
  const publication: JsonRecord = {
    schema_version: "requirement-enrichment-publication-binding/v2",
    binding_id: bindingId,
    binding_preview_id: bindingPreviewId,
    binding_preview_sha256: preview.payload_sha256,
    project_id: "project",
    document_id: "document",
    session_id: "review-session",
    review_revision: 7,
    base_active_requirement_set_id: "set-active",
    projection_sha256: projection.projection_sha256,
    requirement_set_id: "draft-set",
    requirement_set_version: 2,
    requirement_tree_object_key: "trees/draft-set.json",
    requirement_tree_sha256: canonicalJsonSha256(tree),
    requirement_tree_schema_version: "requirement-tree-snapshot/v1",
    row_bindings: rowBindings,
    payload_sha256: "",
  };
  publication.payload_sha256 = canonicalJsonSha256(
    withoutCanonicalField(publication, "payload_sha256"),
  );
  return {
    schema_version: "requirement-enrichment-publish-draft-response/v2",
    project_id: "project",
    document_id: "document",
    serving_impact: "DRAFT_ONLY",
    analysis_started: false,
    requirement_set_created: true,
    requirement_set_activated: false,
    classification: {
      snapshot_id: "accepted-classification",
      revision: 3,
      sha256: sha("c"),
    },
    binding: {
      binding_id: bindingId,
      sha256: String(publication.payload_sha256),
      preview_id: bindingPreviewId,
      preview_sha256: preview.payload_sha256,
      state: "READY",
    },
    counts,
    publication,
    requirement_tree: tree,
    ...overrides,
  };
}

describe("semantic publication v2 model", () => {
  it("matches the backend-generated canonical JSON SHA-256 golden", () => {
    expect(canonicalJsonSha256({})).toBe(
      "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    );
    expect(canonicalJsonSha256(canonicalHashFixture.payload)).toBe(
      canonicalHashFixture.sha256,
    );
    const numericPayload = parseJsonWithNumberSources(
      canonicalHashFixture.numeric_canonical_json,
    );
    expect(canonicalJson(numericPayload)).toBe(
      canonicalHashFixture.numeric_canonical_json,
    );
    expect(canonicalJsonSha256(numericPayload)).toBe(
      canonicalHashFixture.numeric_sha256,
    );
    expect(canonicalJson(canonicalHashFixture.unicode_key_payload)).toBe(
      canonicalHashFixture.unicode_key_canonical_json,
    );
    expect(
      canonicalJsonSha256(canonicalHashFixture.unicode_key_payload),
    ).toBe(canonicalHashFixture.unicode_key_sha256);
  });

  it("preserves nested Python float tokens through API parsing and full preview verification", async () => {
    const classification = acceptedClassification();
    const currentScope = scopeState();
    const request = buildRequirementSemanticPublicationPreviewRequestV2({
      reviewRevision: 7,
      classification,
      scopeState: currentScope,
      projectId: "project",
      documentId: "document",
      sessionId: "review-session",
    });
    const response = previewResponse();
    response.binding_preview.projection.rows[0].content_blocks = [
      parseJsonWithNumberSources(
        '{"bbox":[0.0,1.0,1e-07,-0.0],"confidence":1.0,"kind":"table"}',
      ) as JsonRecord,
    ];
    resealPreviewResponse(response);
    const wireResponse = canonicalJson(response);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(wireResponse, {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );

    const parsed = await api.previewRequirementSemanticPublication(
      "project",
      "document",
      "review-session",
      request,
    );

    expect(
      assertRequirementSemanticPublicationPreviewV2(parsed, {
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
        reviewRevision: 7,
        baseActiveRequirementSetId: "set-active",
        classification,
        scopeState: currentScope,
        request,
      }).counts,
    ).toEqual(counts);
  });

  it("accepts only the exact no-atomization/no-analysis capability", () => {
    expect(exactRequirementSemanticPublicationCapabilityV2(capability())).not.toBeNull();
    expect(exactRequirementSemanticPublicationCapabilityV2(null)).toBeNull();
    expect(
      exactRequirementSemanticPublicationCapabilityV2({
        ...capability(),
        publish_starts_analysis: true,
      }),
    ).toBeNull();
    expect(
      exactRequirementSemanticPublicationCapabilityV2({
        ...capability(),
        unexpected_flag: false,
      }),
    ).toBeNull();
    expect(
      exactRequirementSemanticPublicationCapabilityV2({
        ...capability(),
        default_dispositions: {
          ...capability().default_dispositions,
          EXTRA: "RETAIN",
        },
      }),
    ).toBeNull();
    expect(
      exactRequirementSemanticPublicationCapabilityV2({
        ...capability(),
        default_dispositions: {
          ...capability().default_dispositions,
          INFORMATION: "RETAIN",
        },
      }),
    ).toBeNull();
  });

  it("omits every scope field for NOT_RUN and binds the exact receipt for APPLIED", () => {
    const accepted = acceptedClassification();
    expect(
      buildRequirementSemanticPublicationPreviewRequestV2({
        reviewRevision: 7,
        classification: accepted,
        scopeState: scopeState(),
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
      }),
    ).toEqual({
      schema_version: "requirement-enrichment-publication-preview-request/v2",
      expected_review_revision: 7,
      accepted_entry_classification_snapshot_id: "accepted-classification",
      accepted_entry_classification_revision: 3,
      accepted_entry_classification_sha256: sha("c"),
      atomization_state: "NOT_RUN",
    });

    const applied = buildRequirementSemanticPublicationPreviewRequestV2({
      reviewRevision: 7,
      classification: accepted,
      scopeState: scopeState({
        classification_binding_state: "CONTENT_EQUIVALENT",
        current_receipt: {
          schema_version: "atomization-scope-receipt/v1",
          receipt_id: "atomization-scope-receipt-" + "3".repeat(32),
          payload_sha256: sha("9"),
          scope_revision: 4,
          operation: "APPLY",
          scope: "BRANCH",
          requested_review_node_ids: ["review-1"],
          executed_review_node_ids: ["review-1"],
          new_review_node_ids: ["review-1"],
          reused_review_node_ids: [],
          skipped_targets: [],
          succeeded_review_node_ids: ["review-1"],
          failed_review_node_ids: [],
          pass_through_review_node_ids: [],
          cumulative_review_node_ids: ["review-1"],
          provider_call_count: 1,
          checkpoint_created_count: 1,
          checkpoint_reuse_count: 0,
          resumable: false,
        },
      }),
      projectId: "project",
      documentId: "document",
      sessionId: "review-session",
    });
    expect(applied).toEqual(
      expect.objectContaining({
        atomization_state: "APPLIED",
        atomization_scope_receipt_id:
          "atomization-scope-receipt-" + "3".repeat(32),
        atomization_scope_receipt_sha256: sha("9"),
        atomization_scope_revision: 4,
      }),
    );
  });

  it("accepts a fully bound APPLIED projection with exact run and source-span lineage", () => {
    const receipt = {
      schema_version: "atomization-scope-receipt/v1" as const,
      receipt_id: "atomization-scope-receipt-" + "3".repeat(32),
      payload_sha256: sha("9"),
      project_id: "project",
      document_id: "document",
      session_id: "review-session",
      review_revision: 7,
      lineage_sha256: sha("1"),
      entry_classification_snapshot_id: "accepted-classification",
      entry_classification_revision: 3,
      entry_classification_sha256: sha("c"),
      entry_classification_content_sha256: sha("d"),
      scope_revision: 6,
      active_revision: 6,
      previous_active_revision: 0,
      supersedes_receipt_id: "",
      command_id: "atomization-scope-command-" + "4".repeat(32),
      command_sha256: sha("4"),
      operation: "APPLY" as const,
      scope: "CURRENT" as const,
      target_scope_sha256: sha("4"),
      unselected_policy: "SOURCE_BOUND_PASSTHROUGH",
      requested_review_node_ids: ["workflow-technical"],
      executed_review_node_ids: ["workflow-technical"],
      new_review_node_ids: ["workflow-technical"],
      reused_review_node_ids: [],
      skipped_targets: [],
      succeeded_review_node_ids: ["workflow-technical"],
      failed_review_node_ids: [],
      pass_through_review_node_ids: [],
      cumulative_review_node_ids: ["workflow-technical"],
      provider_call_count: 1,
      checkpoint_created_count: 1,
      checkpoint_reuse_count: 0,
      resumable: false,
      atomization_run_id: "atomization-run-" + "5".repeat(32),
      atomization_run_revision: 6,
      created_at_utc: "2026-08-07T00:02:00Z",
    };
    const appliedScope = scopeState({
      current_scope_revision: 6,
      can_undo: true,
      current_atomization_run_id: receipt.atomization_run_id,
      current_atomization_run_revision: 6,
      current_receipt: receipt,
    });
    const request = buildRequirementSemanticPublicationPreviewRequestV2({
      reviewRevision: 7,
      classification: acceptedClassification(),
      scopeState: appliedScope,
      projectId: "project",
      documentId: "document",
      sessionId: "review-session",
    });
    const response = previewResponse();
    const projection = response.binding_preview.projection;
    projection.atomization_state = "APPLIED";
    projection.atomization_scope_receipt_id = receipt.receipt_id;
    projection.atomization_scope_receipt_sha256 = receipt.payload_sha256;
    projection.atomization_scope_revision = 6;
    projection.atomization_active_revision = 6;
    projection.atomization_classification_content_sha256 = sha("d");
    projection.atomization_run_id = receipt.atomization_run_id;
    projection.atomization_revision = 6;
    projection.atomization_run_sha256 = sha("5");
    projection.publication_preview_id =
      "requirement-publication-preview-" + "6".repeat(32);
    projection.publication_preview_sha256 = sha("6");
    projection.publication_key = sha("7");
    projection.publication_lineage_sha256 = sha("8");
    projection.applied_review_node_ids = ["workflow-technical"];
    projection.rows[0] = projectionRow({
      requirement_tree_node_id: "technical",
      workflow_node_id: "workflow-technical",
      atomization_node_id: "atomization-node-" + "9".repeat(32),
      atomic_requirement_id: "atomic-obligation-" + "a".repeat(32),
      source_spans: [
        {
          semantic_unit_id: "semantic-unit-" + "b".repeat(32),
          owner_structure_node_id: "structure-node-" + "c".repeat(32),
          start_offset: 0,
          end_offset: 20,
        },
      ],
      source_order: 1,
    });
    response.counts = { ...counts, atomized: 1 };
    resealPreviewResponse(response);
    expect(
      assertRequirementSemanticPublicationPreviewV2(response, {
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
        reviewRevision: 7,
        baseActiveRequirementSetId: "set-active",
        classification: acceptedClassification(),
        scopeState: appliedScope,
        request,
      }),
    ).toEqual(expect.objectContaining({ counts: { ...counts, atomized: 1 } }));
  });

  it("blocks a non-empty stale receipt instead of silently degrading it to NOT_RUN", () => {
    const receipt = {
      ...scopeState().current_receipt,
      schema_version: "atomization-scope-receipt/v1" as const,
      receipt_id: "atomization-scope-receipt-" + "3".repeat(32),
      payload_sha256: sha("9"),
      scope_revision: 4,
      operation: "APPLY" as const,
      scope: "BRANCH" as const,
      requested_review_node_ids: ["review-1"],
      executed_review_node_ids: ["review-1"],
      new_review_node_ids: ["review-1"],
      reused_review_node_ids: [],
      skipped_targets: [],
      succeeded_review_node_ids: ["review-1"],
      failed_review_node_ids: [],
      pass_through_review_node_ids: [],
      cumulative_review_node_ids: ["review-1"],
      provider_call_count: 1,
      checkpoint_created_count: 1,
      checkpoint_reuse_count: 0,
      resumable: false,
    };
    expect(() =>
      buildRequirementSemanticPublicationPreviewRequestV2({
        reviewRevision: 7,
        classification: acceptedClassification(),
        scopeState: scopeState({
          classification_binding_state: "STALE",
          current_receipt: receipt,
        }),
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
      }),
    ).toThrow(/重新执行或撤销原子化/);
  });

  it("blocks publication while an empty or first-run atomization attempt is still RUNNING", () => {
    expect(() =>
      buildRequirementSemanticPublicationPreviewRequestV2({
        reviewRevision: 7,
        classification: acceptedClassification(),
        scopeState: scopeState({
          current_receipt: null,
          last_attempt: {
            schema_version: "atomization-scope-attempt/v1",
            operation: "APPLY",
            scope: "ALL",
            status: "RUNNING",
            requested_review_node_ids: ["review-1"],
            executed_review_node_ids: ["review-1"],
            skipped_targets: [],
            cumulative_review_node_ids: [],
            completed_review_node_ids: [],
            failed_review_node_ids: [],
            checkpoint_created_review_node_ids: [],
            checkpoint_reused_review_node_ids: [],
            checkpoint_invalidated_review_node_ids: [],
            provider_call_count: 1,
            retryable: false,
            checkpoint_resumable: false,
            error_code: "",
          },
        }),
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
      }),
    ).toThrow(/仍在执行/);
  });

  it("validates the preview identity, side effects, binding preview and recomputed counts", () => {
    const request = buildRequirementSemanticPublicationPreviewRequestV2({
      reviewRevision: 7,
      classification: acceptedClassification(),
      scopeState: scopeState(),
      projectId: "project",
      documentId: "document",
      sessionId: "review-session",
    });
    expect(
      assertRequirementSemanticPublicationPreviewV2(previewResponse(), {
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
        reviewRevision: 7,
        baseActiveRequirementSetId: "set-active",
        classification: acceptedClassification(),
        scopeState: scopeState(),
        request,
      }),
    ).toEqual(
      expect.objectContaining({
        bindingPreviewId,
        bindingPreviewSha256:
          previewResponse().binding_preview.payload_sha256,
        counts,
      }),
    );
    expect(() =>
      assertRequirementSemanticPublicationPreviewV2(
        previewResponse({ atomization_invoked: true as false }),
        {
          projectId: "project",
          documentId: "document",
          sessionId: "review-session",
          reviewRevision: 7,
          baseActiveRequirementSetId: "set-active",
          classification: acceptedClassification(),
          scopeState: scopeState(),
          request,
        },
      ),
    ).toThrow(/调用原子化/);
    expect(() =>
      assertRequirementSemanticPublicationPreviewV2(
        previewResponse({ counts: { ...counts, total: 4 } }),
        {
          projectId: "project",
          documentId: "document",
          sessionId: "review-session",
          reviewRevision: 7,
          baseActiveRequirementSetId: "set-active",
          classification: acceptedClassification(),
          scopeState: scopeState(),
          request,
        },
      ),
    ).toThrow(/计数/);
    const wrongWorkingTree = previewResponse();
    wrongWorkingTree.binding_preview.projection.accepted_working_tree_id =
      "another-working-tree";
    expect(() =>
      assertRequirementSemanticPublicationPreviewV2(wrongWorkingTree, {
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
        reviewRevision: 7,
        baseActiveRequirementSetId: "set-active",
        classification: acceptedClassification(),
        scopeState: scopeState(),
        request,
      }),
    ).toThrow(/发布预览或当前分类结果/);
  });

  it("rejects a projection hash or row semantic that does not match the canonical payload", () => {
    const request = buildRequirementSemanticPublicationPreviewRequestV2({
      reviewRevision: 7,
      classification: acceptedClassification(),
      scopeState: scopeState(),
      projectId: "project",
      documentId: "document",
      sessionId: "review-session",
    });
    const changedHash = previewResponse();
    changedHash.binding_preview.projection.projection_sha256 = sha("0");
    expect(() =>
      assertRequirementSemanticPublicationPreviewV2(changedHash, {
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
        reviewRevision: 7,
        baseActiveRequirementSetId: "set-active",
        classification: acceptedClassification(),
        scopeState: scopeState(),
        request,
      }),
    ).toThrow(/投影|内容校验/i);

    const invalidAtomicIdentity = previewResponse();
    invalidAtomicIdentity.binding_preview.projection.rows[0] = {
      ...invalidAtomicIdentity.binding_preview.projection.rows[0],
      atomic_requirement_id: "atomic-requirement-" + "1".repeat(32),
    };
    expect(() =>
      assertRequirementSemanticPublicationPreviewV2(
        invalidAtomicIdentity,
        {
          projectId: "project",
          documentId: "document",
          sessionId: "review-session",
          reviewRevision: 7,
          baseActiveRequirementSetId: "set-active",
          classification: acceptedClassification(),
          scopeState: scopeState(),
          request,
        },
      ),
    ).toThrow(/业务行|原子|投影/);
  });

  it("rejects deleted, reordered, or structurally incomplete rows even when hashes and counts are resealed", () => {
    const request = buildRequirementSemanticPublicationPreviewRequestV2({
      reviewRevision: 7,
      classification: acceptedClassification(),
      scopeState: scopeState(),
      projectId: "project",
      documentId: "document",
      sessionId: "review-session",
    });
    const expected = {
      projectId: "project",
      documentId: "document",
      sessionId: "review-session",
      reviewRevision: 7,
      baseActiveRequirementSetId: "set-active",
      classification: acceptedClassification(),
      scopeState: scopeState(),
      request,
    };
    const deleted = previewResponse();
    deleted.binding_preview.projection.rows.pop();
    deleted.counts = {
      total: 2,
      default_analyze: 1,
      default_retain: 1,
      reference: 0,
      atomized: 0,
    };
    resealPreviewResponse(deleted);
    expect(() =>
      assertRequirementSemanticPublicationPreviewV2(deleted, expected),
    ).toThrow(/完整覆盖/);

    const reordered = previewResponse();
    [
      reordered.binding_preview.projection.rows[0],
      reordered.binding_preview.projection.rows[1],
    ] = [
      reordered.binding_preview.projection.rows[1],
      reordered.binding_preview.projection.rows[0],
    ];
    resealPreviewResponse(reordered);
    expect(() =>
      assertRequirementSemanticPublicationPreviewV2(reordered, expected),
    ).toThrow(/完整覆盖|完整顺序/);

    const missingField = previewResponse();
    delete (missingField.binding_preview.projection.rows[0] as JsonRecord)
      .source_refs;
    resealPreviewResponse(missingField);
    expect(() =>
      assertRequirementSemanticPublicationPreviewV2(missingField, expected),
    ).toThrow(/完整的清单投影合同/);
  });

  it("requires a READY publish binding with the same preview, classification, counts, tree and Requirement Set", () => {
    const request = buildRequirementSemanticPublicationPreviewRequestV2({
      reviewRevision: 7,
      classification: acceptedClassification(),
      scopeState: scopeState(),
      projectId: "project",
      documentId: "document",
      sessionId: "review-session",
    });
    const preview = assertRequirementSemanticPublicationPreviewV2(
      previewResponse(),
      {
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
        reviewRevision: 7,
        baseActiveRequirementSetId: "set-active",
        classification: acceptedClassification(),
        scopeState: scopeState(),
        request,
      },
    );
    expect(
      assertRequirementSemanticPublishDraftV2(publishResponse(), {
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
        reviewRevision: 7,
        classification: acceptedClassification(),
        preview,
        workingTree: workingTree(),
      }),
    ).toEqual(expect.objectContaining({ requirementSetId: "draft-set" }));
    expect(() =>
      assertRequirementSemanticPublishDraftV2(
        publishResponse({ counts: { ...counts, atomized: 1 } }),
        {
          projectId: "project",
          documentId: "document",
          sessionId: "review-session",
          reviewRevision: 7,
          classification: acceptedClassification(),
          preview,
          workingTree: workingTree(),
        },
      ),
    ).toThrow(/计数/);
    const wrongTree = publishedTree();
    wrongTree.source_identity.requirement_set_id = "another-set";
    expect(() =>
      assertRequirementSemanticPublishDraftV2(
        publishResponse({ requirement_tree: wrongTree }),
        {
          projectId: "project",
          documentId: "document",
          sessionId: "review-session",
          reviewRevision: 7,
          classification: acceptedClassification(),
          preview,
          workingTree: workingTree(),
        },
      ),
    ).toThrow(/确认清单版本/);
    const wrongProjection = publishResponse();
    wrongProjection.publication.projection_sha256 = sha("0");
    expect(() =>
      assertRequirementSemanticPublishDraftV2(wrongProjection, {
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
        reviewRevision: 7,
        classification: acceptedClassification(),
        preview,
        workingTree: workingTree(),
      }),
    ).toThrow(/完整结构/);
  });

  it("accepts exact optional bilingual evidence and rejects a malformed sidecar", () => {
    const request = buildRequirementSemanticPublicationPreviewRequestV2({
      reviewRevision: 7,
      classification: acceptedClassification(),
      scopeState: scopeState(),
      projectId: "project",
      documentId: "document",
      sessionId: "review-session",
    });
    const preview = assertRequirementSemanticPublicationPreviewV2(
      previewResponse(),
      {
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
        reviewRevision: 7,
        baseActiveRequirementSetId: "set-active",
        classification: acceptedClassification(),
        scopeState: scopeState(),
        request,
      },
    );
    const expected = {
      projectId: "project",
      documentId: "document",
      sessionId: "review-session",
      reviewRevision: 7,
      classification: acceptedClassification(),
      preview,
      workingTree: workingTree(),
    };
    const response = publishResponse();
    const candidate = response.requirement_tree.nodes.find(
      (node) => node.id === "non-technical",
    );
    if (!candidate) throw new Error("tree fixture requires bilingual candidate");
    candidate.bilingual_context = {
      counterpart_group_id: `bilingual-group-${"1".repeat(32)}`,
      document_language_policy_id:
        `document-language-policy-${"2".repeat(32)}`,
      canonical_structure_node_id: "non-technical",
      member_structure_node_ids: ["non-technical"],
      authority_language: "zh-CN",
      authority_basis: "DECLARED_DOCUMENT_POLICY",
      equal_validity: true,
      source_spans: [
        {
          bilingual_span_id: `bilingual-span-${"3".repeat(32)}`,
          language: "en",
          source_ref: { kind: "SOURCE_NODE", source_id: "source-en" },
          owner_structure_node_id: "non-technical",
          start_offset: 0,
          end_offset: 10,
          boundary_basis: "SOURCE_BLOCK_BOUNDARY",
        },
        {
          bilingual_span_id: `bilingual-span-${"4".repeat(32)}`,
          language: "zh-CN",
          source_ref: { kind: "SOURCE_NODE", source_id: "source-zh" },
          owner_structure_node_id: "non-technical",
          start_offset: 0,
          end_offset: 8,
          boundary_basis: "SOURCE_BLOCK_BOUNDARY",
        },
      ],
      mismatch_signal_ids: [],
      mismatch_review_required: false,
    };
    resealPublishResponse(response);
    expect(
      assertRequirementSemanticPublishDraftV2(response, expected),
    ).toEqual(expect.objectContaining({ requirementSetId: "draft-set" }));

    const malformed = publishResponse();
    const malformedCandidate = malformed.requirement_tree.nodes.find(
      (node) => node.id === "non-technical",
    );
    if (!malformedCandidate) throw new Error("tree fixture requires candidate");
    malformedCandidate.bilingual_context = {
      ...candidate.bilingual_context,
      source_spans: [candidate.bilingual_context.source_spans[0]],
    };
    resealPublishResponse(malformed);
    expect(() =>
      assertRequirementSemanticPublishDraftV2(malformed, expected),
    ).toThrow(/无法验证的结构行/);
  });

  it("rejects a READY row binding whose canonical projection-row hash was replaced", () => {
    const request = buildRequirementSemanticPublicationPreviewRequestV2({
      reviewRevision: 7,
      classification: acceptedClassification(),
      scopeState: scopeState(),
      projectId: "project",
      documentId: "document",
      sessionId: "review-session",
    });
    const preview = assertRequirementSemanticPublicationPreviewV2(
      previewResponse(),
      {
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
        reviewRevision: 7,
        baseActiveRequirementSetId: "set-active",
        classification: acceptedClassification(),
        scopeState: scopeState(),
        request,
      },
    );
    const changed = publishResponse();
    const first = (changed.publication.row_bindings as JsonRecord[] | undefined)?.[0];
    if (!first || typeof first !== "object") {
      throw new Error("publication fixture requires a row binding");
    }
    first.projection_row_sha256 = sha("0");
    expect(() =>
      assertRequirementSemanticPublishDraftV2(changed, {
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
        reviewRevision: 7,
        classification: acceptedClassification(),
        preview,
        workingTree: workingTree(),
      }),
    ).toThrow(/发布行|校验值/);
  });

  it("rejects deleted tree rows and changed content after every outer hash is resealed", () => {
    const request = buildRequirementSemanticPublicationPreviewRequestV2({
      reviewRevision: 7,
      classification: acceptedClassification(),
      scopeState: scopeState(),
      projectId: "project",
      documentId: "document",
      sessionId: "review-session",
    });
    const preview = assertRequirementSemanticPublicationPreviewV2(
      previewResponse(),
      {
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
        reviewRevision: 7,
        baseActiveRequirementSetId: "set-active",
        classification: acceptedClassification(),
        scopeState: scopeState(),
        request,
      },
    );
    const expected = {
      projectId: "project",
      documentId: "document",
      sessionId: "review-session",
      reviewRevision: 7,
      classification: acceptedClassification(),
      preview,
      workingTree: workingTree(),
    };
    const deleted = publishResponse();
    deleted.requirement_tree.nodes = deleted.requirement_tree.nodes.filter(
      (node) => node.id !== "information",
    );
    const informationRoot = deleted.requirement_tree.nodes.find(
      (node) => node.id === "root-information",
    );
    if (!informationRoot) throw new Error("tree fixture requires information root");
    informationRoot.child_ids = [];
    resealPublishResponse(deleted);
    expect(() =>
      assertRequirementSemanticPublishDraftV2(deleted, expected),
    ).toThrow(/数据库条目|结构/);

    const changedContent = publishResponse();
    const technical = changedContent.requirement_tree.nodes.find(
      (node) => node.id === "technical",
    );
    if (!technical) throw new Error("tree fixture requires technical row");
    technical.summary = "content changed after preview";
    resealPublishResponse(changedContent);
    expect(() =>
      assertRequirementSemanticPublishDraftV2(changedContent, expected),
    ).toThrow(/正文或证据/);
  });

  it("rejects evidence drift even when evidence, bundle, tree, and publication hashes are resealed", () => {
    const request = buildRequirementSemanticPublicationPreviewRequestV2({
      reviewRevision: 7,
      classification: acceptedClassification(),
      scopeState: scopeState(),
      projectId: "project",
      documentId: "document",
      sessionId: "review-session",
    });
    const preview = assertRequirementSemanticPublicationPreviewV2(
      previewResponse(),
      {
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
        reviewRevision: 7,
        baseActiveRequirementSetId: "set-active",
        classification: acceptedClassification(),
        scopeState: scopeState(),
        request,
      },
    );
    const changed = publishResponse();
    const bundle = changed.requirement_tree
      .requirement_evidence_bundle as JsonRecord;
    const evidence = (bundle.requirement_bindings as JsonRecord[])[0];
    const sourceRefs = evidence.source_refs as JsonRecord[];
    sourceRefs[0] = { ...sourceRefs[0], source_id: "different-source" };
    const evidenceSha256 = canonicalJsonSha256({
      atomic_requirement_id: evidence.atomic_requirement_id,
      requirement_entry_id: evidence.requirement_entry_id,
      source_refs: evidence.source_refs,
      source_spans: evidence.source_spans,
      table_evidence_binding_id: evidence.table_evidence_binding_id,
    });
    evidence.evidence_sha256 = evidenceSha256;
    evidence.binding_id =
      `requirement-evidence-binding-${evidenceSha256.slice(0, 32)}`;
    const technical = changed.requirement_tree.nodes.find(
      (node) => node.id === "technical",
    );
    if (!technical) throw new Error("tree fixture requires technical row");
    technical.requirement_evidence_binding_id = String(evidence.binding_id);
    bundle.evidence_sha256 = canonicalJsonSha256({
      requirement_bindings: bundle.requirement_bindings,
      source_ir_id: bundle.source_ir_id,
      source_sha256: bundle.source_sha256,
      structure_snapshot_id: bundle.structure_snapshot_id,
      table_bindings: bundle.table_bindings,
    });
    resealPublishResponse(changed);
    expect(() =>
      assertRequirementSemanticPublishDraftV2(changed, {
        projectId: "project",
        documentId: "document",
        sessionId: "review-session",
        reviewRevision: 7,
        classification: acceptedClassification(),
        preview,
        workingTree: workingTree(),
      }),
    ).toThrow(/正文或证据/);
  });
});
