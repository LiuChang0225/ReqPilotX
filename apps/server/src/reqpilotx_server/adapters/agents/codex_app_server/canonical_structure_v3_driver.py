from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from pydantic import ValidationError

from reqpilotx_server.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAppServerStageActivation,
)
from reqpilotx_server.adapters.agents.codex_app_server.canonical_dynamic_tools import (
    CanonicalDynamicToolSpec,
)
from reqpilotx_server.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageExecutionReceiptV1,
    CanonicalStageSessionPort,
    CanonicalStructuredStageTransportPort,
    _build_execution_receipt,
    _candidate_validation_reason,
    _host_compilation_reason,
    _invoke_transport,
    _load_runner_output_schema,
    build_canonical_stage_invocation,
)
from reqpilotx_server.adapters.agents.portable_kit import PortableAgentKit
from reqpilotx_server.application.agent_workflow.d2_decision_candidate_v3 import (
    D2_LOGICAL_STRUCTURE_DECISION_CANDIDATE_V3_SCHEMA_VERSION,
    D2LogicalStructureCompilationV3,
    D2LogicalStructureDecisionCandidateV3,
)
from reqpilotx_server.application.agent_workflow.d2_logical_structure import (
    D2LogicalStructureStatus,
    D2ValidatedD1ArtifactIdentityV1,
)
from reqpilotx_server.application.agent_workflow.d2_logical_structure_v2 import (
    D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION,
    D2_LOGICAL_STRUCTURE_V2_VALIDATOR_ID,
    D2_LOGICAL_STRUCTURE_V2_VALIDATOR_VERSION,
    D2LogicalStructureProposalV2,
    D2LogicalStructureValidationReceiptV2,
)
from reqpilotx_server.application.agent_workflow.d2_table_regions import (
    d2_table_region_source_ir_canonical_sha256,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import (
    AgentExtractionStage,
    AgentStageArtifactStatus,
    AgentStageArtifactV1,
    AgentStageFinalReceiptV1,
)
from reqpilotx_server.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
)
from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot
from reqpilotx_server.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError


CANONICAL_D2_V3_STAGE_VALIDATOR_ID = D2_LOGICAL_STRUCTURE_V2_VALIDATOR_ID
CANONICAL_D2_V3_STAGE_VALIDATOR_VERSION = D2_LOGICAL_STRUCTURE_V2_VALIDATOR_VERSION


class D2CanonicalV3CandidateCompilerPort(Protocol):
    source_ir: SourceIRSnapshot
    expected_d1_artifact: D2ValidatedD1ArtifactIdentityV1
    expected_d1_table_map_artifact: AgentD1TableMapArtifactIdentityV1
    kit: PortableAgentKit

    def compile(
        self,
        candidate: D2LogicalStructureDecisionCandidateV3,
    ) -> D2LogicalStructureCompilationV3: ...


@dataclass(frozen=True, slots=True)
class D2CanonicalV3StageResult:
    decision_candidate: D2LogicalStructureDecisionCandidateV3
    compilation: D2LogicalStructureCompilationV3
    proposal: D2LogicalStructureProposalV2
    host_validation_receipt: D2LogicalStructureValidationReceiptV2
    stage_artifact: AgentStageArtifactV1
    final_receipt: AgentStageFinalReceiptV1
    execution_receipt: CanonicalStageExecutionReceiptV1
    tool_activity_summary: Mapping[str, Any]


class D2CanonicalV3StageDriver:
    """Explicit v3/v2 logical-structure vertical slice.

    The existing canonical logical-structure driver remains unchanged. A
    caller must opt into this class until the primary serving path is switched.
    """

    def __init__(
        self,
        *,
        transport: CanonicalStructuredStageTransportPort,
        candidate_compiler: D2CanonicalV3CandidateCompilerPort,
        dynamic_tool_specs: tuple[CanonicalDynamicToolSpec, ...] = (),
        host_diagnostic_signals: tuple[str, ...] = (),
    ) -> None:
        self._transport = transport
        self._candidate_compiler = candidate_compiler
        self._dynamic_tool_specs = dynamic_tool_specs
        self._host_diagnostic_signals = host_diagnostic_signals

    def run(
        self,
        session: CanonicalStageSessionPort,
        *,
        task_binding_sha256: str,
    ) -> D2CanonicalV3StageResult:
        activation = session.require_inference_stage(
            AgentExtractionStage.LOGICAL_STRUCTURE
        )
        invocation = build_canonical_stage_invocation(
            activation,
            task_binding_sha256=task_binding_sha256,
            turn_output_schema_id=(
                D2_LOGICAL_STRUCTURE_DECISION_CANDIDATE_V3_SCHEMA_VERSION
            ),
            turn_output_schema=_load_runner_output_schema(activation),
            expected_final_artifact_schema_id=(
                D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION
            ),
            expected_validator_id=CANONICAL_D2_V3_STAGE_VALIDATOR_ID,
            expected_validator_version=CANONICAL_D2_V3_STAGE_VALIDATOR_VERSION,
            dynamic_tool_specs=self._dynamic_tool_specs,
            host_diagnostic_signals=self._host_diagnostic_signals,
        )
        _validate_compiler_session_lineage(
            activation=activation,
            compiler=self._candidate_compiler,
        )
        transport_result = _invoke_transport(self._transport, invocation)
        try:
            candidate = D2LogicalStructureDecisionCandidateV3.model_validate(
                transport_result.structured_output
            )
        except ValidationError as exc:
            raise LifecycleProbeError(
                _candidate_validation_reason("D2_V3", exc)
            ) from None
        except Exception as exc:
            raise LifecycleProbeError(
                "CANONICAL_D2_V3_STRUCTURED_OUTPUT_INVALID"
            ) from exc
        try:
            raw_compilation = self._candidate_compiler.compile(candidate)
            compilation = D2LogicalStructureCompilationV3.model_validate(
                raw_compilation.model_dump(mode="json")
            )
        except ValidationError as exc:
            raise LifecycleProbeError(
                _candidate_validation_reason("D2_V3_HOST", exc)
            ) from None
        except Exception as exc:
            raise LifecycleProbeError(_host_compilation_reason("D2_V3", exc)) from None
        proposal = compilation.final_compilation.proposal
        host_receipt = compilation.final_compilation.validation_receipt
        _validate_compilation_replay(
            activation=activation,
            compiler=self._candidate_compiler,
            compilation=compilation,
            proposal=proposal,
            receipt=host_receipt,
        )
        artifact, final_receipt = _build_artifact_and_final(
            compilation=compilation,
            proposal=proposal,
            receipt=host_receipt,
        )
        execution_receipt = _build_execution_receipt(
            activation=activation,
            invocation=invocation,
            transport_result=transport_result,
            artifact=artifact,
            final_receipt=final_receipt,
            host_validation_receipt_sha256=host_receipt.receipt_sha256,
        )
        return D2CanonicalV3StageResult(
            decision_candidate=candidate,
            compilation=compilation,
            proposal=proposal,
            host_validation_receipt=host_receipt,
            stage_artifact=artifact,
            final_receipt=final_receipt,
            execution_receipt=execution_receipt,
            tool_activity_summary=transport_result.tool_activity_summary,
        )


def _validate_compiler_session_lineage(
    *,
    activation: CanonicalAppServerStageActivation,
    compiler: D2CanonicalV3CandidateCompilerPort,
) -> None:
    session = activation.session_attestation
    evidence = session.document_evidence
    predecessor = activation.predecessor
    source = SourceIRSnapshot.model_validate(compiler.source_ir.model_dump(mode="json"))
    d1 = D2ValidatedD1ArtifactIdentityV1.model_validate(
        compiler.expected_d1_artifact.model_dump(mode="json")
    )
    table_map = AgentD1TableMapArtifactIdentityV1.model_validate(
        compiler.expected_d1_table_map_artifact.model_dump(mode="json")
    )
    try:
        kit_identity = compiler.kit.identity
    except Exception as exc:
        raise LifecycleProbeError("CANONICAL_D2_V3_KIT_IDENTITY_INVALID") from exc
    if (
        evidence is None
        or predecessor is None
        or activation.stage is not AgentExtractionStage.LOGICAL_STRUCTURE
    ):
        raise LifecycleProbeError("CANONICAL_D2_V3_SESSION_LINEAGE_MISSING")
    if (
        kit_identity != session.kit_identity
        or d1.kit_bundle_sha256 != session.kit_identity.bundle_sha256
    ):
        raise LifecycleProbeError("CANONICAL_D2_V3_KIT_IDENTITY_MISMATCH")
    source_canonical_sha256 = d2_table_region_source_ir_canonical_sha256(source)
    if (
        source.source_ir_id != evidence.source_ir_id
        or source.source_sha256 != evidence.source_sha256
        or source_canonical_sha256 != evidence.source_ir_canonical_sha256
        or d1.run_id != evidence.run_id
        or d1.source_sha256 != evidence.source_sha256
        or d1.page_count != evidence.page_count
        or table_map.source_sha256 != evidence.source_sha256
    ):
        raise LifecycleProbeError("CANONICAL_D2_V3_SOURCE_LINEAGE_MISMATCH")
    if (
        predecessor.stage is not AgentExtractionStage.LOGICAL_STRUCTURE
        or predecessor.predecessor_stage
        is not AgentExtractionStage.DOCUMENT_UNDERSTANDING
        or predecessor.run_id != d1.run_id
        or predecessor.source_sha256 != d1.source_sha256
        or predecessor.artifact_id != d1.artifact_id
        or predecessor.artifact_sha256 != d1.artifact_sha256
        or predecessor.validator_receipt_sha256 != d1.validator_receipt_sha256
        or predecessor.proposal_schema_id != d1.proposal_schema_id
        or predecessor.status is not d1.status
    ):
        raise LifecycleProbeError("CANONICAL_D2_V3_PREDECESSOR_MISMATCH")


def _validate_compilation_replay(
    *,
    activation: CanonicalAppServerStageActivation,
    compiler: D2CanonicalV3CandidateCompilerPort,
    compilation: D2LogicalStructureCompilationV3,
    proposal: D2LogicalStructureProposalV2,
    receipt: D2LogicalStructureValidationReceiptV2,
) -> None:
    session = activation.session_attestation
    evidence = session.document_evidence
    predecessor = activation.predecessor
    source = compiler.source_ir
    d1 = compiler.expected_d1_artifact
    table_map = compiler.expected_d1_table_map_artifact
    base = compilation.base_compilation
    region_artifact = compilation.table_region_artifact
    placement_plan = compilation.placement_plan
    if evidence is None or predecessor is None:
        raise LifecycleProbeError("CANONICAL_D2_V3_SESSION_LINEAGE_MISSING")
    if (
        proposal.run_id != evidence.run_id
        or proposal.source_sha256 != evidence.source_sha256
        or proposal.source_ir_id != evidence.source_ir_id
        or proposal.source_ir_canonical_sha256 != evidence.source_ir_canonical_sha256
        or proposal.kit_identity != session.kit_identity
        or proposal.source_ir_canonical_sha256
        != d2_table_region_source_ir_canonical_sha256(source)
    ):
        raise LifecycleProbeError("CANONICAL_D2_V3_PROPOSAL_SESSION_MISMATCH")
    if (
        base.proposal.predecessor_d1_artifact != d1
        or base.proposal.d1_table_map_artifact != table_map
        or base.validation_receipt.predecessor_d1_artifact_id != d1.artifact_id
        or base.validation_receipt.predecessor_d1_artifact_sha256 != d1.artifact_sha256
        or base.validation_receipt.d1_table_map_artifact_id != table_map.artifact_id
        or base.validation_receipt.d1_table_map_artifact_sha256
        != table_map.artifact_sha256
        or predecessor.artifact_id != d1.artifact_id
        or predecessor.artifact_sha256 != d1.artifact_sha256
        or predecessor.validator_receipt_sha256 != d1.validator_receipt_sha256
    ):
        raise LifecycleProbeError("CANONICAL_D2_V3_D1_LINEAGE_MISMATCH")
    if (
        region_artifact.d1_table_map_artifact_id != table_map.artifact_id
        or region_artifact.d1_table_map_artifact_sha256 != table_map.artifact_sha256
        or region_artifact.kit_bundle_sha256 != session.kit_identity.bundle_sha256
        or proposal.base_logical_structure.artifact_id
        != base.validation_receipt.artifact_id
        or proposal.base_logical_structure.artifact_sha256
        != base.validation_receipt.artifact_sha256
        or proposal.base_logical_structure.validation_receipt_sha256
        != base.validation_receipt.receipt_sha256
        or proposal.table_region_artifact.artifact_id != region_artifact.artifact_id
        or proposal.table_region_artifact.artifact_sha256
        != region_artifact.artifact_sha256
        or proposal.region_placement_plan_sha256 != placement_plan.plan_sha256
    ):
        raise LifecycleProbeError("CANONICAL_D2_V3_COMPILATION_LINEAGE_MISMATCH")
    if (
        receipt.run_id != proposal.run_id
        or receipt.source_sha256 != proposal.source_sha256
        or receipt.source_ir_id != proposal.source_ir_id
        or receipt.source_ir_canonical_sha256 != proposal.source_ir_canonical_sha256
        or receipt.kit_identity != proposal.kit_identity
        or receipt.base_artifact_id != proposal.base_logical_structure.artifact_id
        or receipt.base_artifact_sha256
        != proposal.base_logical_structure.artifact_sha256
        or receipt.base_validation_receipt_sha256
        != proposal.base_logical_structure.validation_receipt_sha256
        or receipt.table_region_artifact_id
        != proposal.table_region_artifact.artifact_id
        or receipt.table_region_artifact_sha256
        != proposal.table_region_artifact.artifact_sha256
        or receipt.region_placement_plan_sha256 != proposal.region_placement_plan_sha256
        or receipt.status is not proposal.status
        or receipt.proposal_sha256 != proposal.proposal_sha256
        or receipt.artifact_sha256 != proposal.proposal_sha256
        or receipt.validator_id != D2_LOGICAL_STRUCTURE_V2_VALIDATOR_ID
        or receipt.validator_version != D2_LOGICAL_STRUCTURE_V2_VALIDATOR_VERSION
    ):
        raise LifecycleProbeError("CANONICAL_D2_V3_HOST_RECEIPT_INVALID")


def _build_artifact_and_final(
    *,
    compilation: D2LogicalStructureCompilationV3,
    proposal: D2LogicalStructureProposalV2,
    receipt: D2LogicalStructureValidationReceiptV2,
) -> tuple[AgentStageArtifactV1, AgentStageFinalReceiptV1]:
    status = (
        AgentStageArtifactStatus.VALIDATED
        if proposal.status is D2LogicalStructureStatus.COMPLETE
        else AgentStageArtifactStatus.PARTIAL
    )
    unresolved_count = (
        0
        if status is AgentStageArtifactStatus.VALIDATED
        else max(
            1,
            len(receipt.unresolved_base_table_object_ids),
            len(receipt.unresolved_region_ids),
            len(receipt.unresolved_empty_table_ids),
            len(receipt.unresolved_supplemental_evidence_ids),
            len(proposal.unresolved_codes),
        )
    )
    artifact = AgentStageArtifactV1(
        artifact_id=receipt.artifact_id,
        run_id=proposal.run_id,
        source_sha256=proposal.source_sha256,
        stage=AgentExtractionStage.LOGICAL_STRUCTURE,
        status=status,
        proposal_schema_id=D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION,
        payload_sha256=receipt.artifact_sha256,
        evidence_sha256=receipt.evidence_sha256,
        validator_receipt_sha256=receipt.receipt_sha256,
        predecessor_artifact_ids=(
            compilation.base_compilation.proposal.predecessor_d1_artifact.artifact_id,
        ),
        unresolved_scope_count=unresolved_count,
        diagnostic_codes=(
            ()
            if status is AgentStageArtifactStatus.VALIDATED
            else proposal.unresolved_codes
        ),
    )
    final = AgentStageFinalReceiptV1(
        schema_version="agent-stage-final-receipt/v1",
        run_id=artifact.run_id,
        source_sha256=artifact.source_sha256,
        stage=artifact.stage,
        kit_bundle_sha256=proposal.kit_identity.bundle_sha256,
        artifact_status=artifact.status,
        proposal_schema_id=artifact.proposal_schema_id,
        proposal_sha256=artifact.payload_sha256,
        evidence_sha256=artifact.evidence_sha256,
        validation_receipt_sha256=artifact.validator_receipt_sha256,
        validator_id=receipt.validator_id,
        validator_version=receipt.validator_version,
    )
    return artifact, final


CanonicalD2V3StageDriver = D2CanonicalV3StageDriver
CanonicalD2V3StageResult = D2CanonicalV3StageResult


__all__ = [
    "CANONICAL_D2_V3_STAGE_VALIDATOR_ID",
    "CANONICAL_D2_V3_STAGE_VALIDATOR_VERSION",
    "D2CanonicalV3CandidateCompilerPort",
    "D2CanonicalV3StageDriver",
    "D2CanonicalV3StageResult",
    "CanonicalD2V3StageDriver",
    "CanonicalD2V3StageResult",
]
