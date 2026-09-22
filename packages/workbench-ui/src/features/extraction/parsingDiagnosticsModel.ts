import { asArray, asRecord } from "../../shared/api/client";
import type { JsonRecord } from "../../shared/api/contracts";

export interface ParsingRisk {
  id: string;
  findingId: string;
  code: string;
  severity: string;
  blocking: boolean;
  stage: string;
  message: string;
  /** Backward-compatible primary page used by existing consumers. */
  pageNumber: number;
  /** Every distinct page grounded by the same diagnostic/finding. */
  pageNumbers: number[];
  location: JsonRecord;
  locations: JsonRecord[];
}

export interface ParsingRiskPage {
  pageNumber: number;
  hardBlockerCount: number;
  severeWarningCount: number;
  riskIds: string[];
  issueCodes: string[];
}

export interface ParsingDiagnosticsSnapshot {
  status: "READY" | "READY_WITH_RISKS" | "BLOCKED" | "UNAVAILABLE";
  sourceSha256: string;
  activeParseArtifactId: string;
  parseIdentitySha256: string;
  pageCount: number;
  hardBlockerCount: number;
  severeWarningCount: number;
  riskCount: number;
  severeIssueIds: string[];
  blockingIssueIds: string[];
  unassignedRiskCount: number;
  riskPages: ParsingRiskPage[];
  risks: ParsingRisk[];
  acknowledgedAt: string;
  acknowledgedArtifactId: string;
  acknowledged: boolean;
}

const sha256Pattern = /^[0-9a-f]{64}$/;

export function normalizeParsingDiagnostics(
  payload: JsonRecord | null,
): ParsingDiagnosticsSnapshot {
  const record = asRecord(payload);
  const sourceSha256 = String(record.source_sha256 || "").toLowerCase();
  const activeParseArtifactId = String(
    record.active_parse_artifact_id || "",
  ).toLowerCase();
  const parseIdentitySha256 = String(
    record.parse_identity_sha256 || "",
  ).toLowerCase();
  const status = String(record.status || "").toUpperCase();
  const schemaVersion = String(record.schema_version || "");
  const validIdentity =
    ["parsing-diagnostics/v1", "parsing-diagnostics/v2"].includes(
      schemaVersion,
    ) &&
    ["READY", "READY_WITH_RISKS", "BLOCKED"].includes(status) &&
    sha256Pattern.test(sourceSha256) &&
    sha256Pattern.test(activeParseArtifactId) &&
    sha256Pattern.test(parseIdentitySha256);
  if (!validIdentity) return unavailableParsingDiagnostics();

  const risks = asArray<JsonRecord>(record.risks).map((risk, index) => {
    const pageNumber = positiveInt(risk.page_number);
    const pageNumbers = positiveIntArray(risk.page_numbers);
    return {
      id: String(risk.id || `parse-risk-${index + 1}`),
      findingId: String(risk.finding_id || ""),
      code: String(risk.code || `PARSE_RISK_${index + 1}`),
      severity: String(risk.severity || "warning").toLowerCase(),
      blocking: risk.blocking === true,
      stage: String(risk.stage || ""),
      message: String(risk.message || risk.code || "解析风险"),
      pageNumber,
      pageNumbers:
        pageNumbers.length > 0
          ? pageNumbers
          : pageNumber > 0
            ? [pageNumber]
            : [],
      location: asRecord(risk.location),
      locations: asArray<JsonRecord>(risk.locations)
        .map(asRecord)
        .filter((location) => Object.keys(location).length > 0),
    };
  });
  const riskPages = asArray<JsonRecord>(record.risk_pages)
    .map((page) => ({
      pageNumber: positiveInt(page.page_number),
      hardBlockerCount: wholeNumber(page.hard_blocker_count),
      severeWarningCount: wholeNumber(page.severe_warning_count),
      riskIds: stringArray(page.risk_ids),
      issueCodes: stringArray(page.issue_codes),
    }))
    .filter((page) => page.pageNumber > 0)
    .sort((left, right) => left.pageNumber - right.pageNumber);
  const acknowledgedAt = String(record.risk_acknowledged_at || "");
  const acknowledgedArtifactId = String(
    record.risk_acknowledged_artifact_id || "",
  ).toLowerCase();
  return {
    status: status as "READY" | "READY_WITH_RISKS" | "BLOCKED",
    sourceSha256,
    activeParseArtifactId,
    parseIdentitySha256,
    pageCount: wholeNumber(record.page_count),
    hardBlockerCount: wholeNumber(record.hard_blocker_count),
    severeWarningCount: wholeNumber(record.severe_warning_count),
    riskCount: wholeNumber(record.risk_count),
    severeIssueIds: stringArray(record.severe_issue_ids),
    blockingIssueIds: stringArray(record.blocking_issue_ids),
    unassignedRiskCount: wholeNumber(record.unassigned_risk_count),
    riskPages,
    risks,
    acknowledgedAt,
    acknowledgedArtifactId,
    acknowledged: Boolean(
      acknowledgedAt && acknowledgedArtifactId === activeParseArtifactId,
    ),
  };
}

export function unavailableParsingDiagnostics(): ParsingDiagnosticsSnapshot {
  return {
    status: "UNAVAILABLE",
    sourceSha256: "",
    activeParseArtifactId: "",
    parseIdentitySha256: "",
    pageCount: 0,
    hardBlockerCount: 0,
    severeWarningCount: 0,
    riskCount: 0,
    severeIssueIds: [],
    blockingIssueIds: [],
    unassignedRiskCount: 0,
    riskPages: [],
    risks: [],
    acknowledgedAt: "",
    acknowledgedArtifactId: "",
    acknowledged: false,
  };
}

function wholeNumber(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : 0;
}

function positiveInt(value: unknown): number {
  const parsed = wholeNumber(value);
  return parsed > 0 ? parsed : 0;
}

function positiveIntArray(value: unknown): number[] {
  return [...new Set(asArray<unknown>(value).map(positiveInt).filter(Boolean))];
}

function stringArray(value: unknown): string[] {
  return asArray<unknown>(value)
    .map(String)
    .filter(Boolean);
}
