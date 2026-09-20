import { describe, expect, it } from "vitest";
import type {
  EntryClassificationResponse,
  EntryClassificationType,
  RequirementAtomizationScopeAttempt,
  RequirementTreeNode,
  RequirementTreeSnapshot,
} from "../../shared/api/contracts";
import atomizationScopeCommandGolden from "../../../../contracts/fixtures/atomization_scope_command_v2.json";
import {
  ATOMIZATION_SCOPE_MAX_TREE_DEPTH,
  atomizationAttemptActionGate,
  atomizationScopeViewModel,
  atomizationPreviewFingerprintPayload,
  atomizationScopeWireHashPayload,
  buildApplyAtomizationScopeWireCommandFromPlan,
  buildAtomizationScopePlan,
  buildUndoAtomizationScopeWireCommand,
  reconcileSelectedAtomizationWorkflowIds,
  sha256AtomizationPreviewFingerprint,
  sha256AtomizationScopeWirePayload,
  type AtomizationPreviewFingerprintPayload,
  type AtomizationScopePlan,
  type AtomizationScopeWireHashInput,
  type AtomizationScopeWireHashPayload,
} from "./atomizationScopeModel";

const sourceSha = "a".repeat(64);
const effectiveTreeSha = "b".repeat(64);
const workingTreeSha = "c".repeat(64);
const eligibleReviewNodeIds = [
  "workflow-tech-parent",
  "workflow-tech-child",
  "workflow-outside-tech",
] as const;

function interruptedScopeAttempt(
  overrides: Partial<RequirementAtomizationScopeAttempt> = {},
): RequirementAtomizationScopeAttempt {
  return {
    schema_version: "atomization-scope-attempt/v1",
    operation: "APPLY",
    scope: "BRANCH",
    status: "INTERRUPTED",
    requested_review_node_ids: [
      "workflow-section",
      "workflow-tech-parent",
      "workflow-info",
      "workflow-tech-child",
      "workflow-non-technical",
      "workflow-unclassified",
      "workflow-indirect",
    ],
    executed_review_node_ids: [
      "workflow-tech-parent",
      "workflow-tech-child",
    ],
    skipped_targets: [],
    cumulative_review_node_ids: [],
    completed_review_node_ids: [],
    failed_review_node_ids: ["workflow-tech-parent"],
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

function treeNode(
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
    summary: `${id} summary`,
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
    projection_role: "STRUCTURE",
    include_in_view: true,
    content_blocks: [],
    table_refs: [],
    figure_refs: [],
    ...overrides,
  };
}

function workingTree(
  overrides: Partial<RequirementTreeSnapshot> = {},
): RequirementTreeSnapshot {
  const root = treeNode("root", {
    kind: "DOCUMENT",
    child_ids: ["section", "outside-tech"],
  });
  const section = treeNode("section", {
    parent_id: root.id,
    depth: 1,
    source_order: 1,
    child_ids: [
      "tech-parent",
      "nontechnical",
      "information",
      "unclassified",
      "structure-leaf",
    ],
  });
  const technicalParent = treeNode("tech-parent", {
    parent_id: section.id,
    depth: 2,
    source_order: 2,
    workflow_node_id: "workflow-tech-parent",
    projection_role: "DIRECT_CONTENT",
    child_ids: ["tech-child"],
  });
  const technicalChild = treeNode("tech-child", {
    parent_id: technicalParent.id,
    depth: 3,
    source_order: 3,
    workflow_node_id: "workflow-tech-child",
    projection_role: "DIRECT_CONTENT",
  });
  const nontechnical = treeNode("nontechnical", {
    parent_id: section.id,
    depth: 2,
    source_order: 4,
    workflow_node_id: "workflow-nontechnical",
    projection_role: "DIRECT_CONTENT",
  });
  const information = treeNode("information", {
    parent_id: section.id,
    depth: 2,
    source_order: 5,
    workflow_node_id: "workflow-information",
    projection_role: "DIRECT_CONTENT",
  });
  const unclassified = treeNode("unclassified", {
    parent_id: section.id,
    depth: 2,
    source_order: 6,
    workflow_node_id: "workflow-unclassified",
    projection_role: "DIRECT_CONTENT",
  });
  const structureLeaf = treeNode("structure-leaf", {
    parent_id: section.id,
    depth: 2,
    source_order: 7,
    label: "仅标题",
  });
  const outsideTechnical = treeNode("outside-tech", {
    parent_id: root.id,
    depth: 1,
    source_order: 8,
    workflow_node_id: "workflow-outside-tech",
    projection_role: "DIRECT_CONTENT",
  });
  return {
    schema_version: "requirement-tree-snapshot/v1",
    source_identity: {
      project_id: "project",
      document_id: "document",
      source_ir_id: "source-ir",
      source_sha256: sourceSha,
      preview_generation_id: "preview",
      requirement_set_id: "",
    },
    structure_identity: {
      base_snapshot_id: "base",
      effective_snapshot_id: "effective",
      effective_tree_sha256: effectiveTreeSha,
      revision: 2,
    },
    mode: "WORKING",
    state: "READY",
    root_node_ids: [root.id],
    // Deliberately shuffled: canonical order comes from roots/child_ids.
    nodes: [
      information,
      root,
      outsideTechnical,
      technicalChild,
      structureLeaf,
      nontechnical,
      section,
      unclassified,
      technicalParent,
    ],
    ...overrides,
  };
}

function classificationEntry(
  treeNodeId: string,
  workflowNodeId: string,
  type: EntryClassificationType | null,
) {
  return {
    tree_node_id: treeNodeId,
    workflow_node_id: workflowNodeId,
    projection_role: "DIRECT_CONTENT",
    suggested_type: null,
    suggestion_source: "NONE" as const,
    effective_type: type,
    human_override: type,
    reason_codes: [],
    confidence: null,
  };
}

function classification(
  overrides: Partial<EntryClassificationResponse["snapshot"]> = {},
): EntryClassificationResponse {
  return {
    schema_version: "entry-classification-response/v1",
    snapshot: {
      schema_version: "entry-classification-snapshot/v1",
      snapshot_id: "accepted-classification",
      payload_sha256: "d".repeat(64),
      project_id: "project",
      sor_document_id: "document",
      source_sha256: sourceSha,
      working_tree_id: "working-tree",
      working_tree_sha256: workingTreeSha,
      effective_structure_id: "effective",
      semantic_snapshot_id: "",
      policy_version: "entry-classification-policy/v1",
      revision: 3,
      status: "ACCEPTED",
      entries: [
        classificationEntry(
          "tech-parent",
          "workflow-tech-parent",
          "TECHNICAL",
        ),
        classificationEntry(
          "tech-child",
          "workflow-tech-child",
          "TECHNICAL",
        ),
        classificationEntry(
          "nontechnical",
          "workflow-nontechnical",
          "NON_TECHNICAL",
        ),
        classificationEntry(
          "information",
          "workflow-information",
          "INFORMATION",
        ),
        classificationEntry(
          "unclassified",
          "workflow-unclassified",
          null,
        ),
        classificationEntry(
          "outside-tech",
          "workflow-outside-tech",
          "TECHNICAL",
        ),
      ],
      supersedes_id: "",
      created_at: "2026-08-07T00:00:00Z",
      accepted_at: "2026-08-07T00:01:00Z",
      accepted_by: "LOCAL_USER",
      ...overrides,
    },
    counts: {
      total: 6,
      technical: 3,
      non_technical: 1,
      information: 1,
      unclassified: 1,
      human_override: 5,
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

function applyWireInput(
  plan: AtomizationScopePlan,
  response: EntryClassificationResponse,
) {
  return {
    plan,
    session_id: "review-session",
    command_id: `apply-${plan.scope.toLowerCase()}`,
    expected_review_revision: 11,
    expected_scope_revision: 4,
    expected_entry_classification_snapshot_id: response.snapshot.snapshot_id,
    expected_entry_classification_revision: response.snapshot.revision,
    expected_entry_classification_sha256: response.snapshot.payload_sha256,
  };
}

describe("atomization scope model", () => {
  it("keeps CURRENT on the exact anchor without expanding its descendants", () => {
    const plan = buildAtomizationScopePlan({
      tree: workingTree(),
      classification: classification(),
      scope: "CURRENT",
      currentTreeNodeId: "tech-parent",
      selectedWorkflowIds: [],
      eligibleReviewNodeIds,
    });

    expect(plan.requestedTreeNodeIds).toEqual(["tech-parent"]);
    expect(plan.executableWorkflowIds).toEqual(["workflow-tech-parent"]);
    expect(plan.items.map((item) => item.treeNodeId)).not.toContain(
      "tech-child",
    );
  });

  it("keeps SELECTED exact while deduplicating and sorting by canonical source order", () => {
    const plan = buildAtomizationScopePlan({
      tree: workingTree(),
      classification: classification(),
      scope: "SELECTED",
      currentTreeNodeId: "outside-tech",
      selectedWorkflowIds: [
        "workflow-outside-tech",
        "workflow-information",
        "workflow-tech-parent",
        "workflow-tech-parent",
      ],
      eligibleReviewNodeIds,
    });

    expect(plan.requestedWorkflowIds).toEqual([
      "workflow-tech-parent",
      "workflow-information",
      "workflow-outside-tech",
    ]);
    expect(plan.executableWorkflowIds).toEqual([
      "workflow-tech-parent",
      "workflow-outside-tech",
    ]);
    expect(plan.skipped).toEqual([
      expect.objectContaining({
        workflowNodeId: "workflow-information",
        reason: "INFORMATION",
      }),
    ]);
  });

  it("uses the canonical descendant tree for BRANCH and ignores filtered visibility", () => {
    const plan = buildAtomizationScopePlan({
      tree: workingTree(),
      classification: classification(),
      scope: "BRANCH",
      currentTreeNodeId: "section",
      selectedWorkflowIds: ["workflow-outside-tech"],
      eligibleReviewNodeIds,
    });

    expect(plan.requestedTreeNodeIds).toEqual([
      "section",
      "tech-parent",
      "tech-child",
      "nontechnical",
      "information",
      "unclassified",
      "structure-leaf",
    ]);
    expect(plan.executableWorkflowIds).toEqual([
      "workflow-tech-parent",
      "workflow-tech-child",
    ]);
    expect(plan.skipped.map((item) => [item.treeNodeId, item.reason])).toEqual([
      ["section", "STRUCTURE"],
      ["nontechnical", "NON_TECHNICAL"],
      ["information", "INFORMATION"],
      ["unclassified", "UNCLASSIFIED"],
      ["structure-leaf", "STRUCTURE"],
    ]);
  });

  it("uses the full canonical tree for ALL but targets only technical entries", () => {
    const plan = buildAtomizationScopePlan({
      tree: workingTree(),
      classification: classification(),
      scope: "ALL",
      currentTreeNodeId: "",
      selectedWorkflowIds: [],
      eligibleReviewNodeIds,
    });

    expect(plan.requestedWorkflowIds).toEqual([
      "workflow-tech-parent",
      "workflow-tech-child",
      "workflow-nontechnical",
      "workflow-information",
      "workflow-unclassified",
      "workflow-outside-tech",
    ]);
    expect(plan.executableWorkflowIds).toEqual([
      "workflow-tech-parent",
      "workflow-tech-child",
      "workflow-outside-tech",
    ]);
    expect(plan.items[0]).toEqual(
      expect.objectContaining({ treeNodeId: "root", reason: "STRUCTURE" }),
    );
  });

  it("keeps the canonical request range but executes only server-eligible technical requirements", () => {
    const plan = buildAtomizationScopePlan({
      tree: workingTree(),
      classification: classification(),
      scope: "ALL",
      currentTreeNodeId: "",
      selectedWorkflowIds: [],
      eligibleReviewNodeIds: ["workflow-tech-parent"],
    });

    expect(plan.requestedWorkflowIds).toEqual([
      "workflow-tech-parent",
      "workflow-tech-child",
      "workflow-nontechnical",
      "workflow-information",
      "workflow-unclassified",
      "workflow-outside-tech",
    ]);
    expect(plan.executableWorkflowIds).toEqual(["workflow-tech-parent"]);
    expect(plan.estimatedRemoteCallCount).toBe(1);
    expect(
      plan.skipped
        .filter((item) => item.reason === "NOT_DIRECT_OR_NOT_REQUIREMENT")
        .map((item) => item.workflowNodeId),
    ).toEqual(["workflow-tech-child", "workflow-outside-tech"]);
  });

  it("never treats suggested or override fallback values as effective classification", () => {
    const response = classification();
    const entry = response.snapshot.entries.find(
      (item) => item.workflow_node_id === "workflow-unclassified",
    );
    if (!entry) throw new Error("test fixture is missing its unclassified entry");
    entry.suggested_type = "TECHNICAL";
    entry.human_override = "TECHNICAL";
    entry.effective_type = null;

    const plan = buildAtomizationScopePlan({
      tree: workingTree(),
      classification: response,
      scope: "CURRENT",
      currentTreeNodeId: "unclassified",
      selectedWorkflowIds: [],
      eligibleReviewNodeIds: ["workflow-unclassified"],
    });

    expect(plan.executableWorkflowIds).toEqual([]);
    expect(plan.skipped[0]).toEqual(
      expect.objectContaining({ reason: "UNCLASSIFIED" }),
    );
  });

  it("requires every declared root to be parentless", () => {
    const tree = workingTree();
    const root = tree.nodes.find((node) => node.id === "root");
    if (!root) throw new Error("test fixture is missing its root");
    root.parent_id = "section";

    expect(() =>
      buildAtomizationScopePlan({
        tree,
        classification: classification(),
        scope: "ALL",
        currentTreeNodeId: "",
        selectedWorkflowIds: [],
        eligibleReviewNodeIds,
      }),
    ).toThrow(/顶层条目/);
  });

  it("reports validation failures in user-facing workflow terms", () => {
    const tree = workingTree();
    tree.root_node_ids = ["missing-entry"];

    let message = "";
    try {
      buildAtomizationScopePlan({
        tree,
        classification: classification(),
        scope: "ALL",
        currentTreeNodeId: "",
        selectedWorkflowIds: [],
        eligibleReviewNodeIds,
      });
    } catch (error) {
      message = error instanceof Error ? error.message : String(error);
    }

    expect(message).toContain("当前整理结果");
    expect(message).not.toMatch(
      /工作树|canonical|锚点|binding|READY|Requirement Set|APPLIED|NOT_RUN|parent_id|parentless/iu,
    );
  });

  it("requires the root collection to equal all parentless nodes", () => {
    const tree = workingTree();
    tree.nodes = [
      ...tree.nodes,
      treeNode("disconnected-root", { include_in_view: false }),
    ];

    expect(() =>
      buildAtomizationScopePlan({
        tree,
        classification: classification(),
        scope: "ALL",
        currentTreeNodeId: "",
        selectedWorkflowIds: [],
        eligibleReviewNodeIds,
      }),
    ).toThrow(/顶层条目/);
  });

  it("rejects a disconnected cyclic component even when all parent links agree", () => {
    const tree = workingTree();
    tree.nodes = [
      ...tree.nodes,
      treeNode("cycle-a", {
        parent_id: "cycle-b",
        child_ids: ["cycle-b"],
        include_in_view: false,
      }),
      treeNode("cycle-b", {
        parent_id: "cycle-a",
        child_ids: ["cycle-a"],
        include_in_view: false,
      }),
    ];

    expect(() =>
      buildAtomizationScopePlan({
        tree,
        classification: classification(),
        scope: "ALL",
        currentTreeNodeId: "",
        selectedWorkflowIds: [],
        eligibleReviewNodeIds,
      }),
    ).toThrow(/循环/);
  });

  it("fails closed when a canonical tree exceeds the explicit depth limit", () => {
    const nodeCount = ATOMIZATION_SCOPE_MAX_TREE_DEPTH + 2;
    const nodes = Array.from({ length: nodeCount }, (_, index) =>
      treeNode(`deep-${index}`, {
        parent_id: index === 0 ? null : `deep-${index - 1}`,
        child_ids: index + 1 < nodeCount ? [`deep-${index + 1}`] : [],
        depth: index,
        source_order: index,
        include_in_view: false,
      }),
    );
    const tree = workingTree({ root_node_ids: [nodes[0].id], nodes });

    expect(() =>
      buildAtomizationScopePlan({
        tree,
        classification: classification({ entries: [] }),
        scope: "ALL",
        currentTreeNodeId: "",
        selectedWorkflowIds: [],
        eligibleReviewNodeIds: [],
      }),
    ).toThrow(/层级超过可处理上限/);
  });

  it("fails closed instead of relabeling a missing classifiable entry as structure", () => {
    const incomplete = classification();
    incomplete.snapshot.entries = incomplete.snapshot.entries.filter(
      (entry) => entry.tree_node_id !== "tech-child",
    );

    expect(() =>
      buildAtomizationScopePlan({
        tree: workingTree(),
        classification: incomplete,
        scope: "ALL",
        currentTreeNodeId: "",
        selectedWorkflowIds: [],
        eligibleReviewNodeIds,
      }),
    ).toThrow("分类结果与当前整理结果的完整顺序不一致");
  });

  it("clears the independent selected IDs when source/effective/working identity changes", () => {
    const initial = reconcileSelectedAtomizationWorkflowIds(
      {
        identityKey: "",
        workflowIds: ["workflow-outside-tech"],
      },
      workingTree(),
      classification(),
    );
    expect(initial.workflowIds).toEqual([]);

    const selected = reconcileSelectedAtomizationWorkflowIds(
      {
        identityKey: initial.identityKey,
        workflowIds: [
          "workflow-outside-tech",
          "workflow-tech-parent",
          "missing",
          "workflow-tech-parent",
        ],
      },
      workingTree(),
      classification(),
    );
    expect(selected.workflowIds).toEqual([
      "workflow-tech-parent",
      "workflow-outside-tech",
    ]);

    const changed = reconcileSelectedAtomizationWorkflowIds(
      selected,
      workingTree(),
      classification({ working_tree_sha256: "e".repeat(64) }),
    );
    expect(changed.workflowIds).toEqual([]);
    expect(changed.identityKey).not.toBe(selected.identityKey);
  });

  it("builds a UI-only preview fingerprint without presenting it as a wire hash", () => {
    const plan = buildAtomizationScopePlan({
      tree: workingTree(),
      classification: classification(),
      scope: "SELECTED",
      currentTreeNodeId: "tech-child",
      selectedWorkflowIds: [
        "workflow-information",
        "workflow-tech-parent",
      ],
      eligibleReviewNodeIds,
    });

    expect(atomizationPreviewFingerprintPayload(plan)).toEqual({
      identity: plan.identity,
      scope: "SELECTED",
      anchorTreeNodeId: null,
      selectedWorkflowIds: [
        "workflow-tech-parent",
        "workflow-information",
      ],
      requestedTreeNodeIds: ["tech-parent", "information"],
      requestedWorkflowIds: [
        "workflow-tech-parent",
        "workflow-information",
      ],
      executableWorkflowIds: ["workflow-tech-parent"],
      skipped: [
        {
          treeNodeId: "information",
          workflowNodeId: "workflow-information",
          reason: "INFORMATION",
        },
      ],
    });
  });

  it("hashes the UI-only preview fingerprint with Web Crypto", async () => {
    const payload: AtomizationPreviewFingerprintPayload = {
      identity: {
        projectId: "project",
        documentId: "document",
        sourceSha256: sourceSha,
        effectiveStructureId: "effective",
        effectiveTreeSha256: effectiveTreeSha,
        workingTreeId: "working",
        workingTreeSha256: workingTreeSha,
      },
      scope: "CURRENT",
      anchorTreeNodeId: null,
      selectedWorkflowIds: [],
      requestedTreeNodeIds: ["entry"],
      requestedWorkflowIds: ["workflow"],
      executableWorkflowIds: ["workflow"],
      skipped: [],
    };

    await expect(sha256AtomizationPreviewFingerprint(payload)).resolves.toBe(
      "3c3fe3001396785f6bb9744f28d7bb51145162c0eb9207c9753154fe32327a8a",
    );
    await expect(
      sha256AtomizationPreviewFingerprint({
        ...payload,
        identity: { ...payload.identity, workingTreeId: "changed" },
      }),
    ).resolves.not.toBe(
      "3c3fe3001396785f6bb9744f28d7bb51145162c0eb9207c9753154fe32327a8a",
    );
  });

  it("maps a BRANCH plan to an APPLY wire with the complete canonical request range", async () => {
    const response = classification();
    const plan = buildAtomizationScopePlan({
      tree: workingTree(),
      classification: response,
      scope: "BRANCH",
      currentTreeNodeId: "section",
      selectedWorkflowIds: [],
      eligibleReviewNodeIds: ["workflow-tech-parent"],
    });

    const command = await buildApplyAtomizationScopeWireCommandFromPlan(
      applyWireInput(plan, response),
    );

    expect(command.operation).toBe("APPLY");
    expect(command.scope).toBe("BRANCH");
    expect(command.anchor_tree_node_id).toBe("section");
    expect(command.target_review_node_ids).toEqual(plan.requestedWorkflowIds);
    expect(command.target_review_node_ids).toContain("workflow-nontechnical");
    expect(plan.executableWorkflowIds).toEqual(["workflow-tech-parent"]);
  });

  it("maps an ALL plan to an APPLY wire with skips included and no anchor", async () => {
    const response = classification();
    const plan = buildAtomizationScopePlan({
      tree: workingTree(),
      classification: response,
      scope: "ALL",
      currentTreeNodeId: "tech-parent",
      selectedWorkflowIds: [],
      eligibleReviewNodeIds: ["workflow-tech-parent"],
    });

    const command = await buildApplyAtomizationScopeWireCommandFromPlan(
      applyWireInput(plan, response),
    );

    expect(command.scope).toBe("ALL");
    expect(command.anchor_tree_node_id).toBeNull();
    expect(command.target_review_node_ids).toEqual(plan.requestedWorkflowIds);
    expect(command.target_review_node_ids).toEqual([
      "workflow-tech-parent",
      "workflow-tech-child",
      "workflow-nontechnical",
      "workflow-information",
      "workflow-unclassified",
      "workflow-outside-tech",
    ]);
  });

  it("refuses to build APPLY when the plan has no executable requirement", async () => {
    const response = classification();
    const plan = buildAtomizationScopePlan({
      tree: workingTree(),
      classification: response,
      scope: "CURRENT",
      currentTreeNodeId: "information",
      selectedWorkflowIds: [],
      eligibleReviewNodeIds,
    });

    await expect(
      buildApplyAtomizationScopeWireCommandFromPlan(
        applyWireInput(plan, response),
      ),
    ).rejects.toThrow(/没有可拆分/);
  });

  it("builds UNDO through a dedicated command path with fixed empty targets", async () => {
    const response = classification();
    const plan = buildAtomizationScopePlan({
      tree: workingTree(),
      classification: response,
      scope: "ALL",
      currentTreeNodeId: "",
      selectedWorkflowIds: [],
      eligibleReviewNodeIds,
    });

    const command = await buildUndoAtomizationScopeWireCommand({
      identity: plan.identity,
      session_id: "review-session",
      command_id: "undo-current-scope",
      expected_review_revision: 11,
      expected_scope_revision: 4,
      expected_entry_classification_snapshot_id: response.snapshot.snapshot_id,
      expected_entry_classification_revision: response.snapshot.revision,
      expected_entry_classification_sha256: response.snapshot.payload_sha256,
    });

    expect(command).toEqual(
      expect.objectContaining({
        operation: "UNDO",
        scope: "CURRENT",
        anchor_tree_node_id: null,
        target_review_node_ids: [],
        unselected_policy: "SOURCE_BOUND_PASSTHROUGH",
      }),
    );
  });

  it("matches the shared Python-compatible v2 wire hash golden", async () => {
    const expectedPayload =
      atomizationScopeCommandGolden.hash_payload as AtomizationScopeWireHashPayload;
    const { schema_version: schemaVersion, ...input } = expectedPayload;

    expect(schemaVersion).toBe("atomization-target-scope/v2");
    expect(
      atomizationScopeWireHashPayload(input as AtomizationScopeWireHashInput),
    ).toEqual(expectedPayload);
    await expect(
      sha256AtomizationScopeWirePayload(expectedPayload),
    ).resolves.toBe(atomizationScopeCommandGolden.expected_sha256);
  });

  it("builds the complete snake_case v2 wire command from the shared golden", async () => {
    const expectedPayload =
      atomizationScopeCommandGolden.hash_payload as AtomizationScopeWireHashPayload;
    const { schema_version: _schemaVersion, ...input } = expectedPayload;
    const plan: AtomizationScopePlan = {
      identity: {
        projectId: input.project_id,
        documentId: input.document_id,
        sourceSha256: "0".repeat(64),
        effectiveStructureId: input.expected_effective_structure_id,
        effectiveTreeSha256: "3".repeat(64),
        workingTreeId: input.expected_working_tree_id,
        workingTreeSha256: input.expected_working_tree_sha256,
      },
      scope: input.scope,
      anchorTreeNodeId: input.anchor_tree_node_id,
      selectedWorkflowIds: [],
      items: [],
      requestedTreeNodeIds: [],
      requestedWorkflowIds: [...input.target_review_node_ids],
      executableWorkflowIds: [input.target_review_node_ids[0]],
      estimatedRemoteCallCount: 1,
      targets: [],
      skipped: [],
    };

    await expect(
      buildApplyAtomizationScopeWireCommandFromPlan({
        plan,
        session_id: input.session_id,
        command_id: input.command_id,
        expected_review_revision: input.expected_review_revision,
        expected_scope_revision: input.expected_scope_revision,
        expected_entry_classification_snapshot_id:
          input.expected_entry_classification_snapshot_id,
        expected_entry_classification_revision:
          input.expected_entry_classification_revision,
        expected_entry_classification_sha256:
          input.expected_entry_classification_sha256,
      }),
    ).resolves.toEqual(atomizationScopeCommandGolden.expected_command);
  });

  it("defines a display view-model that makes pass-through the default", () => {
    const view = atomizationScopeViewModel(
      buildAtomizationScopePlan({
        tree: workingTree(),
        classification: classification(),
        scope: "BRANCH",
        currentTreeNodeId: "section",
        selectedWorkflowIds: [],
        eligibleReviewNodeIds,
      }),
    );

    expect(view.defaultBehavior).toBe("默认保留原条目，不拆分");
    expect(view.summary).toBe(
      "2 项可执行，5 项保留/跳过，预计 AI 最多处理 2 次",
    );
    expect(view.estimatedRemoteCallCount).toBe(2);
    expect(view.actionLabel).toBe("原子化 2 项技术需求");
    expect(view.canApply).toBe(true);
    expect(view.skipped[0]).toEqual(
      expect.objectContaining({ reasonLabel: "结构行不参与原子化" }),
    );
  });

  it("blocks every scope write while the authoritative server attempt is running", () => {
    const plan = buildAtomizationScopePlan({
      tree: workingTree(),
      classification: classification(),
      scope: "BRANCH",
      currentTreeNodeId: "section",
      selectedWorkflowIds: [],
      eligibleReviewNodeIds,
    });
    const gate = atomizationAttemptActionGate(
      plan,
      interruptedScopeAttempt({
        status: "RUNNING",
        requested_review_node_ids: [...plan.requestedWorkflowIds],
        completed_review_node_ids: [],
        failed_review_node_ids: [],
        provider_call_count: 0,
        error_code: "",
      }),
    );

    expect(gate).toEqual(
      expect.objectContaining({
        serverRunning: true,
        openScopeBlocked: true,
        applyBlocked: true,
        undoBlocked: true,
        retryingApply: false,
      }),
    );
    expect(gate.guidance).toContain("每 2.5 秒自动更新");
  });

  it("requires a different canonical APPLY range after a non-retryable interruption", () => {
    const plan = buildAtomizationScopePlan({
      tree: workingTree(),
      classification: classification(),
      scope: "BRANCH",
      currentTreeNodeId: "section",
      selectedWorkflowIds: [],
      eligibleReviewNodeIds,
    });
    const attempt = interruptedScopeAttempt({
      requested_review_node_ids: [...plan.requestedWorkflowIds],
    });

    expect(atomizationAttemptActionGate(plan, attempt)).toEqual(
      expect.objectContaining({
        openScopeBlocked: false,
        applyBlocked: true,
        undoBlocked: false,
        retryingApply: false,
        guidance: expect.stringContaining("调整范围后再执行"),
      }),
    );
    expect(
      atomizationAttemptActionGate(
        { ...plan, scope: "ALL" },
        attempt,
      ).applyBlocked,
    ).toBe(false);
    expect(
      atomizationAttemptActionGate(
        {
          ...plan,
          requestedWorkflowIds: plan.requestedWorkflowIds.slice(1),
        },
        attempt,
      ).applyBlocked,
    ).toBe(false);
  });

  it("allows an explicit retry and blocks only an unretryable failed UNDO", () => {
    const plan = buildAtomizationScopePlan({
      tree: workingTree(),
      classification: classification(),
      scope: "BRANCH",
      currentTreeNodeId: "section",
      selectedWorkflowIds: [],
      eligibleReviewNodeIds,
    });
    const retryableApply = atomizationAttemptActionGate(
      plan,
      interruptedScopeAttempt({
        requested_review_node_ids: [...plan.requestedWorkflowIds],
        retryable: true,
      }),
    );
    expect(retryableApply).toEqual(
      expect.objectContaining({
        applyBlocked: false,
        undoBlocked: false,
        retryingApply: true,
        guidance: expect.stringContaining("可重试当前范围"),
      }),
    );

    const failedUndo = atomizationAttemptActionGate(
      plan,
      interruptedScopeAttempt({
        operation: "UNDO",
        scope: "CURRENT",
        requested_review_node_ids: [],
        executed_review_node_ids: [],
        failed_review_node_ids: [],
      }),
    );
    expect(failedUndo).toEqual(
      expect.objectContaining({
        openScopeBlocked: false,
        applyBlocked: false,
        undoBlocked: true,
        retryingApply: false,
        guidance: expect.stringContaining("撤销不可重试"),
      }),
    );
  });
});
