import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./client";
import { jsonNumberSource } from "./jsonNumberSources";
import type {
  AgentStructureProposalReviseRequestV1,
  AgentStructureProposalStartRequestV1,
  AgentStructureProposalSteerRequestV1,
  DocumentExtractionVisualRepairRequestV1,
  EntryClassificationCommand,
  EntryClassificationPatchCommand,
  NativeStructureFeedbackRequestV1,
  NativeStructureContinueRequestV1,
  NativeStructureProposalDecisionRequestV1,
  SourceRequirementBaselineRunStartRequestV1,
  SourceItemTextBaselineRunStartRequestV1,
  SourceItemScopeRevisionRunStartRequestV1,
  SourceItemScopeUndoRunStartRequestV1,
  SourceRequirementFeedbackRequestV1,
  RequirementAtomizationScopeCommand,
  RequirementSemanticPublicationPreviewRequestV2,
  RequirementSemanticPublishDraftRequestV2,
} from "./contracts";

describe("direct-text SourceItem API", () => {
  it("submits only source-bound node identities", async () => {
    const fetchSpy = installFetchSpy();
    const payload = {
      schema_version: "source-item-text-baseline-start-request/v1",
      command_id: "text-run-1",
      expected_source_sha256: "a".repeat(64),
      expected_artifact_id: "native-reference-1",
      node_ids: ["section-1.1"],
    } satisfies SourceItemTextBaselineRunStartRequestV1;

    await api.startSourceItemTextBaselineRun(
      "project a",
      "document/1",
      payload,
    );

    expect(fetchSpy.mock.calls[0]?.[0]).toBe(
      "/api/projects/project%20a/sor-documents/document%2F1/agent-structure/requirement-baseline/text-runs/start",
    );
    expect(fetchSpy.mock.calls[0]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(payload),
      }),
    );
  });

  it("keeps scope revision and undo commands content-free", async () => {
    const fetchSpy = installFetchSpy();
    const revision = {
      schema_version: "source-item-scope-revision-start-request/v1",
      command_id: "scope-revision-1",
      expected_source_sha256: "a".repeat(64),
      expected_document_map_artifact_sha256: "b".repeat(64),
      expected_baseline_artifact_id:
        "source-requirement-baseline-" + "c".repeat(32),
      expected_baseline_artifact_sha256: "d".repeat(64),
      expected_baseline_revision: 22,
      affected_scope_ids: ["text:section-1.2"],
      comparison_scope_ids: ["text:section-2.3"],
      feedback_ids: ["source-requirement-feedback-" + "e".repeat(32)],
    } satisfies SourceItemScopeRevisionRunStartRequestV1;
    const undo = {
      schema_version: "source-item-scope-undo-start-request/v1",
      command_id: "scope-undo-1",
      expected_source_sha256: "a".repeat(64),
      expected_document_map_artifact_sha256: "b".repeat(64),
      expected_baseline_artifact_id:
        "source-requirement-baseline-" + "f".repeat(32),
      expected_baseline_artifact_sha256: "1".repeat(64),
      expected_baseline_revision: 23,
      expected_scope_revision_receipt_sha256: "2".repeat(64),
    } satisfies SourceItemScopeUndoRunStartRequestV1;

    await api.startSourceItemScopeRevisionRun("project a", "document/1", revision);
    await api.startSourceItemScopeUndoRun("project a", "document/1", undo);

    const root =
      "/api/projects/project%20a/sor-documents/document%2F1/agent-structure/requirement-baseline/revisions";
    expect(fetchSpy.mock.calls[0]).toEqual([
      `${root}/start`,
      expect.objectContaining({ method: "POST", body: JSON.stringify(revision) }),
    ]);
    expect(fetchSpy.mock.calls[1]).toEqual([
      `${root}/undo`,
      expect.objectContaining({ method: "POST", body: JSON.stringify(undo) }),
    ]);
    expect(JSON.stringify(revision)).not.toContain("参照相邻条款");
  });
});

function installFetchSpy() {
  const spy = vi.fn(
    async (_input: RequestInfo | URL, _init?: RequestInit) =>
      new Response(JSON.stringify({}), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", spy);
  return spy;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("agent Structure proposal API", () => {
  it("binds start, revise, reject, and Apply to the exact document proposal", async () => {
    const fetchSpy = installFetchSpy();
    const base = {
      command_id: "proposal-command-1",
      expected_source_sha256: "a".repeat(64),
      expected_revision_number: 2,
      expected_effective_snapshot_id: "effective-structure-" + "b".repeat(32),
      intent: "REVIEW_PARENTAGE" as const,
      focus_structure_node_ids: ["structure-node-" + "c".repeat(32)],
      suggested_operations: [],
      suggested_operations_authority: "NONE" as const,
    };
    const start = {
      ...base,
      schema_version: "agent-structure-proposal-start-request/v1" as const,
    } satisfies AgentStructureProposalStartRequestV1;
    const revise = {
      ...base,
      schema_version: "agent-structure-proposal-revise-request/v1" as const,
      proposal_record_id: "agent-structure-proposal-" + "d".repeat(32),
      expected_proposal_payload_sha256: "e".repeat(64),
    } satisfies AgentStructureProposalReviseRequestV1;
    const controller = new AbortController();
    const steer = {
      schema_version: "agent-structure-proposal-steer-request/v1" as const,
      job_id: "job-agent-1",
      command_id: "steer-1",
      expected_job_generation: 3,
      expected_source_sha256: base.expected_source_sha256,
      expected_revision_number: base.expected_revision_number,
      expected_effective_snapshot_id: base.expected_effective_snapshot_id,
      intent: "REVIEW_ORDER" as const,
      focus_structure_node_ids: base.focus_structure_node_ids,
      suggested_operations: [],
      suggested_operations_authority: "NONE" as const,
    } satisfies AgentStructureProposalSteerRequestV1;

    await api.agentStructureProposalStatus(
      "project a",
      "document/1",
      controller.signal,
    );
    await api.startAgentStructureProposal("project a", "document/1", start);
    await api.reviseAgentStructureProposal("project a", "document/1", revise);
    await api.steerAgentStructureProposal("project a", "document/1", steer);
    await api.rejectAgentStructureProposal(
      "project a",
      "document/1",
      revise.proposal_record_id,
      revise.expected_proposal_payload_sha256,
      "reject-1",
    );
    await api.applyAgentStructureProposal(
      "project a",
      "document/1",
      revise.proposal_record_id,
      revise.expected_proposal_payload_sha256,
      "apply-1",
    );

    const root =
      "/api/projects/project%20a/sor-documents/document%2F1/agent-structure/proposals";
    expect(fetchSpy.mock.calls[0]).toEqual([
      `${root}/status`,
      expect.objectContaining({ signal: controller.signal }),
    ]);
    expect(fetchSpy.mock.calls[1]?.[0]).toBe(`${root}/start`);
    expect(fetchSpy.mock.calls[1]?.[1]).toEqual(
      expect.objectContaining({ method: "POST", body: JSON.stringify(start) }),
    );
    expect(fetchSpy.mock.calls[2]?.[0]).toBe(`${root}/revise`);
    expect(fetchSpy.mock.calls[2]?.[1]).toEqual(
      expect.objectContaining({ method: "POST", body: JSON.stringify(revise) }),
    );
    expect(fetchSpy.mock.calls[3]?.[0]).toBe(`${root}/steer`);
    expect(fetchSpy.mock.calls[3]?.[1]).toEqual(
      expect.objectContaining({ method: "POST", body: JSON.stringify(steer) }),
    );
    expect(fetchSpy.mock.calls[4]?.[0]).toBe(`${root}/reject`);
    expect(JSON.parse(String(fetchSpy.mock.calls[4]?.[1]?.body))).toEqual(
      expect.objectContaining({
        proposal_record_id: revise.proposal_record_id,
        expected_proposal_payload_sha256:
          revise.expected_proposal_payload_sha256,
        command_id: "reject-1",
      }),
    );
    expect(fetchSpy.mock.calls[5]?.[0]).toBe(`${root}/apply`);
    expect(JSON.parse(String(fetchSpy.mock.calls[5]?.[1]?.body))).toEqual(
      expect.objectContaining({
        proposal_record_id: revise.proposal_record_id,
        expected_proposal_payload_sha256:
          revise.expected_proposal_payload_sha256,
        command_id: "apply-1",
      }),
    );
  });
});

describe("native Structure reference bridge API", () => {
  it("reads a lightweight working head and immutable revision-bound support data", async () => {
    const fetchSpy = installFetchSpy();
    const controller = new AbortController();

    await api.nativeStructureWorkingRevisionHead(
      "project a",
      "document/1",
      controller.signal,
    );
    await api.nativeStructureWorkingRevision(
      "project a",
      "document/1",
      "run/1",
      3,
      "a".repeat(64),
      controller.signal,
    );
    await api.nativeStructureWorkingRevisionTablePreview(
      "project a",
      "document/1",
      "run/1",
      3,
      "table/scope",
      "a".repeat(64),
      controller.signal,
    );

    const root =
      "/api/projects/project%20a/sor-documents/document%2F1/agent-structure/native-reference";
    expect(fetchSpy.mock.calls[0]).toEqual([
      `${root}/working-revision-head`,
      expect.objectContaining({ signal: controller.signal }),
    ]);
    expect(fetchSpy.mock.calls[1]).toEqual([
      `${root}/working-revisions/run%2F1/3?expected_revision_sha256=${"a".repeat(64)}`,
      expect.objectContaining({ signal: controller.signal }),
    ]);
    expect(fetchSpy.mock.calls[2]).toEqual([
      `${root}/working-revisions/run%2F1/3/tables/table%2Fscope?expected_revision_sha256=${"a".repeat(64)}`,
      expect.objectContaining({ signal: controller.signal }),
    ]);
  });

  it("preserves Python number tokens in revision-bound structure payloads", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          '{"source_bounds":[0.0,595.0,842.0],"small":1e-07}',
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        ),
      ),
    );

    const revision = (await api.nativeStructureWorkingRevision(
      "project",
      "document",
      "agent-run-" + "1".repeat(32),
      3,
      "a".repeat(64),
    )) as unknown as { source_bounds: number[]; small: number };
    const preview = (await api.nativeStructureWorkingRevisionTablePreview(
      "project",
      "document",
      "agent-run-" + "1".repeat(32),
      3,
      "table-a",
      "a".repeat(64),
    )) as unknown as { source_bounds: number[]; small: number };

    for (const payload of [revision, preview]) {
      expect(payload).toEqual({ source_bounds: [0, 595, 842], small: 1e-7 });
      expect(jsonNumberSource(payload.source_bounds, "0")).toBe("0.0");
      expect(jsonNumberSource(payload.source_bounds, "1")).toBe("595.0");
      expect(jsonNumberSource(payload.source_bounds, "2")).toBe("842.0");
      expect(jsonNumberSource(payload, "small")).toBe("1e-07");
    }
  });

  it("reads the exact document candidate and submits one scope-bound instruction", async () => {
    const fetchSpy = installFetchSpy();
    const controller = new AbortController();
    const payload = {
      schema_version: "native-structure-feedback-request/v1",
      command_id: "native-feedback-1",
      expected_source_sha256: "a".repeat(64),
      expected_artifact_id: "pds-native-v1",
      scope_kind: "LOGICAL_TABLE",
      scope_id: "lt-front",
      message: "标题应保留在主表之外。",
    } satisfies NativeStructureFeedbackRequestV1;

    await api.nativeStructureReference(
      "project a",
      "document/1",
      controller.signal,
    );
    await api.nativeStructureTablePreview(
      "project a",
      "document/1",
      "table/scope",
      controller.signal,
    );
    await api.nativeStructureActivity(
      "project a",
      "document/1",
      7,
      controller.signal,
    );
    await api.submitNativeStructureFeedback("project a", "document/1", payload);
    const decision = {
      schema_version: "native-structure-proposal-decision-request/v1",
      command_id: "native-proposal-decision-1",
      expected_source_sha256: "a".repeat(64),
      expected_artifact_id: "pds-native-v1",
      proposal_id: "proposal-image-1",
      action: "UNDO",
    } satisfies NativeStructureProposalDecisionRequestV1;
    await api.submitNativeStructureProposalDecision(
      "project a",
      "document/1",
      decision,
    );
    const continuation = {
      schema_version: "native-structure-continue-request/v1",
      command_id: "native-continue-1",
      expected_source_sha256: "a".repeat(64),
      expected_artifact_id: "pds-native-v1",
    } satisfies NativeStructureContinueRequestV1;
    await api.continueNativeStructureReference(
      "project a",
      "document/1",
      continuation,
    );
    const renderedReopen = {
      schema_version: "native-structure-rendered-reopen-request/v1" as const,
      command_id: "rendered-reopen-1",
      expected_source_sha256: "a".repeat(64),
      expected_artifact_id: "pds-native-v1",
      handoff_id: "handoff-1",
      attempt_id: "native-structure-handoff-attempt-" + "1".repeat(32),
      scope_id: "scope-1",
      expected_projection_sha256: "c".repeat(64),
      expected_rendered_output_sha256: "b".repeat(64),
    };
    await api.acknowledgeNativeStructureRenderedReopen(
      "project a",
      "document/1",
      renderedReopen,
    );
    const baselineStart = {
      schema_version: "native-structure-baseline-start-request/v1" as const,
      command_id: "native-baseline-1",
      expected_source_sha256: "a".repeat(64),
      expected_artifact_id: "pds-native-v1",
    };
    await api.startNativeStructureBaseline(
      "project a",
      "document/1",
      baselineStart,
    );
    await api.nativeStructureBaselineStatus(
      "project a",
      "document/1",
      controller.signal,
    );
    await api.nativeParentProvisionStatus(
      "project a",
      "document/1",
      controller.signal,
    );
    await api.nativeParentProvisionResult(
      "project a",
      "document/1",
      controller.signal,
    );
    await api.sourceRequirementBaselineStatus(
      "project a",
      "document/1",
      controller.signal,
    );
    const sourceRequirementRun = {
      schema_version: "source-requirement-baseline-start-request/v1",
      command_id: "source-requirement-run-1",
      expected_source_sha256: "a".repeat(64),
      expected_artifact_id: "pds-native-v1",
      logical_table_scope_id: "table/scope",
    } satisfies SourceRequirementBaselineRunStartRequestV1;
    await api.startSourceRequirementBaselineRun(
      "project a",
      "document/1",
      sourceRequirementRun,
    );
    await api.sourceRequirementBaselineRunStatus(
      "project a",
      "document/1",
      controller.signal,
    );
    await api.sourceRequirementBaselineOutline(
      "project a",
      "document/1",
      controller.signal,
    );
    await api.sourceRequirementRecord(
      "project a",
      "document/1",
      "source-record-" + "c".repeat(32),
      controller.signal,
    );
    const sourceRequirementFeedback = {
      schema_version: "source-requirement-feedback-request/v1",
      command_id: "source-feedback-1",
      expected_source_sha256: "a".repeat(64),
      expected_document_map_artifact_sha256: "b".repeat(64),
      expected_baseline_artifact_id:
        "source-requirement-baseline-" + "d".repeat(32),
      source_record_id: "source-record-" + "c".repeat(32),
      message: "保持完整业务记录。",
    } satisfies SourceRequirementFeedbackRequestV1;
    await api.submitSourceRequirementFeedback(
      "project a",
      "document/1",
      sourceRequirementFeedback,
    );

    const root =
      "/api/projects/project%20a/sor-documents/document%2F1/agent-structure/native-reference";
    expect(fetchSpy.mock.calls[0]).toEqual([
      root,
      expect.objectContaining({ signal: controller.signal }),
    ]);
    expect(fetchSpy.mock.calls[1]).toEqual([
      `${root}/tables/table%2Fscope`,
      expect.objectContaining({ signal: controller.signal }),
    ]);
    expect(fetchSpy.mock.calls[2]).toEqual([
      `${root}/activity?after_sequence=7`,
      expect.objectContaining({ signal: controller.signal }),
    ]);
    expect(fetchSpy.mock.calls[3]?.[0]).toBe(`${root}/feedback`);
    expect(fetchSpy.mock.calls[3]?.[1]).toEqual(
      expect.objectContaining({ method: "POST", body: JSON.stringify(payload) }),
    );
    expect(fetchSpy.mock.calls[4]?.[0]).toBe(`${root}/proposal-decisions`);
    expect(fetchSpy.mock.calls[4]?.[1]).toEqual(
      expect.objectContaining({ method: "POST", body: JSON.stringify(decision) }),
    );
    expect(fetchSpy.mock.calls[5]?.[0]).toBe(`${root}/continue`);
    expect(fetchSpy.mock.calls[5]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(continuation),
      }),
    );
    expect(fetchSpy.mock.calls[6]?.[0]).toBe(`${root}/rendered-reopen`);
    expect(fetchSpy.mock.calls[6]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(renderedReopen),
      }),
    );
    expect(fetchSpy.mock.calls[7]?.[0]).toBe(`${root}/advance`);
    expect(fetchSpy.mock.calls[7]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(baselineStart),
      }),
    );
    expect(fetchSpy.mock.calls[8]).toEqual([
      `${root}/advance-status`,
      expect.objectContaining({ signal: controller.signal }),
    ]);
    expect(fetchSpy.mock.calls[9]).toEqual([
      `${root}/parent-provision-status`,
      expect.objectContaining({ signal: controller.signal }),
    ]);
    expect(fetchSpy.mock.calls[10]).toEqual([
      `${root}/parent-provisions`,
      expect.objectContaining({ signal: controller.signal }),
    ]);
    const baselineRoot =
      "/api/projects/project%20a/sor-documents/document%2F1/agent-structure/requirement-baseline";
    expect(fetchSpy.mock.calls[11]).toEqual([
      `${baselineRoot}/status`,
      expect.objectContaining({ signal: controller.signal }),
    ]);
    expect(fetchSpy.mock.calls[12]?.[0]).toBe(`${baselineRoot}/runs/start`);
    expect(fetchSpy.mock.calls[12]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(sourceRequirementRun),
      }),
    );
    expect(fetchSpy.mock.calls[13]).toEqual([
      `${baselineRoot}/runs/status`,
      expect.objectContaining({ signal: controller.signal }),
    ]);
    expect(fetchSpy.mock.calls[14]).toEqual([
      `${baselineRoot}/outline`,
      expect.objectContaining({ signal: controller.signal }),
    ]);
    expect(fetchSpy.mock.calls[15]).toEqual([
      `${baselineRoot}/records/source-record-${"c".repeat(32)}`,
      expect.objectContaining({ signal: controller.signal }),
    ]);
    expect(fetchSpy.mock.calls[16]?.[0]).toBe(`${baselineRoot}/feedback`);
    expect(fetchSpy.mock.calls[16]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(sourceRequirementFeedback),
      }),
    );
  });
});

describe("requirement-set API propagation", () => {
  it("preserves Python JSON number tokens for later response hash verification", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response('{"ratio":1.0,"small":1e-07}', {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );

    const payload = (await api.previewRequirementSemanticPublication(
      "project",
      "document",
      "session",
      {} as RequirementSemanticPublicationPreviewRequestV2,
    )) as unknown as Record<string, unknown>;

    expect(payload).toEqual({ ratio: 1, small: 1e-7 });
    expect(jsonNumberSource(payload, "ratio")).toBe("1.0");
    expect(jsonNumberSource(payload, "small")).toBe("1e-07");
  });

  it("sends the semantic activation CAS body and abort signal unchanged", async () => {
    const fetchSpy = installFetchSpy();
    const payload = {
      schema_version: "requirement-set-semantic-activation-request/v1",
      expected_active_requirement_set_id: "set-before",
      expected_enrichment_publication_binding_id:
        "requirement-enrichment-binding-" + "7".repeat(32),
      expected_enrichment_publication_binding_sha256: "8".repeat(64),
    } as const;
    const controller = new AbortController();

    await api.activateRequirementSet(
      "project a",
      "document/1",
      "set after",
      payload,
      controller.signal,
    );

    expect(fetchSpy.mock.calls[0]).toEqual([
      "/api/projects/project%20a/sor-documents/document%2F1/requirement-sets/set%20after/activate",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(payload),
        signal: controller.signal,
      }),
    ]);
  });

  it("loads the workspace and geometry from the explicitly selected version", async () => {
    const fetchSpy = installFetchSpy();

    await api.workspace("project a", "document/1", false, "set 7");
    await api.geometryFocus("project a", "document/1", "node/2", "set 7");

    expect(fetchSpy.mock.calls[0]?.[0]).toBe(
      "/api/projects/project%20a/sor-documents/document%2F1/workspace?requirement_set_id=set+7",
    );
    expect(fetchSpy.mock.calls[1]?.[0]).toBe(
      "/api/projects/project%20a/sor-documents/document%2F1/extraction/geometry-focus/node%2F2?requirement_set_id=set%207",
    );
  });

  it("pins locking, AI analysis and human decisions to one requirement set", async () => {
    const fetchSpy = installFetchSpy();

    await api.lockReadiness("p", "d", "set-2");
    await api.lockExtraction("p", "d", "set-2", "readiness-7");
    const scopeRequest = {
      schema_version: "requirement-analysis-scope-request/v1" as const,
      command_id: "command-1",
      requirement_set_id: "set-2",
      expected_tree_sha256: "a".repeat(64),
      scope: "SELECTED" as const,
      node_ids: ["node-1"],
    };
    await api.previewAiScope("p", "d", scopeRequest);
    await api.startAi("p", "d", {
      ...scopeRequest,
      expected_scope_sha256: "b".repeat(64),
      profile_id: "profile-1",
    });
    await api.updateRequirementMatrixDecision(
      "p",
      "d",
      "set-2",
      "node-1",
      {
        schema_version: "requirement-matrix-decision-request/v1",
        final_decision: "ACCEPT",
        engineer_reason: "verified",
        expected_revision: 3,
      },
    );

    expect(fetchSpy.mock.calls[0]?.[0]).toBe(
      "/api/projects/p/sor-documents/d/lock-readiness?requirement_set_id=set-2",
    );
    expect(fetchSpy.mock.calls[0]?.[1]?.method).toBeUndefined();
    expect(JSON.parse(String(fetchSpy.mock.calls[1]?.[1]?.body))).toEqual({
      schema_version: "requirement-set-lock-request/v1",
      requirement_set_id: "set-2",
      expected_readiness_sha256: "readiness-7",
      waiver_reason: "",
    });
    expect(fetchSpy.mock.calls[2]?.[0]).toBe(
      "/api/projects/p/sor-documents/d/ai-analysis/scope-preview",
    );
    expect(JSON.parse(String(fetchSpy.mock.calls[2]?.[1]?.body))).toEqual(
      scopeRequest,
    );
    expect(JSON.parse(String(fetchSpy.mock.calls[3]?.[1]?.body))).toEqual({
      ...scopeRequest,
      expected_scope_sha256: "b".repeat(64),
      profile_id: "profile-1",
    });
    expect(JSON.parse(String(fetchSpy.mock.calls[4]?.[1]?.body))).toEqual({
      schema_version: "requirement-matrix-decision-request/v1",
      final_decision: "ACCEPT",
      engineer_reason: "verified",
      expected_revision: 3,
    });
    expect(fetchSpy.mock.calls[4]?.[0]).toBe(
      "/api/projects/p/sor-documents/d/requirement-sets/set-2/matrix/nodes/node-1/decision",
    );
  });

  it("keeps the confirmed legacy all-analysis request for servers without scoped capability", async () => {
    const fetchSpy = installFetchSpy();

    await api.startAiLegacy("project a", "document/1", "set 7", "profile-1");

    expect(fetchSpy.mock.calls[0]?.[0]).toBe(
      "/api/projects/project%20a/sor-documents/document%2F1/run-ai-analysis/start",
    );
    expect(JSON.parse(String(fetchSpy.mock.calls[0]?.[1]?.body))).toEqual({
      requirement_set_id: "set 7",
      profile_id: "profile-1",
    });
  });

  it("fences an AI stop request to the job the user confirmed", async () => {
    const fetchSpy = installFetchSpy();

    await api.stopAi("project a", "document/1", "job-9");

    expect(fetchSpy.mock.calls[0]?.[0]).toBe(
      "/api/projects/project%20a/sor-documents/document%2F1/run-ai-analysis/stop",
    );
    expect(JSON.parse(String(fetchSpy.mock.calls[0]?.[1]?.body))).toEqual({
      expected_job_id: "job-9",
    });
  });

  it("encodes the selected confirmation list in the lock-readiness query", async () => {
    const fetchSpy = installFetchSpy();

    await api.lockReadiness("project a", "document/1", "set 7");

    expect(fetchSpy.mock.calls[0]?.[0]).toBe(
      "/api/projects/project%20a/sor-documents/document%2F1/lock-readiness?requirement_set_id=set%207",
    );
    expect(fetchSpy.mock.calls[0]?.[1]?.method).toBeUndefined();
  });

  it("loads node detail from the version-bound Matrix detail endpoint", async () => {
    const fetchSpy = installFetchSpy();

    await api.requirementMatrixNode("project a", "document/1", "set 7", "node/2");

    expect(fetchSpy.mock.calls[0]?.[0]).toBe(
      "/api/projects/project%20a/sor-documents/document%2F1/requirement-sets/set%207/matrix/nodes/node%2F2/detail",
    );
  });

  it("derives an editable confirmation draft from one locked requirement set", async () => {
    const fetchSpy = installFetchSpy();

    await api.deriveRequirementSet("project a", "document/1", "set 7");

    expect(fetchSpy.mock.calls[0]?.[0]).toBe(
      "/api/projects/project%20a/sor-documents/document%2F1/requirement-sets/set%207/derive",
    );
    expect(fetchSpy.mock.calls[0]?.[1]).toEqual(
      expect.objectContaining({ method: "POST", body: "{}" }),
    );
  });
});

describe("parsing diagnostics API", () => {
  it("reads the current risk summary and writes only an identity-bound acknowledgement", async () => {
    const fetchSpy = installFetchSpy();
    const payload = {
      expected_source_sha256: "a".repeat(64),
      expected_parse_artifact_id: "b".repeat(64),
      expected_parse_identity_sha256: "c".repeat(64),
    };

    await api.parsingDiagnostics("project a", "document/1");
    await api.acknowledgeParsingRisks("project a", "document/1", payload);

    expect(fetchSpy.mock.calls[0]?.[0]).toBe(
      "/api/projects/project%20a/sor-documents/document%2F1/extraction/parse-risks",
    );
    expect(fetchSpy.mock.calls[0]?.[1]?.method).toBeUndefined();
    expect(fetchSpy.mock.calls[1]?.[0]).toBe(
      "/api/projects/project%20a/sor-documents/document%2F1/extraction/parse-risks/acknowledgement",
    );
    expect(fetchSpy.mock.calls[1]?.[1]).toEqual(
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify(payload),
      }),
    );
  });
});

describe("document extraction repair API", () => {
  it("submits only the exact base identities and never client-selected pages", async () => {
    const fetchSpy = installFetchSpy();
    const payload = {
      schema_version: "document-extraction-visual-repair-request/v1",
      expected_base_job_id: "job-terminalvisual",
      expected_base_job_generation: 2,
      expected_base_artifact_sha256: "a".repeat(64),
    } satisfies DocumentExtractionVisualRepairRequestV1;

    await api.repairExtractionVisualPages(
      "project a",
      "document/1",
      payload,
    );

    expect(fetchSpy.mock.calls[0]?.[0]).toBe(
      "/api/projects/project%20a/sor-documents/document%2F1/extraction/visual-repair",
    );
    expect(fetchSpy.mock.calls[0]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(payload),
      }),
    );
    expect(
      JSON.parse(String(fetchSpy.mock.calls[0]?.[1]?.body)),
    ).not.toHaveProperty("page_numbers");
  });
});

describe("entry classification API", () => {
  const command = {
    command_id: "classification-command-1",
    expected_revision: 4,
    expected_snapshot_sha256: "a".repeat(64),
    expected_source_sha256: "b".repeat(64),
    expected_working_tree_sha256: "c".repeat(64),
    expected_effective_structure_id: "structure-7",
    expected_policy_version: "entry-classification-policy/v1",
  } satisfies EntryClassificationCommand;

  it("reads, updates, and accepts the exact document-bound classification snapshot", async () => {
    const fetchSpy = installFetchSpy();
    const acceptanceController = new AbortController();
    const patchCommand = {
      ...command,
      overrides: {
        "workflow/1": "TECHNICAL",
        "workflow/2": null,
      },
    } satisfies EntryClassificationPatchCommand;

    await api.entryClassifications("project a", "document/1");
    await api.updateEntryClassifications(
      "project a",
      "document/1",
      patchCommand,
    );
    await api.acceptEntryClassifications(
      "project a",
      "document/1",
      command,
      acceptanceController.signal,
    );

    const endpoint =
      "/api/projects/project%20a/sor-documents/document%2F1/extraction/entry-classifications";
    expect(fetchSpy.mock.calls[0]?.[0]).toBe(endpoint);
    expect(fetchSpy.mock.calls[0]?.[1]?.method).toBeUndefined();
    expect(fetchSpy.mock.calls[1]?.[0]).toBe(endpoint);
    expect(fetchSpy.mock.calls[1]?.[1]).toEqual(
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify(patchCommand),
      }),
    );
    expect(fetchSpy.mock.calls[2]?.[0]).toBe(`${endpoint}/acceptance`);
    expect(fetchSpy.mock.calls[2]?.[1]).toEqual(
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify(command),
        signal: acceptanceController.signal,
      }),
    );
  });
});

describe("explicit atomization scope API", () => {
  it("reads the exact semantic-session enrichment state without a write", async () => {
    const fetchSpy = installFetchSpy();
    const controller = new AbortController();

    await api.requirementEnrichment(
      "project a",
      "document/1",
      "session 7",
      controller.signal,
    );

    expect(fetchSpy.mock.calls[0]?.[0]).toBe(
      "/api/projects/project%20a/sor-documents/document%2F1/extraction/llm-reviews/session%207/enrichment",
    );
    expect(fetchSpy.mock.calls[0]?.[1]).toEqual(
      expect.objectContaining({ signal: controller.signal }),
    );
    expect(fetchSpy.mock.calls[0]?.[1]?.method).toBeUndefined();
  });

  it("posts the plan-aware v2 command only to the bound session run endpoint", async () => {
    const fetchSpy = installFetchSpy();
    const command = {
      schema_version: "requirement-atomization-run-request/v2",
      command_id: "scope-command-1",
      operation: "APPLY",
      scope: "SELECTED",
      expected_review_revision: 7,
      expected_scope_revision: 3,
      expected_entry_classification_snapshot_id: "classification-4",
      expected_entry_classification_revision: 4,
      expected_entry_classification_sha256: "a".repeat(64),
      expected_working_tree_id: "working-tree-2",
      expected_working_tree_sha256: "b".repeat(64),
      expected_effective_structure_id: "effective-2",
      anchor_tree_node_id: null,
      target_review_node_ids: ["review/2", "review/9"],
      target_scope_sha256: "c".repeat(64),
      unselected_policy: "SOURCE_BOUND_PASSTHROUGH",
    } satisfies RequirementAtomizationScopeCommand;
    const controller = new AbortController();

    await api.runRequirementAtomizationScope(
      "project a",
      "document/1",
      "session 7",
      command,
      controller.signal,
    );

    expect(fetchSpy.mock.calls[0]?.[0]).toBe(
      "/api/projects/project%20a/sor-documents/document%2F1/extraction/llm-reviews/session%207/atomization-runs",
    );
    expect(fetchSpy.mock.calls[0]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(command),
        signal: controller.signal,
      }),
    );
  });

  it("posts semantic publication preview and publish v2 to the exact session endpoints with abort signals", async () => {
    const fetchSpy = installFetchSpy();
    const preview: RequirementSemanticPublicationPreviewRequestV2 = {
      schema_version: "requirement-enrichment-publication-preview-request/v2",
      expected_review_revision: 7,
      accepted_entry_classification_snapshot_id: "accepted-7",
      accepted_entry_classification_revision: 3,
      accepted_entry_classification_sha256: "a".repeat(64),
      atomization_state: "NOT_RUN",
    };
    const publish: RequirementSemanticPublishDraftRequestV2 = {
      schema_version: "requirement-enrichment-publish-draft-request/v2",
      expected_review_revision: 7,
      binding_preview_id:
        "requirement-enrichment-binding-preview-" + "b".repeat(32),
      binding_preview_sha256: "c".repeat(64),
    };
    const previewController = new AbortController();
    const publishController = new AbortController();

    await api.previewRequirementSemanticPublication(
      "project a",
      "document/1",
      "session 7",
      preview,
      previewController.signal,
    );
    await api.publishRequirementSemanticDraft(
      "project a",
      "document/1",
      "session 7",
      publish,
      publishController.signal,
    );

    expect(fetchSpy.mock.calls[0]).toEqual([
      "/api/projects/project%20a/sor-documents/document%2F1/extraction/llm-reviews/session%207/publication-preview",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(preview),
        signal: previewController.signal,
      }),
    ]);
    expect(fetchSpy.mock.calls[1]).toEqual([
      "/api/projects/project%20a/sor-documents/document%2F1/extraction/llm-reviews/session%207/publish-draft",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(publish),
        signal: publishController.signal,
      }),
    ]);
  });
});
