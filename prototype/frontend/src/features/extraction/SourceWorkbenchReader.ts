import type { NativeStructureReferencePublicV1, NativeStructureTablePreviewV1,
  SourceRequirementBaselineOutlineV1, SourceRequirementRecordDetailV1, SourceFindingsV1, SourceScopeSummaryV1 } from "../../shared/api/contracts";
import type { WorkCapability } from "../workspace/documentWorkProvider";
import type { SourceItemDeliveryState } from "./SourceItemReasoning";
import type { SourceItemReviewContext } from "./sourceItemReviewContext";

/** Producer adapter for the existing workbench; never changes its source semantics. */
export interface SourceWorkbenchReadModel {
  revision_id: string;
  source_findings?: SourceFindingsV1;
  scope_summaries?: Record<string, SourceScopeSummaryV1>;
  revision_number?: number;
  source_sha256: string;
  reference: NativeStructureReferencePublicV1;
  outline: SourceRequirementBaselineOutlineV1 | null;
  tables: Record<string, NativeStructureTablePreviewV1>;
  reasoning: SourceItemDeliveryState | null;
  review: SourceItemReviewContext | null;
  analysis_policy?: { status: string; reason: string } | null;
}
export interface SourceWorkbenchReader extends SourceWorkbenchReadModel {
  feedbackCapability?: WorkCapability;
  loadRecord: (sourceId: string, signal?: AbortSignal) => Promise<SourceRequirementRecordDetailV1>;
  saveFeedback: (text: string, nodeId: string, sourceIds: string[]) => Promise<void>;
}
