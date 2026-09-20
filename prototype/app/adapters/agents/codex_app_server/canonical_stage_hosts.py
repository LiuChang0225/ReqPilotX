from __future__ import annotations

from dataclasses import dataclass

from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAppServerStageActivation,
)
from app.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageTransportResult,
    D3DecisionAttestationBundle,
    D3DecisionAttestationRequestV1,
    D3ParentProvisionDecisionCandidateV1,
)
from app.adapters.agents.codex_app_server.d3_region_stage_input_workspace import (
    D3RegionStageInputWorkspaceManifestV1,
)
from app.adapters.agents.d3_parent_provision_attestation import (
    compile_d3_parent_provision_runner_authority,
)
from app.adapters.agents.portable_kit import PortableAgentKit
from app.application.agent_workflow.d2_decision_candidate import (
    D2LogicalStructureCompilationV1,
    D2LogicalStructureDecisionCandidateV2,
    compile_and_validate_d2_logical_structure_candidate,
)
from app.application.agent_workflow.d2_decision_candidate_v3 import (
    D2LogicalStructureCompilationV3,
    D2LogicalStructureDecisionCandidateV3,
    compile_and_validate_d2_logical_structure_candidate_v3,
)
from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalObjectV1,
    D2ValidatedD1ArtifactIdentityV1,
)
from app.application.agent_workflow.d2_logical_structure_v2 import D2LogicalObjectV2
from app.application.agent_workflow.d3_parent_provision_attestation import (
    D3ParentProvisionExecutionReceiptV1,
    D3ParentProvisionPolicyAttestationV1,
    attest_d3_parent_provision_policy,
)
from app.application.agent_workflow.d3_parent_provision_compiler import (
    D3HostCompiledParentProvisionDecisionV1,
    D3ParentBoundaryClassificationCandidateV2,
    compile_d3_parent_provision_decision,
)
from app.application.agent_workflow.d3_parent_provisions import (
    D3ExactSourceSegmentV1,
    D3ValidatedD2ArtifactIdentityV1,
    ParentProvisionProposalV3,
    ParentProvisionValidationReceiptV3,
    validate_parent_provision_proposal,
)
from app.application.agent_workflow.extraction_runtime import (
    AGENT_BASELINE_STAGE_ORDER,
    AgentKitIdentityV1,
    AgentRunnerIdentityV1,
)
from app.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectBatchAggregateV1,
    ParentProvisionObjectBatchCompilationV1,
    ParentProvisionObjectBatchPlanV1,
    ParentProvisionObjectCandidateV1,
    ParentProvisionObjectExecutionMode,
    aggregate_parent_provision_object_batches,
    build_parent_provision_object_batch_plan,
    compile_parent_provision_object_candidate,
    require_parent_provision_object_evidence_recovery,
)
from app.application.agent_workflow.region_parent_runtime_adapter import (
    RegionParentRuntimeAdapterV1,
)
from app.application.agent_workflow.parent_provision_evidence_recovery import (
    ParentProvisionEvidenceGap,
    ParentProvisionEvidenceRecoverability,
    ParentProvisionEvidenceRecoveryPlanV1,
    ParentProvisionEvidenceRecoverySessionV1,
    ParentProvisionEvidenceRecoveryStrategy,
    build_parent_provision_evidence_recovery_plan,
    build_parent_provision_evidence_recovery_session,
)
from app.ports.parent_provision_evidence_recovery import (
    ParentProvisionEvidenceRecoveryExecutionResultV1,
    ParentProvisionEvidenceRecoveryExecutionTargetV1,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
)
from app.domain.extraction.source_ir import SourceIRSnapshot


@dataclass(frozen=True, slots=True)
class CanonicalD2CandidateCompiler:
    source_ir: SourceIRSnapshot
    expected_d1_artifact: D2ValidatedD1ArtifactIdentityV1
    expected_d1_table_map_artifact: AgentD1TableMapArtifactIdentityV1
    kit: PortableAgentKit

    def compile(
        self,
        candidate: D2LogicalStructureDecisionCandidateV2,
    ) -> D2LogicalStructureCompilationV1:
        return compile_and_validate_d2_logical_structure_candidate(
            candidate,
            source_ir=self.source_ir,
            expected_d1_artifact=self.expected_d1_artifact,
            expected_d1_table_map_artifact=self.expected_d1_table_map_artifact,
            expected_kit_identity=self.kit.identity,
        )


@dataclass(frozen=True, slots=True)
class CanonicalD2V3CandidateCompiler:
    """Host compiler for the region-aware logical-structure runner wire."""

    source_ir: SourceIRSnapshot
    expected_d1_artifact: D2ValidatedD1ArtifactIdentityV1
    expected_d1_table_map_artifact: AgentD1TableMapArtifactIdentityV1
    kit: PortableAgentKit

    def compile(
        self,
        candidate: D2LogicalStructureDecisionCandidateV3,
    ) -> D2LogicalStructureCompilationV3:
        return compile_and_validate_d2_logical_structure_candidate_v3(
            candidate,
            source_ir=self.source_ir,
            expected_d1_artifact=self.expected_d1_artifact,
            expected_d1_table_map_artifact=self.expected_d1_table_map_artifact,
            expected_kit_identity=self.kit.identity,
        )


@dataclass(frozen=True, slots=True)
class CanonicalD3CandidateCompiler:
    d2_identity: D3ValidatedD2ArtifactIdentityV1
    source_segments: tuple[D3ExactSourceSegmentV1, ...]
    kit: PortableAgentKit

    def compile(
        self,
        candidate: D3ParentBoundaryClassificationCandidateV2,
    ) -> D3HostCompiledParentProvisionDecisionV1:
        return compile_d3_parent_provision_decision(
            d2_identity=self.d2_identity,
            source_segments=self.source_segments,
            kit_identity=self.kit.identity,
            candidate=candidate,
        )


@dataclass(frozen=True, slots=True)
class CanonicalParentProvisionObjectBatchCompiler:
    """Host-only seam for object-scoped parent recognition before live batching."""

    d2_identity: D3ValidatedD2ArtifactIdentityV1
    logical_objects: tuple[D2LogicalObjectV1 | D2LogicalObjectV2, ...]
    source_segments: tuple[D3ExactSourceSegmentV1, ...]
    stage_input_bundle_sha256: str
    kit_identity: AgentKitIdentityV1
    stage_input_manifest_sha256: str | None = None
    runtime_adapter_sha256: str | None = None

    @classmethod
    def from_region_runtime(
        cls,
        *,
        runtime: RegionParentRuntimeAdapterV1,
        workspace_attestation: D3RegionStageInputWorkspaceManifestV1,
        kit_identity: AgentKitIdentityV1,
    ) -> CanonicalParentProvisionObjectBatchCompiler:
        runtime = RegionParentRuntimeAdapterV1.model_validate(
            runtime.model_dump(mode="json")
        )
        workspace = D3RegionStageInputWorkspaceManifestV1.model_validate(
            workspace_attestation.model_dump(mode="json")
        )
        kit = AgentKitIdentityV1.model_validate(kit_identity.model_dump(mode="json"))
        if (
            runtime.workspace_bundle_sha256 != workspace.bundle_sha256
            or runtime.stage_input_manifest_sha256
            != workspace.stage_input_manifest.manifest_sha256
            or runtime.d2_identity.kit_identity != kit
            or workspace.kit_bundle_sha256 != kit.bundle_sha256
            or workspace.predecessor_artifact_id != runtime.d2_identity.artifact_id
            or workspace.predecessor_artifact_sha256
            != runtime.d2_identity.artifact_sha256
            or workspace.predecessor_validation_receipt_sha256
            != runtime.d2_identity.validator_receipt_sha256
        ):
            raise ValueError("region parent runtime/workspace identity drifted")
        return cls(
            d2_identity=runtime.d2_identity,
            logical_objects=runtime.logical_objects,
            source_segments=runtime.source_segments,
            stage_input_bundle_sha256=workspace.bundle_sha256,
            kit_identity=kit,
            stage_input_manifest_sha256=(
                workspace.stage_input_manifest.manifest_sha256
            ),
            runtime_adapter_sha256=runtime.adapter_sha256,
        )

    def build_plan(self) -> ParentProvisionObjectBatchPlanV1:
        return build_parent_provision_object_batch_plan(
            d2_identity=self.d2_identity,
            logical_objects=self.logical_objects,
            source_segments=self.source_segments,
            stage_input_bundle_sha256=self.stage_input_bundle_sha256,
            stage_input_manifest_sha256=self.stage_input_manifest_sha256,
            runtime_adapter_sha256=self.runtime_adapter_sha256,
        )

    def compile_object(
        self,
        *,
        plan: ParentProvisionObjectBatchPlanV1,
        batch_id: str,
        candidate: ParentProvisionObjectCandidateV1,
    ) -> ParentProvisionObjectBatchCompilationV1:
        return compile_parent_provision_object_candidate(
            plan=plan,
            expected_plan_sha256=plan.plan_sha256,
            batch_id=batch_id,
            d2_identity=self.d2_identity,
            source_segments=self.source_segments,
            kit_identity=self.kit_identity,
            candidate=candidate,
        )

    def require_evidence_recovery(
        self,
        *,
        plan: ParentProvisionObjectBatchPlanV1,
        logical_object_id: str,
        gap: ParentProvisionEvidenceGap,
        recoverability: ParentProvisionEvidenceRecoverability,
        trigger_failure_code: str,
        ordered_strategies: tuple[ParentProvisionEvidenceRecoveryStrategy, ...],
    ) -> tuple[
        ParentProvisionObjectBatchPlanV1,
        ParentProvisionEvidenceRecoveryPlanV1,
        ParentProvisionEvidenceRecoverySessionV1,
    ]:
        current_target = next(
            (
                item
                for item in plan.targets
                if item.logical_object_id == logical_object_id
            ),
            None,
        )
        if current_target is None:
            raise ValueError("canonical recovery target is unknown")
        if (
            current_target.execution_mode
            is ParentProvisionObjectExecutionMode.MODEL_TARGET
        ):
            recovery_object_plan = require_parent_provision_object_evidence_recovery(
                plan=plan,
                expected_plan_sha256=plan.plan_sha256,
                logical_object_id=logical_object_id,
            )
        elif (
            current_target.execution_mode
            is ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED
        ):
            recovery_object_plan = plan
        else:
            raise ValueError("canonical Host-empty target does not require recovery")
        target = next(
            item
            for item in recovery_object_plan.targets
            if item.logical_object_id == logical_object_id
        )
        recovery_plan = build_parent_provision_evidence_recovery_plan(
            run_id=self.d2_identity.run_id,
            source_sha256=self.d2_identity.source_sha256,
            kit_bundle_sha256=self.kit_identity.bundle_sha256,
            predecessor_artifact_id=self.d2_identity.artifact_id,
            predecessor_artifact_sha256=self.d2_identity.artifact_sha256,
            object_plan_id=recovery_object_plan.plan_id,
            object_plan_sha256=recovery_object_plan.plan_sha256,
            batch_id=target.batch_id,
            batch_order=target.batch_order,
            logical_object_id=target.logical_object_id,
            target_scope_sha256=target.target_scope_sha256,
            target_execution_mode=target.execution_mode.value,
            original_stage_input_bundle_sha256=(
                recovery_object_plan.stage_input_bundle_sha256
            ),
            original_target_evidence_sha256=target.target_evidence_sha256,
            gap=gap,
            recoverability=recoverability,
            trigger_failure_code=trigger_failure_code,
            ordered_strategies=ordered_strategies,
            max_attempts=len(ordered_strategies),
        )
        return (
            recovery_object_plan,
            recovery_plan,
            build_parent_provision_evidence_recovery_session(plan=recovery_plan),
        )

    def build_evidence_recovery_execution_target(
        self,
        *,
        plan: ParentProvisionObjectBatchPlanV1,
        logical_object_id: str,
        source_handle: str,
        table_ids: tuple[str, ...] = (),
        asset_ids: tuple[str, ...] = (),
    ) -> ParentProvisionEvidenceRecoveryExecutionTargetV1:
        target = next(
            (
                item
                for item in plan.targets
                if item.logical_object_id == logical_object_id
            ),
            None,
        )
        logical_object = next(
            (
                item
                for item in self.logical_objects
                if item.logical_object_id == logical_object_id
            ),
            None,
        )
        if (
            target is None
            or logical_object is None
            or target.execution_mode
            is not ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED
        ):
            raise ValueError("canonical recovery execution target is unavailable")
        return ParentProvisionEvidenceRecoveryExecutionTargetV1(
            source_handle=source_handle,
            workspace_bundle_sha256=plan.stage_input_bundle_sha256,
            logical_object_id=logical_object_id,
            page_numbers=logical_object.page_numbers,
            table_ids=table_ids,
            asset_ids=asset_ids,
        )

    def aggregate(
        self,
        *,
        plan: ParentProvisionObjectBatchPlanV1,
        results: tuple[ParentProvisionObjectBatchCompilationV1, ...],
    ) -> ParentProvisionObjectBatchAggregateV1:
        return aggregate_parent_provision_object_batches(
            plan=plan,
            expected_plan_sha256=plan.plan_sha256,
            results=results,
            d2_identity=self.d2_identity,
            source_segments=self.source_segments,
            kit_identity=self.kit_identity,
        )


@dataclass(frozen=True, slots=True)
class CanonicalSameEvidenceParentProvisionReplanner:
    """Rebuild a recovered object plan when source text evidence is unchanged."""

    d2_identity: D3ValidatedD2ArtifactIdentityV1
    logical_objects: tuple[D2LogicalObjectV1 | D2LogicalObjectV2, ...]
    source_segments: tuple[D3ExactSourceSegmentV1, ...]
    stage_input_manifest_sha256: str | None = None
    runtime_adapter_sha256: str | None = None

    def rebuild_object_plan(
        self,
        *,
        old_plan: ParentProvisionObjectBatchPlanV1,
        session: ParentProvisionEvidenceRecoverySessionV1,
        execution_result: ParentProvisionEvidenceRecoveryExecutionResultV1,
    ) -> ParentProvisionObjectBatchPlanV1:
        if (
            execution_result.new_stage_input_bundle_sha256 is None
            or execution_result.new_target_evidence_sha256 is None
            or session.plan.object_plan_sha256 != old_plan.plan_sha256
        ):
            raise ValueError("canonical recovery replan input is incomplete")
        new_plan = build_parent_provision_object_batch_plan(
            d2_identity=self.d2_identity,
            logical_objects=self.logical_objects,
            source_segments=self.source_segments,
            stage_input_bundle_sha256=(execution_result.new_stage_input_bundle_sha256),
            stage_input_manifest_sha256=self.stage_input_manifest_sha256,
            runtime_adapter_sha256=self.runtime_adapter_sha256,
        )
        new_target = next(
            item
            for item in new_plan.targets
            if item.logical_object_id == session.plan.logical_object_id
        )
        if (
            new_target.execution_mode
            is not ParentProvisionObjectExecutionMode.MODEL_TARGET
            or new_target.target_evidence_sha256
            != execution_result.new_target_evidence_sha256
        ):
            raise ValueError(
                "canonical same-evidence replan cannot consume changed source evidence"
            )
        return new_plan


@dataclass(frozen=True, slots=True)
class CanonicalD3DecisionAttestor:
    kit: PortableAgentKit

    def attest_decision(
        self,
        *,
        candidate: D3ParentProvisionDecisionCandidateV1,
        request: D3DecisionAttestationRequestV1,
        activation: CanonicalAppServerStageActivation,
        transport_result: CanonicalStageTransportResult,
    ) -> D3DecisionAttestationBundle:
        closure = activation.d3_policy_closure
        if closure is None or closure.kit_identity != self.kit.identity:
            raise ValueError("canonical D3 policy closure is unavailable or stale")
        session = activation.session_attestation
        authority = compile_d3_parent_provision_runner_authority(
            self.kit,
            session.runner_profile_id,
        )
        execution = D3ParentProvisionExecutionReceiptV1(
            run_id=candidate.run_id,
            source_sha256=candidate.source_sha256,
            d2_artifact_id=candidate.predecessor_d2_artifact_id,
            d2_artifact_sha256=candidate.predecessor_d2_artifact_sha256,
            kit_identity=candidate.kit_identity,
            agent_id=closure.agent_id,
            runner_identity=AgentRunnerIdentityV1(
                runner_profile_id=session.runner_profile_id,
                runner_profile_sha256=session.runner_profile_sha256,
                runner_kind=session.runner_kind,
                adapter_id=session.adapter_id,
                adapter_version=session.adapter_version,
                runtime_version=str(
                    activation.runtime.protocol.get("codex_version", "unknown")
                ),
            ),
            model_id=session.model_id,
            reasoning_effort=session.reasoning_effort,
            requested_stages=AGENT_BASELINE_STAGE_ORDER[:4],
            loaded_policy_closure=closure,
            stage_session_attestation_sha256=(request.stage_session_attestation_sha256),
            execution_trace_sha256=request.execution_trace_sha256,
            decision_candidate_sha256=request.decision_candidate_sha256,
            parent_provision_decision_sha256=(request.parent_provision_decision_sha256),
            evidence_refs=request.evidence_refs,
        )
        attestation = attest_d3_parent_provision_policy(
            expected_policy_closure=closure,
            expected_runner=authority,
            expected_run_id=candidate.run_id,
            expected_source_sha256=candidate.source_sha256,
            expected_d2_artifact_id=candidate.predecessor_d2_artifact_id,
            expected_d2_artifact_sha256=(candidate.predecessor_d2_artifact_sha256),
            expected_stage_session_attestation_sha256=(
                request.stage_session_attestation_sha256
            ),
            expected_execution_trace_sha256=request.execution_trace_sha256,
            expected_decision_candidate_sha256=request.decision_candidate_sha256,
            expected_parent_provision_decision_sha256=(
                request.parent_provision_decision_sha256
            ),
            expected_evidence_refs=request.evidence_refs,
            execution_receipt=execution,
        )
        if not attestation.policy_attested or attestation.diagnostic_codes:
            raise ValueError("canonical D3 professional policy was not attested")
        return D3DecisionAttestationBundle(
            execution_receipt=execution,
            policy_attestation=attestation,
        )


@dataclass(frozen=True, slots=True)
class CanonicalD3HostValidator:
    d2_identity: D3ValidatedD2ArtifactIdentityV1
    source_segments: tuple[D3ExactSourceSegmentV1, ...]

    def validate(
        self,
        *,
        proposal: ParentProvisionProposalV3,
        execution_receipt: D3ParentProvisionExecutionReceiptV1,
        policy_attestation: D3ParentProvisionPolicyAttestationV1,
    ) -> ParentProvisionValidationReceiptV3:
        return validate_parent_provision_proposal(
            d2_identity=self.d2_identity,
            source_segments=self.source_segments,
            parent_execution_receipt=execution_receipt,
            parent_policy_attestation=policy_attestation,
            proposal=proposal,
        )


__all__ = [
    "CanonicalD2CandidateCompiler",
    "CanonicalD2V3CandidateCompiler",
    "CanonicalD3CandidateCompiler",
    "CanonicalD3DecisionAttestor",
    "CanonicalD3HostValidator",
    "CanonicalParentProvisionObjectBatchCompiler",
    "CanonicalSameEvidenceParentProvisionReplanner",
]
