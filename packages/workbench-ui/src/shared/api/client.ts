import { createProjectDocumentClient } from './projectDocuments';
import { applicationFetch, applicationNamespace } from '../../host/applicationTransport';
import { saasCsrfFor, expireSaasSession } from './saasSession';
import type {
  AiProviderSetup,
  AgentStructureProposalApplyResultV1,
  AgentStructureProposalJobPublicV1,
  AgentStructureProposalPublicV1,
  AgentStructureProposalReviseRequestV1,
  AgentStructureProposalStartRequestV1,
  AgentStructureProposalStatusV1,
  AgentStructureProposalSteerRequestV1,
  AgentStructureSteerPublicV1,
  ApiErrorPayload,
  DocumentExtractionVisualRepairRequestV1,
  EntryClassificationCommand,
  EntryClassificationPatchCommand,
  EntryClassificationResponse,
  ExtractionStatus,
  JsonRecord,
  KnowledgeDocument,
  NativeStructureActivityStreamV1,
  NativeStructureBaselineJobPublicV1,
  NativeStructureBaselineStartRequestV1,
  NativeStructureBaselineStatusV1,
  NativeStructureFeedbackRequestV1,
  NativeStructureFeedbackResultV1,
  NativeStructureContinueRequestV1,
  NativeStructureContinueResultV1,
  NativeStructureProposalDecisionRequestV1,
  NativeStructureProposalDecisionResultV1,
  NativeStructureReferencePublicV1,
  NativeStructureRenderedReopenRequestV1,
  NativeStructureRenderedReopenResultV1,
  NativeStructureTablePreviewV1,
  NativeStructureWorkingRevisionHeadResponseV1,
  NativeStructureWorkingRevisionV1,
  NativeParentProvisionStatusV1,
  NativeParentProvisionBaselinePublicV1,
  SourceRequirementBaselineOutlineV1,
  SourceItemTextBaselineRunStartRequestV1,
  SourceItemScopeRevisionRunStartRequestV1,
  SourceItemScopeUndoRunStartRequestV1,
  SourceRequirementBaselineRunJobPublicV1,
  SourceRequirementBaselineRunStartRequestV1,
  SourceRequirementBaselineRunStatusV1,
  SourceRequirementBaselineStatusV1,
  SourceRequirementFeedbackRequestV1,
  SourceRequirementFeedbackResultV1,
  SourceRequirementRecordDetailV1,
  ProcessingProfilePayload,
  RequirementAtomizationScopeCommand,
  RequirementAtomizationScopeRunResponse,
  RequirementEnrichmentState,
  RequirementSemanticPublicationPreviewRequestV2,
  RequirementSemanticPublicationPreviewResponseV2,
  RequirementSemanticPublishDraftRequestV2,
  RequirementSemanticPublishDraftResponseV2,
  RequirementMatrixNodeDetail,
  RequirementMatrixSnapshot,
  RequirementMatrixTableDetail,
  RequirementSetLockReadiness,
  RequirementSetLockReceipt,
  RequirementAnalysisScopePreview,
  RequirementAnalysisScopeRequest,
  RequirementSetDeriveResponse,
  RequirementSetSemanticActivationRequestV1,
  RequirementSetSemanticActivationResponseV1,
  RequirementSetsPayload,
  RestorationRun,
  RunProfileOverride,
  RuntimeCapabilityCatalog,
  RuntimeReadiness,
  RuntimeWorkerFleetStatusV1,
  StructureOptimizationPayload,
  StructurePayload,
  TableRecordProjectionRowGroupReview,
  TableRecordProjectionRevisionRequest,
  TableRecordProjectionRevisionResponse,
  WorkspacePayload,
} from "./contracts";
import { parseJsonWithNumberSources } from "./jsonNumberSources";
import {
  uploadWithProgress,
  type UploadTransportOptions,
} from "./uploadTransport";

export class ApiError extends Error {
  readonly status: number;
  readonly payload: ApiErrorPayload | null;

  constructor(message: string, status: number, payload: ApiErrorPayload | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

function errorMessage(payload: ApiErrorPayload | null, fallback: string): string {
  if (!payload) return fallback;
  const detail = payload.detail ?? payload.error ?? payload.message;
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (detail && typeof detail === "object") {
    const nested = detail as JsonRecord;
    const message = nested.message ?? nested.detail ?? nested.error;
    if (typeof message === "string" && message.trim()) return message.trim();
  }
  return fallback;
}

export async function request<T>(
  path: string,
  init: RequestInit = {},
  options: { preserveNumberSources?: boolean } = {},
): Promise<T> {
  return requestWithTransport<T>(applicationFetch, path, init, options);
}

/** Keep response/error parsing shared while a host supplies authentication and routing. */
export async function requestWithTransport<T>(
  transport: typeof fetch,
  path: string,
  init: RequestInit = {},
  options: { preserveNumberSources?: boolean } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  const bodyIsForm = init.body instanceof FormData;
  if (init.body && !bodyIsForm && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("Accept", "application/json");
  const csrf = saasCsrfFor(path);
  if (csrf && !["GET", "HEAD", "OPTIONS"].includes((init.method || "GET").toUpperCase())) {
    headers.set("X-ReqPilot-CSRF", csrf);
  }

  const response = await transport(path, {
    credentials: "same-origin",
    ...init,
    headers,
  });
  expireSaasSession(response.status, csrf);
  const contentType = response.headers.get("content-type") || "";
  let payload: unknown = null;
  if (response.status !== 204) {
    if (contentType.includes("application/json")) {
      if (options.preserveNumberSources) {
        const responseText = await response.text();
        payload = responseText
          ? parseJsonWithNumberSources(responseText)
          : null;
      } else {
        payload = await response.json();
      }
    } else {
      payload = await response.text();
    }
  }
  if (!response.ok) {
    const fallback = `请求失败（HTTP ${response.status}）`;
    const normalized =
      payload && typeof payload === "object" ? (payload as ApiErrorPayload) : null;
    throw new ApiError(errorMessage(normalized, fallback), response.status, normalized);
  }
  return payload as T;
}

export const api = {
  readiness: () => request<RuntimeReadiness>("/api/runtime-readiness"),
  codexReadiness: () =>
    request<JsonRecord>("/api/runtime/codex-readiness"),
  aiSetup: () => request<AiProviderSetup>("/api/runtime/ai-provider-setup"),
  workerFleet: () =>
    request<RuntimeWorkerFleetStatusV1>("/api/runtime/worker-fleet"),
  saveAiSetup: (payload: JsonRecord) =>
    request<AiProviderSetup>("/api/runtime/ai-provider-setup", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  probeAliyunSetup: () =>
    request<JsonRecord>(
      "/api/runtime/ai-provider-setup/aliyun-probe",
      { method: "POST", body: "{}" },
    ),
  probeAzureRbac: () =>
    request<JsonRecord>(
      "/api/runtime/ai-provider-setup/azure-rbac-probe",
      { method: "POST", body: "{}" },
    ),
  runtimeCapabilities: () =>
    request<RuntimeCapabilityCatalog>("/api/runtime/capabilities"),

  ...createProjectDocumentClient(request, uploadWithProgress, { namespace: applicationNamespace }),
  projectProcessingProfile: (projectId: string) =>
    request<ProcessingProfilePayload>(
      `/api/projects/${encodeURIComponent(projectId)}/processing-profile`,
    ),
  saveProjectProcessingProfile: (
    projectId: string,
    payload: JsonRecord,
  ) =>
    request<ProcessingProfilePayload>(
      `/api/projects/${encodeURIComponent(projectId)}/processing-profile`,
      { method: "PUT", body: JSON.stringify(payload) },
    ),

  knowledgeDocuments: (projectId: string) =>
    request<KnowledgeDocument[]>(
      `/api/projects/${encodeURIComponent(projectId)}/knowledge-documents`,
    ),
  uploadKnowledgeDocument: (
    projectId: string,
    file: File,
    options: UploadTransportOptions = {},
  ) => {
    const form = new FormData();
    form.append("file", file);
    form.append("doc_type", "REFERENCE");
    form.append("status", "ACTIVE");
    return uploadWithProgress<KnowledgeDocument>(
      `/api/projects/${encodeURIComponent(projectId)}/knowledge-documents`,
      form,
      options,
    );
  },
  deleteKnowledgeDocument: (projectId: string, documentId: string) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/knowledge-documents/${encodeURIComponent(documentId)}`,
      { method: "DELETE" },
    ),
  resyncKnowledgeDocument: (projectId: string, documentId: string) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/knowledge-documents/${encodeURIComponent(documentId)}/resync`,
      { method: "POST" },
    ),

  workspace: (
    projectId: string,
    documentId: string,
    includePreview = false,
    requirementSetId = "",
  ) => {
    const params = new URLSearchParams();
    if (includePreview) params.set("include_preview", "1");
    if (requirementSetId) {
      params.set("requirement_set_id", requirementSetId);
    }
    const query = params.size ? `?${params.toString()}` : "";
    return (
    request<WorkspacePayload>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/workspace${query}`,
    )
    );
  },
  requirementSets: (projectId: string, documentId: string) =>
    request<RequirementSetsPayload>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/requirement-sets`,
    ),
  requirementMatrix: (
    projectId: string,
    documentId: string,
    requirementSetId: string,
  ) =>
    request<RequirementMatrixSnapshot>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/requirement-sets/${encodeURIComponent(requirementSetId)}/matrix`,
    ),
  requirementMatrixNode: (
    projectId: string,
    documentId: string,
    requirementSetId: string,
    nodeId: string,
  ) =>
    request<RequirementMatrixNodeDetail>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/requirement-sets/${encodeURIComponent(requirementSetId)}/matrix/nodes/${encodeURIComponent(nodeId)}/detail`,
    ),
  requirementMatrixLogicalTable: (
    projectId: string,
    documentId: string,
    requirementSetId: string,
    logicalTableId: string,
  ) =>
    request<RequirementMatrixTableDetail>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/requirement-sets/${encodeURIComponent(requirementSetId)}/matrix/tables/${encodeURIComponent(logicalTableId)}`,
    ),
  updateRequirementMatrixDisposition: (
    projectId: string,
    documentId: string,
    requirementSetId: string,
    nodeId: string,
    payload: JsonRecord,
  ) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/requirement-sets/${encodeURIComponent(requirementSetId)}/matrix/nodes/${encodeURIComponent(nodeId)}/disposition`,
      { method: "PATCH", body: JSON.stringify(payload) },
    ),
  updateRequirementMatrixDispositions: (
    projectId: string,
    documentId: string,
    requirementSetId: string,
    payload: JsonRecord,
  ) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/requirement-sets/${encodeURIComponent(requirementSetId)}/matrix/dispositions`,
      { method: "PATCH", body: JSON.stringify(payload) },
    ),
  updateRequirementMatrixDecision: (
    projectId: string,
    documentId: string,
    requirementSetId: string,
    nodeId: string,
    payload: JsonRecord,
  ) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/requirement-sets/${encodeURIComponent(requirementSetId)}/matrix/nodes/${encodeURIComponent(nodeId)}/decision`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  activateRequirementSet: (
    projectId: string,
    documentId: string,
    requirementSetId: string,
    payload?: RequirementSetSemanticActivationRequestV1,
    signal?: AbortSignal,
  ) =>
    request<JsonRecord | RequirementSetSemanticActivationResponseV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/requirement-sets/${encodeURIComponent(requirementSetId)}/activate`,
      {
        method: "POST",
        body: JSON.stringify(payload ?? {}),
        signal,
      },
    ),
  deriveRequirementSet: (
    projectId: string,
    documentId: string,
    requirementSetId: string,
  ) =>
    request<RequirementSetDeriveResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/requirement-sets/${encodeURIComponent(requirementSetId)}/derive`,
      { method: "POST", body: "{}" },
    ),

  extractionStatus: (projectId: string, documentId: string) =>
    request<ExtractionStatus>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/llm-document/status`,
    ),
  startExtraction: (
    projectId: string,
    documentId: string,
    runProfileOverride?: RunProfileOverride,
  ) =>
    request<ExtractionStatus>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/start`,
      {
        method: "POST",
        body: JSON.stringify({
          schema_version: "document-extraction-start-request/v1",
          ...(runProfileOverride
            ? { run_profile_override: runProfileOverride }
            : {}),
        }),
      },
    ),
  cancelExtraction: (
    projectId: string,
    documentId: string,
    expectedJobId: string,
  ) =>
    request<ExtractionStatus>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/cancel`,
      {
        method: "POST",
        body: JSON.stringify({
          schema_version: "document-extraction-cancel-request/v1",
          expected_job_id: expectedJobId,
        }),
      },
    ),
  repairExtractionVisualPages: (
    projectId: string,
    documentId: string,
    payload: DocumentExtractionVisualRepairRequestV1,
  ) =>
    request<ExtractionStatus>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/visual-repair`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),
  structure: (projectId: string, documentId: string) =>
    request<StructurePayload>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/structure`,
    ),
  agentStructureProposalStatus: (
    projectId: string,
    documentId: string,
    signal?: AbortSignal,
  ) =>
    request<AgentStructureProposalStatusV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/proposals/status`,
      { signal },
    ),
  startAgentStructureProposal: (
    projectId: string,
    documentId: string,
    payload: AgentStructureProposalStartRequestV1,
  ) =>
    request<AgentStructureProposalJobPublicV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/proposals/start`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  reviseAgentStructureProposal: (
    projectId: string,
    documentId: string,
    payload: AgentStructureProposalReviseRequestV1,
  ) =>
    request<AgentStructureProposalJobPublicV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/proposals/revise`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  steerAgentStructureProposal: (
    projectId: string,
    documentId: string,
    payload: AgentStructureProposalSteerRequestV1,
  ) =>
    request<AgentStructureSteerPublicV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/proposals/steer`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  rejectAgentStructureProposal: (
    projectId: string,
    documentId: string,
    proposalRecordId: string,
    proposalPayloadSha256: string,
    commandId: string,
  ) =>
    request<AgentStructureProposalPublicV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/proposals/reject`,
      {
        method: "POST",
        body: JSON.stringify({
          schema_version: "agent-structure-proposal-reject-request/v1",
          proposal_record_id: proposalRecordId,
          expected_proposal_payload_sha256: proposalPayloadSha256,
          command_id: commandId,
          reason_code: "user_rejected",
        }),
      },
    ),
  applyAgentStructureProposal: (
    projectId: string,
    documentId: string,
    proposalRecordId: string,
    proposalPayloadSha256: string,
    commandId: string,
  ) =>
    request<AgentStructureProposalApplyResultV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/proposals/apply`,
      {
        method: "POST",
        body: JSON.stringify({
          schema_version: "agent-structure-proposal-apply-request/v1",
          proposal_record_id: proposalRecordId,
          expected_proposal_payload_sha256: proposalPayloadSha256,
          command_id: commandId,
        }),
      },
    ),
  nativeStructureReference: (
    projectId: string,
    documentId: string,
    signal?: AbortSignal,
  ) =>
    request<NativeStructureReferencePublicV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/native-reference`,
      { signal },
    ),
  nativeStructureWorkingRevisionHead: (
    projectId: string,
    documentId: string,
    signal?: AbortSignal,
  ) =>
    request<NativeStructureWorkingRevisionHeadResponseV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/native-reference/working-revision-head`,
      { signal },
    ),
  nativeStructureWorkingRevision: (
    projectId: string,
    documentId: string,
    runId: string,
    revision: number,
    expectedRevisionSha256: string | undefined,
    signal?: AbortSignal,
  ) =>
    request<NativeStructureWorkingRevisionV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/native-reference/working-revisions/${encodeURIComponent(runId)}/${encodeURIComponent(String(revision))}${expectedRevisionSha256 ? `?expected_revision_sha256=${encodeURIComponent(expectedRevisionSha256)}` : ""}`,
      { signal },
      { preserveNumberSources: true },
    ),
  nativeStructureWorkingRevisionTablePreview: (
    projectId: string,
    documentId: string,
    runId: string,
    revision: number,
    scopeId: string,
    expectedRevisionSha256: string,
    signal?: AbortSignal,
  ) =>
    request<NativeStructureTablePreviewV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/native-reference/working-revisions/${encodeURIComponent(runId)}/${encodeURIComponent(String(revision))}/tables/${encodeURIComponent(scopeId)}?expected_revision_sha256=${encodeURIComponent(expectedRevisionSha256)}`,
      { signal },
      { preserveNumberSources: true },
    ),
  nativeStructureActivity: (
    projectId: string,
    documentId: string,
    afterSequence = 0,
    signal?: AbortSignal,
  ) =>
    request<NativeStructureActivityStreamV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/native-reference/activity?after_sequence=${encodeURIComponent(String(afterSequence))}`,
      { signal },
    ),
  nativeStructureTablePreview: (
    projectId: string,
    documentId: string,
    scopeId: string,
    signal?: AbortSignal,
  ) =>
    request<NativeStructureTablePreviewV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/native-reference/tables/${encodeURIComponent(scopeId)}`,
      { signal },
    ),
  submitNativeStructureFeedback: (
    projectId: string,
    documentId: string,
    payload: NativeStructureFeedbackRequestV1,
  ) =>
    request<NativeStructureFeedbackResultV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/native-reference/feedback`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  submitNativeStructureProposalDecision: (
    projectId: string,
    documentId: string,
    payload: NativeStructureProposalDecisionRequestV1,
  ) =>
    request<NativeStructureProposalDecisionResultV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/native-reference/proposal-decisions`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  continueNativeStructureReference: (
    projectId: string,
    documentId: string,
    payload: NativeStructureContinueRequestV1,
  ) =>
    request<NativeStructureContinueResultV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/native-reference/continue`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  acknowledgeNativeStructureRenderedReopen: (
    projectId: string,
    documentId: string,
    payload: NativeStructureRenderedReopenRequestV1,
  ) =>
    request<NativeStructureRenderedReopenResultV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/native-reference/rendered-reopen`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  startNativeStructureBaseline: (
    projectId: string,
    documentId: string,
    payload: NativeStructureBaselineStartRequestV1,
  ) =>
    request<NativeStructureBaselineJobPublicV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/native-reference/advance`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  nativeStructureBaselineStatus: (
    projectId: string,
    documentId: string,
    signal?: AbortSignal,
  ) =>
    request<NativeStructureBaselineStatusV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/native-reference/advance-status`,
      { signal },
    ),
  nativeParentProvisionStatus: (
    projectId: string,
    documentId: string,
    signal?: AbortSignal,
  ) =>
    request<NativeParentProvisionStatusV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/native-reference/parent-provision-status`,
      { signal },
    ),
  nativeParentProvisionResult: (
    projectId: string,
    documentId: string,
    signal?: AbortSignal,
  ) =>
    request<NativeParentProvisionBaselinePublicV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/native-reference/parent-provisions`,
      { signal },
    ),
  sourceRequirementBaselineStatus: (
    projectId: string,
    documentId: string,
    signal?: AbortSignal,
  ) =>
    request<SourceRequirementBaselineStatusV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/requirement-baseline/status`,
      { signal },
    ),
  startSourceRequirementBaselineRun: (
    projectId: string,
    documentId: string,
    payload: SourceRequirementBaselineRunStartRequestV1,
  ) =>
    request<SourceRequirementBaselineRunJobPublicV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/requirement-baseline/runs/start`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  startSourceItemTextBaselineRun: (
    projectId: string,
    documentId: string,
    payload: SourceItemTextBaselineRunStartRequestV1,
  ) =>
    request<SourceRequirementBaselineRunJobPublicV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/requirement-baseline/text-runs/start`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  startSourceItemScopeRevisionRun: (
    projectId: string,
    documentId: string,
    payload: SourceItemScopeRevisionRunStartRequestV1,
  ) =>
    request<SourceRequirementBaselineRunJobPublicV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/requirement-baseline/revisions/start`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  startSourceItemScopeUndoRun: (
    projectId: string,
    documentId: string,
    payload: SourceItemScopeUndoRunStartRequestV1,
  ) =>
    request<SourceRequirementBaselineRunJobPublicV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/requirement-baseline/revisions/undo`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  sourceRequirementBaselineRunStatus: (
    projectId: string,
    documentId: string,
    signal?: AbortSignal,
  ) =>
    request<SourceRequirementBaselineRunStatusV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/requirement-baseline/runs/status`,
      { signal },
    ),
  sourceRequirementBaselineOutline: (
    projectId: string,
    documentId: string,
    signal?: AbortSignal,
  ) =>
    request<SourceRequirementBaselineOutlineV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/requirement-baseline/outline`,
      { signal },
    ),
  sourceRequirementRecord: (
    projectId: string,
    documentId: string,
    sourceRecordId: string,
    signal?: AbortSignal,
  ) =>
    request<SourceRequirementRecordDetailV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/requirement-baseline/records/${encodeURIComponent(sourceRecordId)}`,
      { signal },
    ),
  submitSourceRequirementFeedback: (
    projectId: string,
    documentId: string,
    payload: SourceRequirementFeedbackRequestV1,
  ) =>
    request<SourceRequirementFeedbackResultV1>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/requirement-baseline/feedback`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  entryClassifications: (projectId: string, documentId: string) =>
    request<EntryClassificationResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/entry-classifications`,
    ),
  updateEntryClassifications: (
    projectId: string,
    documentId: string,
    payload: EntryClassificationPatchCommand,
  ) =>
    request<EntryClassificationResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/entry-classifications`,
      { method: "PATCH", body: JSON.stringify(payload) },
    ),
  acceptEntryClassifications: (
    projectId: string,
    documentId: string,
    payload: EntryClassificationCommand,
    signal?: AbortSignal,
  ) =>
    request<EntryClassificationResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/entry-classifications/acceptance`,
      { method: "PUT", body: JSON.stringify(payload), signal },
    ),
  patchStructure: (
    projectId: string,
    documentId: string,
    payload: JsonRecord,
  ) =>
    request<StructurePayload>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/structure/patch`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  previewBoundaryCalibration: (
    projectId: string,
    documentId: string,
    payload: JsonRecord,
  ) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/structure/patch/preview`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  undoStructure: (
    projectId: string,
    documentId: string,
    payload: JsonRecord = {},
  ) =>
    request<StructurePayload>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/structure/undo`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  reviseTableRecordProjection: (
    projectId: string,
    documentId: string,
    projectionId: string,
    payload: TableRecordProjectionRevisionRequest,
  ) =>
    request<TableRecordProjectionRevisionResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/table-record-projections/${encodeURIComponent(projectionId)}/revise`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  tableRecordProjectionRowGroups: (
    projectId: string,
    documentId: string,
    projectionId: string,
    signal?: AbortSignal,
  ) =>
    request<TableRecordProjectionRowGroupReview>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/table-record-projections/${encodeURIComponent(projectionId)}/row-groups`,
      { signal },
    ),
  startStructureOptimization: (
    projectId: string,
    documentId: string,
    llmProviderOverride?: "aliyun" | "azure_openai",
  ) =>
    request<StructureOptimizationPayload>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/structure/optimization`,
      {
        method: "POST",
        body: JSON.stringify({
          schema_version: "structure-optimization-start-request/v1",
          ...(llmProviderOverride
            ? { llm_provider_override: llmProviderOverride }
            : {}),
        }),
      },
    ),
  structureOptimizationStatus: (projectId: string, documentId: string) =>
    request<StructureOptimizationPayload>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/structure/optimization/status`,
    ),
  reviewSession: (
    projectId: string,
    documentId: string,
    sessionId: string,
  ) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/llm-reviews/${encodeURIComponent(sessionId)}`,
    ),
  requirementEnrichment: (
    projectId: string,
    documentId: string,
    sessionId: string,
    signal?: AbortSignal,
  ) =>
    request<RequirementEnrichmentState>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/llm-reviews/${encodeURIComponent(sessionId)}/enrichment`,
      { signal },
    ),
  previewRequirementSemanticPublication: (
    projectId: string,
    documentId: string,
    sessionId: string,
    payload: RequirementSemanticPublicationPreviewRequestV2,
    signal?: AbortSignal,
  ) =>
    request<RequirementSemanticPublicationPreviewResponseV2>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/llm-reviews/${encodeURIComponent(sessionId)}/publication-preview`,
      { method: "POST", body: JSON.stringify(payload), signal },
      { preserveNumberSources: true },
    ),
  publishRequirementSemanticDraft: (
    projectId: string,
    documentId: string,
    sessionId: string,
    payload: RequirementSemanticPublishDraftRequestV2,
    signal?: AbortSignal,
  ) =>
    request<RequirementSemanticPublishDraftResponseV2>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/llm-reviews/${encodeURIComponent(sessionId)}/publish-draft`,
      { method: "POST", body: JSON.stringify(payload), signal },
      { preserveNumberSources: true },
    ),
  runRequirementAtomizationScope: (
    projectId: string,
    documentId: string,
    sessionId: string,
    payload: RequirementAtomizationScopeCommand,
    signal?: AbortSignal,
  ) =>
    request<RequirementAtomizationScopeRunResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/llm-reviews/${encodeURIComponent(sessionId)}/atomization-runs`,
      { method: "POST", body: JSON.stringify(payload), signal },
    ),
  saveReviewSession: (
    projectId: string,
    documentId: string,
    sessionId: string,
    payload: JsonRecord,
  ) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/llm-reviews/${encodeURIComponent(sessionId)}`,
      { method: "PUT", body: JSON.stringify(payload) },
    ),
  confirmReviewNode: (
    projectId: string,
    documentId: string,
    sessionId: string,
    payload: JsonRecord,
  ) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/llm-reviews/${encodeURIComponent(sessionId)}/confirmation`,
      { method: "PATCH", body: JSON.stringify(payload) },
    ),

  lockExtraction: (
    projectId: string,
    documentId: string,
    requirementSetId: string,
    expectedReadinessSha256: string,
    waiverReason = "",
  ) =>
    request<RequirementSetLockReceipt>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/lock-extraction`,
      {
        method: "POST",
        body: JSON.stringify({
          schema_version: "requirement-set-lock-request/v1",
          requirement_set_id: requirementSetId,
          expected_readiness_sha256: expectedReadinessSha256,
          waiver_reason: waiverReason,
        }),
      },
    ),
  lockReadiness: (
    projectId: string,
    documentId: string,
    requirementSetId: string,
  ) =>
    request<RequirementSetLockReadiness>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/lock-readiness?requirement_set_id=${encodeURIComponent(requirementSetId)}`,
    ),
  previewAiScope: (
    projectId: string,
    documentId: string,
    payload: RequirementAnalysisScopeRequest,
  ) =>
    request<RequirementAnalysisScopePreview>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/ai-analysis/scope-preview`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),
  startAi: (
    projectId: string,
    documentId: string,
    payload: RequirementAnalysisScopeRequest & {
      expected_scope_sha256: string;
      profile_id?: string;
    },
  ) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/run-ai-analysis/start`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),
  startAiLegacy: (
    projectId: string,
    documentId: string,
    requirementSetId: string,
    profileId?: string,
  ) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/run-ai-analysis/start`,
      {
        method: "POST",
        body: JSON.stringify({
          requirement_set_id: requirementSetId,
          ...(profileId ? { profile_id: profileId } : {}),
        }),
      },
    ),
  stopAi: (projectId: string, documentId: string, expectedJobId = "") =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/run-ai-analysis/stop`,
      {
        method: "POST",
        body: JSON.stringify(
          expectedJobId ? { expected_job_id: expectedJobId } : {},
        ),
      },
    ),
  aiStatus: (projectId: string, documentId: string) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/ai-analysis-status?event_limit=120`,
    ),
  exports: (projectId: string) =>
    request<JsonRecord[]>(`/api/projects/${encodeURIComponent(projectId)}/exports`),
  createExport: (projectId: string, payload: JsonRecord) =>
    request<JsonRecord>(`/api/projects/${encodeURIComponent(projectId)}/export`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  exportJob: (projectId: string, jobId: string) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/export-jobs/${encodeURIComponent(jobId)}`,
    ),

  visualCapabilities: (projectId: string, documentId: string) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/visual-capabilities`,
    ),
  startRestoration: (
    projectId: string,
    documentId: string,
    payload: JsonRecord,
  ) =>
    request<RestorationRun>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/runs`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  latestRestoration: (projectId: string, documentId: string) =>
    request<RestorationRun | null>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/runs/latest`,
    ),
  restorationRun: (
    projectId: string,
    documentId: string,
    runId: string,
  ) =>
    request<RestorationRun>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/runs/${encodeURIComponent(runId)}`,
    ),
  restorationPage: (
    projectId: string,
    documentId: string,
    runId: string,
    page: number,
  ) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/runs/${encodeURIComponent(runId)}/pages/${page}`,
    ),
  sourcePage: (projectId: string, documentId: string, page: number) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/source-pages/${page}`,
    ),
  stagedSourcePage: (
    jobId: string,
    attemptGeneration: number,
    page: number,
    pageUrlTemplate = "",
  ) => {
    const fallbackPath = `/api/extraction/attempts/${encodeURIComponent(jobId)}/generations/${attemptGeneration}/source-pages/${page}`;
    const templatePath = pageUrlTemplate
      .replaceAll("{job_id}", encodeURIComponent(jobId))
      .replaceAll("{jobId}", encodeURIComponent(jobId))
      .replaceAll("{attempt_generation}", String(attemptGeneration))
      .replaceAll("{generation}", String(attemptGeneration))
      .replaceAll("{page_number}", String(page))
      .replaceAll("{page}", String(page));
    const path = templatePath.startsWith("/api/")
      ? templatePath
      : fallbackPath;
    return request<JsonRecord>(path);
  },
  parsingDiagnostics: (projectId: string, documentId: string) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/parse-risks`,
    ),
  acknowledgeParsingRisks: (
    projectId: string,
    documentId: string,
    payload: JsonRecord,
  ) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/parse-risks/acknowledgement`,
      { method: "PUT", body: JSON.stringify(payload) },
    ),
  geometryFocus: (
    projectId: string,
    documentId: string,
    nodeId: string,
    requirementSetId = "",
  ) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/geometry-focus/${encodeURIComponent(nodeId)}${
        requirementSetId
          ? `?requirement_set_id=${encodeURIComponent(requirementSetId)}`
          : ""
      }`,
    ),
  boundaryCalibrationPage: (
    projectId: string,
    documentId: string,
    page: number,
    nodeId = "",
  ) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/boundary-calibration/pages/${page}${
        nodeId ? `?node_id=${encodeURIComponent(nodeId)}` : ""
      }`,
    ),
  restorationDetails: (
    projectId: string,
    documentId: string,
    runId: string,
  ) =>
    request<JsonRecord>(
      `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/extraction/runs/${encodeURIComponent(runId)}/details`,
    ),
};

export function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

export function asRecord(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}
