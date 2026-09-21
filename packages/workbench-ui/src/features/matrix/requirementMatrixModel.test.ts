import { describe, expect, it } from "vitest";
import type {
  RequirementMatrixNode,
  RequirementMatrixSnapshot,
} from "../../shared/api/contracts";
import {
  firstMatrixSelectionId,
  flattenRequirementMatrix,
  isAiAnalysisActiveStatus,
  matrixDisplayPreview,
  matrixConclusionContract,
  matrixHierarchyLabel,
  matrixAnalysisTone,
  matrixAnalysisStatus,
  matrixAiSuggestion,
  matrixNeedsAnalysis,
  matrixHumanDecision,
  matrixNodeLabel,
  matrixNodeMatchesView,
  matrixRowAccessibleName,
  matrixSourceNumber,
  requirementMatrixMatchCounts,
  suggestedMatrixExpandedIds,
} from "./requirementMatrixModel";

describe("isAiAnalysisActiveStatus", () => {
  it("finds a retained native source by SRC and surfaces source issues without human acceptance", () => {
    const value = node({node_id:"native-reference", matrix_visibility:"VISIBLE", requirement_code:"",
      candidate_review:{sourceId:"SRC-0390", requirementness:"NON_REQUIREMENT", status:"unreviewed",
        openIssues:[{id:"source-gap"}]}});
    expect(matrixNodeMatchesView(value,"SRC-0390","all")).toBe(true);
    expect(matrixNodeMatchesView(value,"","attention")).toBe(true);
    expect(matrixNodeMatchesView(value,"","analysis")).toBe(false);
  });
  it.each([
    "QUEUED",
    "PENDING",
    "STARTING",
    "RUNNING",
    "CANCEL_REQUESTED",
    "PUBLISHING",
  ])("keeps %s active until the job reaches a terminal state", (status) => {
    expect(isAiAnalysisActiveStatus(status)).toBe(true);
  });

  it.each(["COMPLETED", "FAILED", "CANCELLED", ""])(
    "treats %s as terminal",
    (status) => {
      expect(isAiAnalysisActiveStatus(status)).toBe(false);
    },
  );
});

function node(
  value: Partial<RequirementMatrixNode> & Pick<RequirementMatrixNode, "node_id">,
): RequirementMatrixNode {
  const { node_id, ...overrides } = value;
  return {
    node_id,
    parent_id: null,
    child_ids: [],
    depth: 0,
    source_order: 0,
    display_role: "STRUCTURE",
    label: "",
    summary: "",
    content_blocks: [],
    table_refs: [],
    figure_refs: [],
    source_refs: [],
    page_start: 1,
    page_end: 1,
    structure_node_id: null,
    requirement_entry_id: null,
    requirement_id: null,
    requirement_code: "",
    requirement_text: "",
    table_record_id: null,
    logical_table_ids: [],
    matrix_visibility: "VISIBLE",
    disposition: "RETAIN",
    disposition_reason: "",
    analysis_eligibility: "NOT_ELIGIBLE",
    analysis_status: "NOT_APPLICABLE",
    ai_assessment: null,
    manual_decision: null,
    compliance_status: "",
    save_revision: 0,
    updated_at: "",
    ...overrides,
  };
}

function matrix(): RequirementMatrixSnapshot {
  const nodes = [
    node({
      node_id: "root",
      child_ids: ["clause", "table", "cover"],
      label: "4 技术要求",
      source_kind: "SECTION",
      source_number: "4",
      display_title: "技术要求",
    }),
    node({
      node_id: "clause",
      parent_id: "root",
      child_ids: ["atomic", "excluded"],
      depth: 1,
      source_order: 1,
      display_role: "CLAUSE",
      label: "4.1 热性能",
      source_kind: "CLAUSE",
      source_number: "4.1",
      display_title: "热性能",
    }),
    node({
      node_id: "atomic",
      parent_id: "clause",
      depth: 2,
      source_order: 2,
      display_role: "ATOMIC_REQUIREMENT",
      label: "4.1.1 控制器应通过高温验证",
      source_kind: "REQUIREMENT",
      source_number: "4.1.1",
      display_title: "控制器应通过高温验证",
      display_text: "控制器应通过高温验证",
      requirement_code: "REQ-0042",
      requirement_text: "控制器应通过高温验证",
      disposition: "ANALYZE",
      analysis_eligibility: "ELIGIBLE",
      analysis_status: "ASSESSED",
      ai_assessment: {
        decision: "NEED_CLARIFICATION",
        compliance_status: "INSUFFICIENT_INFORMATION",
        reason: "缺少 85°C 报告",
        confidence: 0.74,
        assessment_mode: "EVIDENCE",
      },
    }),
    node({
      node_id: "excluded",
      parent_id: "clause",
      depth: 2,
      source_order: 3,
      display_role: "CONTEXT",
      label: "报价说明",
      matrix_visibility: "HUMAN_EXCLUDED",
      disposition: "EXCLUDE",
    }),
    node({
      node_id: "table",
      parent_id: "root",
      depth: 1,
      source_order: 4,
      display_role: "TABLE",
      label: "表 4-1 性能参数",
      source_kind: "TABLE",
      source_number: "表 4-1",
      display_title: "性能参数",
    }),
    node({
      node_id: "cover",
      parent_id: "root",
      depth: 1,
      source_order: 5,
      display_role: "CONTEXT",
      label: "封面",
      matrix_visibility: "SYSTEM_NON_BUSINESS",
    }),
  ];
  return {
    schema_version: "requirement-matrix-snapshot/v1",
    state: "READY",
    project_id: "project",
    document_id: "document",
    requirement_set_id: "set",
    requirement_set_status: "LOCKED",
    tree_sha256: "a".repeat(64),
    root_node_ids: ["root"],
    nodes,
    logical_tables: [],
    counts: {
      total_nodes: nodes.length,
      visible_nodes: 4,
      system_non_business_nodes: 1,
      human_excluded_nodes: 1,
      analyze_nodes: 1,
      retained_nodes: 4,
      excluded_nodes: 1,
      analysis_eligible_nodes: 1,
      assessed_nodes: 1,
      decided_nodes: 0,
      logical_tables: 1,
    },
  };
}

describe("matrixAnalysisStatus", () => {
  it("labels historical analysis as needing rerun instead of current", () => {
    expect(
      matrixAnalysisStatus(
        node({
          node_id: "stale",
          disposition: "ANALYZE",
          analysis_eligibility: "ELIGIBLE",
          analysis_status: "NOT_STARTED",
          analysis_result_state: "STALE_REFERENCE",
          stale_ai_assessment: {
            decision: "ACCEPT",
            compliance_status: "COMPLIANT",
            reason: "旧版本结论",
            confidence: 0.9,
            assessment_mode: "EVIDENCE",
          },
        }),
      ),
    ).toBe("需重新分析");
  });

  it("treats a current AI conclusion as accepted by default until a human changes it", () => {
    const current = matrix();
    const assessed = current.nodes.find((item) => item.node_id === "atomic")!;

    expect(matrixHumanDecision(assessed)).toBe("未人工修改");
    expect(requirementMatrixMatchCounts(current, "", "pending")).toEqual({
      matched: 0,
      retainedAncestors: 0,
    });
  });

  it("mirrors the five backend conclusion sources and their effective values", () => {
    const aiDefault = node({
      node_id: "ai-default",
      disposition: "ANALYZE",
      analysis_eligibility: "ELIGIBLE",
      analysis_status: "ASSESSED",
      analysis_result_state: "CURRENT",
      ai_assessment: {
        decision: "ACCEPT",
        compliance_status: "COMPLIANT",
        reason: "证据充分",
        confidence: 0.9,
        assessment_mode: "EVIDENCE",
      },
    });
    const humanOverride = {
      ...aiDefault,
      node_id: "human-override",
      analysis_status: "DECIDED" as const,
      manual_decision: {
        decision: "REJECT",
        reason: "人工发现偏差",
        decided_at: "2026-08-08T00:00:00Z",
      },
    };
    const humanNative = {
      ...humanOverride,
      node_id: "human-native",
      ai_assessment: null,
    };
    const pending = node({
      node_id: "pending",
      disposition: "ANALYZE",
      analysis_eligibility: "ELIGIBLE",
      analysis_status: "NOT_STARTED",
      analysis_result_state: "NONE",
    });
    const stale = node({
      node_id: "stale-source",
      disposition: "ANALYZE",
      analysis_eligibility: "ELIGIBLE",
      analysis_status: "NOT_STARTED",
      analysis_result_state: "STALE_REFERENCE",
      stale_ai_assessment: aiDefault.ai_assessment,
    });

    expect(matrixConclusionContract(aiDefault)).toEqual({
      analysisTarget: true,
      effectiveConclusion: "ACCEPT",
      conclusionSource: "AI_DEFAULT",
      overridesAi: false,
    });
    expect(matrixConclusionContract(humanOverride)).toEqual({
      analysisTarget: true,
      effectiveConclusion: "REJECT",
      conclusionSource: "HUMAN_OVERRIDE",
      overridesAi: true,
    });
    expect(matrixConclusionContract(humanNative)).toEqual({
      analysisTarget: true,
      effectiveConclusion: "REJECT",
      conclusionSource: "HUMAN_NATIVE",
      overridesAi: false,
    });
    expect(matrixConclusionContract(pending)).toEqual({
      analysisTarget: true,
      effectiveConclusion: "",
      conclusionSource: "NONE",
      overridesAi: false,
    });
    expect(matrixConclusionContract(stale)).toEqual({
      analysisTarget: true,
      effectiveConclusion: "",
      conclusionSource: "STALE_REFERENCE",
      overridesAi: false,
    });

    expect(matrixAnalysisStatus(aiDefault)).toBe("AI 结论已采用");
    expect(matrixAnalysisStatus(humanOverride)).toBe("人工已修改");
    expect(matrixAnalysisStatus(humanNative)).toBe("人工已录入");
    expect(matrixAnalysisStatus(pending)).toBe("待分析");
    expect(matrixAnalysisStatus(stale)).toBe("需重新分析");
    expect(matrixHumanDecision(aiDefault)).toBe("未人工修改");
    expect(matrixHumanDecision(humanOverride)).toBe("拒绝");
    expect(matrixHumanDecision(humanNative)).toBe("拒绝");
    expect(matrixNeedsAnalysis(aiDefault)).toBe(false);
    expect(matrixNeedsAnalysis(pending)).toBe(true);
    expect(matrixNeedsAnalysis(stale)).toBe(true);
  });

  it("does not promote a top-level legacy compliance value to an AI conclusion", () => {
    const legacyOnly = node({
      node_id: "legacy-only",
      disposition: "ANALYZE",
      analysis_eligibility: "ELIGIBLE",
      analysis_status: "ASSESSED",
      analysis_result_state: "CURRENT",
      compliance_status: "COMPLIANT",
    });

    expect(matrixAiSuggestion(legacyOnly)).toBe("待生成");
    expect(matrixConclusionContract(legacyOnly)).toMatchObject({
      effectiveConclusion: "",
      conclusionSource: "NONE",
    });
  });

  it("marks a terminal reason-only AI payload as incomplete attention, not pending", () => {
    const reasonOnly = node({
      node_id: "reason-only",
      disposition: "ANALYZE",
      analysis_eligibility: "ELIGIBLE",
      analysis_status: "ASSESSED",
      analysis_result_state: "CURRENT",
      ai_assessment: {
        decision: "",
        compliance_status: "",
        reason: "只有说明，没有结论",
        confidence: 0.8,
        assessment_mode: "EVIDENCE",
      },
    });

    expect(matrixAnalysisStatus(reasonOnly)).toBe("结论不完整");
    expect(matrixNodeMatchesView(reasonOnly, "", "attention")).toBe(true);
    expect(matrixNodeMatchesView(reasonOnly, "", "pending")).toBe(false);
  });

  it("treats a manual conclusion beside reason-only AI output as human-native", () => {
    const humanNative = node({
      node_id: "reason-only-with-manual",
      disposition: "ANALYZE",
      analysis_eligibility: "ELIGIBLE",
      analysis_status: "DECIDED",
      analysis_result_state: "CURRENT",
      ai_assessment: {
        decision: "",
        compliance_status: "",
        reason: "只有说明，没有 AI 结论",
        confidence: 0.4,
        assessment_mode: "EVIDENCE",
      },
      manual_decision: {
        decision: "ACCEPT",
        reason: "工程师独立录入结论",
        decided_at: "2026-08-09T00:00:00Z",
      },
    });

    expect(matrixConclusionContract(humanNative)).toEqual({
      analysisTarget: true,
      effectiveConclusion: "ACCEPT",
      conclusionSource: "HUMAN_NATIVE",
      overridesAi: false,
    });
    expect(matrixAnalysisStatus(humanNative)).toBe("人工已录入");
    expect(matrixAiSuggestion(humanNative)).toBe("待生成");
  });
});

describe("requirement matrix hierarchy projection", () => {
  it("keeps source hierarchy and selects the first visible source row by default", () => {
    const rows = flattenRequirementMatrix(
      matrix(),
      new Set(["root", "clause"]),
    );
    expect(rows.map((row) => row.node.node_id)).toEqual([
      "root",
      "clause",
      "atomic",
      "excluded",
      "table",
    ]);
    expect(firstMatrixSelectionId(rows)).toBe("root");
  });

  it("treats frozen child IDs as the authoritative sibling order", () => {
    const snapshot = matrix();
    const tiedRows = [
      node({
        node_id: "row-z",
        parent_id: "table",
        source_order: 4,
        display_role: "TABLE_RECORD",
        source_kind: "TABLE_RECORD",
        source_number: "2",
        display_title: "第二行",
      }),
      node({
        node_id: "row-a",
        parent_id: "table",
        source_order: 4,
        display_role: "TABLE_RECORD",
        source_kind: "TABLE_RECORD",
        source_number: "1",
        display_title: "第一行",
      }),
    ];
    const withTiedRows: RequirementMatrixSnapshot = {
      ...snapshot,
      nodes: snapshot.nodes
        .map((item) =>
          item.node_id === "table"
            ? { ...item, child_ids: ["row-z", "row-a"] }
            : item,
        )
        .concat(tiedRows),
    };

    expect(
      flattenRequirementMatrix(
        withTiedRows,
        new Set(["root", "table"]),
      )
        .filter((row) => row.node.parent_id === "table")
        .map((row) => row.node.node_id),
    ).toEqual(["row-z", "row-a"]);
  });

  it("keeps system wrappers for lineage but promotes their visible children", () => {
    const snapshot = matrix();
    const withSystemRoot = {
      ...snapshot,
      nodes: snapshot.nodes.map((item) =>
        item.node_id === "root"
          ? { ...item, matrix_visibility: "SYSTEM_NON_BUSINESS" as const }
          : item,
      ),
    };
    const rows = flattenRequirementMatrix(
      withSystemRoot,
      new Set(["root", "clause"]),
    );

    expect(rows.map((row) => row.node.node_id)).toEqual([
      "clause",
      "atomic",
      "excluded",
      "table",
    ]);
    expect(rows[0]?.depth).toBe(0);
    expect(firstMatrixSelectionId(rows)).toBe("clause");
  });

  it("preserves ancestors for search and only suggests expansion once", () => {
    const snapshot = matrix();
    const suggested = suggestedMatrixExpandedIds(
      snapshot,
      "高温验证",
      "all",
    );
    expect(suggested).toEqual(new Set(["root", "clause"]));
    expect(
      flattenRequirementMatrix(snapshot, suggested, "高温验证", "all").map(
        (row) => row.node.node_id,
      ),
    ).toEqual(["root", "clause", "atomic"]);

    const manuallyCollapsed = new Set(suggested);
    manuallyCollapsed.delete("clause");
    expect(
      flattenRequirementMatrix(
        snapshot,
        manuallyCollapsed,
        "高温验证",
        "all",
      ).map((row) => row.node.node_id),
    ).toEqual(["root", "clause"]);
    expect(requirementMatrixMatchCounts(snapshot, "高温验证", "all")).toEqual({
      matched: 1,
      retainedAncestors: 2,
    });
  });

  it("keeps human exclusions in all, hides system content and preserves the explicit filter", () => {
    const snapshot = matrix();
    const all = flattenRequirementMatrix(
      snapshot,
      new Set(["root", "clause"]),
      "",
      "all",
    );
    expect(all.map((row) => row.node.node_id)).toContain("excluded");
    expect(all.map((row) => row.node.node_id)).not.toContain("cover");
    const excluded = snapshot.nodes.find((item) => item.node_id === "excluded")!;
    expect(matrixHierarchyLabel(excluded)).toBe("上下文");
    expect(matrixAnalysisStatus(excluded)).toBe("已排除");
    expect(matrixHumanDecision(excluded)).toBe("—");
    const excludedRow = all.find((row) => row.node.node_id === "excluded")!;
    expect(matrixRowAccessibleName(excludedRow)).toContain("人工修改 —");
    expect(matrixRowAccessibleName(excludedRow)).not.toContain(
      "人工修改 已排除",
    );

    const suggested = suggestedMatrixExpandedIds(snapshot, "", "excluded");
    expect(
      flattenRequirementMatrix(snapshot, suggested, "", "excluded").map(
        (row) => row.node.node_id,
      ),
    ).toEqual(["root", "clause", "excluded"]);
  });

  it("uses success only for completed conclusions and neutral for non-analysis dispositions", () => {
    expect(matrixAnalysisTone("AI 结论已采用")).toBe("success");
    expect(matrixAnalysisTone("人工已修改")).toBe("success");
    expect(matrixAnalysisTone("人工已录入")).toBe("success");
    expect(matrixAnalysisTone("结论不完整")).toBe("warning");
    expect(matrixAnalysisTone("已排除")).toBe("neutral");
    expect(matrixAnalysisTone("仅保留")).toBe("neutral");
    expect(matrixAnalysisTone("不需分析")).toBe("neutral");
  });

  it("filters tables without flattening them and separates source numbers from content", () => {
    const snapshot = matrix();
    const suggested = suggestedMatrixExpandedIds(snapshot, "", "tables");
    expect(
      flattenRequirementMatrix(snapshot, suggested, "", "tables").map(
        (row) => row.node.node_id,
      ),
    ).toEqual(["root", "table"]);
    expect(matrixSourceNumber(snapshot.nodes[2])).toBe("4.1.1");
    expect(matrixNodeLabel(snapshot.nodes[2])).toBe("控制器应通过高温验证");
    expect(matrixHierarchyLabel(snapshot.nodes[2])).toBe("原子需求");
    expect(
      matrixNodeLabel({
        ...snapshot.nodes[2],
        label: "控制器应通过高温验证",
      }),
    ).toBe("控制器应通过高温验证");
  });

  it("does not repeat a numbered heading inside the content column", () => {
    const heading = node({
      node_id: "heading",
      source_kind: "SECTION",
      source_number: "1",
      display_title: "说明",
      label: "1 说明",
      summary: "1 说明",
    });

    expect(matrixDisplayPreview(heading)).toBe("");
  });

  it("does not preview a container's descendant roll-up summary", () => {
    const container = node({
      node_id: "section",
      child_ids: ["child"],
      source_number: "9",
      display_title: "技术要求",
      label: "9 技术要求",
      summary: "9.1 子条款正文不应重复到父级",
    });

    expect(matrixDisplayPreview(container)).toBe("");
  });
});
