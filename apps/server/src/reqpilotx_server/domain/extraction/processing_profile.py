from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


PROJECT_PROCESSING_PROFILE_SCHEMA_VERSION = "project-processing-profile/v1"
RESOLVED_PROCESSING_PLAN_SCHEMA_VERSION = "resolved-processing-plan/v1"
RUNTIME_CAPABILITY_SCHEMA_VERSION = "runtime-capability/v1"
RUN_PROFILE_OVERRIDE_SCHEMA_VERSION = "run-profile-override/v1"

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_PLAN_ID_PATTERN = r"^processing-plan-[0-9a-f]{32}$"

DocumentPrimaryParser = Literal["pymupdf", "azure_document_intelligence"]
DocumentEnhancer = Literal[
    "azure_document_intelligence",
    "bailian_ocr",
    "bailian_layout",
]
ComplexVisionProvider = Literal[
    "azure_gpt56_sol",
    "bailian_vision",
]
LlmProvider = Literal["none", "codex", "aliyun", "azure_openai"]
KnowledgeBackend = Literal["none", "aliyun_qdrant", "azure_ai_search"]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class CapabilityState(str, Enum):
    READY = "READY"
    CHECKING = "CHECKING"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTH_FAILED = "AUTH_FAILED"
    MODEL_NOT_DEPLOYED = "MODEL_NOT_DEPLOYED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    UNREACHABLE = "UNREACHABLE"


class DocumentProcessingProfileV1(_StrictFrozenModel):
    primary_parser: DocumentPrimaryParser = "pymupdf"
    enhancement_chain: tuple[DocumentEnhancer, ...] = ()
    complex_vision: ComplexVisionProvider | None = None
    auto_escalation: StrictBool = True

    @field_validator("enhancement_chain")
    @classmethod
    def validate_enhancement_chain(
        cls,
        value: tuple[DocumentEnhancer, ...],
    ) -> tuple[DocumentEnhancer, ...]:
        if len(value) > 2:
            raise ValueError("document enhancement_chain supports at most two stages")
        if len(value) != len(set(value)):
            raise ValueError("document enhancement_chain must not contain duplicates")
        return value

    @model_validator(mode="after")
    def reject_redundant_primary_stage(self) -> DocumentProcessingProfileV1:
        if (
            self.primary_parser == "azure_document_intelligence"
            and "azure_document_intelligence" in self.enhancement_chain
        ):
            raise ValueError(
                "azure_document_intelligence cannot be both the primary parser "
                "and an enhancement stage"
            )
        if {"bailian_ocr", "bailian_layout"}.issubset(self.enhancement_chain):
            raise ValueError(
                "bailian_ocr and bailian_layout are mutually exclusive "
                "enhancement stages"
            )
        if (
            self.primary_parser == "azure_document_intelligence"
            and "bailian_ocr" in self.enhancement_chain
        ):
            raise ValueError(
                "bailian_ocr structure restoration requires pymupdf as the "
                "primary parser"
            )
        return self


class ProjectProcessingProfileV1(_StrictFrozenModel):
    schema_version: Literal["project-processing-profile/v1"] = (
        PROJECT_PROCESSING_PROFILE_SCHEMA_VERSION
    )
    project_id: StrictStr = Field(min_length=1, max_length=160)
    document: DocumentProcessingProfileV1
    llm_provider: LlmProvider = "none"
    knowledge_backend: KnowledgeBackend = "none"
    revision: StrictInt = Field(ge=0)
    canonical_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_canonical_hash(self) -> ProjectProcessingProfileV1:
        expected = compute_project_processing_profile_sha256(
            project_id=self.project_id,
            document=self.document,
            llm_provider=self.llm_provider,
            knowledge_backend=self.knowledge_backend,
            revision=self.revision,
        )
        if self.canonical_sha256 != expected:
            raise ValueError("project processing profile canonical hash mismatch")
        return self


class ProjectProcessingProfileUpdateV1(_StrictFrozenModel):
    schema_version: Literal["project-processing-profile-update/v1"] = (
        "project-processing-profile-update/v1"
    )
    expected_revision: StrictInt = Field(ge=0)
    document: DocumentProcessingProfileV1
    llm_provider: LlmProvider = "none"
    knowledge_backend: KnowledgeBackend = "none"
    reindex_knowledge: StrictBool = False


class RunProfileOverrideV1(_StrictFrozenModel):
    schema_version: Literal["run-profile-override/v1"] = (
        RUN_PROFILE_OVERRIDE_SCHEMA_VERSION
    )
    document: DocumentProcessingProfileV1 | None = None
    llm_provider: LlmProvider | None = None
    force_visual_pages: tuple[StrictInt, ...] = ()

    @field_validator("force_visual_pages")
    @classmethod
    def validate_force_visual_pages(
        cls,
        value: tuple[StrictInt, ...],
    ) -> tuple[StrictInt, ...]:
        if len(value) > 256:
            raise ValueError("force_visual_pages exceeds the 256-page run limit")
        if any(page < 1 for page in value):
            raise ValueError("force_visual_pages use one-based positive page numbers")
        if tuple(sorted(set(value))) != value:
            raise ValueError("force_visual_pages must be unique and sorted")
        return value


class ResolvedProviderBindingsV1(_StrictFrozenModel):
    primary_parser_provider: StrictStr = Field(min_length=1, max_length=160)
    enhancement_providers: tuple[StrictStr, ...] = ()
    complex_vision_provider: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    complex_vision_deployment: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    llm_transport_provider: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    structure_overview_deployment: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    structure_repair_deployment: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    semantic_deployment: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    atomization_deployment: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    refinement_deployment: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )


class ResolvedProcessingPlanV1(_StrictFrozenModel):
    schema_version: Literal["resolved-processing-plan/v1"] = (
        RESOLVED_PROCESSING_PLAN_SCHEMA_VERSION
    )
    plan_id: StrictStr = Field(pattern=_PLAN_ID_PATTERN)
    project_id: StrictStr = Field(min_length=1, max_length=160)
    document_id: StrictStr = Field(min_length=1, max_length=160)
    source_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    project_profile_revision: StrictInt = Field(ge=0)
    project_profile_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    document: DocumentProcessingProfileV1
    llm_provider: LlmProvider
    knowledge_backend: KnowledgeBackend
    provider_bindings: ResolvedProviderBindingsV1
    connection_revision: StrictInt = Field(ge=0)
    force_visual_pages: tuple[StrictInt, ...] = ()
    canonical_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_identity(self) -> ResolvedProcessingPlanV1:
        expected = compute_resolved_processing_plan_sha256(
            project_id=self.project_id,
            document_id=self.document_id,
            source_sha256=self.source_sha256,
            project_profile_revision=self.project_profile_revision,
            project_profile_sha256=self.project_profile_sha256,
            document=self.document,
            llm_provider=self.llm_provider,
            knowledge_backend=self.knowledge_backend,
            provider_bindings=self.provider_bindings,
            connection_revision=self.connection_revision,
            force_visual_pages=self.force_visual_pages,
        )
        if self.canonical_sha256 != expected:
            raise ValueError("resolved processing plan canonical hash mismatch")
        expected_id = f"processing-plan-{expected[:32]}"
        if self.plan_id != expected_id:
            raise ValueError("resolved processing plan ID does not match its hash")
        return self


class RuntimeCapabilityV1(_StrictFrozenModel):
    schema_version: Literal["runtime-capability/v1"] = RUNTIME_CAPABILITY_SCHEMA_VERSION
    capability_id: StrictStr = Field(min_length=1, max_length=160)
    category: Literal[
        "document_parser",
        "document_enhancement",
        "complex_vision",
        "llm",
        "embedding",
        "vector_store",
    ]
    provider: StrictStr = Field(min_length=1, max_length=80)
    label: StrictStr = Field(min_length=1, max_length=160)
    state: CapabilityState
    reason_code: StrictStr = Field(default="", max_length=160)
    message: StrictStr = Field(default="", max_length=500)
    blocked_by: tuple[StrictStr, ...] = ()
    settings_path: StrictStr = Field(default="/settings", max_length=240)
    connection_revision: StrictInt = Field(ge=0)

    @property
    def ready(self) -> bool:
        return self.state is CapabilityState.READY

    @model_validator(mode="after")
    def validate_state_details(self) -> RuntimeCapabilityV1:
        if self.state is CapabilityState.READY and (
            self.reason_code or self.blocked_by
        ):
            raise ValueError("READY capabilities cannot carry blockers")
        if self.state is not CapabilityState.READY and not self.reason_code:
            raise ValueError("unavailable capabilities require a reason_code")
        return self


def provider_bindings_require_connection(
    bindings: ResolvedProviderBindingsV1,
) -> bool:
    """Return whether frozen extraction providers need runtime AI connections.

    The provider bindings are the execution authority.  Profile labels such as
    ``llm_provider=none`` are not sufficient because a no-LLM plan may still
    call remote OCR/layout, while a fully local plan must remain independent
    from unrelated administrator connection revisions.
    """

    return bool(
        not bindings.primary_parser_provider.startswith("local:")
        or bindings.enhancement_providers
        or bindings.complex_vision_provider is not None
        or bindings.llm_transport_provider not in {None, "codex-app-server"}
    )


def processing_plan_requires_connection(plan: ResolvedProcessingPlanV1) -> bool:
    """Return the runtime-connection dependency of one immutable plan."""

    return provider_bindings_require_connection(plan.provider_bindings)


def build_project_processing_profile(
    *,
    project_id: str,
    document: DocumentProcessingProfileV1,
    llm_provider: LlmProvider,
    knowledge_backend: KnowledgeBackend,
    revision: int,
) -> ProjectProcessingProfileV1:
    canonical_sha256 = compute_project_processing_profile_sha256(
        project_id=project_id,
        document=document,
        llm_provider=llm_provider,
        knowledge_backend=knowledge_backend,
        revision=revision,
    )
    return ProjectProcessingProfileV1(
        project_id=project_id,
        document=document,
        llm_provider=llm_provider,
        knowledge_backend=knowledge_backend,
        revision=revision,
        canonical_sha256=canonical_sha256,
    )


def build_local_rules_profile(
    *,
    project_id: str,
    revision: int = 0,
) -> ProjectProcessingProfileV1:
    return build_project_processing_profile(
        project_id=project_id,
        document=DocumentProcessingProfileV1(
            primary_parser="pymupdf",
            enhancement_chain=(),
            complex_vision=None,
            auto_escalation=True,
        ),
        llm_provider="none",
        knowledge_backend="none",
        revision=revision,
    )


def build_codex_agent_profile(
    *,
    project_id: str,
    revision: int = 0,
) -> ProjectProcessingProfileV1:
    """Build the fixed local Codex document-processing profile.

    ``codex`` is an internal execution identity, not a user-selectable cloud
    provider. Source capture remains deterministic; semantic interpretation is
    delegated to the configured local Codex App Server runtime.
    """

    return build_project_processing_profile(
        project_id=project_id,
        document=DocumentProcessingProfileV1(
            primary_parser="pymupdf",
            enhancement_chain=(),
            complex_vision=None,
            auto_escalation=True,
        ),
        llm_provider="codex",
        knowledge_backend="none",
        revision=revision,
    )


def build_resolved_processing_plan(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    profile: ProjectProcessingProfileV1,
    document: DocumentProcessingProfileV1,
    llm_provider: LlmProvider,
    provider_bindings: ResolvedProviderBindingsV1,
    connection_revision: int,
    force_visual_pages: tuple[int, ...] = (),
) -> ResolvedProcessingPlanV1:
    canonical_sha256 = compute_resolved_processing_plan_sha256(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        project_profile_revision=profile.revision,
        project_profile_sha256=profile.canonical_sha256,
        document=document,
        llm_provider=llm_provider,
        knowledge_backend=profile.knowledge_backend,
        provider_bindings=provider_bindings,
        connection_revision=connection_revision,
        force_visual_pages=force_visual_pages,
    )
    return ResolvedProcessingPlanV1(
        plan_id=f"processing-plan-{canonical_sha256[:32]}",
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        project_profile_revision=profile.revision,
        project_profile_sha256=profile.canonical_sha256,
        document=document,
        llm_provider=llm_provider,
        knowledge_backend=profile.knowledge_backend,
        provider_bindings=provider_bindings,
        connection_revision=connection_revision,
        force_visual_pages=force_visual_pages,
        canonical_sha256=canonical_sha256,
    )


def compute_project_processing_profile_sha256(
    *,
    project_id: str,
    document: DocumentProcessingProfileV1,
    llm_provider: LlmProvider,
    knowledge_backend: KnowledgeBackend,
    revision: int,
) -> str:
    return _canonical_sha256(
        {
            "schema_version": PROJECT_PROCESSING_PROFILE_SCHEMA_VERSION,
            "project_id": project_id,
            "document": document.model_dump(mode="json"),
            "llm_provider": llm_provider,
            "knowledge_backend": knowledge_backend,
            "revision": revision,
        }
    )


def compute_resolved_processing_plan_sha256(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    project_profile_revision: int,
    project_profile_sha256: str,
    document: DocumentProcessingProfileV1,
    llm_provider: LlmProvider,
    knowledge_backend: KnowledgeBackend,
    provider_bindings: ResolvedProviderBindingsV1,
    connection_revision: int,
    force_visual_pages: tuple[int, ...],
) -> str:
    return _canonical_sha256(
        {
            "schema_version": RESOLVED_PROCESSING_PLAN_SCHEMA_VERSION,
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": source_sha256,
            "project_profile_revision": project_profile_revision,
            "project_profile_sha256": project_profile_sha256,
            "document": document.model_dump(mode="json"),
            "llm_provider": llm_provider,
            "knowledge_backend": knowledge_backend,
            "provider_bindings": provider_bindings.model_dump(mode="json"),
            "connection_revision": connection_revision,
            "force_visual_pages": list(force_visual_pages),
        }
    )


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "CapabilityState",
    "ComplexVisionProvider",
    "DocumentEnhancer",
    "DocumentPrimaryParser",
    "DocumentProcessingProfileV1",
    "KnowledgeBackend",
    "LlmProvider",
    "ProjectProcessingProfileUpdateV1",
    "ProjectProcessingProfileV1",
    "ResolvedProcessingPlanV1",
    "ResolvedProviderBindingsV1",
    "RunProfileOverrideV1",
    "RuntimeCapabilityV1",
    "build_local_rules_profile",
    "build_codex_agent_profile",
    "build_project_processing_profile",
    "build_resolved_processing_plan",
    "compute_project_processing_profile_sha256",
    "compute_resolved_processing_plan_sha256",
    "processing_plan_requires_connection",
    "provider_bindings_require_connection",
]
