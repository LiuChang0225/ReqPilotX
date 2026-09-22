import type {
  DocumentProcessingProfile,
  KnowledgeBackend,
  LlmProvider,
  ProjectProcessingProfile,
  RunProfileOverride,
  RuntimeCapability,
} from "../api/contracts";

export const CAPABILITY_IDS = {
  pymupdf: "document.pymupdf",
  codexAgent: "codex.agent",
  azureDi: "document.azure_document_intelligence",
  bailianOcr: "document.bailian_ocr",
  bailianLayout: "document.bailian_layout",
  azureSol: "azure_openai.gpt56_sol",
  azureTerra: "azure_openai.gpt56_terra",
  aliyunLlm: "aliyun.llm",
  bailianVision: "aliyun.vision",
  azureEmbedding: "azure_openai.embedding",
  azureSearch: "azure_ai_search",
  aliyunEmbedding: "aliyun.embedding",
  qdrant: "qdrant",
} as const;

export interface CapabilityGate {
  ready: boolean;
  blockedBy: RuntimeCapability[];
  message: string;
}

export function capability(
  capabilities: RuntimeCapability[] | null | undefined,
  capabilityId: string,
): RuntimeCapability | null {
  return (
    capabilities?.find((item) => item.capability_id === capabilityId) || null
  );
}

export function capabilityReady(
  capabilities: RuntimeCapability[] | null | undefined,
  capabilityId: string,
): boolean {
  return capability(capabilities, capabilityId)?.state === "READY";
}

export function gateCapabilities(
  capabilities: RuntimeCapability[] | null | undefined,
  capabilityIds: string[],
): CapabilityGate {
  const blockedBy = capabilityIds.flatMap((id) => {
    const item = capability(capabilities, id);
    if (item?.state === "READY") return [];
    return [
      item || {
          capability_id: id,
          category: "llm" as const,
          provider: "unknown",
          label: id,
          state: "CHECKING" as const,
          ready: false,
          reason_code: "capability_not_loaded",
          message: "能力状态尚未加载。",
        },
    ];
  });
  return {
    ready: blockedBy.length === 0,
    blockedBy,
    message: blockedBy
      .map(
        (item) =>
          item.message ||
          `${item.label}：${item.reason_code || item.state}`,
      )
      .join("；"),
  };
}

export function llmCapabilityIds(provider: LlmProvider): string[] {
  if (provider === "codex") return [CAPABILITY_IDS.codexAgent];
  if (provider === "azure_openai") {
    return [CAPABILITY_IDS.azureSol, CAPABILITY_IDS.azureTerra];
  }
  if (provider === "aliyun") return [CAPABILITY_IDS.aliyunLlm];
  return [];
}

export function knowledgeCapabilityIds(
  backend: KnowledgeBackend,
): string[] {
  if (backend === "azure_ai_search") {
    return [CAPABILITY_IDS.azureEmbedding, CAPABILITY_IDS.azureSearch];
  }
  if (backend === "aliyun_qdrant") {
    return [CAPABILITY_IDS.aliyunEmbedding, CAPABILITY_IDS.qdrant];
  }
  return [];
}

export function knowledgeGate(
  profile: ProjectProcessingProfile | null | undefined,
  capabilities: RuntimeCapability[] | null | undefined,
): CapabilityGate {
  if (!profile || profile.knowledge_backend === "none") {
    return {
      ready: false,
      blockedBy: [],
      message: "项目尚未选择知识检索后端。",
    };
  }
  return gateCapabilities(
    capabilities,
    knowledgeCapabilityIds(profile.knowledge_backend),
  );
}

export function documentCapabilityIds(
  document: DocumentProcessingProfile,
): string[] {
  const ids: string[] = [
    document.primary_parser === "pymupdf"
      ? CAPABILITY_IDS.pymupdf
      : CAPABILITY_IDS.azureDi,
  ];
  for (const stage of document.enhancement_chain) {
    ids.push(
      {
        azure_document_intelligence: CAPABILITY_IDS.azureDi,
        bailian_ocr: CAPABILITY_IDS.bailianOcr,
        bailian_layout: CAPABILITY_IDS.bailianLayout,
      }[stage],
    );
  }
  if (document.complex_vision === "azure_gpt56_sol") {
    ids.push(CAPABILITY_IDS.azureSol);
  }
  if (document.complex_vision === "bailian_vision") {
    ids.push(CAPABILITY_IDS.bailianVision);
  }
  return [...new Set(ids)];
}

export function profileGate(
  document: DocumentProcessingProfile,
  llmProvider: LlmProvider,
  capabilities: RuntimeCapability[] | null | undefined,
): CapabilityGate {
  return gateCapabilities(
    capabilities,
    [
      ...new Set([
        ...documentCapabilityIds(document),
        ...llmCapabilityIds(llmProvider),
      ]),
    ],
  );
}

export function buildForcedAzureSolPageOverride(
  profile: ProjectProcessingProfile,
  page: number,
): RunProfileOverride {
  if (!Number.isInteger(page) || page < 1) {
    throw new RangeError("Forced visual page must be a one-based integer.");
  }
  return {
    schema_version: "run-profile-override/v1",
    document: {
      ...profile.document,
      enhancement_chain: [...profile.document.enhancement_chain],
      complex_vision: "azure_gpt56_sol",
    },
    llm_provider: profile.llm_provider,
    force_visual_pages: [page],
  };
}

export function buildVisualRestorationOverride(
  profile: ProjectProcessingProfile | null | undefined,
  capabilities: RuntimeCapability[] | null | undefined,
): RunProfileOverride | null {
  if (!profile) return null;
  const pymupdfReady = capabilityReady(capabilities, CAPABILITY_IDS.pymupdf);
  const bailianOcrReady = capabilityReady(
    capabilities,
    CAPABILITY_IDS.bailianOcr,
  );
  const bailianLayoutReady = capabilityReady(
    capabilities,
    CAPABILITY_IDS.bailianLayout,
  );
  const azureDiReady = capabilityReady(capabilities, CAPABILITY_IDS.azureDi);
  const semanticProviderReady = llmCapabilityIds(profile.llm_provider).every(
    (capabilityId) => capabilityReady(capabilities, capabilityId),
  );

  if (!semanticProviderReady) return null;

  if (pymupdfReady && (bailianOcrReady || bailianLayoutReady)) {
    return {
      schema_version: "run-profile-override/v1",
      document: {
        primary_parser: "pymupdf",
        enhancement_chain: [
          bailianLayoutReady ? "bailian_layout" : "bailian_ocr",
        ],
        complex_vision: null,
        auto_escalation: false,
      },
      // OCR/Layout restores source evidence. Semantic interpretation remains
      // owned by the project's Agent profile instead of silently falling back
      // to local rules.
      llm_provider: profile.llm_provider,
      force_visual_pages: [],
    };
  }
  if (azureDiReady) {
    return {
      schema_version: "run-profile-override/v1",
      document: {
        primary_parser: "azure_document_intelligence",
        enhancement_chain: [],
        complex_vision: null,
        auto_escalation: false,
      },
      llm_provider: profile.llm_provider,
      force_visual_pages: [],
    };
  }
  return null;
}
