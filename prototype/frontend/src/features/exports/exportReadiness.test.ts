import { describe, expect, it } from "vitest";
import type {
  RequirementMatrixNode,
  RequirementMatrixSnapshot,
  RequirementSet,
} from "../../shared/api/contracts";
import { matrixExportReadiness } from "./exportReadiness";

function matrixNode(
  id: string,
  overrides: Partial<RequirementMatrixNode> = {},
): RequirementMatrixNode {
  return {
    node_id: id,
    parent_id: null,
    child_ids: [],
    depth: 0,
    source_order: 0,
    display_role: "ATOMIC_REQUIREMENT",
    label: id,
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
    requirement_text: id,
    table_record_id: null,
    logical_table_ids: [],
    matrix_visibility: "VISIBLE",
    disposition: "ANALYZE",
    disposition_reason: "",
    analysis_eligibility: "ELIGIBLE",
    analysis_status: "NOT_STARTED",
    ai_assessment: null,
    manual_decision: null,
    compliance_status: "",
    save_revision: 0,
    updated_at: "",
    ...overrides,
  };
}

function snapshot(nodes: RequirementMatrixNode[]): RequirementMatrixSnapshot {
  return {
    schema_version: "requirement-matrix-snapshot/v1",
    state: "READY",
    project_id: "p",
    document_id: "d",
    requirement_set_id: "s",
    requirement_set_status: "LOCKED",
    tree_sha256: "a".repeat(64),
    root_node_ids: nodes.map((node) => node.node_id),
    nodes,
    logical_tables: [],
    counts: {
      total_nodes: nodes.length,
      visible_nodes: nodes.length,
      system_non_business_nodes: 0,
      human_excluded_nodes: 0,
      analyze_nodes: 99,
      retained_nodes: 99,
      excluded_nodes: 0,
      analysis_eligible_nodes: 99,
      assessed_nodes: 0,
      decided_nodes: 99,
      logical_tables: 0,
    },
  };
}

function lockedSet(overrides: Partial<RequirementSet> = {}): RequirementSet {
  return {
    id: "s",
    status: "LOCKED",
    preview_generation_id: "preview-1",
    ...overrides,
  };
}

describe("matrix export readiness", () => {
  it("uses only visible ANALYZE and ELIGIBLE nodes instead of legacy set counters", () => {
    const result = matrixExportReadiness(
      snapshot([
        matrixNode("decided", {
          analysis_status: "DECIDED",
          manual_decision: {
            decision: "ACCEPT",
            reason: "checked",
            decided_at: "2026-07-31T00:00:00Z",
          },
        }),
        matrixNode("pending"),
        matrixNode("retained", { disposition: "RETAIN" }),
        matrixNode("excluded", {
          matrix_visibility: "HUMAN_EXCLUDED",
          disposition: "EXCLUDE",
        }),
      ]),
      lockedSet(),
    );

    expect(result).toEqual({
      targetCount: 2,
      effectiveConclusionCount: 1,
      missingEffectiveConclusionCount: 1,
      effectiveCoverage: 50,
      aiDefaultCount: 0,
      humanOverrideCount: 0,
      humanNativeCount: 1,
      decisionCount: 1,
      coverage: 50,
      matrixReady: true,
      previewStable: true,
      draftReady: true,
      formalReady: false,
    });
  });

  it("accepts a current terminal AI conclusion by default", () => {
    const result = matrixExportReadiness(
      snapshot([
        matrixNode("ai", {
          analysis_status: "ASSESSED",
          analysis_result_state: "CURRENT",
          ai_assessment: {
            decision: "ACCEPT",
            compliance_status: "COMPLIANT",
            reason: "证据充分",
            confidence: 0.92,
            assessment_mode: "EVIDENCE",
          },
        }),
      ]),
      lockedSet(),
    );

    expect(result).toMatchObject({
      targetCount: 1,
      effectiveConclusionCount: 1,
      missingEffectiveConclusionCount: 0,
      effectiveCoverage: 100,
      aiDefaultCount: 1,
      humanOverrideCount: 0,
      humanNativeCount: 0,
      formalReady: true,
    });
  });

  it("blocks FORMAL when a locked set has no stable preview proof", () => {
    const result = matrixExportReadiness(
      snapshot([
        matrixNode("ai-without-preview-proof", {
          analysis_status: "ASSESSED",
          analysis_result_state: "CURRENT",
          ai_assessment: {
            decision: "ACCEPT",
            compliance_status: "COMPLIANT",
            reason: "证据充分",
            confidence: 0.92,
            assessment_mode: "EVIDENCE",
          },
        }),
      ]),
      lockedSet({ preview_generation_id: "" }),
    );

    expect(result).toMatchObject({
      matrixReady: true,
      previewStable: false,
      effectiveCoverage: 100,
      formalReady: false,
    });
  });

  it.each([
    ["preview generation id", lockedSet()],
    [
      "READY status captured at lock",
      lockedSet({ preview_generation_id: "", preview_status_at_lock: "READY" }),
    ],
  ])("accepts stable preview proof from %s", (_name, requirementSet) => {
    const result = matrixExportReadiness(
      snapshot([
        matrixNode("ai-with-preview-proof", {
          analysis_status: "ASSESSED",
          analysis_result_state: "CURRENT",
          ai_assessment: {
            decision: "ACCEPT",
            compliance_status: "COMPLIANT",
            reason: "证据充分",
            confidence: 0.92,
            assessment_mode: "EVIDENCE",
          },
        }),
      ]),
      requirementSet,
    );

    expect(result.previewStable).toBe(true);
    expect(result.formalReady).toBe(true);
  });

  it("counts human overrides separately from human-native conclusions", () => {
    const ai = {
      decision: "ACCEPT",
      compliance_status: "COMPLIANT",
      reason: "AI 结论",
      confidence: 0.8,
      assessment_mode: "EVIDENCE",
    };
    const manual = {
      decision: "REJECT",
      reason: "人工修正",
      decided_at: "2026-08-08T00:00:00Z",
    };
    const result = matrixExportReadiness(
      snapshot([
        matrixNode("override", {
          analysis_status: "DECIDED",
          analysis_result_state: "CURRENT",
          ai_assessment: ai,
          manual_decision: manual,
        }),
        matrixNode("native", {
          analysis_status: "DECIDED",
          analysis_result_state: "CURRENT",
          manual_decision: manual,
        }),
      ]),
      lockedSet(),
    );

    expect(result).toMatchObject({
      effectiveConclusionCount: 2,
      humanOverrideCount: 1,
      humanNativeCount: 1,
      aiDefaultCount: 0,
      formalReady: true,
    });
  });

  it.each([
    [
      "stale reference",
      {
        analysis_status: "NOT_STARTED" as const,
        analysis_result_state: "STALE_REFERENCE" as const,
        stale_ai_assessment: {
          decision: "ACCEPT",
          compliance_status: "COMPLIANT",
          reason: "旧结论",
          confidence: 0.8,
          assessment_mode: "EVIDENCE",
        },
      },
    ],
    [
      "NOT_STARTED payload",
      {
        analysis_status: "NOT_STARTED" as const,
        analysis_result_state: "CURRENT" as const,
        ai_assessment: {
          decision: "ACCEPT",
          compliance_status: "COMPLIANT",
          reason: "未发布结果",
          confidence: 0.8,
          assessment_mode: "EVIDENCE",
        },
      },
    ],
    [
      "reason-only AI payload",
      {
        analysis_status: "ASSESSED" as const,
        analysis_result_state: "CURRENT" as const,
        ai_assessment: {
          decision: "",
          compliance_status: "",
          reason: "只有说明，没有结论",
          confidence: 0.8,
          assessment_mode: "EVIDENCE",
        },
      },
    ],
    [
      "top-level legacy compliance",
      {
        analysis_status: "ASSESSED" as const,
        analysis_result_state: "CURRENT" as const,
        compliance_status: "COMPLIANT",
      },
    ],
  ])("blocks FORMAL for %s", (_name, overrides) => {
    const result = matrixExportReadiness(
      snapshot([matrixNode("incomplete", overrides)]),
      lockedSet(),
    );

    expect(result).toMatchObject({
      targetCount: 1,
      effectiveConclusionCount: 0,
      missingEffectiveConclusionCount: 1,
      effectiveCoverage: 0,
      formalReady: false,
    });
  });

  it("keeps current AI payloads from older snapshots compatible", () => {
    const result = matrixExportReadiness(
      snapshot([
        matrixNode("legacy-current", {
          analysis_status: "ASSESSED",
          ai_assessment: {
            decision: "ACCEPT",
            compliance_status: "COMPLIANT",
            reason: "旧快照缺少 result state",
            confidence: 0.8,
            assessment_mode: "EVIDENCE",
          },
        }),
      ]),
      lockedSet(),
    );

    expect(result.aiDefaultCount).toBe(1);
    expect(result.formalReady).toBe(true);
  });

  it("never displays incomplete coverage as 100 percent", () => {
    const assessed = Array.from({ length: 199 }, (_, index) =>
      matrixNode(`assessed-${index}`, {
        analysis_status: "ASSESSED",
        analysis_result_state: "CURRENT",
        ai_assessment: {
          decision: "ACCEPT",
          compliance_status: "COMPLIANT",
          reason: "已完成",
          confidence: 0.9,
          assessment_mode: "EVIDENCE",
        },
      }),
    );
    const result = matrixExportReadiness(
      snapshot([...assessed, matrixNode("pending-200")]),
      lockedSet(),
    );

    expect(result.targetCount).toBe(200);
    expect(result.effectiveConclusionCount).toBe(199);
    expect(result.effectiveCoverage).toBe(99.5);
    expect(result.coverage).toBe(99.5);
    expect(result.formalReady).toBe(false);
  });

  it("treats a locked ready matrix with no analysis targets as fully covered", () => {
    const result = matrixExportReadiness(
      snapshot([matrixNode("context", { disposition: "RETAIN" })]),
      lockedSet(),
    );
    expect(result.targetCount).toBe(0);
    expect(result.effectiveConclusionCount).toBe(0);
    expect(result.missingEffectiveConclusionCount).toBe(0);
    expect(result.effectiveCoverage).toBe(100);
    expect(result.coverage).toBe(100);
    expect(result.draftReady).toBe(true);
    expect(result.formalReady).toBe(true);
  });

  it("blocks both draft and formal when the matrix is missing or needs re-extraction", () => {
    const missing = matrixExportReadiness(null, lockedSet());
    expect(missing.matrixReady).toBe(false);
    expect(missing.draftReady).toBe(false);
    expect(missing.formalReady).toBe(false);

    const stale = {
      ...snapshot([matrixNode("decided")]),
      state: "REQUIRES_REEXTRACTION" as const,
    };
    const requiresReextraction = matrixExportReadiness(stale, lockedSet());
    expect(requiresReextraction.matrixReady).toBe(false);
    expect(requiresReextraction.draftReady).toBe(false);
    expect(requiresReextraction.formalReady).toBe(false);
  });
});
