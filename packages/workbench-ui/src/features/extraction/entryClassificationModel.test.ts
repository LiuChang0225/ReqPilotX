import { describe, expect, it } from "vitest";
import type {
  EntryClassificationResponse,
  RequirementTreeNode,
  RequirementTreeSnapshot,
} from "../../shared/api/contracts";
import {
  assertEntryClassificationMatchesTree,
  classificationFilterNodeIds,
  classificationWorkflowIdsForScope,
  entryClassificationCommandPayload,
  assertPublicationMatchesAcceptedClassification,
  assertPublicationReadyForActivation,
} from "./entryClassificationModel";

const sha = (value: string) => value.repeat(64);

function node(
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
    projection_role: "STRUCTURE",
    include_in_view: true,
    content_blocks: [],
    table_refs: [],
    figure_refs: [],
    ...overrides,
  };
}

function tree(): RequirementTreeSnapshot {
  const root = node("root", {
    kind: "DOCUMENT",
    child_ids: ["section"],
  });
  const section = node("section", {
    parent_id: root.id,
    depth: 1,
    child_ids: ["entry-a", "entry-b"],
  });
  const first = node("entry-a", {
    parent_id: section.id,
    depth: 2,
    projection_role: "STRUCTURE_PROXY",
    workflow_node_id: "workflow-a",
  });
  const second = node("entry-b", {
    parent_id: section.id,
    depth: 2,
    projection_role: "DIRECT_CONTENT",
    workflow_node_id: "workflow-b",
  });
  return {
    schema_version: "requirement-tree-snapshot/v1",
    source_identity: {
      project_id: "project",
      document_id: "document",
      source_ir_id: "source-ir",
      source_sha256: sha("a"),
      preview_generation_id: "preview",
      requirement_set_id: "",
    },
    structure_identity: {
      base_snapshot_id: "base",
      effective_snapshot_id: "effective",
      effective_tree_sha256: sha("b"),
      revision: 1,
    },
    mode: "WORKING",
    state: "READY",
    root_node_ids: [root.id],
    nodes: [root, section, first, second],
  };
}

function response(): EntryClassificationResponse {
  return {
    schema_version: "entry-classification-response/v1",
    snapshot: {
      schema_version: "entry-classification-snapshot/v1",
      snapshot_id: "working-1",
      payload_sha256: sha("c"),
      project_id: "project",
      sor_document_id: "document",
      source_sha256: sha("a"),
      working_tree_id: sha("d"),
      working_tree_sha256: sha("d"),
      effective_structure_id: "effective",
      semantic_snapshot_id: "",
      policy_version: "entry-classification-policy/v1",
      revision: 1,
      status: "WORKING",
      entries: [
        {
          tree_node_id: "entry-a",
          workflow_node_id: "workflow-a",
          projection_role: "STRUCTURE_PROXY",
          suggested_type: null,
          suggestion_source: "NONE",
          effective_type: null,
          human_override: null,
          reason_codes: ["NO_DEDICATED_CLASSIFICATION_SUGGESTION"],
          confidence: null,
        },
        {
          tree_node_id: "entry-b",
          workflow_node_id: "workflow-b",
          projection_role: "DIRECT_CONTENT",
          suggested_type: null,
          suggestion_source: "NONE",
          effective_type: "INFORMATION",
          human_override: "INFORMATION",
          reason_codes: ["NO_DEDICATED_CLASSIFICATION_SUGGESTION"],
          confidence: null,
        },
      ],
      supersedes_id: "initial-1",
      created_at: "2026-08-07T00:00:00Z",
      accepted_at: "",
      accepted_by: "",
    },
    counts: {
      total: 2,
      technical: 0,
      non_technical: 0,
      information: 1,
      unclassified: 1,
      human_override: 1,
    },
    can_accept: false,
    can_publish: false,
    actions: {
      can_override: true,
      can_accept: false,
      can_publish: false,
    },
    changed: false,
    serving_impact: "NONE",
  };
}

describe("entry classification model", () => {
  it("validates the exact working-tree lineage and ordered entry set", () => {
    expect(assertEntryClassificationMatchesTree(response(), tree())).toEqual(
      response(),
    );

    const stale = response();
    stale.snapshot.effective_structure_id = "stale-effective";
    expect(() => assertEntryClassificationMatchesTree(stale, tree())).toThrow(
      "当前整理内容",
    );
  });

  it("keeps matching entries and their structural ancestors in filters", () => {
    expect(
      classificationFilterNodeIds(tree(), response(), "UNCLASSIFIED"),
    ).toEqual(new Set(["root", "section", "entry-a"]));
    expect(
      classificationFilterNodeIds(tree(), response(), "INFORMATION"),
    ).toEqual(new Set(["root", "section", "entry-b"]));
  });

  it("resolves current branch, current filter and all as explicit batch scopes", () => {
    const snapshot = response();
    expect(
      classificationWorkflowIdsForScope(tree(), snapshot, {
        scope: "BRANCH",
        selectedNodeId: "section",
        visibleNodeIds: new Set(),
      }),
    ).toEqual(["workflow-a", "workflow-b"]);
    expect(
      classificationWorkflowIdsForScope(tree(), snapshot, {
        scope: "FILTERED",
        selectedNodeId: "section",
        visibleNodeIds: new Set(["root", "section", "entry-a"]),
      }),
    ).toEqual(["workflow-a"]);
    expect(
      classificationWorkflowIdsForScope(tree(), snapshot, {
        scope: "ALL",
        selectedNodeId: "",
        visibleNodeIds: new Set(),
      }),
    ).toEqual(["workflow-a", "workflow-b"]);
  });

  it("builds optimistic command identity from the exact snapshot receipt", () => {
    expect(
      entryClassificationCommandPayload(response().snapshot, "command-1"),
    ).toEqual({
      command_id: "command-1",
      expected_revision: 1,
      expected_snapshot_sha256: sha("c"),
      expected_source_sha256: sha("a"),
      expected_working_tree_sha256: sha("d"),
      expected_effective_structure_id: "effective",
      expected_policy_version: "entry-classification-policy/v1",
    });
  });

  it("fails closed when a DRAFT does not echo the accepted receipt", () => {
    const accepted = response();
    accepted.snapshot.status = "ACCEPTED";
    accepted.snapshot.accepted_at = "2026-08-07T00:00:00Z";
    accepted.snapshot.accepted_by = "LOCAL_USER";
    expect(() =>
      assertPublicationMatchesAcceptedClassification(
        {
          publication: {
            entry_classification_snapshot_id: "another-snapshot",
            entry_classification_snapshot_sha256:
              accepted.snapshot.payload_sha256,
          },
        },
        accepted,
      ),
    ).toThrow("未绑定刚接受的清单分类");
  });

  it("waits instead of activating a matching publication still being built", () => {
    const accepted = response();
    accepted.snapshot.status = "ACCEPTED";
    expect(() =>
      assertPublicationReadyForActivation(
        {
          publication: {
            publication_state: "PUBLISHING",
            requirement_tree_persisted: false,
            entry_classification_snapshot_id: accepted.snapshot.snapshot_id,
            entry_classification_snapshot_sha256:
              accepted.snapshot.payload_sha256,
          },
        },
        accepted,
      ),
    ).toThrow("仍在生成中，请稍后重试");
  });

  it("requires a persisted tree before activating a READY publication", () => {
    const accepted = response();
    accepted.snapshot.status = "ACCEPTED";
    const publication = {
      publication: {
        publication_state: "READY",
        requirement_tree_persisted: false,
        entry_classification_snapshot_id: accepted.snapshot.snapshot_id,
        entry_classification_snapshot_sha256:
          accepted.snapshot.payload_sha256,
      },
    };

    expect(() =>
      assertPublicationReadyForActivation(publication, accepted),
    ).toThrow("可信需求树尚未持久化");
    publication.publication.requirement_tree_persisted = true;
    expect(() =>
      assertPublicationReadyForActivation(publication, accepted),
    ).not.toThrow();
  });
});
