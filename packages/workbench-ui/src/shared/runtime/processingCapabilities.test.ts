import { describe, expect, it } from "vitest";
import type {
  ProjectProcessingProfile,
  RuntimeCapability,
} from "../api/contracts";
import {
  CAPABILITY_IDS,
  buildForcedAzureSolPageOverride,
  buildVisualRestorationOverride,
  documentCapabilityIds,
  knowledgeGate,
  profileGate,
} from "./processingCapabilities";

function runtimeCapability(
  capabilityId: string,
  state: RuntimeCapability["state"] = "READY",
): RuntimeCapability {
  return {
    capability_id: capabilityId,
    category: "llm",
    provider: "test",
    label: capabilityId,
    state,
    ready: state === "READY",
    reason_code: state === "READY" ? "" : "not_configured",
    message: state === "READY" ? "" : `${capabilityId} 尚未配置`,
  };
}

function profile(
  patch: Partial<ProjectProcessingProfile> = {},
): ProjectProcessingProfile {
  return {
    schema_version: "project-processing-profile/v1",
    project_id: "project-1",
    document: {
      primary_parser: "pymupdf",
      enhancement_chain: [],
      complex_vision: null,
      auto_escalation: true,
    },
    llm_provider: "codex",
    knowledge_backend: "none",
    revision: 0,
    canonical_sha256: "a".repeat(64),
    ...patch,
  };
}

describe("processing capability gates", () => {
  it("keeps local-rules runnable with only PyMuPDF ready", () => {
    const gate = profileGate(
      profile().document,
      "none",
      [runtimeCapability(CAPABILITY_IDS.pymupdf)],
    );
    expect(gate.ready).toBe(true);
    expect(gate.blockedBy).toEqual([]);
  });

  it("requires the fixed Codex Agent capability for automatic extraction", () => {
    const blocked = profileGate(profile().document, "codex", [
      runtimeCapability(CAPABILITY_IDS.pymupdf),
      runtimeCapability(CAPABILITY_IDS.codexAgent, "AUTH_FAILED"),
    ]);
    const ready = profileGate(profile().document, "codex", [
      runtimeCapability(CAPABILITY_IDS.pymupdf),
      runtimeCapability(CAPABILITY_IDS.codexAgent),
    ]);

    expect(blocked.ready).toBe(false);
    expect(blocked.blockedBy[0].capability_id).toBe(CAPABILITY_IDS.codexAgent);
    expect(ready.ready).toBe(true);
  });

  it("keeps Azure choices visible but blocked with component reasons", () => {
    const capabilities = [
      runtimeCapability(CAPABILITY_IDS.pymupdf),
      runtimeCapability(CAPABILITY_IDS.azureDi, "NOT_CONFIGURED"),
      runtimeCapability(CAPABILITY_IDS.azureSol, "MODEL_NOT_DEPLOYED"),
      runtimeCapability(CAPABILITY_IDS.azureTerra, "AUTH_FAILED"),
    ];
    const gate = profileGate(
      {
        primary_parser: "pymupdf",
        enhancement_chain: ["azure_document_intelligence"],
        complex_vision: "azure_gpt56_sol",
        auto_escalation: true,
      },
      "azure_openai",
      capabilities,
    );
    expect(gate.ready).toBe(false);
    expect(gate.blockedBy.map((item) => item.capability_id)).toEqual([
      CAPABILITY_IDS.azureDi,
      CAPABILITY_IDS.azureSol,
      CAPABILITY_IDS.azureTerra,
    ]);
    expect(gate.message).toContain("尚未配置");
  });

  it("requires both embedding and the selected vector store for knowledge", () => {
    const selected = profile({ knowledge_backend: "aliyun_qdrant" });
    const blocked = knowledgeGate(selected, [
      runtimeCapability(CAPABILITY_IDS.aliyunEmbedding),
      runtimeCapability(CAPABILITY_IDS.qdrant, "UNREACHABLE"),
    ]);
    expect(blocked.ready).toBe(false);
    expect(blocked.blockedBy[0].capability_id).toBe(CAPABILITY_IDS.qdrant);

    const ready = knowledgeGate(selected, [
      runtimeCapability(CAPABILITY_IDS.aliyunEmbedding),
      runtimeCapability(CAPABILITY_IDS.qdrant),
    ]);
    expect(ready.ready).toBe(true);
  });

  it("supports the PyMuPDF + Azure DI + Sol escalation combination", () => {
    expect(
      documentCapabilityIds({
        primary_parser: "pymupdf",
        enhancement_chain: ["azure_document_intelligence"],
        complex_vision: "azure_gpt56_sol",
        auto_escalation: true,
      }),
    ).toEqual([
      CAPABILITY_IDS.pymupdf,
      CAPABILITY_IDS.azureDi,
      CAPABILITY_IDS.azureSol,
    ]);
  });

  it("builds a one-page Sol rerun without changing the project profile", () => {
    const current = profile();
    const override = buildForcedAzureSolPageOverride(current, 7);
    expect(override.force_visual_pages).toEqual([7]);
    expect(override.document?.complex_vision).toBe("azure_gpt56_sol");
    expect(current.document.complex_vision).toBeNull();
    expect(() => buildForcedAzureSolPageOverride(current, 0)).toThrow(
      RangeError,
    );
  });

  it("prefers validated page layout restoration while preserving the Agent profile", () => {
    const current = profile({
      llm_provider: "aliyun",
    });
    const override = buildVisualRestorationOverride(current, [
      runtimeCapability(CAPABILITY_IDS.pymupdf),
      runtimeCapability(CAPABILITY_IDS.bailianOcr),
      runtimeCapability(CAPABILITY_IDS.bailianLayout),
      runtimeCapability(CAPABILITY_IDS.aliyunLlm),
    ]);

    expect(override?.document).toEqual({
      primary_parser: "pymupdf",
      enhancement_chain: ["bailian_layout"],
      complex_vision: null,
      auto_escalation: false,
    });
    expect(override?.llm_provider).toBe("aliyun");
    expect(current.llm_provider).toBe("aliyun");
  });

  it("uses Qwen OCR structure restoration only when layout is unavailable", () => {
    const override = buildVisualRestorationOverride(profile(), [
      runtimeCapability(CAPABILITY_IDS.pymupdf),
      runtimeCapability(CAPABILITY_IDS.codexAgent),
      runtimeCapability(CAPABILITY_IDS.bailianOcr),
      runtimeCapability(CAPABILITY_IDS.bailianLayout, "UNREACHABLE"),
    ]);

    expect(override?.document).toEqual({
      primary_parser: "pymupdf",
      enhancement_chain: ["bailian_ocr"],
      complex_vision: null,
      auto_escalation: false,
    });
    expect(override?.llm_provider).toBe("codex");
  });

  it("does not repeat an unusable local-only run when no restorer is ready", () => {
    expect(
      buildVisualRestorationOverride(profile(), [
        runtimeCapability(CAPABILITY_IDS.pymupdf),
        runtimeCapability(CAPABILITY_IDS.codexAgent),
        runtimeCapability(CAPABILITY_IDS.bailianOcr, "NOT_CONFIGURED"),
        runtimeCapability(CAPABILITY_IDS.bailianLayout, "NOT_CONFIGURED"),
        runtimeCapability(CAPABILITY_IDS.azureDi, "NOT_CONFIGURED"),
      ]),
    ).toBeNull();
  });

  it("does not restore pages while silently dropping an unavailable Codex baseline", () => {
    expect(
      buildVisualRestorationOverride(profile(), [
        runtimeCapability(CAPABILITY_IDS.pymupdf),
        runtimeCapability(CAPABILITY_IDS.codexAgent, "AUTH_FAILED"),
        runtimeCapability(CAPABILITY_IDS.bailianOcr),
      ]),
    ).toBeNull();
  });
});
