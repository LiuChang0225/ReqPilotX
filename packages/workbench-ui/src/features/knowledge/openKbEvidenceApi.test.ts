import { afterEach, describe, expect, it, vi } from "vitest";
import { request } from "../../shared/api/client";
import { evidenceFileName, openKbEvidenceApi, validateEvidenceSession, validateEvidenceStatus, validateOriginalEvidence, type EvidenceScope, type EvidenceSession, type OpenKbEvidenceStatus, type OriginalEvidenceResult } from "./openKbEvidenceApi";

vi.mock("../../shared/api/client", () => ({ request: vi.fn() }));
afterEach(() => vi.mocked(request).mockReset());
const hash = (character = "a") => character.repeat(64);
const scope: EvidenceScope = { projectId: "project/p", documentId: "SOR/d", sourceSha256: hash("b"), baselineSha256: hash("c") };
function status(): OpenKbEvidenceStatus { return { available: true, knowledge_base_version: 3, catalog_sha256: hash(), generation: `generation-${hash()}`, live_openkb: "NOT_RUN", runtime_available: false }; }
function session(): EvidenceSession { return { session_id: "d".repeat(32), project_id: scope.projectId, document_id: scope.documentId, source_sha256: scope.sourceSha256, baseline_sha256: scope.baselineSha256,
  generation: status().generation, packet_sha256: hash("d"), catalog_sha256: hash("e"), directory_catalog_sha256: status().catalog_sha256, knowledge_base_version: 3, unit_ids: ["unit"], catalog_gaps: [], human_acceptance: "NOT_CLAIMED", live_openkb: "NOT_RUN" }; }
function originals(): OriginalEvidenceResult { return { generation: session().generation, document_id: "knowledge", assessment: "NOT_PERFORMED", gaps: [], evidence: [{ evidence_id: `original-evidence-${hash("f")}`, text: "registered original", source_ref: { location: { page_number: 2 } },
  reference: { schema_version: "openkb-original-reference/v1", project_id: scope.projectId, kb: "project-kb", generation: session().generation, document_id: "knowledge", source_sha256: hash("f"), fact_id: "fact", source_ref: { location: { page_number: 2 } }, quote_start: 0, quote_end: 19, excerpt: "registered original", text_sha256: hash("b") }, authority: "UNKNOWN", applicability: "UNKNOWN" }] }; }

describe("native source evidence API identity", () => {
  it("accepts honest unavailable runtime without inventing a live model run", () => {
    expect(validateEvidenceStatus(status()).runtime_available).toBe(false);
    expect(() => validateEvidenceStatus({ ...status(), live_openkb: "RUN" as never })).toThrow("资料状态不完整");
    expect(() => validateEvidenceStatus({ ...status(), generation: "old" })).toThrow("资料状态不完整");
  });
  it("binds session to the SOR source, baseline, units and full-directory version but allows a selected-scope catalog", () => {
    expect(validateEvidenceSession(session(), scope, status(), ["unit"]).catalog_sha256).not.toEqual(status().catalog_sha256);
    for (const patch of [{ project_id: "other" }, { document_id: "other" }, { source_sha256: hash("e") }, { baseline_sha256: hash("e") },
      { generation: `generation-${hash("e")}` }, { directory_catalog_sha256: hash("e") }, { knowledge_base_version: 4 }, { unit_ids: ["other"] }]) {
      expect(() => validateEvidenceSession({ ...session(), ...patch }, scope, status(), ["unit"])).toThrow("当前项目、文档或分析单元不一致");
    }
  });
  it("rejects fabricated originals, cross-project/reference generations, mixed locators and changed source hashes", () => {
    expect(validateOriginalEvidence(originals(), scope, session(), "knowledge", hash("f")).evidence[0].text).toBe("registered original");
    for (const patch of [{ project_id: "other" }, { document_id: "SOR/d" }, { generation: `generation-${hash("e")}` }, { source_sha256: hash("e") }, { excerpt: "invented" }, { source_ref: { location: { page_number: 9 } } }]) {
      const value = originals(); value.evidence[0].reference = { ...value.evidence[0].reference, ...patch };
      expect(() => validateOriginalEvidence(value, scope, session(), "knowledge", hash("f"))).toThrow("当前资料版本不一致");
    }
    const duplicate = originals(); duplicate.evidence.push(duplicate.evidence[0]);
    expect(() => validateOriginalEvidence(duplicate, scope, session(), "knowledge", hash("f"))).toThrow("当前资料版本不一致");
    expect(() => validateOriginalEvidence({ ...originals(), assessment: "COMPLIANT" as never }, scope, session(), "knowledge", hash("f"))).toThrow("响应不完整");
  });
  it("escapes project/document IDs and sends exact current hashes, scope and abort signal", async () => {
    vi.mocked(request).mockResolvedValue(session());
    const signal = new AbortController().signal;
    await openKbEvidenceApi.createSession(scope, status(), [], ["unit"], signal);
    const [path, init] = vi.mocked(request).mock.calls[0];
    expect(path).toBe("/api/projects/project%2Fp/sor-documents/SOR%2Fd/agent-structure/delivery/openkb-evidence/sessions");
    expect(init?.signal).toBe(signal);
    expect(JSON.parse(String(init?.body))).toEqual({ expected_source_sha256: scope.sourceSha256, expected_baseline_sha256: scope.baselineSha256, expected_knowledge_base_version: 3, knowledge_document_ids: [], selected_unit_ids: ["unit"] });
  });
  it("keeps default knowledge scope distinct from explicitly empty scope", async () => {
    vi.mocked(request).mockResolvedValue(session());
    await openKbEvidenceApi.createSession(scope, status(), null, ["unit"]);
    expect(JSON.parse(String(vi.mocked(request).mock.calls[0][1]?.body)).knowledge_document_ids).toBeNull();
  });
  it("uploads real bytes with repeated files fields and relative names, without a server path or indexing request", async () => {
    const file = new File(["real original"], "source.txt");
    Object.defineProperty(file, "webkitRelativePath", { value: "folder/source.txt" });
    vi.mocked(request).mockResolvedValue({ documents: [{ file_name: "folder/source.txt", status: "REGISTERED", document_id: "registered" }], registered_count: 1, failed_count: 0, knowledge_base_version: 4 });
    await openKbEvidenceApi.upload(scope.projectId, [file], { doc_type: "REFERENCE", authority: "UNKNOWN", claim_role: "UNKNOWN", applicability: "UNKNOWN" });
    const form = vi.mocked(request).mock.calls[0][1]?.body as FormData;
    expect(evidenceFileName(file)).toBe("folder/source.txt");
    expect((form.get("files") as File).name).toBe("folder/source.txt");
    expect(form.get("authority")).toBe("UNKNOWN");
    expect([...form.keys()]).toEqual(["files", "doc_type", "authority", "claim_role", "applicability"]);
  });
  it("rejects incomplete upload receipts rather than claiming all files were saved", async () => {
    vi.mocked(request).mockResolvedValue({ documents: [], registered_count: 1, failed_count: 0, knowledge_base_version: 4 });
    await expect(openKbEvidenceApi.upload("p", [new File(["original"], "source.txt")], { doc_type: "REFERENCE", authority: "UNKNOWN", claim_role: "UNKNOWN", applicability: "UNKNOWN" })).rejects.toThrow("服务端可能已保存部分原件");
  });
  it("saves only selected issued IDs with the frozen packet hash and checks no-model/no-acceptance receipt", async () => {
    const receipt = { status: "HOST_PENDING_DRAFT_SAVED", job_id: null, assessment_sha256: hash("e"), pending_unit_count: 1, human_acceptance: "NOT_CLAIMED", semantic_validation: "NOT_PERFORMED", live_openkb: "NOT_RUN" };
    vi.mocked(request).mockResolvedValue(receipt);
    const eid = originals().evidence[0].evidence_id;
    expect(await openKbEvidenceApi.prepareReview(scope, session(), [eid])).toEqual(receipt);
    expect(JSON.parse(String(vi.mocked(request).mock.calls[0][1]?.body))).toEqual({ expected_packet_sha256: session().packet_sha256, evidence_ids: [eid] });
    vi.mocked(request).mockResolvedValue({ ...receipt, job_id: "model-job" });
    await expect(openKbEvidenceApi.prepareReview(scope, session(), [])).rejects.toThrow("勿重复提交");
    vi.mocked(request).mockResolvedValue({ ...receipt, human_acceptance: "ACCEPTED" });
    await expect(openKbEvidenceApi.prepareReview(scope, session(), [])).rejects.toThrow("勿重复提交");
  });
  it("rejects duplicate or fabricated evidence IDs before mutation", async () => {
    const eid = originals().evidence[0].evidence_id;
    await expect(openKbEvidenceApi.prepareReview(scope, session(), [eid, eid])).rejects.toThrow("最多 32 段");
    await expect(openKbEvidenceApi.prepareReview(scope, session(), ["made-up"])).rejects.toThrow("最多 32 段");
    expect(request).not.toHaveBeenCalled();
  });
});
