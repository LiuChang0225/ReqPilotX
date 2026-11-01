import { request } from "../../shared/api/client";

export type OpenKbEvidenceStatus = {
  available: boolean; knowledge_base_version: number; catalog_sha256: string; generation: string;
  live_openkb: "NOT_RUN" | "UNAVAILABLE_OR_NOT_RUN"; runtime_available?: boolean;
};
export type EvidenceUploadMetadata = { doc_type: string; authority: string; claim_role: string; applicability: string };
export type EvidenceUploadResult = {
  documents: Array<{ file_name: string; status: "REGISTERED" | "FAILED"; document_id?: string; message?: string }>;
  registered_count: number; failed_count: number; knowledge_base_version: number;
};
export type EvidenceScope = { projectId: string; documentId: string; sourceSha256: string; baselineSha256: string };
export type EvidenceSession = {
  session_id: string; project_id: string; document_id: string; generation: string; packet_sha256: string;
  knowledge_base_version: number; catalog_sha256: string; directory_catalog_sha256: string;
  source_sha256: string; baseline_sha256: string;
  unit_ids: string[]; catalog_gaps: EvidenceGap[]; human_acceptance: "NOT_CLAIMED"; live_openkb: "NOT_RUN";
};
export type EvidenceGap = { code?: string; message?: string; document_id?: string };
export type OriginalReference = {
  schema_version: "openkb-original-reference/v1"; project_id: string; kb: string; generation: string;
  document_id: string; source_sha256: string; fact_id: string; source_ref: Record<string, unknown>;
  quote_start: number; quote_end: number; excerpt: string; text_sha256: string;
};
export type OriginalEvidence = {
  evidence_id: string; text: string; source_ref: Record<string, unknown>; reference: OriginalReference;
  authority?: string; applicability?: string; claim_role?: string;
};
export type OriginalEvidenceResult = { generation: string; document_id: string; assessment: "NOT_PERFORMED"; evidence: OriginalEvidence[]; gaps: EvidenceGap[] };
export type PreparedEvidenceReview = {
  status: "HOST_PENDING_DRAFT_SAVED"; job_id: null; assessment_sha256: string; pending_unit_count: number;
  human_acceptance: "NOT_CLAIMED"; live_openkb: "NOT_RUN";
  semantic_validation: "NOT_PERFORMED";
};
const sha = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const generation = (value: unknown): value is string => typeof value === "string" && /^generation-[a-f0-9]{64}$/.test(value);
const count = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0;
const record = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const ids = (value: unknown): value is string[] => Array.isArray(value) && value.every(id => typeof id === "string" && Boolean(id)) && new Set(value).size === value.length;
const fail = (message: string): never => { throw new Error(message); };
const canonical = (value: unknown): string => JSON.stringify(value, (_key, item: unknown) => record(item)
  ? Object.fromEntries(Object.entries(item).sort(([left], [right]) => left.localeCompare(right))) : item);
const projectRoot = (project: string) => `/api/projects/${encodeURIComponent(project)}/openkb-evidence`;
const sessionRoot = (scope: EvidenceScope) => `/api/projects/${encodeURIComponent(scope.projectId)}/sor-documents/${encodeURIComponent(scope.documentId)}/agent-structure/delivery/openkb-evidence/sessions`;

export function validateEvidenceStatus(value: OpenKbEvidenceStatus) {
  if (!value || typeof value.available !== "boolean" || !count(value.knowledge_base_version) || !sha(value.catalog_sha256)
    || !generation(value.generation) || !["NOT_RUN", "UNAVAILABLE_OR_NOT_RUN"].includes(value.live_openkb)) fail("资料状态不完整，请重新同步项目资料。");
  return value;
}
export function validateEvidenceSession(value: EvidenceSession, scope: EvidenceScope, status: OpenKbEvidenceStatus, selectedUnits: string[]) {
  if (!/^[a-f0-9]{32}$/.test(value?.session_id || "") || value.project_id !== scope.projectId || value.document_id !== scope.documentId
    || value.source_sha256 !== scope.sourceSha256 || value.baseline_sha256 !== scope.baselineSha256
    || value.generation !== status.generation || !sha(value.catalog_sha256) || value.directory_catalog_sha256 !== status.catalog_sha256
    || value.knowledge_base_version !== status.knowledge_base_version || !sha(value.packet_sha256) || !ids(value.unit_ids)
    || !Array.isArray(value.catalog_gaps) || value.human_acceptance !== "NOT_CLAIMED" || value.live_openkb !== "NOT_RUN"
    || selectedUnits.length && (value.unit_ids.length !== selectedUnits.length || selectedUnits.some(id => !value.unit_ids.includes(id)))) {
    fail("原文证据包与当前项目、文档或分析单元不一致，请重新读取。");
  }
  return value;
}
export function validateOriginalEvidence(value: OriginalEvidenceResult, scope: EvidenceScope, session: EvidenceSession, documentId: string, sourceSha256: string) {
  if (!value || value.generation !== session.generation || value.document_id !== documentId || value.assessment !== "NOT_PERFORMED"
    || !Array.isArray(value.evidence) || !Array.isArray(value.gaps)) fail("原文证据响应不完整，请重新读取。");
  const seen = new Set<string>();
  for (const item of value.evidence) {
    const ref = item?.reference;
    if (!/^original-evidence-[a-f0-9]{64}$/.test(item?.evidence_id || "") || seen.has(item.evidence_id) || typeof item.text !== "string" || !record(item.source_ref)
      || ref?.schema_version !== "openkb-original-reference/v1" || ref.project_id !== scope.projectId
      || ref.document_id !== documentId || ref.generation !== session.generation || !ref.kb || !ref.fact_id
      || ref.source_sha256 !== sourceSha256 || !sha(ref.source_sha256) || !sha(ref.text_sha256) || !record(ref.source_ref) || !count(ref.quote_start)
      || !count(ref.quote_end) || ref.quote_end < ref.quote_start || ref.excerpt !== item.text || canonical(item.source_ref) !== canonical(ref.source_ref)) {
      fail("原文引用与当前资料版本不一致，未载入未核实的证据。");
    }
    if ([item.authority, item.applicability, item.claim_role].some(value => value !== undefined && typeof value !== "string")) fail("原文来源登记信息不完整，请重新同步。");
    seen.add(item.evidence_id);
  }
  return value;
}

/** Upload actual selected bytes; relative folder names are labels, never server filesystem paths. */
export function evidenceFileName(file: File) { return file.webkitRelativePath || file.name; }
export const openKbEvidenceApi = {
  status: async (project: string, signal?: AbortSignal) => validateEvidenceStatus(await request<OpenKbEvidenceStatus>(`${projectRoot(project)}/status`, { signal })),
  upload: async (project: string, files: File[], metadata: EvidenceUploadMetadata, signal?: AbortSignal) => {
    const form = new FormData();
    files.forEach(file => form.append("files", file, evidenceFileName(file)));
    Object.entries(metadata).forEach(([key, value]) => form.append(key, value));
    const result = await request<EvidenceUploadResult>(`${projectRoot(project)}/upload`, { method: "POST", body: form, signal });
    if (!result || !Array.isArray(result.documents) || !count(result.registered_count) || !count(result.failed_count)
      || !count(result.knowledge_base_version) || result.documents.length !== files.length
      || result.documents.some(item => !item || typeof item.file_name !== "string" || !["REGISTERED", "FAILED"].includes(item.status)
        || item.status === "REGISTERED" && !item.document_id)
      || result.registered_count !== result.documents.filter(item => item.status === "REGISTERED").length
      || result.failed_count !== result.documents.filter(item => item.status === "FAILED").length
      || result.registered_count + result.failed_count !== files.length
      || canonical(result.documents.map(item => item.file_name).sort()) !== canonical(files.map(evidenceFileName).sort())) {
      fail("登记回执不完整。服务端可能已保存部分原件，请同步资料后核对再重试。");
    }
    return result;
  },
  createSession: async (scope: EvidenceScope, status: OpenKbEvidenceStatus, documents: string[] | null, selectedUnits: string[], signal?: AbortSignal) => {
    if (!scope.projectId || !scope.documentId || !sha(scope.sourceSha256) || !sha(scope.baselineSha256) || !ids(selectedUnits)
      || documents !== null && (!ids(documents) || documents.length > 64)) fail("当前原文版本或资料范围不完整，请同步后重新选择。");
    const value = await request<EvidenceSession>(sessionRoot(scope), { method: "POST", signal, body: JSON.stringify({
      expected_source_sha256: scope.sourceSha256, expected_baseline_sha256: scope.baselineSha256,
      expected_knowledge_base_version: status.knowledge_base_version, knowledge_document_ids: documents, selected_unit_ids: selectedUnits,
    }) });
    return validateEvidenceSession(value, scope, status, selectedUnits);
  },
  originals: async (scope: EvidenceScope, session: EvidenceSession, document: string, sourceSha256: string, signal?: AbortSignal) => validateOriginalEvidence(
    await request<OriginalEvidenceResult>(`${sessionRoot(scope)}/${encodeURIComponent(session.session_id)}/originals/${encodeURIComponent(document)}`, { signal }), scope, session, document, sourceSha256),
  prepareReview: async (scope: EvidenceScope, session: EvidenceSession, evidenceIds: string[], signal?: AbortSignal) => {
    if (!ids(evidenceIds) || evidenceIds.length > 32 || evidenceIds.some(id => !/^original-evidence-[a-f0-9]{64}$/.test(id))) fail("请仅选择本次读取的原文，最多 32 段。");
    const value = await request<PreparedEvidenceReview>(`${sessionRoot(scope)}/${encodeURIComponent(session.session_id)}/prepare-review`, {
      method: "POST", signal, body: JSON.stringify({ expected_packet_sha256: session.packet_sha256, evidence_ids: evidenceIds }),
    });
    if (value?.status !== "HOST_PENDING_DRAFT_SAVED" || value.job_id !== null || !sha(value.assessment_sha256)
      || !count(value.pending_unit_count) || value.human_acceptance !== "NOT_CLAIMED" || value.live_openkb !== "NOT_RUN" || value.semantic_validation !== "NOT_PERFORMED") {
      fail("草稿回执不完整。请同步工作账本核对，勿重复提交。");
    }
    return value;
  },
};
