import type {
  RequirementMatrixSnapshot,
  RequirementSet,
} from "../../shared/api/contracts";
import {
  matrixAnalysisEligible,
  matrixConclusionContract,
} from "../matrix/requirementMatrixModel";

export interface MatrixExportReadiness {
  targetCount: number;
  effectiveConclusionCount: number;
  missingEffectiveConclusionCount: number;
  effectiveCoverage: number;
  aiDefaultCount: number;
  humanOverrideCount: number;
  humanNativeCount: number;
  /** @deprecated Use effectiveConclusionCount. */
  decisionCount: number;
  /** @deprecated Use effectiveCoverage. */
  coverage: number;
  matrixReady: boolean;
  previewStable: boolean;
  draftReady: boolean;
  formalReady: boolean;
}

export type MatrixExportRequirementSetContext = Pick<
  RequirementSet,
  "status" | "preview_generation_id" | "preview_status_at_lock"
>;

export function matrixExportReadiness(
  matrix: RequirementMatrixSnapshot | null,
  requirementSet: MatrixExportRequirementSetContext | null,
): MatrixExportReadiness {
  const contracts = (matrix?.nodes || [])
    .filter(matrixAnalysisEligible)
    .map(matrixConclusionContract);
  const effective = contracts.filter((item) => item.effectiveConclusion);
  const targetCount = contracts.length;
  const effectiveConclusionCount = effective.length;
  const missingEffectiveConclusionCount =
    targetCount - effectiveConclusionCount;
  const matrixReady = matrix?.state === "READY";
  const previewStable =
    Boolean(String(requirementSet?.preview_generation_id || "").trim()) ||
    String(requirementSet?.preview_status_at_lock || "")
      .trim()
      .toUpperCase() === "READY";
  const effectiveCoverage = matrix
    ? targetCount
      ? Math.floor((effectiveConclusionCount / targetCount) * 1000) / 10
      : 100
    : 0;
  return {
    targetCount,
    effectiveConclusionCount,
    missingEffectiveConclusionCount,
    effectiveCoverage,
    aiDefaultCount: effective.filter(
      (item) => item.conclusionSource === "AI_DEFAULT",
    ).length,
    humanOverrideCount: effective.filter(
      (item) => item.conclusionSource === "HUMAN_OVERRIDE",
    ).length,
    humanNativeCount: effective.filter(
      (item) => item.conclusionSource === "HUMAN_NATIVE",
    ).length,
    decisionCount: effectiveConclusionCount,
    coverage: effectiveCoverage,
    matrixReady,
    previewStable,
    draftReady: matrixReady,
    formalReady:
      matrixReady &&
      String(requirementSet?.status || "").trim().toUpperCase() === "LOCKED" &&
      previewStable &&
      effectiveConclusionCount === targetCount,
  };
}
