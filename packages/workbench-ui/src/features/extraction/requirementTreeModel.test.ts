import { describe, expect, it } from "vitest";
import type { RequirementTreeSnapshot } from "../../shared/api/contracts";
import {
  clauseLevelExpandedIds,
  expandableRequirementTreeIds,
  flattenRequirementTree,
  initialVisibleSelectionId,
  isExactRequirementTreeContentLeaf,
  isPublishedRequirementNode,
  isRequirementTreeReviewItem,
  isRequirementTreeTocNode,
  isWholeTableReviewNode,
  matchingRequirementTreeTableNode,
  nearestRequirementTreeTableNode,
  parentClause,
  requirementTreeEditableStructureNodeIds,
  requirementTreeNodeDisplayLabel,
  requirementTreeAncestorIds,
  requirementTreeNodeRoleLabel,
  requirementTreeNodeStatusLabel,
  requirementTreeRowSecondaryLabel,
  requirementTreeReviewCandidateLeaves,
  requirementTreeReviewCandidateRoleCounts,
  requirementTreeTableFocus,
  subtreeExpandableRequirementTreeIds,
  tableRecordFocus,
} from "./requirementTreeModel";

const tree: RequirementTreeSnapshot = {
  schema_version: "requirement-tree-snapshot/v1",
  source_identity: {
    project_id: "p",
    document_id: "d",
    source_ir_id: "source",
    source_sha256: "a".repeat(64),
    preview_generation_id: "",
    requirement_set_id: "",
  },
  structure_identity: {
    base_snapshot_id: "base",
    effective_snapshot_id: "effective",
    effective_tree_sha256: "b".repeat(64),
    revision: 0,
  },
  mode: "WORKING",
  state: "READY",
  root_node_ids: ["chapter"],
  nodes: [
    {
      id: "chapter",
      parent_id: null,
      kind: "SECTION",
      depth: 0,
      source_order: 0,
      label: "3 产品要求",
      summary: "",
      child_ids: ["clause", "proxy-clause", "table"],
      structure_node_id: "chapter",
      requirement_entry_id: null,
      workflow_node_id: null,
      page_start: 3,
      page_end: 5,
      source_refs: [],
      requirement_count: 2,
      attention_count: 1,
      reviewed_count: 0,
      status: "READY",
      classification: "STRUCTURE",
      projection_role: "STRUCTURE",
      semantic_state: "NOT_CLASSIFIED",
      deterministic_rule_evidence: [],
      table_record_id: null,
      review_item_count: 4,
      include_in_view: true,
      content_blocks: [],
      table_refs: [],
      figure_refs: [],
    },
    {
      id: "clause",
      parent_id: "chapter",
      kind: "CLAUSE",
      depth: 1,
      source_order: 1,
      label: "3.1 强度",
      summary: "强度要求",
      child_ids: ["requirement:req-1"],
      structure_node_id: "clause",
      requirement_entry_id: null,
      workflow_node_id: null,
      page_start: 4,
      page_end: 4,
      source_refs: [],
      requirement_count: 1,
      attention_count: 1,
      reviewed_count: 0,
      status: "READY",
      classification: "STRUCTURE",
      projection_role: "STRUCTURE",
      semantic_state: "NOT_CLASSIFIED",
      deterministic_rule_evidence: [],
      table_record_id: null,
      review_item_count: 1,
      include_in_view: true,
      content_blocks: [],
      table_refs: [],
      figure_refs: [],
    },
    {
      id: "requirement:req-1",
      parent_id: "clause",
      kind: "REQUIREMENT",
      depth: 2,
      source_order: 2,
      label: "REQ-1",
      summary: "强度不低于 40kN",
      child_ids: [],
      structure_node_id: "clause",
      requirement_entry_id: "entry-1",
      workflow_node_id: "review-node-1",
      page_start: 4,
      page_end: 4,
      source_refs: [],
      requirement_count: 1,
      attention_count: 1,
      reviewed_count: 0,
      status: "NEEDS_ATTENTION",
      classification: "PERFORMANCE",
      projection_role: "REQUIREMENT",
      semantic_state: "PROPOSED",
      deterministic_rule_evidence: [],
      table_record_id: null,
      review_item_count: 1,
      include_in_view: true,
      content_blocks: [],
      table_refs: [],
      figure_refs: [],
    },
    {
      id: "proxy-clause",
      parent_id: "chapter",
      kind: "CLAUSE",
      depth: 1,
      source_order: 3,
      label: "3.2 安装条件",
      summary: "安装空间应满足图纸要求",
      child_ids: [],
      structure_node_id: "proxy-clause",
      requirement_entry_id: null,
      workflow_node_id: null,
      page_start: 4,
      page_end: 4,
      source_refs: [],
      requirement_count: 0,
      attention_count: 0,
      reviewed_count: 0,
      status: "PENDING",
      classification: "NOT_CLASSIFIED",
      projection_role: "STRUCTURE_PROXY",
      semantic_state: "NOT_CLASSIFIED",
      deterministic_rule_evidence: [
        {
          source: "local-rule",
          conclusion: "candidate",
          reason_code: "MODAL_PHRASE",
        },
      ],
      table_record_id: null,
      review_item_count: 1,
      include_in_view: true,
      content_blocks: [],
      table_refs: [],
      figure_refs: [],
    },
    {
      id: "table",
      parent_id: "chapter",
      kind: "TABLE",
      depth: 1,
      source_order: 4,
      label: "表 3-1 性能参数",
      summary: "性能参数原表",
      child_ids: ["table-record"],
      structure_node_id: "table",
      requirement_entry_id: null,
      workflow_node_id: null,
      page_start: 5,
      page_end: 5,
      source_refs: [],
      requirement_count: 1,
      attention_count: 0,
      reviewed_count: 0,
      status: "READY",
      classification: "STRUCTURE",
      projection_role: "STRUCTURE",
      semantic_state: "NOT_CLASSIFIED",
      deterministic_rule_evidence: [],
      table_record_id: null,
      review_item_count: 2,
      include_in_view: true,
      content_blocks: [],
      table_refs: [],
      figure_refs: [],
    },
    {
      id: "table-record",
      parent_id: "table",
      kind: "TABLE_RECORD",
      depth: 2,
      source_order: 5,
      label: "额定载荷",
      summary: "额定载荷不得低于 12kN",
      child_ids: ["requirement:req-table"],
      structure_node_id: "table",
      requirement_entry_id: null,
      workflow_node_id: null,
      page_start: 5,
      page_end: 5,
      source_refs: [],
      requirement_count: 1,
      attention_count: 0,
      reviewed_count: 0,
      status: "PENDING",
      classification: "NOT_CLASSIFIED",
      projection_role: "TABLE_RECORD",
      semantic_state: "NOT_CLASSIFIED",
      deterministic_rule_evidence: [],
      table_record_id: "table-record-1",
      review_item_count: 2,
      include_in_view: true,
      content_blocks: [
        {
          kind: "table_record",
          record_id: "table-record-1",
          logical_table_id: "logical-table-3",
          row_bindings: [
            {
              table_id: "table-3",
              row_index: 1,
              cell_ids: ["cell-name", "cell-value"],
            },
          ],
          column_bindings: [
            {
              table_id: "table-3",
              column_index: 0,
              cell_ids: ["cell-name"],
            },
            {
              table_id: "table-3",
              column_index: 1,
              cell_ids: ["cell-value"],
            },
          ],
          header_bindings: [
            {
              table_id: "table-3",
              axis: "COLUMN",
              cell_ids: ["cell-heading"],
              text: "项目要求",
            },
          ],
          shared_merged_values: [
            {
              table_id: "table-3",
              cell_id: "cell-heading",
              text: "前后减振器",
            },
          ],
          notes: [{ text: "数值以正式图纸为准" }],
          image_refs: [{ asset_id: "asset-1", cell_id: "cell-value" }],
        },
      ],
      table_refs: [],
      figure_refs: [{ asset_id: "asset-1", cell_id: "cell-value" }],
    },
    {
      id: "requirement:req-table",
      parent_id: "table-record",
      kind: "REQUIREMENT",
      depth: 3,
      source_order: 6,
      label: "REQ-TABLE",
      summary: "额定载荷不得低于 12kN",
      child_ids: [],
      structure_node_id: "table",
      requirement_entry_id: "entry-table",
      workflow_node_id: "review-node-table",
      page_start: 5,
      page_end: 5,
      source_refs: [],
      requirement_count: 1,
      attention_count: 0,
      reviewed_count: 0,
      status: "PENDING",
      classification: "PERFORMANCE",
      projection_role: "REQUIREMENT",
      semantic_state: "PROPOSED",
      deterministic_rule_evidence: [],
      table_record_id: null,
      review_item_count: 1,
      include_in_view: true,
      content_blocks: [],
      table_refs: [],
      figure_refs: [],
    },
  ],
};

describe("no-LLM tree roles", () => {
  it("keeps structural content non-semantic and reserves requirement wording for published requirements", () => {
    const heading = tree.nodes[0];
    const content = tree.nodes[3];
    const requirement = tree.nodes[2];

    expect(requirementTreeNodeRoleLabel(heading)).toBe("仅标题");
    expect(requirementTreeRowSecondaryLabel(heading)).toBe("仅标题");
    expect(requirementTreeNodeRoleLabel(content)).toBe(
      "条款内容 · 未做语义判断",
    );
    expect(requirementTreeNodeRoleLabel(content)).not.toContain("规则建议");
    expect(requirementTreeNodeRoleLabel(requirement)).toBe("需求 · 需关注");
    expect(
      requirementTreeNodeRoleLabel({
        ...requirement,
        status: "REVIEWED",
        semantic_state: "CONFIRMED",
      }),
    ).toBe("已确认需求");
  });

  it("recognizes a contents node only from exact structural evidence", () => {
    const toc = {
      ...tree.nodes[0],
      id: "toc",
      label: "目录",
      source_refs: [{ source_kind: "TOC_ENTRY" }],
    };

    expect(isRequirementTreeTocNode(toc)).toBe(true);
    expect(requirementTreeNodeRoleLabel(toc)).toBe("目录");
    expect(isRequirementTreeTocNode(tree.nodes[0])).toBe(false);
    expect(
      isRequirementTreeTocNode({
        ...tree.nodes[0],
        id: "ordinary-directory-heading",
        label: "目录",
        source_refs: [],
        toc_entries: [],
      }),
    ).toBe(false);
    expect(
      isRequirementTreeTocNode({
        ...tree.nodes[2],
        label: "目录",
        source_refs: [{ source_kind: "TOC_ENTRY" }],
      }),
    ).toBe(false);
  });
});

describe("review candidate list", () => {
  it("projects one flat downstream queue from review-item leaves", () => {
    expect(
      requirementTreeReviewCandidateLeaves(tree).map((node) => node.id),
    ).toEqual([
      "requirement:req-1",
      "proxy-clause",
      "requirement:req-table",
    ]);
    expect(requirementTreeReviewCandidateRoleCounts(tree)).toEqual({
      total: 3,
      requirements: 2,
      directContents: 0,
      wholeTables: 0,
      tableRecords: 0,
      structureProxies: 1,
    });
  });

  it("keeps multiple direct-content segments as undecided candidates even with requirement-shaped leaves", () => {
    const directContentOne = {
      ...tree.nodes[2],
      id: "direct-content-1",
      parent_id: "container",
      depth: 1,
      label: "",
      summary: "供应商应提交验证资料",
      projection_role: "DIRECT_CONTENT" as const,
      requirement_entry_id: "candidate-entry-1",
      status: "PENDING",
      semantic_state: "NOT_CLASSIFIED",
    };
    const directContentTwo = {
      ...directContentOne,
      id: "direct-content-2",
      source_order: directContentOne.source_order + 1,
      summary: "试验报告应覆盖全部工况",
      requirement_entry_id: "candidate-entry-2",
    };
    const container = {
      ...tree.nodes[0],
      id: "container",
      child_ids: [directContentOne.id, directContentTwo.id],
      requirement_count: 0,
      review_item_count: 2,
    };
    const directTree: RequirementTreeSnapshot = {
      ...tree,
      root_node_ids: [container.id],
      nodes: [container, directContentOne, directContentTwo],
    };

    expect(isRequirementTreeReviewItem(directContentOne)).toBe(true);
    expect(isPublishedRequirementNode(directContentOne)).toBe(false);
    expect(isExactRequirementTreeContentLeaf(directContentOne)).toBe(true);
    expect(requirementTreeReviewCandidateLeaves(directTree)).toEqual([
      directContentOne,
      directContentTwo,
    ]);
    expect(requirementTreeReviewCandidateRoleCounts(directTree)).toEqual({
      total: 2,
      requirements: 0,
      directContents: 2,
      wholeTables: 0,
      tableRecords: 0,
      structureProxies: 0,
    });
    expect(requirementTreeNodeDisplayLabel(directContentOne)).toBe(
      "供应商应提交验证资料",
    );
    expect(requirementTreeNodeDisplayLabel(directContentTwo)).toBe(
      "试验报告应覆盖全部工况",
    );
    expect(requirementTreeNodeStatusLabel(directContentOne)).toBe(
      "条款内容 · 未做语义判断",
    );
    expect(parentClause(directContentOne, directTree)?.id).toBe(container.id);
    expect(requirementTreeRowSecondaryLabel(directContentOne)).toBe(
      "条款内容 · 未做语义判断",
    );
    expect(requirementTreeRowSecondaryLabel(directContentOne)).not.toContain(
      directContentOne.summary,
    );
  });

  it("uses explicit editable bindings for direct content and never falls back to its reading owner", () => {
    const directContent = {
      ...tree.nodes[2],
      id: "direct-content-editable",
      projection_role: "DIRECT_CONTENT" as const,
      structure_node_id: "reading-owner-clause",
      editable_structure_node_ids: ["content-a", "content-b", "content-a", ""],
    };
    const legacyDirectContent = {
      ...directContent,
      editable_structure_node_ids: undefined,
    };

    expect(requirementTreeEditableStructureNodeIds(directContent)).toEqual([
      "content-a",
      "content-b",
    ]);
    expect(requirementTreeEditableStructureNodeIds(legacyDirectContent)).toEqual([]);
    expect(
      requirementTreeEditableStructureNodeIds({
        ...tree.nodes[1],
        id: "ordinary-structure-node",
        structure_node_id: "ordinary-structure-node",
      }),
    ).toEqual(["ordinary-structure-node"]);
    expect(
      requirementTreeEditableStructureNodeIds({
        ...tree.nodes[1],
        id: "ordinary-structure-node",
        projection_role: "STRUCTURE_PROXY",
        structure_node_id: "ordinary-structure-node",
      }),
    ).toEqual(["ordinary-structure-node"]);
    for (const projectionRole of [
      "WHOLE_TABLE_REVIEW",
      "TABLE_RECORD",
      "REQUIREMENT",
    ] as const) {
      expect(
        requirementTreeEditableStructureNodeIds({
          ...tree.nodes[1],
          id: `semantic-${projectionRole}`,
          projection_role: projectionRole,
          structure_node_id: "reading-owner-clause",
        }),
      ).toEqual([]);
    }
    expect(
      requirementTreeEditableStructureNodeIds({
        ...tree.nodes[1],
        id: "structure-alias",
        projection_role: "STRUCTURE",
        structure_node_id: "different-structure-node",
      }),
    ).toEqual([]);
  });

  it("keeps a whole-table review leaf independent from row records and formal requirements", () => {
    const wholeTable = {
      ...tree.nodes[4],
      id: "whole-table-review",
      parent_id: null,
      depth: 0,
      label: "",
      summary: "参数 | 要求 | 额定载荷 | 12kN",
      child_ids: [],
      projection_role: "WHOLE_TABLE_REVIEW" as const,
      requirement_count: 0,
      review_item_count: 1,
      status: "PENDING",
    };
    const wholeTableTree: RequirementTreeSnapshot = {
      ...tree,
      root_node_ids: [wholeTable.id],
      nodes: [wholeTable],
    };

    expect(isWholeTableReviewNode(wholeTable)).toBe(true);
    expect(isRequirementTreeReviewItem(wholeTable)).toBe(true);
    expect(isPublishedRequirementNode(wholeTable)).toBe(false);
    expect(isExactRequirementTreeContentLeaf(wholeTable)).toBe(true);
    expect(requirementTreeNodeDisplayLabel(wholeTable)).toBe("完整表格");
    expect(requirementTreeNodeStatusLabel(wholeTable)).toBe(
      "完整表格 · 未做语义判断",
    );
    expect(requirementTreeReviewCandidateLeaves(wholeTableTree)).toEqual([
      wholeTable,
    ]);
    expect(requirementTreeReviewCandidateRoleCounts(wholeTableTree)).toEqual({
      total: 1,
      requirements: 0,
      directContents: 0,
      wholeTables: 1,
      tableRecords: 0,
      structureProxies: 0,
    });
    expect(parentClause(wholeTable, wholeTableTree)?.id).toBe(wholeTable.id);
  });
});

describe("requirement tree projection", () => {
  it("filters one tree while retaining necessary ancestors", () => {
    const expanded = expandableRequirementTreeIds(
      tree,
      "40kN",
      "items",
    );
    const rows = flattenRequirementTree(
      tree,
      expanded,
      "40kN",
      "items",
    );
    expect(rows.map((row) => row.node.id)).toEqual([
      "chapter",
      "clause",
      "requirement:req-1",
    ]);
  });

  it("allows a filtered branch to be collapsed after its matches are revealed", () => {
    const expanded = expandableRequirementTreeIds(
      tree,
      "40kN",
      "items",
    );
    expanded.delete("clause");

    expect(
      flattenRequirementTree(tree, expanded, "40kN", "items").map(
        (row) => row.node.id,
      ),
    ).toEqual(["chapter", "clause"]);
  });

  it("returns bounded expansion sets for whole-tree, clause and branch actions", () => {
    expect([...expandableRequirementTreeIds(tree)]).toEqual([
      "chapter",
      "clause",
      "table",
      "table-record",
    ]);
    expect([...clauseLevelExpandedIds(tree)]).toEqual(["chapter"]);
    expect([
      ...subtreeExpandableRequirementTreeIds(tree, "clause"),
    ]).toEqual(["clause"]);
  });

  it("returns the selected node path without expanding the selected node itself", () => {
    expect([...requirementTreeAncestorIds(tree, "requirement:req-1")]).toEqual([
      "clause",
      "chapter",
    ]);
  });

  it("initially selects only a visible item instead of a hidden descendant", () => {
    const collapsedRows = flattenRequirementTree(tree, new Set());
    expect(initialVisibleSelectionId(collapsedRows)).toBe("chapter");

    const chapterRows = flattenRequirementTree(tree, new Set(["chapter"]));
    expect(initialVisibleSelectionId(chapterRows)).toBe("proxy-clause");
  });

  it("uses the complete parent clause as context for a requirement leaf", () => {
    expect(parentClause(tree.nodes[2], tree)?.id).toBe("clause");
    expect(parentClause(tree.nodes[3], tree)?.id).toBe("proxy-clause");
  });

  it("filters structure proxies, table records and requirements as review items", () => {
    const expanded = expandableRequirementTreeIds(tree, "", "items");
    const rows = flattenRequirementTree(tree, expanded, "", "items");

    expect(rows.map((row) => row.node.id)).toEqual([
      "chapter",
      "clause",
      "requirement:req-1",
      "proxy-clause",
      "table",
      "table-record",
      "requirement:req-table",
    ]);
  });

  it("uses the complete table for a record and the enclosing clause for a published table requirement", () => {
    expect(parentClause(tree.nodes[5], tree)?.id).toBe("table");
    expect(parentClause(tree.nodes[6], tree)?.id).toBe("chapter");
  });

  it("keeps every self-contained structural scope on itself", () => {
    const structuralKinds = ["SECTION", "CLAUSE", "LIST_GROUP", "GROUP"] as const;
    structuralKinds.forEach((kind) => {
      const structural = { ...tree.nodes[0], id: `self-${kind}`, kind };
      const scopedTree = {
        ...tree,
        root_node_ids: [structural.id],
        nodes: [structural],
      };
      expect(parentClause(structural, scopedTree)?.id).toBe(structural.id);
    });
  });

  it("finds the editable table context from a reviewed requirement leaf", () => {
    const selectedRequirement = tree.nodes[6];
    const tableContext = nearestRequirementTreeTableNode(
      selectedRequirement,
      tree,
    );

    expect(tableContext?.id).toBe("table-record");
    expect(
      requirementTreeTableFocus(selectedRequirement, tree),
    ).toEqual({
      nodeId: "table-record",
      structureNodeId: "table",
      tableRecordId: "table-record-1",
      logicalTableIds: ["logical-table-3"],
    });
  });

  it("repositions a review-table focus in the working tree by stable identity", () => {
    const reviewFocus = requirementTreeTableFocus(tree.nodes[6], tree);
    const workingTree: RequirementTreeSnapshot = {
      ...tree,
      mode: "WORKING",
      nodes: tree.nodes.map((node) =>
        node.id === "table-record"
          ? { ...node, id: "working-table-record" }
          : node.id === "requirement:req-table"
            ? {
                ...node,
                id: "working-requirement",
                parent_id: "working-table-record",
              }
            : node.kind === "TABLE"
              ? { ...node, child_ids: ["working-table-record"] }
              : node,
      ),
    };

    expect(
      matchingRequirementTreeTableNode(workingTree, reviewFocus)?.id,
    ).toBe("working-table-record");
  });

  it("derives a source-bound table focus without promoting the record to a requirement", () => {
    const focus = tableRecordFocus(tree.nodes[5], tree);

    expect(focus).toMatchObject({
      recordId: "table-record-1",
      recordLabel: "额定载荷",
      mode: "ROW",
      logicalTableId: "logical-table-3",
      headerTexts: ["项目要求"],
      sharedValues: ["前后减振器"],
      notes: ["数值以正式图纸为准"],
      imageCount: 1,
    });
    expect(focus?.rowBindings).toEqual([
      {
        tableId: "table-3",
        index: 1,
        cellIds: ["cell-name", "cell-value"],
      },
    ]);
    expect(
      requirementTreeNodeStatusLabel({
        ...tree.nodes[5],
        status: "REVIEWED",
        semantic_state: "CONFIRMED",
      }),
    ).toBe("表格条目 · 未做语义判断");
  });

  it("shows the complete requirement when no clause ancestor exists", () => {
    const document = {
      ...tree.nodes[0],
      id: "document",
      kind: "DOCUMENT" as const,
      child_ids: ["requirement:req-1"],
      review_item_count: 1,
    };
    const requirement = {
      ...tree.nodes[2],
      parent_id: "document",
      depth: 1,
    };
    const flatTree: RequirementTreeSnapshot = {
      ...tree,
      root_node_ids: ["document"],
      nodes: [document, requirement],
    };

    expect(parentClause(requirement, flatTree)?.id).toBe(
      "requirement:req-1",
    );
  });
});
