from __future__ import annotations

from dataclasses import dataclass, replace
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.adapters.agents.agent_kit_materializer import (
    MaterializedAgentKit,
    materialize_agent_kit,
)
from app.adapters.agents.codex_app_server.lifecycle import (
    CanaryRuntimeState,
    LifecycleProbeError,
)
from app.adapters.agents.codex_app_server.agent_native_read_only_runtime import (
    AgentNativeReadOnlyRuntimePolicyV1,
    configure_agent_native_read_only_runtime,
    protocol_supports_restricted_read_access,
)
from app.adapters.agents.codex_app_server.pdf_skill_canary import (
    _workspace_manifest,
)
from app.adapters.agents.codex_app_server.primary_runtime_pdf import (
    OpenAIPrimaryRuntimePdfAttestationV1,
    OpenAIPrimaryRuntimePdfDependencies,
    discover_openai_primary_runtime_pdf_dependencies,
)
from app.adapters.agents.d3_parent_provision_attestation import (
    compile_d3_parent_provision_policy_closure,
)
from app.adapters.agents.codex_app_server.d3_stage_input_workspace import (
    D3StageInputBundleAttestationV1,
    D3StageInputPredecessorBindingV1,
    MaterializedD3StageInputWorkspace,
    materialize_d3_stage_input_workspace,
)
from app.adapters.agents.codex_app_server.d3_region_stage_input_workspace import (
    D3RegionStageInputWorkspaceManifestV1,
    MaterializedD3RegionStageInputWorkspace,
    materialize_d3_region_stage_input_workspace,
)
from app.adapters.agents.codex_app_server.document_map_scope_stage_input import (
    CanonicalDocumentMapScopeStageInputAttestationV1,
    DOCUMENT_MAP_SCOPE_EXECUTION_MODE,
    MaterializedCanonicalDocumentMapScopeStageInput,
    materialize_document_map_scope_stage_input,
)
from app.adapters.agents.codex_app_server.document_map_replacement_stage_input import (
    CanonicalDocumentMapReplacementStageInputAttestationV1,
    MaterializedCanonicalDocumentMapReplacementStageInput,
    materialize_document_map_replacement_stage_input,
)
from app.adapters.agents.portable_kit import (
    AgentKitRunnerOutputSchemaVariantV1,
    AgentKitStageSpecV1,
    AgentKitToolSpecV2,
    AgentKitValidationError,
    PortableAgentKit,
    load_portable_agent_kit,
)
from app.adapters.agents.reference_workspace import (
    materialize_product_evidence_workspace,
    verify_reference_workspace,
)
from app.adapters.agents.stage_context import (
    CompiledAgentStageContext,
    compile_agent_stage_context,
)
from app.application.agent_workflow.d3_parent_provision_attestation import (
    D3ParentProvisionPolicyClosureV1,
    canonical_sha256,
)
from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalStructureProposalV1,
    D2LogicalStructureValidationReceiptV1,
)
from app.application.agent_workflow.d2_logical_structure_v2 import (
    D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION,
    D2LogicalStructureProposalV2,
    D2LogicalStructureStatus,
    D2LogicalStructureValidationReceiptV2,
)
from app.application.agent_workflow.d2_table_region_parent_input import (
    D2TableRegionParentInputReceiptV1,
    D2TableRegionParentInputV1,
)
from app.application.agent_workflow.d3_region_stage_input import (
    D3RegionStageInputLineageV1,
)
from app.application.agent_workflow.d3_parent_provisions import (
    D3ExactSourceSegmentV1,
)
from app.application.agent_workflow.document_map_scope import (
    DocumentMapScopeRequestV1,
)
from app.application.agent_workflow.document_map_replacement import (
    DOCUMENT_MAP_SCOPE_REPLACEMENT_EXECUTION_MODE,
    DocumentMapScopeReplacementRequestV1,
)
from app.application.agent_workflow.extraction_runtime import (
    AgentExtractionStage,
    AgentKitIdentityV1,
    AgentRunnerKind,
    AgentStageArtifactStatus,
)
from app.domain.extraction.source_ir import SourceIRSnapshot


CANONICAL_APP_SERVER_SESSION_ATTESTATION_SCHEMA_VERSION = (
    "reqpilotx-canonical-app-server-session-attestation/v1"
)
CANONICAL_AGENT_KIT_ID = "reqpilotx-document-extraction"
CANONICAL_AGENT_KIT_VERSION = "0.15.2"
CANONICAL_STRUCTURE_HANDOFF_EXECUTION_MODE = "STRUCTURE_HANDOFF"
CANONICAL_SOURCE_REQUIREMENT_EXECUTION_MODES = frozenset(
    {
        "SOURCE_ITEM_TEXT_BATCH",
        "SOURCE_ITEM_TEXT_BOUNDARY_PLAN",
        "SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW",
        "SOURCE_REQUIREMENT_BOUNDARY_PLAN",
        "SOURCE_REQUIREMENT_CLASSIFICATION_WINDOW",
    }
)
_PRODUCT_RUNNER_PROFILE_ID = "app-server-product"
_PRIVATE_SESSIONS_DIRECTORY = "canonical-app-server-document-sessions"
_POLICY_CONTEXT_HEADER = "REQPILOTX_APP_SERVER_STAGE_POLICY_CLOSURE_V1"

CanonicalStageDiagnosticCode = Literal[
    "CANONICAL_STAGE_ARTIFACT_SCHEMA_MISSING",
    "CANONICAL_STAGE_IMPLEMENTATION_PLANNED",
    "CANONICAL_STAGE_REQUIRED_TOOL_CONTRACT_MISSING",
    "CANONICAL_STAGE_REQUIRED_TOOL_PLANNED",
    "CANONICAL_STAGE_VALIDATOR_MISSING",
]

CanonicalNativeReadCapabilityDiagnostic = Literal[
    "CANONICAL_NATIVE_READ_LIFECYCLE_FEATURES_DISABLED",
    "CANONICAL_NATIVE_READ_RESTRICTED_ROOT_UNSUPPORTED",
    "CANONICAL_NATIVE_READ_ROOT_CONFINEMENT_UNATTESTED",
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


@dataclass(frozen=True, slots=True)
class CanonicalProductEvidenceRequest:
    source_path: Path
    expected_source_sha256: str
    document_role: str
    authorized_corpus_root: Path
    run_id: str
    opaque_source_handle: str


@dataclass(frozen=True, slots=True)
class CanonicalSourceRequirementStageInputRequest:
    execution_mode: Literal[
        "SOURCE_ITEM_TEXT_BATCH",
        "SOURCE_ITEM_TEXT_BOUNDARY_PLAN",
        "SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW",
        "SOURCE_REQUIREMENT_BOUNDARY_PLAN",
        "SOURCE_REQUIREMENT_CLASSIFICATION_WINDOW",
    ]
    source_sha256: str
    document_map_artifact_id: str
    document_map_artifact_sha256: str
    request_sha256: str
    scope_id: str
    json_files: tuple[tuple[str, Mapping[str, Any]], ...]


@dataclass(frozen=True, slots=True)
class CanonicalD3StageInputRequest:
    d2_proposal: D2LogicalStructureProposalV1
    d2_validation_receipt: D2LogicalStructureValidationReceiptV1
    source_segments: tuple[D3ExactSourceSegmentV1, ...]


@dataclass(frozen=True, slots=True)
class CanonicalD3RegionStageInputRequest:
    source_ir: SourceIRSnapshot
    d2_proposal: D2LogicalStructureProposalV2
    d2_validation_receipt: D2LogicalStructureValidationReceiptV2
    parent_input: D2TableRegionParentInputV1
    parent_input_receipt: D2TableRegionParentInputReceiptV1
    expected_lineage: D3RegionStageInputLineageV1


class CanonicalDocumentEvidenceAttestationV1(_StrictFrozenModel):
    schema_version: Literal["reqpilotx-product-evidence-workspace/v2"] = (
        "reqpilotx-product-evidence-workspace/v2"
    )
    workspace_kind: Literal["APP_SERVER_PRODUCT_READ_ONLY"] = (
        "APP_SERVER_PRODUCT_READ_ONLY"
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    opaque_source_handle: str = Field(pattern=r"^source-handle-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_role: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    page_count: int = Field(ge=1, le=250)
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_authorized: Literal[True] = True
    customer_content_redacted: Literal[False] = False


class CanonicalStagePredecessorBindingV1(_StrictFrozenModel):
    stage: Literal[
        AgentExtractionStage.LOGICAL_STRUCTURE,
        AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
    ]
    predecessor_stage: Literal[
        AgentExtractionStage.DOCUMENT_UNDERSTANDING,
        AgentExtractionStage.LOGICAL_STRUCTURE,
    ]
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_schema_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}/v[0-9]+$")
    status: Literal[
        AgentStageArtifactStatus.VALIDATED,
        AgentStageArtifactStatus.PARTIAL,
    ]

    @model_validator(mode="after")
    def validate_stage_pair(self) -> CanonicalStagePredecessorBindingV1:
        expected = {
            AgentExtractionStage.LOGICAL_STRUCTURE: (
                AgentExtractionStage.DOCUMENT_UNDERSTANDING
            ),
            AgentExtractionStage.PARENT_PROVISION_RECOGNITION: (
                AgentExtractionStage.LOGICAL_STRUCTURE
            ),
        }[self.stage]
        if self.predecessor_stage is not expected:
            raise ValueError("canonical stage predecessor is not adjacent")
        return self


class CanonicalStructureHandoffBindingV1(_StrictFrozenModel):
    schema_version: Literal["canonical-structure-handoff-binding/v1"] = (
        "canonical-structure-handoff-binding/v1"
    )
    content_free: Literal[True] = True
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    handoff_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["ACKNOWLEDGED"] = "ACKNOWLEDGED"
    affected_scope_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    work_item_count: int = Field(ge=1, le=256)
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_binding(self) -> CanonicalStructureHandoffBindingV1:
        if self.affected_scope_ids != tuple(sorted(set(self.affected_scope_ids))):
            raise ValueError("handoff scopes must be unique and sorted")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"binding_sha256"})
        )
        if self.binding_sha256 != expected:
            raise ValueError("handoff binding digest drifted")
        return self


class CanonicalStageFileDigestV1(_StrictFrozenModel):
    path: str = Field(
        pattern=(
            r"^(?:\.agents/skills/[a-z0-9-]+/SKILL\.md|"
            r"policy/[a-z0-9.-]+\.json|references/[a-z0-9-]+\.md|"
            r"schemas/[a-z0-9.-]+\.schema\.json)$"
        )
    )
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CanonicalSourceRequirementInputFileV1(_StrictFrozenModel):
    path: str = Field(pattern=r"^[a-z][a-z0-9.-]{0,119}\.json$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CanonicalSourceRequirementStageInputAttestationV1(_StrictFrozenModel):
    schema_version: Literal[
        "canonical-source-requirement-stage-input/v1"
    ] = "canonical-source-requirement-stage-input/v1"
    content_bearing: Literal[True] = True
    private_workspace_only: Literal[True] = True
    execution_mode: Literal[
        "SOURCE_ITEM_TEXT_BATCH",
        "SOURCE_ITEM_TEXT_BOUNDARY_PLAN",
        "SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW",
        "SOURCE_REQUIREMENT_BOUNDARY_PLAN",
        "SOURCE_REQUIREMENT_CLASSIFICATION_WINDOW",
    ]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
    )
    files: tuple[CanonicalSourceRequirementInputFileV1, ...] = Field(
        min_length=1,
        max_length=16,
    )
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_manifest(
        self,
    ) -> CanonicalSourceRequirementStageInputAttestationV1:
        if self.files != tuple(sorted(self.files, key=lambda item: item.path)) or len(
            {item.path for item in self.files}
        ) != len(self.files):
            raise ValueError("source-requirement stage-input files must be sorted")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"manifest_sha256"})
        )
        if self.manifest_sha256 != expected:
            raise ValueError("source-requirement stage-input manifest drifted")
        return self


@dataclass(frozen=True, slots=True)
class MaterializedCanonicalSourceRequirementStageInput:
    root: Path
    manifest_path: Path
    attestation: CanonicalSourceRequirementStageInputAttestationV1


class CanonicalStageRunnerOutputSchemaVariantV1(_StrictFrozenModel):
    """Content-free lock proof for one execution-mode runner schema."""

    content_free: Literal[True] = True
    execution_mode: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    schema_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}/v[0-9]+$")
    schema_file: CanonicalStageFileDigestV1
    variant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_variant(self) -> CanonicalStageRunnerOutputSchemaVariantV1:
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"variant_sha256"})
        )
        if self.variant_sha256 != expected:
            raise ValueError("runner-output schema variant digest drifted")
        return self


class CanonicalStageToolAttestationV1(_StrictFrozenModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
    implementation_status: Literal["AVAILABLE", "PLANNED"]
    implementation_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9-]{0,119}$",
    )
    implementation_version: str | None = Field(
        default=None,
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$",
    )
    input_schema: CanonicalStageFileDigestV1 | None = None
    output_schema: CanonicalStageFileDigestV1 | None = None

    @model_validator(mode="after")
    def validate_implementation(self) -> CanonicalStageToolAttestationV1:
        implementation_fields = (
            self.implementation_id,
            self.implementation_version,
            self.input_schema,
            self.output_schema,
        )
        if self.implementation_status == "AVAILABLE" and not all(implementation_fields):
            raise ValueError("available stage tools require a complete contract")
        if self.implementation_status == "PLANNED" and any(implementation_fields):
            raise ValueError("planned stage tools cannot claim a live contract")
        return self


class CanonicalStageCapabilitySkillAttestationV1(_StrictFrozenModel):
    """One locked, non-owning capability Skill attached to a stage turn."""

    skill_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    skill_file: CanonicalStageFileDigestV1
    attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_attestation(
        self,
    ) -> CanonicalStageCapabilitySkillAttestationV1:
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"attestation_sha256"})
        )
        if self.attestation_sha256 != expected:
            raise ValueError("capability Skill attestation digest drifted")
        return self


class CanonicalAgentNativeReadCapabilityV1(_StrictFrozenModel):
    status: Literal["ATTESTED", "BLOCKED"]
    shell_tool_enabled: bool
    unified_exec_enabled: bool
    view_image_enabled: bool
    local_image_input_enabled: bool
    session_root_read_confinement_attested: bool
    network_access_enabled: Literal[False] = False
    filesystem_write_enabled: Literal[False] = False
    real_transport_eligible: bool
    diagnostic_codes: tuple[CanonicalNativeReadCapabilityDiagnostic, ...]

    @model_validator(mode="after")
    def validate_capability(self) -> CanonicalAgentNativeReadCapabilityV1:
        if self.diagnostic_codes != tuple(sorted(set(self.diagnostic_codes))):
            raise ValueError("native-read diagnostics must be unique and sorted")
        available = (
            self.shell_tool_enabled
            and self.unified_exec_enabled
            and (self.view_image_enabled or self.local_image_input_enabled)
            and self.session_root_read_confinement_attested
            and not self.network_access_enabled
            and not self.filesystem_write_enabled
            and not self.diagnostic_codes
        )
        if self.real_transport_eligible != available:
            raise ValueError("native-read eligibility does not match capabilities")
        if (self.status == "ATTESTED") != available:
            raise ValueError("native-read status does not match capabilities")
        return self


class CanonicalAppServerStageAttestationV1(_StrictFrozenModel):
    stage: AgentExtractionStage
    implementation_status: Literal["AVAILABLE", "PLANNED"]
    context_kind: Literal["EXECUTABLE_STAGE_CONTEXT", "POLICY_CLOSURE_ONLY"]
    skill_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    skill: CanonicalStageFileDigestV1
    capability_skills: tuple[CanonicalStageCapabilitySkillAttestationV1, ...] = ()
    policy: CanonicalStageFileDigestV1
    references: tuple[CanonicalStageFileDigestV1, ...]
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_schema_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}/v[0-9]+$")
    artifact_schema: CanonicalStageFileDigestV1 | None = None
    runner_output_schema_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9-]{0,119}/v[0-9]+$",
    )
    runner_output_schema: CanonicalStageFileDigestV1 | None = None
    runner_output_schema_variants: tuple[
        CanonicalStageRunnerOutputSchemaVariantV1, ...
    ] = ()
    runner_output_schema_variants_sha256: str = Field(
        default=canonical_sha256([]),
        pattern=r"^[0-9a-f]{64}$",
    )
    validator_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9-]{0,119}$",
    )
    validator_version: str | None = Field(
        default=None,
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$",
    )
    required_tools: tuple[CanonicalStageToolAttestationV1, ...]
    inference_available: bool
    diagnostic_codes: tuple[CanonicalStageDiagnosticCode, ...]

    @model_validator(mode="after")
    def validate_readiness(self) -> CanonicalAppServerStageAttestationV1:
        capability_ids = tuple(item.skill_id for item in self.capability_skills)
        capability_paths = tuple(
            item.skill_file.path for item in self.capability_skills
        )
        if (
            capability_ids != tuple(sorted(set(capability_ids)))
            or len(capability_paths) != len(set(capability_paths))
            or self.skill_id in capability_ids
            or self.skill.path in capability_paths
        ):
            raise ValueError("stage capability Skill closure drifted")
        if self.references != tuple(
            sorted(self.references, key=lambda item: item.path)
        ) or len({item.path for item in self.references}) != len(self.references):
            raise ValueError("stage reference closure must be unique and sorted")
        if self.diagnostic_codes != tuple(sorted(set(self.diagnostic_codes))):
            raise ValueError("stage diagnostics must be unique and sorted")
        if tuple(item.name for item in self.required_tools) != tuple(
            sorted({item.name for item in self.required_tools})
        ):
            raise ValueError("required stage tools must be unique and sorted")
        variant_modes = tuple(
            item.execution_mode for item in self.runner_output_schema_variants
        )
        variant_ids = tuple(
            item.schema_id for item in self.runner_output_schema_variants
        )
        variant_paths = tuple(
            item.schema_file.path for item in self.runner_output_schema_variants
        )
        if variant_modes != tuple(sorted(set(variant_modes))):
            raise ValueError(
                "runner-output schema variant modes must be unique and sorted"
            )
        if len(variant_ids) != len(set(variant_ids)) or len(variant_paths) != len(
            set(variant_paths)
        ):
            raise ValueError("runner-output schema variant identities must be unique")
        if self.runner_output_schema_id in set(variant_ids) or (
            self.runner_output_schema is not None
            and self.runner_output_schema.path in set(variant_paths)
        ):
            raise ValueError("runner-output schema variants must differ from primary")
        expected_variant_digest = canonical_sha256(
            [
                item.model_dump(mode="json")
                for item in self.runner_output_schema_variants
            ]
        )
        if self.runner_output_schema_variants_sha256 != expected_variant_digest:
            raise ValueError("runner-output schema variants digest drifted")
        complete = (
            self.implementation_status == "AVAILABLE"
            and self.context_kind == "EXECUTABLE_STAGE_CONTEXT"
            and self.artifact_schema is not None
            and self.runner_output_schema_id is not None
            and self.runner_output_schema is not None
            and self.validator_id is not None
            and self.validator_version is not None
            and all(
                item.implementation_status == "AVAILABLE"
                and item.implementation_id is not None
                and item.implementation_version is not None
                and item.input_schema is not None
                and item.output_schema is not None
                for item in self.required_tools
            )
            and not self.diagnostic_codes
        )
        if self.inference_available != complete:
            raise ValueError("stage inference readiness does not match its closure")
        if self.inference_available == bool(self.diagnostic_codes):
            raise ValueError("stage diagnostics do not match inference readiness")
        return self


class CanonicalAppServerDocumentSessionAttestationV1(_StrictFrozenModel):
    schema_version: Literal["reqpilotx-canonical-app-server-session-attestation/v1"] = (
        CANONICAL_APP_SERVER_SESSION_ATTESTATION_SCHEMA_VERSION
    )
    attestation_id: str = Field(pattern=r"^canonical-session-attestation-[0-9a-f]{32}$")
    session_id: str = Field(pattern=r"^canonical-document-session-[0-9a-f]{32}$")
    content_free: Literal[True] = True
    kit_identity: AgentKitIdentityV1
    runner_profile_id: Literal["app-server-product"] = _PRODUCT_RUNNER_PROFILE_ID
    runner_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_kind: Literal[AgentRunnerKind.APP_SERVER_PRODUCT] = (
        AgentRunnerKind.APP_SERVER_PRODUCT
    )
    adapter_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    adapter_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    model_id: str = Field(min_length=1, max_length=160)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]
    domain_logic_source: Literal["PORTABLE_AGENT_KIT"] = "PORTABLE_AGENT_KIT"
    exact_bundle_materialized: Literal[True] = True
    private_runtime_state: Literal[True] = True
    runtime_assets_copied: Literal[False] = False
    agent_native_read_capability: CanonicalAgentNativeReadCapabilityV1
    native_runtime_policy: AgentNativeReadOnlyRuntimePolicyV1 | None = None
    pdf_primary_runtime: OpenAIPrimaryRuntimePdfAttestationV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    document_evidence: CanonicalDocumentEvidenceAttestationV1 | None = None
    stage_predecessors: tuple[CanonicalStagePredecessorBindingV1, ...] = ()
    structure_handoff: CanonicalStructureHandoffBindingV1 | None = None
    d3_stage_input: D3StageInputBundleAttestationV1 | None = None
    d3_region_stage_input: D3RegionStageInputWorkspaceManifestV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    source_requirement_stage_input: (
        CanonicalSourceRequirementStageInputAttestationV1 | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    document_map_scope_stage_input: (
        CanonicalDocumentMapScopeStageInputAttestationV1 | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    document_map_replacement_stage_input: (
        CanonicalDocumentMapReplacementStageInputAttestationV1 | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    workspace_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_manifest_unchanged: Literal[True] = True
    stages: tuple[CanonicalAppServerStageAttestationV1, ...]
    d3_policy_closure_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_attestation(self) -> CanonicalAppServerDocumentSessionAttestationV1:
        stage_values = tuple(item.stage for item in self.stages)
        if not stage_values or stage_values != tuple(
            sorted(set(stage_values), key=tuple(AgentExtractionStage).index)
        ):
            raise ValueError("session stages must be unique and canonically ordered")
        contains_d3 = AgentExtractionStage.PARENT_PROVISION_RECOGNITION in stage_values
        if contains_d3 != (self.d3_policy_closure_sha256 is not None):
            raise ValueError("D3 session binding requires one policy closure digest")
        native_attested = self.agent_native_read_capability.status == "ATTESTED"
        if native_attested != (
            self.native_runtime_policy is not None
            and self.document_evidence is not None
        ):
            raise ValueError(
                "native-read attestation requires runtime policy and document evidence"
            )
        requires_pdf_runtime = any(
            capability.skill_id == "pdf"
            for stage in self.stages
            for capability in stage.capability_skills
        )
        if native_attested and requires_pdf_runtime != (
            self.pdf_primary_runtime is not None
        ):
            raise ValueError(
                "PDF capability requires one attested OpenAI primary runtime"
            )
        predecessor_stages = tuple(item.stage for item in self.stage_predecessors)
        if predecessor_stages != tuple(
            sorted(set(predecessor_stages), key=tuple(AgentExtractionStage).index)
        ) or any(stage not in stage_values for stage in predecessor_stages):
            raise ValueError("session predecessors must be unique and stage-bound")
        if self.document_evidence is not None and any(
            item.run_id != self.document_evidence.run_id
            or item.source_sha256 != self.document_evidence.source_sha256
            for item in self.stage_predecessors
        ):
            raise ValueError("session predecessor source identity drifted")
        if self.structure_handoff is not None and (
            AgentExtractionStage.LOGICAL_STRUCTURE not in stage_values
            or self.document_evidence is None
            or self.structure_handoff.run_id != self.document_evidence.run_id
            or self.structure_handoff.source_sha256
            != self.document_evidence.source_sha256
        ):
            raise ValueError("structure handoff drifted from document evidence")
        if self.d3_stage_input is not None:
            d3_predecessor = next(
                (
                    item
                    for item in self.stage_predecessors
                    if item.stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
                ),
                None,
            )
            if (
                AgentExtractionStage.PARENT_PROVISION_RECOGNITION not in stage_values
                or d3_predecessor is None
                or self.document_evidence is None
                or self.d3_stage_input.run_id != self.document_evidence.run_id
                or self.d3_stage_input.source_sha256
                != self.document_evidence.source_sha256
                or self.d3_stage_input.kit_bundle_sha256
                != self.kit_identity.bundle_sha256
                or self.d3_stage_input.predecessor.artifact_id
                != d3_predecessor.artifact_id
                or self.d3_stage_input.predecessor.artifact_sha256
                != d3_predecessor.artifact_sha256
            ):
                raise ValueError("D3 stage input drifted from its session closure")
        input_contract_count = sum(
            value is not None
            for value in (
                self.d3_stage_input,
                self.d3_region_stage_input,
                self.source_requirement_stage_input,
                self.document_map_scope_stage_input,
                self.document_map_replacement_stage_input,
            )
        )
        if input_contract_count > 1:
            raise ValueError("canonical session cannot carry multiple stage inputs")
        if self.d3_region_stage_input is not None:
            d3_predecessor = next(
                (
                    item
                    for item in self.stage_predecessors
                    if item.stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
                ),
                None,
            )
            lineage = self.d3_region_stage_input.stage_input_manifest.lineage
            if (
                AgentExtractionStage.PARENT_PROVISION_RECOGNITION not in stage_values
                or d3_predecessor is None
                or self.document_evidence is None
                or d3_predecessor.proposal_schema_id
                != D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION
                or self.d3_region_stage_input.run_id != self.document_evidence.run_id
                or self.d3_region_stage_input.source_sha256
                != self.document_evidence.source_sha256
                or self.d3_region_stage_input.kit_bundle_sha256
                != self.kit_identity.bundle_sha256
                or self.d3_region_stage_input.predecessor_artifact_id
                != d3_predecessor.artifact_id
                or self.d3_region_stage_input.predecessor_artifact_sha256
                != d3_predecessor.artifact_sha256
                or self.d3_region_stage_input.predecessor_validation_receipt_sha256
                != d3_predecessor.validator_receipt_sha256
                or lineage.run_id != d3_predecessor.run_id
                or lineage.source_sha256 != d3_predecessor.source_sha256
                or lineage.source_ir_id != self.document_evidence.source_ir_id
                or lineage.source_ir_canonical_sha256
                != self.document_evidence.source_ir_canonical_sha256
                or lineage.kit_bundle_sha256 != self.kit_identity.bundle_sha256
                or lineage.v2_artifact_id != d3_predecessor.artifact_id
                or lineage.v2_artifact_sha256 != d3_predecessor.artifact_sha256
                or lineage.v2_validation_receipt_sha256
                != d3_predecessor.validator_receipt_sha256
            ):
                raise ValueError(
                    "region D3 stage input drifted from its session closure"
                )
        if self.source_requirement_stage_input is not None and (
            AgentExtractionStage.PARENT_PROVISION_RECOGNITION not in stage_values
            or self.document_evidence is None
            or self.source_requirement_stage_input.source_sha256
            != self.document_evidence.source_sha256
        ):
            raise ValueError(
                "source-requirement stage input drifted from document evidence"
            )
        if self.document_map_scope_stage_input is not None and (
            AgentExtractionStage.DOCUMENT_UNDERSTANDING not in stage_values
            or self.document_evidence is None
            or self.document_map_scope_stage_input.run_id
            != self.document_evidence.run_id
            or self.document_map_scope_stage_input.source_sha256
            != self.document_evidence.source_sha256
            or self.document_map_scope_stage_input.source_ir_id
            != self.document_evidence.source_ir_id
            or self.document_map_scope_stage_input.source_ir_canonical_sha256
            != self.document_evidence.source_ir_canonical_sha256
            or self.document_map_scope_stage_input.evidence_workspace_bundle_sha256
            != self.document_evidence.workspace_bundle_sha256
        ):
            raise ValueError(
                "document-map scope stage input drifted from document evidence"
            )
        if self.document_map_replacement_stage_input is not None and (
            AgentExtractionStage.DOCUMENT_UNDERSTANDING not in stage_values
            or self.document_evidence is None
            or self.document_map_replacement_stage_input.run_id
            != self.document_evidence.run_id
            or self.document_map_replacement_stage_input.source_sha256
            != self.document_evidence.source_sha256
            or self.document_map_replacement_stage_input.source_ir_id
            != self.document_evidence.source_ir_id
            or self.document_map_replacement_stage_input.source_ir_canonical_sha256
            != self.document_evidence.source_ir_canonical_sha256
            or self.document_map_replacement_stage_input.evidence_workspace_bundle_sha256
            != self.document_evidence.workspace_bundle_sha256
        ):
            raise ValueError(
                "document-map replacement input drifted from document evidence"
            )
        payload = self.model_dump(mode="json", exclude={"attestation_id"})
        expected = f"canonical-session-attestation-{canonical_sha256(payload)[:32]}"
        if self.attestation_id != expected:
            raise ValueError("session attestation identity does not match its closure")
        return self


@dataclass(frozen=True, slots=True)
class CanonicalAppServerStageBinding:
    attestation: CanonicalAppServerStageAttestationV1
    skill_path: Path
    capability_skill_paths: tuple[tuple[str, Path], ...]
    context_text: str
    compiled_stage_context: CompiledAgentStageContext | None


@dataclass(frozen=True, slots=True)
class CanonicalAppServerStageActivation:
    runtime: CanaryRuntimeState
    stage: AgentExtractionStage
    skill_paths: dict[str, Path]
    context_text: str
    context_sha256: str
    artifact_schema_path: Path
    artifact_schema_sha256: str
    runner_output_schema_id: str
    runner_output_schema_path: Path
    runner_output_schema_sha256: str
    validator_id: str
    validator_version: str
    session_attestation: CanonicalAppServerDocumentSessionAttestationV1
    d3_policy_closure: D3ParentProvisionPolicyClosureV1 | None
    evidence_workspace_path: Path | None = None
    visual_input_paths: tuple[Path, ...] = ()
    turn_sandbox_policy: dict[str, object] | None = None
    predecessor: CanonicalStagePredecessorBindingV1 | None = None
    structure_handoff: CanonicalStructureHandoffBindingV1 | None = None
    stage_input_manifest_path: Path | None = None
    d3_stage_input_attestation: D3StageInputBundleAttestationV1 | None = None
    d3_region_stage_input_manifest_path: Path | None = None
    d3_region_stage_input_attestation: D3RegionStageInputWorkspaceManifestV1 | None = (
        None
    )
    source_requirement_stage_input_attestation: (
        CanonicalSourceRequirementStageInputAttestationV1 | None
    ) = None
    document_map_scope_stage_input_attestation: (
        CanonicalDocumentMapScopeStageInputAttestationV1 | None
    ) = None
    document_map_replacement_stage_input_attestation: (
        CanonicalDocumentMapReplacementStageInputAttestationV1 | None
    ) = None
    runner_output_execution_mode: str | None = None
    runner_output_schema_variants_sha256: str = canonical_sha256([])


class CanonicalAppServerDocumentSession:
    """Private, exact-Kit platform state shared by future canonical stage drivers.

    This object does not open an App Server thread and cannot perform inference.
    A driver must first request an activation for one AVAILABLE stage, then apply
    the returned one-Skill allowlist to the App Server runtime.
    """

    def __init__(
        self,
        *,
        runtime: CanaryRuntimeState,
        session_id: str,
        session_root: Path,
        materialized: MaterializedAgentKit,
        kit: PortableAgentKit,
        bindings: tuple[CanonicalAppServerStageBinding, ...],
        d3_policy_closure: D3ParentProvisionPolicyClosureV1 | None,
        native_read_capability: CanonicalAgentNativeReadCapabilityV1,
        native_runtime_policy: AgentNativeReadOnlyRuntimePolicyV1 | None,
        pdf_primary_runtime: OpenAIPrimaryRuntimePdfAttestationV1 | None,
        document_evidence: CanonicalDocumentEvidenceAttestationV1 | None,
        evidence_workspace_path: Path | None,
        turn_sandbox_policy: dict[str, object] | None,
        stage_predecessors: tuple[CanonicalStagePredecessorBindingV1, ...],
        structure_handoff: CanonicalStructureHandoffBindingV1 | None,
        d3_stage_input: MaterializedD3StageInputWorkspace | None,
        d3_region_stage_input: MaterializedD3RegionStageInputWorkspace | None,
        source_requirement_stage_input: (
            MaterializedCanonicalSourceRequirementStageInput | None
        ),
        document_map_scope_stage_input: (
            MaterializedCanonicalDocumentMapScopeStageInput | None
        ),
        document_map_replacement_stage_input: (
            MaterializedCanonicalDocumentMapReplacementStageInput | None
        ),
    ) -> None:
        self.session_id = session_id
        self.session_root = session_root
        self.materialized = materialized
        self.kit = kit
        self.runtime = runtime
        self.bindings = bindings
        self.d3_policy_closure = d3_policy_closure
        self.native_read_capability = native_read_capability
        self.native_runtime_policy = native_runtime_policy
        self.pdf_primary_runtime = pdf_primary_runtime
        self.document_evidence = document_evidence
        self.evidence_workspace_path = evidence_workspace_path
        self.turn_sandbox_policy = turn_sandbox_policy
        if len({item.stage for item in stage_predecessors}) != len(stage_predecessors):
            raise AgentKitValidationError("duplicate canonical stage predecessor")
        self._stage_predecessors = {item.stage: item for item in stage_predecessors}
        self.structure_handoff = structure_handoff
        self.d3_stage_input = d3_stage_input
        self.d3_region_stage_input = d3_region_stage_input
        self.source_requirement_stage_input = source_requirement_stage_input
        self.document_map_scope_stage_input = document_map_scope_stage_input
        self.document_map_replacement_stage_input = (
            document_map_replacement_stage_input
        )
        self._workspace_before = _workspace_manifest(session_root)
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def binding_for_stage(
        self,
        stage: AgentExtractionStage,
    ) -> CanonicalAppServerStageBinding:
        self._require_open()
        try:
            return next(
                item for item in self.bindings if item.attestation.stage is stage
            )
        except StopIteration as exc:
            raise AgentKitValidationError(
                f"stage is not bound to this canonical document session: {stage.value}"
            ) from exc

    def bind_validated_predecessor(
        self,
        binding: CanonicalStagePredecessorBindingV1,
    ) -> None:
        self._require_open()
        if binding.stage not in {item.attestation.stage for item in self.bindings}:
            raise AgentKitValidationError(
                "predecessor target stage is not bound to this session"
            )
        if binding.stage in self._stage_predecessors:
            raise LifecycleProbeError("CANONICAL_STAGE_PREDECESSOR_ALREADY_BOUND")
        if self.document_evidence is None or (
            binding.run_id != self.document_evidence.run_id
            or binding.source_sha256 != self.document_evidence.source_sha256
        ):
            raise LifecycleProbeError("CANONICAL_STAGE_PREDECESSOR_SOURCE_MISMATCH")
        self._stage_predecessors[binding.stage] = binding

    def attest(self) -> CanonicalAppServerDocumentSessionAttestationV1:
        self._require_open()
        current = _workspace_manifest(self.session_root)
        if current != self._workspace_before:
            raise LifecycleProbeError("MODEL_WORKSPACE_MUTATED")
        profile = self.materialized.runner_profile
        d3_closure_sha256 = (
            canonical_sha256(self.d3_policy_closure.model_dump(mode="json"))
            if self.d3_policy_closure is not None
            else None
        )
        payload = {
            "session_id": self.session_id,
            "content_free": True,
            "kit_identity": self.kit.identity,
            "runner_profile_id": _PRODUCT_RUNNER_PROFILE_ID,
            "runner_profile_sha256": self.materialized.runner_profile_sha256,
            "runner_kind": AgentRunnerKind.APP_SERVER_PRODUCT,
            "adapter_id": profile.adapter_id,
            "adapter_version": profile.adapter_version,
            "model_id": profile.model_id,
            "reasoning_effort": profile.reasoning_effort,
            "domain_logic_source": "PORTABLE_AGENT_KIT",
            "exact_bundle_materialized": True,
            "private_runtime_state": True,
            "runtime_assets_copied": False,
            "agent_native_read_capability": self.native_read_capability,
            "native_runtime_policy": self.native_runtime_policy,
            "pdf_primary_runtime": self.pdf_primary_runtime,
            "document_evidence": self.document_evidence,
            "stage_predecessors": tuple(
                self._stage_predecessors[stage]
                for stage in AgentExtractionStage
                if stage in self._stage_predecessors
            ),
            "structure_handoff": self.structure_handoff,
            "d3_stage_input": (
                self.d3_stage_input.attestation
                if self.d3_stage_input is not None
                else None
            ),
            "d3_region_stage_input": (
                self.d3_region_stage_input.attestation
                if self.d3_region_stage_input is not None
                else None
            ),
            "source_requirement_stage_input": (
                self.source_requirement_stage_input.attestation
                if self.source_requirement_stage_input is not None
                else None
            ),
            "document_map_scope_stage_input": (
                self.document_map_scope_stage_input.attestation
                if self.document_map_scope_stage_input is not None
                else None
            ),
            "document_map_replacement_stage_input": (
                self.document_map_replacement_stage_input.attestation
                if self.document_map_replacement_stage_input is not None
                else None
            ),
            "workspace_manifest_sha256": canonical_sha256(current),
            "workspace_manifest_unchanged": True,
            "stages": tuple(item.attestation for item in self.bindings),
            "d3_policy_closure_sha256": d3_closure_sha256,
        }
        serialized = CanonicalAppServerDocumentSessionAttestationV1.model_construct(
            attestation_id="canonical-session-attestation-" + "0" * 32,
            **payload,
        ).model_dump(mode="json", exclude={"attestation_id"})
        return CanonicalAppServerDocumentSessionAttestationV1(
            attestation_id=(
                f"canonical-session-attestation-{canonical_sha256(serialized)[:32]}"
            ),
            **payload,
        )

    def require_inference_stage(
        self,
        stage: AgentExtractionStage,
        *,
        execution_mode: str | None = None,
    ) -> CanonicalAppServerStageActivation:
        binding = self.binding_for_stage(stage)
        if not binding.attestation.inference_available:
            reason = binding.attestation.diagnostic_codes[0]
            raise LifecycleProbeError(reason)
        session_attestation = self.attest()
        if (
            stage
            in {
                AgentExtractionStage.LOGICAL_STRUCTURE,
                AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
            }
            and not session_attestation.agent_native_read_capability.real_transport_eligible
        ):
            reason = session_attestation.agent_native_read_capability.diagnostic_codes[
                0
            ]
            raise LifecycleProbeError(reason)
        predecessor = self._stage_predecessors.get(stage)
        handoff_mode = (
            stage is AgentExtractionStage.LOGICAL_STRUCTURE
            and execution_mode == CANONICAL_STRUCTURE_HANDOFF_EXECUTION_MODE
        )
        source_requirement_mode = (
            stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
            and execution_mode in CANONICAL_SOURCE_REQUIREMENT_EXECUTION_MODES
        )
        document_map_scope_mode = (
            stage is AgentExtractionStage.DOCUMENT_UNDERSTANDING
            and execution_mode == DOCUMENT_MAP_SCOPE_EXECUTION_MODE
        )
        document_map_replacement_mode = (
            stage is AgentExtractionStage.DOCUMENT_UNDERSTANDING
            and execution_mode == DOCUMENT_MAP_SCOPE_REPLACEMENT_EXECUTION_MODE
        )
        if document_map_scope_mode and (
            self.document_map_scope_stage_input is None
            or not session_attestation.agent_native_read_capability.real_transport_eligible
        ):
            raise LifecycleProbeError(
                "CANONICAL_DOCUMENT_MAP_SCOPE_STAGE_INPUT_MISSING"
            )
        if document_map_replacement_mode and (
            self.document_map_replacement_stage_input is None
            or not session_attestation.agent_native_read_capability.real_transport_eligible
        ):
            raise LifecycleProbeError(
                "CANONICAL_DOCUMENT_MAP_REPLACEMENT_STAGE_INPUT_MISSING"
            )
        if (
            stage
            in {
                AgentExtractionStage.LOGICAL_STRUCTURE,
                AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
            }
            and predecessor is None
            and not handoff_mode
            and not source_requirement_mode
        ):
            raise LifecycleProbeError("CANONICAL_STAGE_PREDECESSOR_MISSING")
        if handoff_mode and (self.structure_handoff is None or predecessor is not None):
            raise LifecycleProbeError("CANONICAL_STRUCTURE_HANDOFF_BINDING_MISSING")
        if (
            stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
            and self.d3_stage_input is None
            and self.d3_region_stage_input is None
            and not source_requirement_mode
        ):
            raise LifecycleProbeError("CANONICAL_D3_STAGE_INPUT_MISSING")
        if source_requirement_mode and (
            self.source_requirement_stage_input is None
            or self.source_requirement_stage_input.attestation.execution_mode
            != execution_mode
            or predecessor is not None
        ):
            raise LifecycleProbeError(
                "CANONICAL_SOURCE_REQUIREMENT_STAGE_INPUT_MISSING"
            )
        context = binding.compiled_stage_context
        if context is None:
            raise LifecycleProbeError("CANONICAL_STAGE_CONTEXT_NOT_EXECUTABLE")
        runner_schema = binding.attestation.runner_output_schema
        runner_schema_id = binding.attestation.runner_output_schema_id
        if runner_schema is None or runner_schema_id is None:
            raise LifecycleProbeError("CANONICAL_STAGE_RUNNER_SCHEMA_MISSING")
        if execution_mode is not None:
            variant = next(
                (
                    item
                    for item in binding.attestation.runner_output_schema_variants
                    if item.execution_mode == execution_mode
                ),
                None,
            )
            if variant is None:
                raise LifecycleProbeError(
                    "CANONICAL_STAGE_RUNNER_SCHEMA_VARIANT_NOT_FOUND"
                )
            runner_schema = variant.schema_file
            runner_schema_id = variant.schema_id
        runner_schema_path = _locked_path(self.kit, runner_schema.path)
        return CanonicalAppServerStageActivation(
            runtime=self.runtime,
            stage=stage,
            skill_paths={
                binding.attestation.skill_id: binding.skill_path,
                **dict(binding.capability_skill_paths),
            },
            context_text=binding.context_text,
            context_sha256=binding.attestation.context_sha256,
            artifact_schema_path=context.artifact_schema_path,
            artifact_schema_sha256=context.artifact_schema_sha256,
            runner_output_schema_id=runner_schema_id,
            runner_output_schema_path=runner_schema_path,
            runner_output_schema_sha256=runner_schema.sha256,
            runner_output_execution_mode=execution_mode,
            runner_output_schema_variants_sha256=(
                binding.attestation.runner_output_schema_variants_sha256
            ),
            validator_id=context.validator_id,
            validator_version=context.validator_version,
            session_attestation=session_attestation,
            d3_policy_closure=(
                self.d3_policy_closure
                if stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
                else None
            ),
            evidence_workspace_path=self.evidence_workspace_path,
            visual_input_paths=(
                _visual_input_paths(self.evidence_workspace_path)
                if stage
                in {
                    AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                    AgentExtractionStage.LOGICAL_STRUCTURE,
                    AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
                }
                else ()
            ),
            turn_sandbox_policy=self.turn_sandbox_policy,
            predecessor=predecessor,
            structure_handoff=(self.structure_handoff if handoff_mode else None),
            stage_input_manifest_path=(
                self.document_map_replacement_stage_input.manifest_path
                if document_map_replacement_mode
                and self.document_map_replacement_stage_input is not None
                else (
                    self.document_map_scope_stage_input.manifest_path
                    if document_map_scope_mode
                    and self.document_map_scope_stage_input is not None
                    else (
                        self.source_requirement_stage_input.manifest_path
                        if source_requirement_mode
                        and self.source_requirement_stage_input is not None
                        else (
                            (
                                self.d3_region_stage_input.manifest_path
                                if self.d3_region_stage_input is not None
                                else self.d3_stage_input.manifest_path
                            )
                            if stage
                            is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
                            and (
                                self.d3_stage_input is not None
                                or self.d3_region_stage_input is not None
                            )
                            else None
                        )
                    )
                )
            ),
            d3_stage_input_attestation=(
                self.d3_stage_input.attestation
                if stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
                and self.d3_stage_input is not None
                else None
            ),
            d3_region_stage_input_manifest_path=(
                self.d3_region_stage_input.manifest_path
                if stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
                and self.d3_region_stage_input is not None
                else None
            ),
            d3_region_stage_input_attestation=(
                self.d3_region_stage_input.attestation
                if stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
                and self.d3_region_stage_input is not None
                else None
            ),
            source_requirement_stage_input_attestation=(
                self.source_requirement_stage_input.attestation
                if source_requirement_mode
                and self.source_requirement_stage_input is not None
                else None
            ),
            document_map_scope_stage_input_attestation=(
                self.document_map_scope_stage_input.attestation
                if document_map_scope_mode
                and self.document_map_scope_stage_input is not None
                else None
            ),
            document_map_replacement_stage_input_attestation=(
                self.document_map_replacement_stage_input.attestation
                if document_map_replacement_mode
                and self.document_map_replacement_stage_input is not None
                else None
            ),
        )

    def close(self) -> None:
        if self._closed:
            return
        failure: BaseException | None = None
        try:
            if _workspace_manifest(self.session_root) != self._workspace_before:
                failure = LifecycleProbeError("MODEL_WORKSPACE_MUTATED")
        except BaseException as exc:
            failure = exc
        finally:
            self._closed = True
            _remove_private_session_root(
                self.session_root,
                state_path=self.runtime.state_path,
            )
        if failure is not None:
            raise failure

    def __enter__(self) -> CanonicalAppServerDocumentSession:
        self._require_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise LifecycleProbeError("CANONICAL_DOCUMENT_SESSION_CLOSED")


def prepare_canonical_app_server_document_session(
    *,
    base_runtime: CanaryRuntimeState,
    canonical_kit_root: Path,
    stages: tuple[AgentExtractionStage, ...],
    evidence_request: CanonicalProductEvidenceRequest | None = None,
    stage_predecessors: tuple[CanonicalStagePredecessorBindingV1, ...] = (),
    structure_handoff: CanonicalStructureHandoffBindingV1 | None = None,
    repository_root: Path | None = None,
    d3_stage_input_request: CanonicalD3StageInputRequest | None = None,
    d3_region_stage_input_request: CanonicalD3RegionStageInputRequest | None = None,
    source_requirement_stage_input_request: (
        CanonicalSourceRequirementStageInputRequest | None
    ) = None,
    document_map_scope_stage_input_request: DocumentMapScopeRequestV1 | None = None,
    document_map_replacement_stage_input_request: (
        DocumentMapScopeReplacementRequestV1 | None
    ) = None,
    evidence_workspace_template: Path | None = None,
    primary_runtime_root: Path | None = None,
) -> CanonicalAppServerDocumentSession:
    """Materialize one exact Kit and, when supplied, one locked source plane."""

    if sum(
        value is not None
        for value in (
            d3_stage_input_request,
            d3_region_stage_input_request,
            source_requirement_stage_input_request,
            document_map_scope_stage_input_request,
            document_map_replacement_stage_input_request,
        )
    ) > 1:
        raise AgentKitValidationError(
            "canonical stage-input requests are mutually exclusive"
        )
    if source_requirement_stage_input_request is not None and (
        stages != (AgentExtractionStage.PARENT_PROVISION_RECOGNITION,)
        or evidence_request is None
        or stage_predecessors
        or source_requirement_stage_input_request.source_sha256
        != evidence_request.expected_source_sha256
    ):
        raise AgentKitValidationError(
            "canonical source-requirement stage input binding is invalid"
        )
    if document_map_scope_stage_input_request is not None and (
        stages != (AgentExtractionStage.DOCUMENT_UNDERSTANDING,)
        or evidence_request is None
        or stage_predecessors
        or document_map_scope_stage_input_request.run_id != evidence_request.run_id
        or document_map_scope_stage_input_request.source_sha256
        != evidence_request.expected_source_sha256
    ):
        raise AgentKitValidationError(
            "canonical document-map scope stage input binding is invalid"
        )
    if document_map_replacement_stage_input_request is not None and (
        stages != (AgentExtractionStage.DOCUMENT_UNDERSTANDING,)
        or evidence_request is None
        or stage_predecessors
        or document_map_replacement_stage_input_request.run_id
        != evidence_request.run_id
        or document_map_replacement_stage_input_request.source_sha256
        != evidence_request.expected_source_sha256
    ):
        raise AgentKitValidationError(
            "canonical document-map replacement input binding is invalid"
        )
    if evidence_workspace_template is not None and evidence_request is None:
        raise AgentKitValidationError(
            "canonical evidence workspace template requires a source request"
        )
    if structure_handoff is not None and (
        stages != (AgentExtractionStage.LOGICAL_STRUCTURE,)
        or stage_predecessors
        or evidence_request is None
        or structure_handoff.run_id != evidence_request.run_id
        or structure_handoff.source_sha256 != evidence_request.expected_source_sha256
    ):
        raise AgentKitValidationError("canonical structure handoff binding is invalid")
    _validate_requested_stages(stages)
    _validate_private_runtime(base_runtime)
    canonical_kit = load_portable_agent_kit(canonical_kit_root)
    if (
        canonical_kit.identity.kit_id != CANONICAL_AGENT_KIT_ID
        or canonical_kit.identity.kit_version != CANONICAL_AGENT_KIT_VERSION
    ):
        raise AgentKitValidationError("canonical product Agent Kit identity drifted")
    profile = canonical_kit.runner_profile(_PRODUCT_RUNNER_PROFILE_ID)
    if (
        profile.runner_kind is not AgentRunnerKind.APP_SERVER_PRODUCT
        or profile.domain_logic_source != "PORTABLE_AGENT_KIT"
    ):
        raise AgentKitValidationError("product runner profile is not canonical")
    session_id = f"canonical-document-session-{uuid4().hex}"
    sessions_root = base_runtime.state_path / _PRIVATE_SESSIONS_DIRECTORY
    session_root = sessions_root / session_id
    destination = session_root / "agent-kit"
    materialized: MaterializedAgentKit | None = None
    try:
        materialized = materialize_agent_kit(
            canonical_kit,
            destination,
            runner_profile_id=_PRODUCT_RUNNER_PROFILE_ID,
        )
        sessions_root.chmod(0o700)
        session_root.chmod(0o700)
        kit = load_portable_agent_kit(materialized.root)
        evidence_workspace_path: Path | None = None
        document_evidence: CanonicalDocumentEvidenceAttestationV1 | None = None
        session_runtime = replace(base_runtime, sterile_cwd=materialized.root)
        native_runtime_policy: AgentNativeReadOnlyRuntimePolicyV1 | None = None
        pdf_primary_runtime: OpenAIPrimaryRuntimePdfDependencies | None = None
        turn_sandbox_policy: dict[str, object] | None = None
        native_read_capability = _blocked_native_read_capability()
        if evidence_request is not None:
            evidence_workspace_path = session_root / "evidence-workspace"
            if evidence_workspace_template is None:
                evidence_manifest = materialize_product_evidence_workspace(
                    evidence_request.source_path,
                    expected_source_sha256=evidence_request.expected_source_sha256,
                    document_role=evidence_request.document_role,
                    output_directory=evidence_workspace_path,
                    corpus_root=evidence_request.authorized_corpus_root,
                    repository_root=(
                        repository_root.resolve(strict=True)
                        if repository_root is not None
                        else canonical_kit_root.resolve(strict=True).parent
                    ),
                    run_id=evidence_request.run_id,
                    opaque_source_handle=evidence_request.opaque_source_handle,
                    agent_kit_root=canonical_kit_root,
                )
            else:
                evidence_manifest = _clone_product_evidence_workspace(
                    evidence_workspace_template,
                    destination=evidence_workspace_path,
                    state_path=base_runtime.state_path,
                    expected_source_sha256=(
                        evidence_request.expected_source_sha256
                    ),
                )
            document_evidence = _document_evidence_attestation(
                evidence_manifest,
                request=evidence_request,
                kit_identity=kit.identity,
            )
            requires_pdf_runtime = any(
                capability.skill_id == "pdf"
                for stage in stages
                for capability in kit.capability_skills_for_stage(stage)
            )
            if requires_pdf_runtime:
                provenance = kit.skill_provenance.get("pdf")
                if provenance is None:
                    raise AgentKitValidationError(
                        "canonical PDF capability provenance is missing"
                    )
                pdf_primary_runtime = (
                    discover_openai_primary_runtime_pdf_dependencies(
                        provenance=provenance,
                        runtime_root=primary_runtime_root,
                    )
                )
            if protocol_supports_restricted_read_access(session_runtime):
                configured = configure_agent_native_read_only_runtime(
                    session_runtime,
                    readable_root=session_root,
                    pdf_dependencies=pdf_primary_runtime,
                )
                session_runtime = configured.runtime
                native_runtime_policy = configured.policy
                turn_sandbox_policy = dict(configured.turn_sandbox_policy)
                native_read_capability = _attested_native_read_capability()
            else:
                native_read_capability = _unsupported_restricted_read_capability()
        d3_stage_input: MaterializedD3StageInputWorkspace | None = None
        d3_region_stage_input: MaterializedD3RegionStageInputWorkspace | None = None
        source_requirement_stage_input: (
            MaterializedCanonicalSourceRequirementStageInput | None
        ) = None
        document_map_scope_stage_input: (
            MaterializedCanonicalDocumentMapScopeStageInput | None
        ) = None
        document_map_replacement_stage_input: (
            MaterializedCanonicalDocumentMapReplacementStageInput | None
        ) = None
        if d3_stage_input_request is not None:
            predecessor = next(
                (
                    item
                    for item in stage_predecessors
                    if item.stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
                ),
                None,
            )
            if predecessor is None:
                raise AgentKitValidationError(
                    "D3 stage input requires one canonical D2 predecessor"
                )
            d3_stage_input = materialize_d3_stage_input_workspace(
                session_root=session_root,
                d2_proposal=d3_stage_input_request.d2_proposal,
                d2_validation_receipt=(d3_stage_input_request.d2_validation_receipt),
                source_segments=d3_stage_input_request.source_segments,
                predecessor=D3StageInputPredecessorBindingV1.model_validate(
                    predecessor.model_dump(mode="json")
                ),
            )
        if d3_region_stage_input_request is not None:
            predecessor = next(
                (
                    item
                    for item in stage_predecessors
                    if item.stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
                ),
                None,
            )
            if predecessor is None:
                raise AgentKitValidationError(
                    "region D3 stage input requires one canonical D2 predecessor"
                )
            _validate_canonical_d3_region_stage_input_request(
                d3_region_stage_input_request,
                predecessor=predecessor,
                expected_kit_identity=kit.identity,
            )
            if document_evidence is not None and (
                document_evidence.run_id
                != d3_region_stage_input_request.d2_proposal.run_id
                or document_evidence.source_sha256
                != d3_region_stage_input_request.d2_proposal.source_sha256
            ):
                raise AgentKitValidationError(
                    "region D3 stage input differs from document evidence"
                )
            d3_region_stage_input = materialize_d3_region_stage_input_workspace(
                session_root=session_root,
                source_ir=d3_region_stage_input_request.source_ir,
                d2_proposal=d3_region_stage_input_request.d2_proposal,
                d2_validation_receipt=(
                    d3_region_stage_input_request.d2_validation_receipt
                ),
                parent_input=d3_region_stage_input_request.parent_input,
                parent_input_receipt=(
                    d3_region_stage_input_request.parent_input_receipt
                ),
                expected_lineage=d3_region_stage_input_request.expected_lineage,
            )
        if source_requirement_stage_input_request is not None:
            source_requirement_stage_input = (
                _materialize_source_requirement_stage_input(
                    session_root=session_root,
                    request=source_requirement_stage_input_request,
                )
            )
        if document_map_scope_stage_input_request is not None:
            if document_evidence is None or (
                document_map_scope_stage_input_request.source_ir_id
                != document_evidence.source_ir_id
                or document_map_scope_stage_input_request.source_ir_canonical_sha256
                != document_evidence.source_ir_canonical_sha256
                or document_map_scope_stage_input_request.evidence_workspace_bundle_sha256
                != document_evidence.workspace_bundle_sha256
            ):
                raise AgentKitValidationError(
                    "document-map scope stage input differs from document evidence"
                )
            document_map_scope_stage_input = (
                materialize_document_map_scope_stage_input(
                    session_root=session_root,
                    request=document_map_scope_stage_input_request,
                )
            )
        if document_map_replacement_stage_input_request is not None:
            replacement_scope = (
                document_map_replacement_stage_input_request.scope_request
            )
            if document_evidence is None or (
                replacement_scope.source_ir_id != document_evidence.source_ir_id
                or replacement_scope.source_ir_canonical_sha256
                != document_evidence.source_ir_canonical_sha256
                or replacement_scope.evidence_workspace_bundle_sha256
                != document_evidence.workspace_bundle_sha256
            ):
                raise AgentKitValidationError(
                    "document-map replacement input differs from document evidence"
                )
            document_map_replacement_stage_input = (
                materialize_document_map_replacement_stage_input(
                    session_root=session_root,
                    request=document_map_replacement_stage_input_request,
                )
            )
        bindings = tuple(
            compile_canonical_app_server_stage_binding(kit, stage) for stage in stages
        )
        d3_policy_closure = (
            compile_d3_parent_provision_policy_closure(kit)
            if AgentExtractionStage.PARENT_PROVISION_RECOGNITION in stages
            else None
        )
        return CanonicalAppServerDocumentSession(
            runtime=session_runtime,
            session_id=session_id,
            session_root=session_root,
            materialized=materialized,
            kit=kit,
            bindings=bindings,
            d3_policy_closure=d3_policy_closure,
            native_read_capability=native_read_capability,
            native_runtime_policy=native_runtime_policy,
            pdf_primary_runtime=(
                pdf_primary_runtime.attestation
                if pdf_primary_runtime is not None
                else None
            ),
            document_evidence=document_evidence,
            evidence_workspace_path=evidence_workspace_path,
            turn_sandbox_policy=turn_sandbox_policy,
            stage_predecessors=stage_predecessors,
            structure_handoff=structure_handoff,
            d3_stage_input=d3_stage_input,
            d3_region_stage_input=d3_region_stage_input,
            source_requirement_stage_input=source_requirement_stage_input,
            document_map_scope_stage_input=document_map_scope_stage_input,
            document_map_replacement_stage_input=(
                document_map_replacement_stage_input
            ),
        )
    except BaseException:
        if session_root.exists():
            _remove_private_session_root(
                session_root,
                state_path=base_runtime.state_path,
            )
        raise


def compile_canonical_app_server_stage_binding(
    kit: PortableAgentKit,
    stage: AgentExtractionStage,
) -> CanonicalAppServerStageBinding:
    stage_spec = _stage_spec(kit, stage)
    if stage_spec.owner == "HOST" or stage_spec.skill_id is None:
        raise AgentKitValidationError("canonical App Server stages must be Agent-owned")
    skill_spec = next(
        (item for item in kit.manifest.skills if item.skill_id == stage_spec.skill_id),
        None,
    )
    if skill_spec is None or skill_spec.stage is not stage:
        raise AgentKitValidationError("canonical stage Skill identity drifted")
    skill_path = _locked_path(kit, skill_spec.path)
    skill_digest = CanonicalStageFileDigestV1(
        path=skill_spec.path,
        sha256=_sha256(skill_path),
    )
    capability_skill_paths: list[tuple[str, Path]] = []
    capability_skills: list[CanonicalStageCapabilitySkillAttestationV1] = []
    for capability in kit.capability_skills_for_stage(stage):
        capability_path = _locked_path(kit, capability.path)
        skill_file = CanonicalStageFileDigestV1(
            path=capability.path,
            sha256=_sha256(capability_path),
        )
        values = {
            "skill_id": capability.skill_id,
            "skill_file": skill_file,
        }
        draft = CanonicalStageCapabilitySkillAttestationV1.model_construct(
            **values,
            attestation_sha256="0" * 64,
        )
        capability_skills.append(
            CanonicalStageCapabilitySkillAttestationV1(
                **values,
                attestation_sha256=canonical_sha256(
                    draft.model_dump(
                        mode="json",
                        exclude={"attestation_sha256"},
                    )
                ),
            )
        )
        capability_skill_paths.append((capability.skill_id, capability_path))
    policy_path = _locked_path(kit, kit.manifest.policy_path)
    policy_digest = CanonicalStageFileDigestV1(
        path=kit.manifest.policy_path,
        sha256=_sha256(policy_path),
    )
    references = tuple(
        CanonicalStageFileDigestV1(
            path=relative,
            sha256=_sha256(_locked_path(kit, relative)),
        )
        for relative in stage_spec.reference_paths
    )
    runner_output_schema = (
        CanonicalStageFileDigestV1(
            path=stage_spec.runner_output_schema_path,
            sha256=_sha256(_locked_path(kit, stage_spec.runner_output_schema_path)),
        )
        if stage_spec.runner_output_schema_path is not None
        else None
    )
    runner_output_schema_variants = tuple(
        _compile_runner_output_schema_variant(kit, variant)
        for variant in stage_spec.runner_output_schema_variants
    )
    runner_output_schema_variants_sha256 = canonical_sha256(
        [item.model_dump(mode="json") for item in runner_output_schema_variants]
    )
    required_tools = tuple(
        _compile_required_tool_attestation(kit, tool)
        for tool in kit.required_tools_for_stage(stage)
    )
    stage_contract_diagnostics = _stage_contract_diagnostics(stage_spec)
    diagnostics = tuple(
        sorted(
            {
                *stage_contract_diagnostics,
                *_required_tool_diagnostics(required_tools),
            }
        )
    )
    compiled: CompiledAgentStageContext | None = None
    contract_complete = (
        stage_spec.artifact_schema_path is not None
        and stage_spec.validator_id is not None
        and stage_spec.validator_version is not None
    )
    if not stage_contract_diagnostics:
        compiled = compile_agent_stage_context(kit, stage)
        context_text = compiled.context_text
        context_sha256 = compiled.context_sha256
        artifact_schema = CanonicalStageFileDigestV1(
            path=stage_spec.artifact_schema_path or "",
            sha256=compiled.artifact_schema_sha256,
        )
        context_kind = "EXECUTABLE_STAGE_CONTEXT"
    else:
        context_text, context_sha256 = _compile_policy_closure_context(
            kit=kit,
            stage_spec=stage_spec,
            skill=skill_digest,
            policy=policy_digest,
            references=references,
        )
        artifact_schema = (
            CanonicalStageFileDigestV1(
                path=stage_spec.artifact_schema_path,
                sha256=_sha256(_locked_path(kit, stage_spec.artifact_schema_path)),
            )
            if contract_complete and stage_spec.artifact_schema_path is not None
            else None
        )
        context_kind = "POLICY_CLOSURE_ONLY"
    attestation = CanonicalAppServerStageAttestationV1(
        stage=stage,
        implementation_status=stage_spec.implementation_status,
        context_kind=context_kind,
        skill_id=stage_spec.skill_id,
        skill=skill_digest,
        capability_skills=tuple(capability_skills),
        policy=policy_digest,
        references=references,
        context_sha256=context_sha256,
        artifact_schema_id=stage_spec.artifact_schema_id,
        artifact_schema=artifact_schema,
        runner_output_schema_id=stage_spec.runner_output_schema_id,
        runner_output_schema=runner_output_schema,
        runner_output_schema_variants=runner_output_schema_variants,
        runner_output_schema_variants_sha256=(runner_output_schema_variants_sha256),
        validator_id=stage_spec.validator_id if contract_complete else None,
        validator_version=(stage_spec.validator_version if contract_complete else None),
        required_tools=required_tools,
        inference_available=not diagnostics,
        diagnostic_codes=diagnostics,
    )
    return CanonicalAppServerStageBinding(
        attestation=attestation,
        skill_path=skill_path,
        capability_skill_paths=tuple(capability_skill_paths),
        context_text=context_text,
        compiled_stage_context=compiled,
    )


def _validate_canonical_d3_region_stage_input_request(
    request: CanonicalD3RegionStageInputRequest,
    *,
    predecessor: CanonicalStagePredecessorBindingV1,
    expected_kit_identity: AgentKitIdentityV1,
) -> None:
    try:
        proposal = D2LogicalStructureProposalV2.model_validate(
            request.d2_proposal.model_dump(mode="json")
        )
        receipt = D2LogicalStructureValidationReceiptV2.model_validate(
            request.d2_validation_receipt.model_dump(mode="json")
        )
        parent_input = D2TableRegionParentInputV1.model_validate(
            request.parent_input.model_dump(mode="json")
        )
        parent_receipt = D2TableRegionParentInputReceiptV1.model_validate(
            request.parent_input_receipt.model_dump(mode="json")
        )
        lineage = D3RegionStageInputLineageV1.model_validate(
            request.expected_lineage.model_dump(mode="json")
        )
    except (TypeError, ValueError) as exc:
        raise AgentKitValidationError(
            "canonical region D3 stage-input request is invalid"
        ) from exc
    expected_status = (
        AgentStageArtifactStatus.VALIDATED
        if proposal.status is D2LogicalStructureStatus.COMPLETE
        else AgentStageArtifactStatus.PARTIAL
    )
    if (
        predecessor.proposal_schema_id
        != D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION
        or predecessor.run_id != proposal.run_id
        or predecessor.source_sha256 != proposal.source_sha256
        or predecessor.artifact_id != receipt.artifact_id
        or predecessor.artifact_sha256 != receipt.artifact_sha256
        or predecessor.validator_receipt_sha256 != receipt.receipt_sha256
        or predecessor.status is not expected_status
        or proposal.kit_identity != expected_kit_identity
        or parent_input.kit_bundle_sha256 != expected_kit_identity.bundle_sha256
        or lineage.run_id != proposal.run_id
        or lineage.source_ir_id != proposal.source_ir_id
        or lineage.source_sha256 != proposal.source_sha256
        or lineage.source_ir_canonical_sha256 != proposal.source_ir_canonical_sha256
        or lineage.kit_bundle_sha256 != expected_kit_identity.bundle_sha256
        or lineage.v2_artifact_id != receipt.artifact_id
        or lineage.v2_artifact_sha256 != receipt.artifact_sha256
        or lineage.v2_validation_receipt_sha256 != receipt.receipt_sha256
        or lineage.parent_input_id != parent_input.stage_input_id
        or lineage.parent_input_sha256 != parent_input.stage_input_sha256
        or lineage.parent_input_receipt_sha256 != parent_receipt.receipt_sha256
    ):
        raise AgentKitValidationError("canonical region D3 stage-input lineage drifted")


def _compile_runner_output_schema_variant(
    kit: PortableAgentKit,
    variant: AgentKitRunnerOutputSchemaVariantV1,
) -> CanonicalStageRunnerOutputSchemaVariantV1:
    schema_path = _locked_path(kit, variant.schema_path)
    schema_file = CanonicalStageFileDigestV1(
        path=variant.schema_path,
        sha256=_sha256(schema_path),
    )
    values = {
        "content_free": True,
        "execution_mode": variant.execution_mode,
        "schema_id": variant.schema_id,
        "schema_file": schema_file,
    }
    return CanonicalStageRunnerOutputSchemaVariantV1(
        **values,
        variant_sha256=canonical_sha256(
            {
                **values,
                "schema_file": schema_file.model_dump(mode="json"),
            }
        ),
    )


def _stage_spec(
    kit: PortableAgentKit,
    stage: AgentExtractionStage,
) -> AgentKitStageSpecV1:
    try:
        return next(item for item in kit.manifest.stages if item.stage is stage)
    except StopIteration as exc:
        raise AgentKitValidationError("canonical Agent Kit stage is missing") from exc


def _stage_contract_diagnostics(
    stage_spec: AgentKitStageSpecV1,
) -> tuple[str, ...]:
    diagnostics: set[str] = set()
    if stage_spec.implementation_status != "AVAILABLE":
        diagnostics.add("CANONICAL_STAGE_IMPLEMENTATION_PLANNED")
    if stage_spec.artifact_schema_path is None:
        diagnostics.add("CANONICAL_STAGE_ARTIFACT_SCHEMA_MISSING")
    if stage_spec.validator_id is None or stage_spec.validator_version is None:
        diagnostics.add("CANONICAL_STAGE_VALIDATOR_MISSING")
    return tuple(sorted(diagnostics))


def _compile_required_tool_attestation(
    kit: PortableAgentKit,
    tool: AgentKitToolSpecV2,
) -> CanonicalStageToolAttestationV1:
    input_schema = (
        CanonicalStageFileDigestV1(
            path=tool.input_schema_path,
            sha256=_sha256(_locked_path(kit, tool.input_schema_path)),
        )
        if tool.input_schema_path is not None
        else None
    )
    output_schema = (
        CanonicalStageFileDigestV1(
            path=tool.output_schema_path,
            sha256=_sha256(_locked_path(kit, tool.output_schema_path)),
        )
        if tool.output_schema_path is not None
        else None
    )
    return CanonicalStageToolAttestationV1(
        name=tool.name,
        implementation_status=tool.implementation_status,
        implementation_id=tool.implementation_id,
        implementation_version=tool.implementation_version,
        input_schema=input_schema,
        output_schema=output_schema,
    )


def _required_tool_diagnostics(
    tools: tuple[CanonicalStageToolAttestationV1, ...],
) -> tuple[str, ...]:
    diagnostics: set[str] = set()
    if any(item.implementation_status != "AVAILABLE" for item in tools):
        diagnostics.add("CANONICAL_STAGE_REQUIRED_TOOL_PLANNED")
    if any(
        item.implementation_id is None
        or item.implementation_version is None
        or item.input_schema is None
        or item.output_schema is None
        for item in tools
    ):
        diagnostics.add("CANONICAL_STAGE_REQUIRED_TOOL_CONTRACT_MISSING")
    return tuple(sorted(diagnostics))


def _compile_policy_closure_context(
    *,
    kit: PortableAgentKit,
    stage_spec: AgentKitStageSpecV1,
    skill: CanonicalStageFileDigestV1,
    policy: CanonicalStageFileDigestV1,
    references: tuple[CanonicalStageFileDigestV1, ...],
) -> tuple[str, str]:
    sections = [
        _POLICY_CONTEXT_HEADER,
        f"stage={stage_spec.stage.value}",
        f"kit_id={kit.identity.kit_id}",
        f"kit_version={kit.identity.kit_version}",
        f"kit_bundle_sha256={kit.identity.bundle_sha256}",
        f"implementation_status={stage_spec.implementation_status}",
        f"skill_id={stage_spec.skill_id}",
        f"skill_sha256={skill.sha256}",
        "inference_authority=NONE_UNTIL_STAGE_AVAILABLE",
        "",
        f"BEGIN POLICY {policy.path} {policy.sha256}",
        _locked_path(kit, policy.path).read_text(encoding="utf-8"),
        "END POLICY",
    ]
    for reference in references:
        sections.extend(
            [
                "",
                f"BEGIN REFERENCE {reference.path} {reference.sha256}",
                _locked_path(kit, reference.path).read_text(encoding="utf-8"),
                "END REFERENCE",
            ]
        )
    context_text = "\n".join(sections).rstrip() + "\n"
    context_sha256 = canonical_sha256(
        {
            "stage": stage_spec.stage.value,
            "kit_bundle_sha256": kit.identity.bundle_sha256,
            "implementation_status": stage_spec.implementation_status,
            "skill": skill.model_dump(mode="json"),
            "policy": policy.model_dump(mode="json"),
            "references": [item.model_dump(mode="json") for item in references],
            "context_text": context_text,
        }
    )
    return context_text, context_sha256


def _locked_path(kit: PortableAgentKit, relative: str) -> Path:
    locked = {item.path: item.sha256 for item in kit.lock.files}
    expected = locked.get(relative)
    if expected is None:
        raise AgentKitValidationError("canonical stage file is not locked")
    path = kit.root / relative
    if path.is_symlink() or not path.is_file():
        raise AgentKitValidationError("canonical stage file must be regular")
    if _sha256(path) != expected:
        raise AgentKitValidationError("canonical stage file drifted from the lock")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_requested_stages(stages: tuple[AgentExtractionStage, ...]) -> None:
    if not stages:
        raise AgentKitValidationError("canonical document session requires stages")
    indices = tuple(tuple(AgentExtractionStage).index(item) for item in stages)
    if indices != tuple(sorted(set(indices))):
        raise AgentKitValidationError(
            "canonical document session stages must be unique and ordered"
        )
    if any(stage is AgentExtractionStage.SOURCE_PREFLIGHT for stage in stages):
        raise AgentKitValidationError("Host-owned D0 cannot run in App Server")


def _validate_private_runtime(runtime: CanaryRuntimeState) -> None:
    state_path = runtime.state_path.resolve(strict=True)
    if runtime.state_path.is_symlink() or not state_path.is_dir():
        raise AgentKitValidationError("App Server runtime state must be private")
    try:
        runtime.sterile_cwd.resolve(strict=True).relative_to(state_path)
    except (FileNotFoundError, ValueError) as exc:
        raise AgentKitValidationError(
            "App Server workspace must stay inside private runtime state"
        ) from exc
    if runtime.sterile_cwd.is_symlink() or not runtime.sterile_cwd.is_dir():
        raise AgentKitValidationError("App Server workspace must be a real directory")


def _clone_product_evidence_workspace(
    template: Path,
    *,
    destination: Path,
    state_path: Path,
    expected_source_sha256: str,
) -> dict[str, Any]:
    resolved_state = state_path.resolve(strict=True)
    candidate = template.expanduser()
    if candidate.is_symlink():
        raise LifecycleProbeError("CANONICAL_EVIDENCE_TEMPLATE_INVALID")
    try:
        resolved_template = candidate.resolve(strict=True)
        resolved_template.relative_to(resolved_state)
    except (FileNotFoundError, ValueError) as exc:
        raise LifecycleProbeError("CANONICAL_EVIDENCE_TEMPLATE_INVALID") from exc
    if not resolved_template.is_dir() or destination.exists():
        raise LifecycleProbeError("CANONICAL_EVIDENCE_TEMPLATE_INVALID")
    lock_path = resolved_template / "workspace.lock.json"
    if lock_path.is_symlink() or not lock_path.is_file():
        raise LifecycleProbeError("CANONICAL_EVIDENCE_TEMPLATE_INVALID")
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleProbeError("CANONICAL_EVIDENCE_TEMPLATE_INVALID") from exc
    bundle_sha256 = str(lock.get("bundle_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", bundle_sha256):
        raise LifecycleProbeError("CANONICAL_EVIDENCE_TEMPLATE_INVALID")
    verify_reference_workspace(
        resolved_template,
        expected_source_sha256=expected_source_sha256,
        expected_bundle_sha256=bundle_sha256,
    )
    try:
        shutil.copytree(resolved_template, destination, copy_function=shutil.copy2)
        verify_reference_workspace(
            destination,
            expected_source_sha256=expected_source_sha256,
            expected_bundle_sha256=bundle_sha256,
        )
        manifest = json.loads(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )
        if not isinstance(manifest, dict):
            raise LifecycleProbeError("CANONICAL_EVIDENCE_TEMPLATE_INVALID")
        return {**manifest, "workspace_bundle_sha256": bundle_sha256}
    except BaseException:
        if destination.exists():
            shutil.rmtree(destination)
        raise


def _remove_private_session_root(session_root: Path, *, state_path: Path) -> None:
    resolved_state = state_path.resolve(strict=True)
    try:
        resolved_parent = session_root.parent.resolve(strict=True)
        resolved_parent.relative_to(resolved_state)
    except (FileNotFoundError, ValueError) as exc:
        raise LifecycleProbeError("CANONICAL_SESSION_CLEANUP_TARGET_INVALID") from exc
    if session_root.is_symlink():
        raise LifecycleProbeError("CANONICAL_SESSION_CLEANUP_TARGET_INVALID")
    if session_root.exists():
        shutil.rmtree(session_root)
    sessions_root = resolved_state / _PRIVATE_SESSIONS_DIRECTORY
    if sessions_root.is_dir() and not any(sessions_root.iterdir()):
        try:
            sessions_root.rmdir()
        except FileNotFoundError:
            # A sibling App Server window removed the shared empty parent first.
            pass
        except OSError as exc:
            if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                raise


def _document_evidence_attestation(
    manifest: dict[str, object],
    *,
    request: CanonicalProductEvidenceRequest,
    kit_identity: AgentKitIdentityV1,
) -> CanonicalDocumentEvidenceAttestationV1:
    run = manifest.get("run")
    document = manifest.get("document")
    capture = manifest.get("capture")
    authorization = manifest.get("authorization")
    if not all(
        isinstance(item, dict) for item in (run, document, capture, authorization)
    ):
        raise LifecycleProbeError("CANONICAL_DOCUMENT_EVIDENCE_INVALID")
    assert isinstance(run, dict)
    assert isinstance(document, dict)
    assert isinstance(capture, dict)
    assert isinstance(authorization, dict)
    if (
        manifest.get("workspace_kind") != "APP_SERVER_PRODUCT_READ_ONLY"
        or run.get("run_id") != request.run_id
        or run.get("opaque_source_handle") != request.opaque_source_handle
        or document.get("source_sha256") != request.expected_source_sha256
        or document.get("role") != request.document_role
        or capture.get("control_kit_bundle_sha256") != kit_identity.bundle_sha256
        or authorization.get("source_is_inside_authorized_corpus") is not True
    ):
        raise LifecycleProbeError("CANONICAL_DOCUMENT_EVIDENCE_IDENTITY_MISMATCH")
    try:
        return CanonicalDocumentEvidenceAttestationV1(
            run_id=request.run_id,
            opaque_source_handle=request.opaque_source_handle,
            source_sha256=request.expected_source_sha256,
            document_role=request.document_role,
            page_count=document["page_count"],
            source_ir_id=capture["source_ir_id"],
            source_ir_canonical_sha256=capture["source_ir_canonical_sha256"],
            workspace_bundle_sha256=manifest["workspace_bundle_sha256"],
            evidence_inventory_sha256=manifest["evidence_inventory_sha256"],
            source_authorized=True,
            customer_content_redacted=False,
        )
    except Exception as exc:
        raise LifecycleProbeError("CANONICAL_DOCUMENT_EVIDENCE_INVALID") from exc


def _materialize_source_requirement_stage_input(
    *,
    session_root: Path,
    request: CanonicalSourceRequirementStageInputRequest,
) -> MaterializedCanonicalSourceRequirementStageInput:
    if (
        request.execution_mode not in CANONICAL_SOURCE_REQUIREMENT_EXECUTION_MODES
        or len(request.source_sha256) != 64
        or not request.document_map_artifact_id.startswith("document-map-")
        or len(request.document_map_artifact_sha256) != 64
        or len(request.request_sha256) != 64
        or not request.scope_id
    ):
        raise AgentKitValidationError(
            "canonical source-requirement stage input identity is invalid"
        )
    allowed_names = {
        "document-context.json",
        "scope.json",
        "document-map.full.json",
        "scope.full.json",
        "phase-input.json",
        "previous-candidate.json",
        "revision-guidance.json",
        "validation-feedback.json",
    }
    names = tuple(name for name, _ in request.json_files)
    if (
        not names
        or len(names) != len(set(names))
        or any(name not in allowed_names for name in names)
    ):
        raise AgentKitValidationError(
            "canonical source-requirement stage input files are invalid"
        )
    root = session_root / "source-requirement-stage-input"
    root.mkdir(mode=0o700)
    files: list[CanonicalSourceRequirementInputFileV1] = []
    for name, payload in sorted(request.json_files, key=lambda item: item[0]):
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        path = root / name
        with path.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o400)
        files.append(
            CanonicalSourceRequirementInputFileV1(
                path=name,
                sha256=hashlib.sha256(encoded).hexdigest(),
            )
        )
    values = {
        "execution_mode": request.execution_mode,
        "source_sha256": request.source_sha256,
        "document_map_artifact_id": request.document_map_artifact_id,
        "document_map_artifact_sha256": request.document_map_artifact_sha256,
        "request_sha256": request.request_sha256,
        "scope_id": request.scope_id,
        "files": tuple(files),
    }
    draft = CanonicalSourceRequirementStageInputAttestationV1.model_construct(
        **values,
        manifest_sha256="0" * 64,
    )
    attestation = CanonicalSourceRequirementStageInputAttestationV1(
        **values,
        manifest_sha256=canonical_sha256(
            draft.model_dump(mode="json", exclude={"manifest_sha256"})
        ),
    )
    manifest_path = root / "manifest.json"
    encoded_manifest = json.dumps(
        attestation.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with manifest_path.open("xb") as stream:
        stream.write(encoded_manifest)
        stream.flush()
        os.fsync(stream.fileno())
    manifest_path.chmod(0o400)
    return MaterializedCanonicalSourceRequirementStageInput(
        root=root,
        manifest_path=manifest_path,
        attestation=attestation,
    )


def _visual_input_paths(evidence_workspace: Path | None) -> tuple[Path, ...]:
    if evidence_workspace is None:
        return ()
    contact_sheet = evidence_workspace / "contact-sheet.png"
    if contact_sheet.is_symlink() or not contact_sheet.is_file():
        raise LifecycleProbeError("CANONICAL_DOCUMENT_VISUAL_INPUT_MISSING")
    return (contact_sheet.resolve(strict=True),)


def _blocked_native_read_capability() -> CanonicalAgentNativeReadCapabilityV1:
    """The lifecycle canary runtime disables native reads and proves no root jail."""

    return CanonicalAgentNativeReadCapabilityV1(
        status="BLOCKED",
        shell_tool_enabled=False,
        unified_exec_enabled=False,
        view_image_enabled=False,
        local_image_input_enabled=False,
        session_root_read_confinement_attested=False,
        network_access_enabled=False,
        filesystem_write_enabled=False,
        real_transport_eligible=False,
        diagnostic_codes=(
            "CANONICAL_NATIVE_READ_LIFECYCLE_FEATURES_DISABLED",
            "CANONICAL_NATIVE_READ_ROOT_CONFINEMENT_UNATTESTED",
        ),
    )


def _attested_native_read_capability() -> CanonicalAgentNativeReadCapabilityV1:
    return CanonicalAgentNativeReadCapabilityV1(
        status="ATTESTED",
        shell_tool_enabled=True,
        unified_exec_enabled=True,
        view_image_enabled=True,
        local_image_input_enabled=True,
        session_root_read_confinement_attested=True,
        network_access_enabled=False,
        filesystem_write_enabled=False,
        real_transport_eligible=True,
        diagnostic_codes=(),
    )


def _unsupported_restricted_read_capability() -> CanonicalAgentNativeReadCapabilityV1:
    return CanonicalAgentNativeReadCapabilityV1(
        status="BLOCKED",
        shell_tool_enabled=False,
        unified_exec_enabled=False,
        view_image_enabled=False,
        local_image_input_enabled=True,
        session_root_read_confinement_attested=False,
        network_access_enabled=False,
        filesystem_write_enabled=False,
        real_transport_eligible=False,
        diagnostic_codes=(
            "CANONICAL_NATIVE_READ_RESTRICTED_ROOT_UNSUPPORTED",
            "CANONICAL_NATIVE_READ_ROOT_CONFINEMENT_UNATTESTED",
        ),
    )


__all__ = [
    "CANONICAL_AGENT_KIT_ID",
    "CANONICAL_AGENT_KIT_VERSION",
    "CANONICAL_STRUCTURE_HANDOFF_EXECUTION_MODE",
    "CANONICAL_SOURCE_REQUIREMENT_EXECUTION_MODES",
    "CANONICAL_APP_SERVER_SESSION_ATTESTATION_SCHEMA_VERSION",
    "CanonicalAgentNativeReadCapabilityV1",
    "CanonicalDocumentEvidenceAttestationV1",
    "CanonicalD3RegionStageInputRequest",
    "CanonicalD3StageInputRequest",
    "CanonicalProductEvidenceRequest",
    "CanonicalSourceRequirementStageInputAttestationV1",
    "CanonicalSourceRequirementStageInputRequest",
    "CanonicalStagePredecessorBindingV1",
    "CanonicalStructureHandoffBindingV1",
    "CanonicalAppServerDocumentSession",
    "CanonicalAppServerDocumentSessionAttestationV1",
    "CanonicalAppServerStageActivation",
    "CanonicalAppServerStageAttestationV1",
    "CanonicalAppServerStageBinding",
    "CanonicalStageDiagnosticCode",
    "CanonicalStageCapabilitySkillAttestationV1",
    "CanonicalNativeReadCapabilityDiagnostic",
    "CanonicalStageFileDigestV1",
    "CanonicalStageRunnerOutputSchemaVariantV1",
    "MaterializedCanonicalSourceRequirementStageInput",
    "compile_canonical_app_server_stage_binding",
    "prepare_canonical_app_server_document_session",
]
