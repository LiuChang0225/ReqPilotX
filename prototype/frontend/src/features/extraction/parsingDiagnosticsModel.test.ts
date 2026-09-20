import { describe, expect, it } from "vitest";
import { normalizeParsingDiagnostics } from "./parsingDiagnosticsModel";


describe("parsing diagnostics model", () => {
  it("keeps reading the legacy v1 primary-page payload", () => {
    const result = normalizeParsingDiagnostics({
      schema_version: "parsing-diagnostics/v1",
      status: "READY_WITH_RISKS",
      source_sha256: "a".repeat(64),
      active_parse_artifact_id: "b".repeat(64),
      parse_identity_sha256: "c".repeat(64),
      page_count: 1,
      hard_blocker_count: 0,
      severe_warning_count: 1,
      risk_count: 1,
      severe_issue_ids: ["legacy-finding:0"],
      blocking_issue_ids: [],
      unassigned_risk_count: 0,
      risk_pages: [],
      risks: [
        {
          id: "legacy-finding:0",
          code: "legacy_finding",
          severity: "warning",
          blocking: false,
          stage: "STRUCTURE",
          message: "Legacy finding",
          page_number: 1,
        },
      ],
    });

    expect(result.status).toBe("READY_WITH_RISKS");
    expect(result.risks[0]?.pageNumbers).toEqual([1]);
    expect(result.risks[0]?.locations).toEqual([]);
  });

  it("accepts only a complete source and artifact bound summary", () => {
    const artifactId = "b".repeat(64);
    const result = normalizeParsingDiagnostics({
      schema_version: "parsing-diagnostics/v2",
      status: "READY_WITH_RISKS",
      source_sha256: "a".repeat(64),
      active_parse_artifact_id: artifactId,
      parse_identity_sha256: "c".repeat(64),
      page_count: 8,
      hard_blocker_count: 0,
      severe_warning_count: 2,
      risk_count: 2,
      severe_issue_ids: ["risk-1", "risk-2"],
      blocking_issue_ids: [],
      unassigned_risk_count: 0,
      risk_pages: [
        {
          page_number: 3,
          hard_blocker_count: 0,
          severe_warning_count: 2,
          risk_ids: ["risk-1", "risk-2"],
          issue_codes: ["VISUAL_GAP"],
        },
      ],
      risks: [
        {
          id: "risk-1",
          code: "VISUAL_GAP",
          severity: "warning",
          blocking: false,
          stage: "SOURCE_IR",
          message: "Visual content may be incomplete.",
          page_number: 3,
        },
      ],
      risk_acknowledged_at: "2026-08-06T08:00:00Z",
      risk_acknowledged_artifact_id: artifactId,
    });

    expect(result.status).toBe("READY_WITH_RISKS");
    expect(result.acknowledged).toBe(true);
    expect(result.riskPages[0]).toMatchObject({
      pageNumber: 3,
      severeWarningCount: 2,
    });
    expect(result.risks[0]).toMatchObject({
      code: "VISUAL_GAP",
      pageNumber: 3,
      pageNumbers: [3],
      locations: [],
    });
    expect(result.severeIssueIds).toEqual(["risk-1", "risk-2"]);
  });

  it("keeps one finding identity with every distinct location and page", () => {
    const result = normalizeParsingDiagnostics({
      schema_version: "parsing-diagnostics/v1",
      status: "READY_WITH_RISKS",
      source_sha256: "a".repeat(64),
      active_parse_artifact_id: "b".repeat(64),
      parse_identity_sha256: "c".repeat(64),
      page_count: 12,
      hard_blocker_count: 0,
      severe_warning_count: 1,
      risk_count: 1,
      severe_issue_ids: ["finding-1:0"],
      blocking_issue_ids: [],
      unassigned_risk_count: 0,
      risk_pages: [
        {
          page_number: 7,
          hard_blocker_count: 0,
          severe_warning_count: 1,
          risk_ids: ["finding-1:0"],
          issue_codes: ["STRUCTURE_GAP"],
        },
        {
          page_number: 9,
          hard_blocker_count: 0,
          severe_warning_count: 1,
          risk_ids: ["finding-1:0"],
          issue_codes: ["STRUCTURE_GAP"],
        },
      ],
      risks: [
        {
          id: "finding-1:0",
          finding_id: "finding-1",
          code: "STRUCTURE_GAP",
          severity: "warning",
          blocking: false,
          stage: "STRUCTURE",
          message: "structure_gap",
          page_number: 7,
          location: { page_number: 7, bbox: [1, 2, 3, 4] },
          page_numbers: [7, 9, 7, 0],
          locations: [
            { page_number: 7, bbox: [1, 2, 3, 4] },
            { page_number: 9, bbox: [5, 6, 7, 8] },
          ],
        },
      ],
    });

    expect(result.riskCount).toBe(1);
    expect(result.risks).toHaveLength(1);
    expect(result.risks[0]).toMatchObject({
      id: "finding-1:0",
      findingId: "finding-1",
      pageNumber: 7,
      pageNumbers: [7, 9],
      location: { page_number: 7, bbox: [1, 2, 3, 4] },
    });
    expect(result.risks[0].locations).toHaveLength(2);
    expect(result.riskPages.map((page) => page.pageNumber)).toEqual([7, 9]);
  });

  it("fails closed to unavailable when identity fields are incomplete", () => {
    const result = normalizeParsingDiagnostics({
      schema_version: "parsing-diagnostics/v1",
      status: "READY",
      source_sha256: "a".repeat(64),
      active_parse_artifact_id: "not-an-artifact",
      parse_identity_sha256: "c".repeat(64),
      severe_warning_count: 9,
    });

    expect(result.status).toBe("UNAVAILABLE");
    expect(result.severeWarningCount).toBe(0);
    expect(result.acknowledged).toBe(false);
  });
});
